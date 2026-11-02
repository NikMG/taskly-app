#!/usr/bin/env python3
"""Render the PR comment for the test agent.

Every string that originates from the agent, the PR, or a patch is passed through
sanitize() before it is placed in markdown. Only numbers taken from verify.json are
presented as verified. This script never fails the pipeline because an input is
missing or corrupt: it states what is missing instead.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

MARKER = "<!-- test-agent-report -->"
MAX_COMMENT = 60000
TRUNC_NOTICE = "\n\n> Comment truncated: it exceeded the maximum length.\n"
MAX_INPUT_BYTES = 5_000_000
ZWSP = "&#8203;"
SCHEMA_PATH = Path(__file__).with_name("report.schema.json")
RUN_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+(/.*)?", re.ASCII)
RUN_URL_SAFE_RE = re.compile(r"[A-Za-z0-9/_.\-?=&%#]*", re.ASCII)
SHA_RE = re.compile(r"[0-9a-fA-F]{40}", re.ASCII)
_DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_URL_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):(?=/)")
_MD_ESCAPE_RE = re.compile(r"([\\`*_~\[\]()!|])")


# ---------------------------------------------------------------- sanitizing

def sanitize(value: Any, max_len: int = 200) -> str:
    """Make arbitrary text inert inside a GitHub-flavoured markdown comment.

    Order matters: drop invisible chars, collapse whitespace, truncate the raw text
    (so escapes are never cut in half), then HTML-escape, then markdown-escape, then
    insert zero-width entities that break autolinks, mentions and issue refs.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    out = []
    for ch in s:
        if ch in "\n\r\t\v\f\x85  ":
            out.append(" ")
        elif unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        else:
            out.append(ch)
    s = re.sub(r" {2,}", " ", "".join(out)).strip()
    if len(s) > max_len:
        s = s[:max_len] if max_len <= 3 else s[: max_len - 3] + "..."
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _MD_ESCAPE_RE.sub(r"\\\1", s)
    # neutralise issue refs first: later steps insert `&#8203;`, which contains `#`
    s = re.sub(r"#(?=\d)", f"#{ZWSP}", s)
    s = re.sub(r"(?i)\bGH-(?=\d)", f"GH-{ZWSP}", s)
    s = _URL_SCHEME_RE.sub(lambda m: f"{m.group(1)}:{ZWSP}", s)
    s = re.sub(r"(?i)\bwww\.", f"www{ZWSP}.", s)
    s = s.replace("@", f"@{ZWSP}")
    return s


def safe_run_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not RUN_URL_RE.fullmatch(url):
        return None
    tail = url[len("https://github.com/"):]
    return url if RUN_URL_SAFE_RE.fullmatch(tail) else None


def safe_sha(sha: str | None) -> str | None:
    return sha.lower() if isinstance(sha, str) and SHA_RE.fullmatch(sha) else None


# ---------------------------------------------------------------- loading

def load_json(path: str | None) -> tuple[Any, str]:
    """Return (data, status) where status is 'ok', 'missing' or 'corrupt'."""
    if not path:
        return None, "missing"
    try:
        p = Path(path)
        if p.stat().st_size > MAX_INPUT_BYTES:
            return None, "corrupt"
        return json.loads(p.read_bytes()), "ok"
    except OSError:
        return None, "missing"
    except (ValueError, RecursionError):
        return None, "corrupt"


def validate_report(report: Any) -> tuple[bool, int]:
    """Return (valid, error_count). Content of errors is never exposed."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(report))
    return not errors, len(errors)


def audit_agent_log(path: str | None) -> dict[str, Any] | None:
    """Parse Claude Code `--output-format stream-json`; tolerant of bad lines."""
    if not path:
        return None
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    skills: dict[str, int] = {}
    tools: dict[str, int] = {}
    denials: dict[str, int] = {}
    info: dict[str, Any] = {"cost_usd": None, "num_turns": None, "duration_ms": None}
    malformed = 0

    def num(x: Any) -> Any:
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):
                malformed += 1
                continue
            if not isinstance(ev, dict):
                malformed += 1
                continue
            if ev.get("type") == "assistant" and isinstance(ev.get("message"), dict):
                content = ev["message"].get("content")
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if not isinstance(name, str):
                        continue
                    tools[name] = tools.get(name, 0) + 1
                    inp = block.get("input")
                    if name == "Skill" and isinstance(inp, dict) \
                            and isinstance(inp.get("skill"), str):
                        skills[inp["skill"]] = skills.get(inp["skill"], 0) + 1
            elif ev.get("type") == "result":
                info["cost_usd"] = num(ev.get("total_cost_usd"))
                info["num_turns"] = num(ev.get("num_turns"))
                info["duration_ms"] = num(ev.get("duration_ms"))
                pd = ev.get("permission_denials")
                for d in pd if isinstance(pd, list) else []:
                    n = d.get("tool_name") if isinstance(d, dict) else None
                    key = n if isinstance(n, str) else "(unknown)"
                    denials[key] = denials.get(key, 0) + 1
    return {"skills": skills, "tools": tools, "denials": denials,
            "malformed_lines": malformed, **info}


# ---------------------------------------------------------------- formatting

def _n(x: Any) -> str:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return "n/a"
    if isinstance(x, float):
        return f"{x:.2f}" if x == x and abs(x) < 1e12 else "n/a"
    return str(x) if abs(x) < 10**12 else "n/a"


def _run(d: Any) -> str:
    if not isinstance(d, dict):
        return "n/a"
    return f"exit {_n(d.get('exit_code'))}, {_n(d.get('tests'))} tests, {_n(d.get('failures'))} failures"


def _cov(d: Any) -> str:
    if not isinstance(d, dict):
        return "n/a"
    return f"{_n(d.get('before'))}% -> {_n(d.get('after'))}%"


def section_verify(verify: Any, status: str) -> list[str]:
    out = ["### Verified test results", ""]
    if status != "ok" or not isinstance(verify, dict):
        out.append(f"Verification results are {'unavailable' if status == 'missing' else 'corrupt and were not rendered'}.")
        return out
    st = verify.get("status")
    st = st if st in ("passed", "failed", "skipped", "error") else "invalid"
    out.append(f"Verification status: **{st}**")
    if verify.get("preexisting_failures") is True:
        out.append("")
        out.append("Note: the baseline (without the patch) already had failing tests.")
    stacks = verify.get("stacks") if isinstance(verify.get("stacks"), dict) else {}
    for name in ("backend", "frontend"):
        d = stacks.get(name)
        if not isinstance(d, dict):
            continue
        fr = d.get("flaky_rerun")
        flaky = "n/a"
        if isinstance(fr, dict):
            ap = fr.get("all_passed")
            flaky = f"{_n(fr.get('runs'))} run(s), all passed: {'yes' if ap is True else 'no' if ap is False else 'n/a'}"
        out += ["", f"**{name.capitalize()}**", "", "| Check | Result |", "| --- | --- |",
                f"| Baseline (without patch) | {_run(d.get('baseline'))} |",
                f"| With patch | {_run(d.get('with_patch'))} |",
                f"| Flaky re-run | {flaky} |",
                f"| Coverage | {_cov(d.get('coverage'))} |",
                f"| Diff coverage | {_cov(d.get('diff_coverage'))} |"]
    return out


def section_patch(pc: Any, status: str) -> list[str]:
    out = ["### Patch check", ""]
    if status != "ok" or not isinstance(pc, dict):
        out.append("Patch check results are " + ("unavailable." if status == "missing" else "corrupt and were not rendered."))
        return out
    out.append(f"Result: **{'passed' if pc.get('ok') is True else 'failed'}**")
    stats = pc.get("stats") if isinstance(pc.get("stats"), dict) else {}
    if stats:
        out.append("")
        out.append(f"Files: {_n(stats.get('files'))}, added lines: {_n(stats.get('added'))}, "
                   f"removed lines: {_n(stats.get('removed'))}, assertions added/removed: "
                   f"{_n(stats.get('assertions_added'))}/{_n(stats.get('assertions_removed'))}")
    if isinstance(pc.get("error"), str):
        out += ["", f"Error: {sanitize(pc['error'], 300)}"]
    for key, title in (("violations", "Violations"), ("warnings", "Warnings")):
        items = pc.get(key)
        if not isinstance(items, list) or not items:
            continue
        out += ["", f"{title} ({len(items)}):"]
        for it in items[:20]:
            if not isinstance(it, dict):
                continue
            line = it.get("line")
            where = f":{_n(line)}" if isinstance(line, int) and not isinstance(line, bool) else ""
            out.append(f"- {sanitize(it.get('rule'), 40)} - {sanitize(it.get('file'), 200)}{where}"
                       f" - {sanitize(it.get('detail'), 300)}")
        if len(items) > 20:
            out.append(f"- ... and {len(items) - 20} more")
    return out


def section_audit(audit: dict[str, Any] | None) -> list[str]:
    out = ["### Skills used and agent stats (from the agent log)", ""]
    if audit is None:
        out.append("Agent log unavailable.")
        return out
    skills = audit["skills"]
    out.append("Skills used: " + (", ".join(f"{sanitize(k, 80)} (x{v})" for k, v in
               sorted(skills.items())[:30]) if skills else "none"))
    tools = audit["tools"]
    out.append("")
    out.append("Tool calls: " + (", ".join(f"{sanitize(k, 60)}: {v}" for k, v in
               sorted(tools.items())[:40]) if tools else "none"))
    den = audit["denials"]
    out.append("")
    out.append("Denied tool calls: " + (", ".join(f"{sanitize(k, 60)}: {v}" for k, v in
               sorted(den.items())[:40]) if den else "none"))
    cost = audit["cost_usd"]
    out.append("")
    out.append(f"Cost (USD): {'n/a' if cost is None else f'{cost:.4f}'}, turns: {_n(audit['num_turns'])}, "
               f"duration (ms): {_n(audit['duration_ms'])}")
    if audit["malformed_lines"]:
        out += ["", f"{audit['malformed_lines']} malformed log line(s) were ignored."]
    return out


def section_tests(pc: Any) -> list[str]:
    out = ["### Tests written (files in the checked patch)", ""]
    files = pc.get("files") if isinstance(pc, dict) else None
    if not isinstance(files, list):
        out.append("File list unavailable.")
        return out
    if not files:
        out.append("No files.")
    for f in files[:50]:
        if isinstance(f, dict):
            out.append(f"- {sanitize(f.get('path'), 200)}")
    if len(files) > 50:
        out.append(f"- ... and {len(files) - 50} more")
    return out


def section_claims(report: Any, rstatus: str) -> list[str]:
    out = ["### Agent claims (not independently verified)", "",
           "Everything in this section was written by the agent and has not been checked by CI.", ""]
    if rstatus == "missing":
        out.append("No agent report was produced.")
        return out
    if rstatus == "corrupt":
        out.append("The agent report could not be parsed as JSON and was not rendered.")
        return out
    try:
        valid, nerr = validate_report(report)
    except Exception:  # noqa: BLE001 - schema file/library problem must not break the comment
        out.append("Agent report could not be validated and was not rendered.")
        return out
    if not valid:
        out.append(f"Agent report failed schema validation ({nerr} error(s)); its content is not shown.")
        return out
    out.append(f"Decision: {sanitize(report['decision'], 40)} (mode: {sanitize(report['mode'], 20)})")
    out += ["", f"Summary: {sanitize(report['summary'], 600)}"]
    if report["files_analyzed"]:
        out += ["", "Files analyzed:"]
        for f in report["files_analyzed"][:50]:
            out.append(f"- {sanitize(f['path'], 200)} ({sanitize(f['stack'], 20)}), tests needed: "
                       f"{'yes' if f['needs_tests'] else 'no'} - {sanitize(f['reason'], 300)}")
    if report["tests_written"]:
        out += ["", "Tests written (agent description):"]
        for t in report["tests_written"][:50]:
            covers = "; ".join(sanitize(c, 150) for c in t["covers"][:10])
            out.append(f"- {sanitize(t['path'], 200)} ({sanitize(t['action'], 20)}) covers: {covers or 'n/a'}"
                       f" - {sanitize(t['rationale'], 300)}")
    out += ["", "Suspected bugs:"]
    if not report["suspected_bugs"]:
        out.append("- none reported")
    for b in report["suspected_bugs"][:20]:
        line = f":{b['line']}" if isinstance(b["line"], int) else ""
        out.append(f"- {sanitize(b['path'], 200)}{line} - {sanitize(b['description'], 500)}"
                   f" (evidence: {sanitize(b['evidence'], 500)})")
    if report["obsolete_tests"]:
        out += ["", "Obsolete tests (left in place for a human to review):"]
        for t in report["obsolete_tests"][:20]:
            out.append(f"- {sanitize(t['path'], 200)} - {sanitize(t['test_name'], 200)}"
                       f" - {sanitize(t['reason'], 300)}")
    if report["limitations"]:
        out += ["", "Limitations:"]
        out += [f"- {sanitize(x, 300)}" for x in report["limitations"][:20]]
    return out


def truncate(text: str) -> str:
    if len(text) <= MAX_COMMENT:
        return text
    cut = text[: MAX_COMMENT - len(TRUNC_NOTICE)]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + TRUNC_NOTICE


def render(scope: Any, report: Any, rstatus: str, patch_check: Any, pstatus: str,
           verify: Any, vstatus: str, audit: dict[str, Any] | None,
           run_url: str | None, pushed_sha: str | None, outcome: str | None) -> str:
    lines = [MARKER, "## Test agent report", ""]
    url = safe_run_url(run_url)
    sha = safe_sha(pushed_sha)

    if not isinstance(scope, dict):
        lines.append("Scope information is missing or corrupt, so no further details are available.")
    elif scope.get("run_agent") is not True:
        code = sanitize(scope.get("skip_reason"), 40) or "unknown"
        detail = sanitize(scope.get("skip_detail"), 300)
        lines.append(f"**Skipped.** Reason: `{code}`" + (f" - {detail}" if detail else ""))
    else:
        lines.append(f"**Outcome:** {sanitize(outcome, 100) if outcome else 'not provided'}")
        if sha:
            lines.append("")
            lines.append(f"Pushed commit: `{sha}`")
        for sec in (section_verify(verify, vstatus), section_patch(patch_check, pstatus),
                    section_audit(audit), section_tests(patch_check),
                    section_claims(report, rstatus)):
            lines += [""] + sec
    if url:
        lines += ["", f"[Workflow run]({url})"]
    return truncate("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--output", required=True)
    for opt in ("report", "patch-check", "verify", "agent-log", "run-url", "pushed-sha", "outcome"):
        ap.add_argument(f"--{opt}")
    args = ap.parse_args(argv)

    try:
        scope, _ = load_json(args.scope)
        report, rs = load_json(args.report)
        pc, ps = load_json(args.patch_check)
        ver, vs = load_json(args.verify)
        try:
            audit = audit_agent_log(args.agent_log)
        except Exception:  # noqa: BLE001
            audit = None
        text = render(scope, report, rs, pc, ps, ver, vs, audit,
                      args.run_url, args.pushed_sha, args.outcome)
    except Exception:  # noqa: BLE001 - always produce a comment
        text = f"{MARKER}\n## Test agent report\n\nThe report could not be rendered due to an internal error.\n"
    try:
        Path(args.output).write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"cannot write output: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
