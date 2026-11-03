#!/usr/bin/env python3
"""Decide, deterministically and before any LLM spend, whether the test agent runs.

Inputs are the GitHub event JSON plus NUL-delimited git output. Everything here is
untrusted PR data; any parse problem fails closed (run_agent=false, exit 1).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import PurePosixPath
from typing import Any

OPT_OUT_LABEL = "skip-test-agent"
AGENT_COMMIT_LINE = "Test-Agent-Commit: true"
AGENT_BRANCH_PREFIX = "test-agent/"
# A false match only skips a run, so these can stay loose about the surrounding text.
AGENT_MERGE_RES = [re.compile(p) for p in (
    r"Merge pull request #\d+ from [^\s/]+/test-agent/pr-\d+",
    r"Merge (?:remote-tracking )?branch '(?:origin/)?test-agent/pr-\d+'(?: of \S+)?(?: into \S+)?",
)]

SKIP_DETAILS = {
    "fork": "pull request comes from a fork",
    "draft": "pull request is a draft",
    "bot_author": "pull request author is a bot",
    "opt_out_label": f"label '{OPT_OUT_LABEL}' is set",
    "control_plane_changed": "PR modifies agent/CI control-plane files",
    "agent_branch": "pull request comes from a test agent branch",
    "agent_commit": "head commit was created by the test agent",
    "no_testable_changes": "no testable source files changed",
    "diff_too_large": "diff exceeds size limits",
}


class ScopeError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def parse_name_status(data: bytes) -> list[dict[str, Any]]:
    """Parse `git diff --name-status -z --find-renames`."""
    tokens = data.decode("utf-8", errors="surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status or not re.fullmatch(r"[ACDMRTUXB][0-9]*", status):
            raise ScopeError(f"bad name-status status token {status!r}")
        kind = status[0]
        npaths = 2 if kind in "RC" else 1
        paths = tokens[i + 1 : i + 1 + npaths]
        if len(paths) != npaths or any(p == "" for p in paths):
            raise ScopeError("truncated name-status record")
        entries.append({"status": kind, "old": paths[0], "new": paths[-1]})
        i += 1 + npaths
    return entries


def parse_numstat(data: bytes) -> int:
    """Return total changed lines from `git diff --numstat -z`; binary counts 0."""
    tokens = data.decode("utf-8", errors="surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    total = 0
    i = 0
    while i < len(tokens):
        parts = tokens[i].split("\t", 2)
        if len(parts) != 3:
            raise ScopeError("bad numstat record")
        added, removed, path = parts
        for n in (added, removed):
            if n == "-":
                continue
            if not n.isascii() or not n.isdigit():
                raise ScopeError("bad numstat count")
            total += int(n)
        # Renames/copies: empty path field, followed by old and new path tokens.
        i += 3 if path == "" else 1
        if i > len(tokens):
            raise ScopeError("truncated numstat record")
    return total


# ---------------------------------------------------------------- classification

def _norm(path: str) -> str:
    return path.replace("\\", "/")


def is_control_plane(path: str) -> bool:
    # casefold so that case-insensitive filesystems cannot sidestep the match
    parts = [p.casefold() for p in _norm(path).split("/") if p not in ("", ".")]
    if not parts:
        return False
    if parts[0] == ".github":
        return True
    if ".claude" in parts[:-1] or parts[-1] == ".claude":
        return True
    return parts[-1] in ("claude.md", ".mcp.json")


def classify(path: str) -> str:
    """Return 'backend', 'frontend', 'test' or 'other' for a surviving path."""
    p = PurePosixPath(_norm(path))
    s = str(p)
    name = p.name
    if s.startswith("backend/tests/"):
        return "test"
    if s.startswith("frontend/src/"):
        if s.startswith("frontend/src/test/") or re.search(r"\.test\.tsx?$", name):
            return "test"
        if not re.search(r"\.tsx?$", name):
            return "other"
        if (
            re.search(r"\.(stories|test)\.", name)
            or name.endswith(".d.ts")
            or name in ("types.ts", "index.ts")
            or name.endswith(".types.ts")
            or s == "frontend/src/main.tsx"
            or s.startswith("frontend/src/design-system/")
        ):
            return "other"
        return "frontend"
    if s.startswith("backend/app/") and name.endswith(".py"):
        if name == "__init__.py" or s == "backend/app/main.py":
            return "other"
        return "backend"
    return "other"


# ---------------------------------------------------------------- event gates

def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            raise ScopeError(f"event missing {'.'.join(keys)}")
        d = d[k]
    return d


def read_event(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            ev = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise ScopeError(f"cannot read event: {e}") from e
    pr = _get(ev, "pull_request")
    if not isinstance(pr, dict):
        raise ScopeError("pull_request is not an object")
    draft = _get(pr, "draft")
    if not isinstance(draft, bool):
        raise ScopeError("pull_request.draft is not a bool")
    base = _get(pr, "base", "repo", "full_name")
    head_repo = _get(pr, "head", "repo")  # null when the fork was deleted
    head = _get(head_repo, "full_name") if head_repo is not None else None
    head_ref = _get(pr, "head", "ref")
    labels = _get(pr, "labels")
    if not isinstance(labels, list):
        raise ScopeError("labels is not a list")
    names = [_get(lb, "name") for lb in labels]
    login = _get(pr, "user", "login")
    utype = _get(pr, "user", "type")
    if not isinstance(base, str) or not all(isinstance(x, str) for x in names + [login, utype, head_ref]):
        raise ScopeError("event fields have wrong types")
    if head is not None and not isinstance(head, str):
        raise ScopeError("head repo name has wrong type")
    return {"draft": draft, "head": head, "base": base, "labels": names,
            "login": login, "type": utype, "head_ref": head_ref}


def is_agent_commit(commit_msg: str) -> bool:
    lines = [ln.rstrip("\r") for ln in commit_msg.split("\n")]
    return AGENT_COMMIT_LINE in lines or any(r.fullmatch(lines[0]) for r in AGENT_MERGE_RES)


def decide(ev: dict[str, Any], entries: list[dict[str, Any]], lines: int,
           commit_msg: str, max_files: int, max_lines: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_agent": False, "skip_reason": None, "skip_detail": "", "stacks": [],
        "source_files": {"backend": [], "frontend": []}, "test_files": [],
        "other_files": [], "totals": {"files": len(entries), "lines": lines},
    }
    all_paths: list[str] = []
    for e in entries:
        all_paths += [e["old"], e["new"]]
        if e["status"] == "D":
            result["other_files"].append(e["old"])
            continue
        kind = classify(e["new"])
        if kind == "test":
            result["test_files"].append(e["new"])
        elif kind == "other":
            result["other_files"].append(e["new"])
        else:
            result["source_files"][kind].append(e["new"])
    result["stacks"] = [s for s in ("backend", "frontend") if result["source_files"][s]]

    gates = [
        ("fork", ev["head"] is None or ev["head"] != ev["base"]),
        ("draft", ev["draft"]),
        ("bot_author", ev["type"] == "Bot" or ev["login"].endswith("[bot]")),
        ("opt_out_label", OPT_OUT_LABEL in ev["labels"]),
        ("control_plane_changed", any(is_control_plane(p) for p in all_paths)),
        ("agent_branch", ev["head_ref"].startswith(AGENT_BRANCH_PREFIX)),
        ("agent_commit", is_agent_commit(commit_msg)),
        ("no_testable_changes", not result["stacks"]),
        ("diff_too_large", len(entries) > max_files or lines > max_lines),
    ]
    for code, hit in gates:
        if hit:
            result["skip_reason"] = code
            result["skip_detail"] = SKIP_DETAILS[code]
            return result
    result["run_agent"] = True
    return result


def error_result(detail: str) -> dict[str, Any]:
    return {
        "run_agent": False, "skip_reason": "scope_error", "skip_detail": detail,
        "stacks": [], "source_files": {"backend": [], "frontend": []},
        "test_files": [], "other_files": [], "totals": {"files": 0, "lines": 0},
    }


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise ScopeError(f"cannot read {path}: {e.strerror}") from e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event", required=True)
    ap.add_argument("--name-status", required=True)
    ap.add_argument("--numstat", required=True)
    ap.add_argument("--head-commit-message", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-files", type=int, default=50)
    ap.add_argument("--max-lines", type=int, default=2000)
    args = ap.parse_args(argv)

    code = 0
    try:
        ev = read_event(args.event)
        entries = parse_name_status(_read(args.name_status))
        lines = parse_numstat(_read(args.numstat))
        msg = _read(args.head_commit_message).decode("utf-8", errors="replace")
        result = decide(ev, entries, lines, msg, args.max_files, args.max_lines)
    except ScopeError as e:
        result, code = error_result(str(e)), 1
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=True)
            f.write("\n")
    except OSError as e:
        print(f"cannot write output: {e}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
