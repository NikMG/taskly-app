#!/usr/bin/env python3
"""Verify an agent-produced patch only touches allowed test files and is not hostile.

The authoritative file list comes from git's own parser (`git apply --numstat -z`
and `--summary`, run in an empty temp dir, so nothing touches a worktree). We also
parse the patch text ourselves (hunk-aware) for the added lines and to detect
parser differentials: if the two file sets or line counts disagree, that is a
`parser_mismatch` violation. Any inability to parse exits 2 (fail closed).

The content rules (disabled_test, dangerous_call, trivial_assertion, ...) are a
best-effort heuristic: they catch mistakes and lazy test-weakening, not a determined
adversary. Real containment is the sandbox and the test-count comparison in verify.

Exit codes: 0 ok, 1 violations, 2 unparseable / internal error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any

ALLOWED_PATH_RES = [re.compile(p) for p in (
    r"backend/tests/(?:.+/)?test_[^/]*\.py",
    r"backend/tests/(?:.+/)?conftest\.py",
    r"backend/tests/(?:.+/)?__init__\.py",
    r"backend/tests/mocks\.py",
    r"frontend/src/(?:.+/)?[^/]+\.test\.tsx?",
    r"frontend/src/test/(?:.+/)?[^/]+\.tsx?",
)]
# files whose new-file form must contain assertions
TEST_FILE_RES = [re.compile(r"(?:.*/)?test_[^/]*\.py"), re.compile(r".*\.test\.tsx?")]
SUPPORT_FILE_RES = [re.compile(p) for p in (
    r"(?:.*/)?conftest\.py", r"backend/tests/mocks\.py", r"frontend/src/test/.*",
    r"(?:.*/)?__init__\.py",
)]

DISABLED_RES = [re.compile(p) for p in (
    r"\.only\(", r"\.skip\(", r"\.skipIf\(", r"\.runIf\(", r"\.todo\(", r"\.fails\(",
    r"(?<![\w.])(?:xit|xdescribe|fit|fdescribe)\(",
    r"pytest\.mark\.skip", r"xfail", r"pytest\.skip\(", r"importorskip",
    r"@ts-nocheck", r"@ts-ignore", r"@ts-expect-error",
    # any member access, so chains such as it.skip.each(...) match; `.todos`,
    # `.skipWhile` and `todoKeys` do not (the name must end at ( or a known modifier)
    r"\.(?:skip|only|todo|fails|skipIf|runIf)(?:\(|\.(?:each|for|concurrent|sequential|"
    r"skip|only|todo|fails|skipIf|runIf)\b)",
    r"\[\s*['\"`](?:skip|only|todo|fails|skipIf|runIf)['\"`]\s*\]",
    r"\bcollect_ignore(?:_glob)?\b", r"\b__test__\b", r"\bpytest_collection_modifyitems\b",
    r"\bpytest_ignore_collect\b", r"\bpytest_collect_file\b", r"\bpytest_runtest_",
    r"\bpytest_pycollect_", r"\btestPathIgnorePatterns\b", r"\bpassWithNoTests\b",
)]
_LIT = r"(?:True|\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'|true)"
TRIVIAL_RES = [re.compile(p) for p in (
    rf"(?<![\w.])assert\s*\(?\s*{_LIT}\s*(?:(?:==|is)\s*{_LIT}\s*)?\)?\s*(?:,.*)?;?\s*$",
    rf"\bexpect\(\s*{_LIT}\s*\)\.(?:toBe|toEqual|toStrictEqual)\(\s*{_LIT}\s*\)",
    rf"\bexpect\(\s*{_LIT}\s*\)\.(?:toBeTruthy|toBeDefined)\(\s*\)",
)]

_HTTPX_VERBS = r"(?:get|post|put|patch|delete|head|options|request|stream|Client|AsyncClient)"
DANGEROUS_RES = [(re.compile(p), n) for p, n in (
    (r"\bsubprocess\b", "subprocess"),
    (r"\bos\.(?:system|popen|exec\w*|spawn\w*|putenv)\b", "os process call"),
    (r"(?<![\w.])(?:eval|exec)\(", "eval/exec"),
    (r"\b(?:window|globalThis|builtins|self)\.(?:eval|exec)\(", "eval/exec"),
    (r"\bchild_process\b", "child_process"),
    (r"\bsocket\.|^\s*(?:import|from)\s+socket\b", "socket"),
    (r"\burllib\b", "urllib"),
    (r"\brequests\.|^\s*(?:import|from)\s+requests\b", "requests"),
    (r"fetch\(\s*[`'\"]https?:", "network fetch"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
    (r"\bprocess\.env\b", "process.env (use vi.stubEnv)"),
    (r"\b__import__\(", "__import__"),
    (r"\bimportlib\b", "importlib"),
    (r"\bgetattr\(\s*(?:os|subprocess)\b", "getattr on os/subprocess"),
)]
# httpx: only real request calls / free-standing clients are flagged. A client built
# on an in-process ASGI/WSGI transport on the same line is allowed, and so is
# `import httpx` or type references. Multi-line constructions are not recognised.
HTTPX_RE = re.compile(rf"\bhttpx\.{_HTTPX_VERBS}\b")
HTTPX_OK_RE = re.compile(r"ASGITransport|WSGITransport")
ENV_WRITE_RE = re.compile(
    r"os\.environ\s*\[[^\]]*\]\s*=(?!=)|os\.environ\.(?:update|setdefault|pop|clear|popitem)\(|"
    r"\bdel\s+os\.environ|os\.environ\s*(?:\|=|=(?!=))")
ASSERT_RE = re.compile(r"(?<![\w.])assert\s|\bexpect\(|pytest\.raises|toHaveBeenCalled")
REMOVED_TEST_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+test_\w+|^\s*(?:it|test)(?:\.\w+)?\(\s*[`'\"]")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


# ---------------------------------------------------------------- git's view

def _git_apply(patch: bytes, *flags: str) -> bytes:
    with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as home:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "LC_ALL": "C"}
        try:
            p = subprocess.run(["git", "apply", *flags], input=patch, cwd=empty, env=env,
                               capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PatchError(f"git apply could not run: {e}") from e
    if p.returncode != 0:
        raise PatchError("git apply rejected the patch: " +
                         p.stderr.decode("utf-8", "replace").strip()[:300])
    return p.stdout


def _expand_rename(body: str) -> tuple[str, str]:
    """Expand git's `pre{old => new}post` / `old => new` rename summary form."""
    m = re.fullmatch(r"(.*)\{(.*) => (.*)\}(.*)", body)
    if m:
        pre, old, new, post = m.groups()
        return (pre + old + post).replace("//", "/"), (pre + new + post).replace("//", "/")
    if body.count(" => ") != 1:
        raise PatchError("ambiguous rename line in git summary")
    old, new = body.split(" => ")
    return old, new


def git_view(patch: bytes) -> dict[str, Any]:
    raw = _git_apply(patch, "--numstat", "-z").decode("utf-8", "surrogateescape")
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    numstat: list[tuple[str, int | None, int | None]] = []
    for tok in tokens:
        parts = tok.split("\t", 2)
        if len(parts) != 3:
            # `git apply` emits the old/new path tokens of renames only in the
            # summary, so anything else here is not a record we understand.
            raise PatchError("unexpected numstat output")
        a, r, path = parts
        if path == "":
            raise PatchError("unexpected empty path in numstat")
        if a == "-" and r == "-":
            numstat.append((path, None, None))
        elif a.isdigit() and r.isdigit():
            numstat.append((path, int(a), int(r)))
        else:
            raise PatchError("bad numstat counts")

    summary = _git_apply(patch, "--summary").decode("utf-8", "surrogateescape")
    creates: dict[str, str] = {}
    deletes: set[str] = set()
    mode_changes = 0
    bad_modes: list[str] = []
    renames: list[tuple[str, str]] = []
    for line in summary.split("\n"):
        m = re.fullmatch(r" create mode (\d+) (.*)", line)
        if m:
            creates[m.group(2)] = m.group(1)
            continue
        m = re.fullmatch(r" delete mode (\d+) (.*)", line)
        if m:
            deletes.add(m.group(2))
            if m.group(1) != "100644":
                bad_modes.append(m.group(2))
            continue
        if re.match(r" mode change \d+ => \d+", line):
            mode_changes += 1
            continue
        m = re.fullmatch(r" (?:rename|copy) (.*) \(\d+%\)", line)
        if m:
            renames.append(_expand_rename(m.group(1)))
    return {"numstat": numstat, "creates": creates, "deletes": deletes,
            "mode_changes": mode_changes, "bad_modes": bad_modes, "renames": renames}


# ---------------------------------------------------------------- our own parse

def _strip_prefix(p: str) -> str | None:
    p = p.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    return p.split("/", 1)[1] if "/" in p else p


def _split_git_header(rest: str) -> str | None:
    """Return the single path if `a/P b/P` (unambiguous), else None."""
    start = 0
    while True:
        i = rest.find(" b/", start)
        if i < 0:
            return None
        left, right = rest[:i], rest[i + 1:]
        if left.startswith("a/") and left[2:] == right[2:]:
            return left[2:]
        start = i + 1


def parse_text(text: str) -> list[dict[str, Any]]:
    """Hunk-aware parse. Splits on \\n only, like git (never str.splitlines)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    old_left = new_left = 0
    new_no = 0
    pending_minus: str | None = None

    def new_file(header_path: str | None) -> dict[str, Any]:
        f = {"paths": set(), "added": [], "removed": [], "modes": [], "deleted": False,
             "created": False, "binary": False, "quoted": False,
             "plus_seen": False}
        if header_path is not None:
            f["paths"].add(header_path)
        files.append(f)
        return f

    for idx, line in enumerate(lines, 1):
        if old_left > 0 or new_left > 0:
            c = line[:1]
            if c == " " or line == "":
                old_left -= 1
                new_left -= 1
                new_no += 1
            elif c == "-":
                old_left -= 1
                cur["removed"].append(line[1:])
            elif c == "+":
                new_left -= 1
                cur["added"].append((new_no, line[1:]))
                new_no += 1
            elif c == "\\":
                pass
            else:
                raise PatchError(f"line {idx}: unexpected line inside hunk")
            if old_left < 0 or new_left < 0:
                raise PatchError(f"line {idx}: hunk longer than declared")
            continue
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            cur = new_file(_split_git_header(rest))
            if '"' in rest:
                cur["quoted"] = True
            pending_minus = None
        elif line.startswith("--- "):
            pending_minus = line[4:]
        elif line.startswith("+++ ") and pending_minus is not None:
            if cur is None or cur["plus_seen"]:
                cur = new_file(None)
            cur["plus_seen"] = True
            for raw in (pending_minus, line[4:]):
                if raw.startswith('"'):
                    cur["quoted"] = True
                p = _strip_prefix(raw)
                if p is not None:
                    cur["paths"].add(p)
            if pending_minus == "/dev/null":
                cur["created"] = True
            if line[4:].split("\t", 1)[0] == "/dev/null":
                cur["deleted"] = True
            pending_minus = None
        elif cur is not None and line.startswith(("rename from ", "rename to ",
                                                  "copy from ", "copy to ")):
            cur["paths"].add(line.split(" ", 2)[2])
        elif cur is not None and line.startswith(("new file mode ", "deleted file mode ",
                                                  "old mode ", "new mode ")):
            cur["modes"].append(line)
            if line.startswith("new file mode "):
                cur["created"] = True
            elif line.startswith("deleted file mode "):
                cur["deleted"] = True
        elif cur is not None and line.startswith("index "):
            m = re.fullmatch(r"index \S+\.\.\S+ (\d+)", line)
            if m:
                cur["modes"].append(line)
        elif cur is not None and (line.startswith("GIT binary patch")
                                  or (line.startswith("Binary files ") and line.endswith(" differ"))):
            cur["binary"] = True
        elif line.startswith("@@ "):
            m = HUNK_RE.match(line)
            if not m or cur is None:
                raise PatchError(f"line {idx}: bad hunk header")
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            new_no = int(m.group(3))
            pending_minus = None
    if old_left > 0 or new_left > 0:
        raise PatchError("patch ends inside a hunk")
    return files


# ---------------------------------------------------------------- rules

def _unsafe_reason(p: str) -> str | None:
    if p.startswith('"'):
        return "git-quoted path"
    if p == "" or p.startswith("/"):
        return "empty or absolute path"
    if "\\" in p:
        return "backslash in path"
    if any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) < 0xA0 for c in p):
        return "control character in path"
    segs = p.split("/")
    if ".." in segs or "." in segs or "" in segs:
        return "path traversal or non-normalized path"
    if any(s.casefold() == ".git" for s in segs):
        return ".git path segment"
    return None


def path_allowed(p: str) -> bool:
    return any(r.fullmatch(p) for r in ALLOWED_PATH_RES)


def _is_support(p: str) -> bool:
    return any(r.fullmatch(p) for r in SUPPORT_FILE_RES)


def analyze(patch: bytes, max_files: int, max_added: int) -> dict[str, Any]:
    if not patch:
        return {"ok": True, "violations": [], "warnings": [], "files": [],
                "stats": {"files": 0, "added": 0, "removed": 0,
                          "assertions_added": 0, "assertions_removed": 0}}
    gv = git_view(patch)
    tfiles = parse_text(patch.decode("utf-8", "surrogateescape"))

    viol: list[dict[str, Any]] = []
    warns: list[dict[str, Any]] = []

    def v(file: str, rule: str, detail: str, line: int | None = None) -> None:
        viol.append({"file": file, "rule": rule, "line": line, "detail": detail})

    git_paths: set[str] = {p for p, _, _ in gv["numstat"]}
    for old, new in gv["renames"]:
        git_paths.update((old, new))
    text_paths: set[str] = set().union(*(f["paths"] for f in tfiles)) if tfiles else set()
    if git_paths != text_paths:
        extra_t = sorted(text_paths - git_paths)[:5]
        extra_g = sorted(git_paths - text_paths)[:5]
        v("", "parser_mismatch",
          f"git and text parsers disagree on files (text-only {extra_t}, git-only {extra_g})")

    # per-path line counts must agree between the parsers (binary excluded)
    git_counts: dict[str, list[int]] = {}
    binary_paths: set[str] = set()
    for p, a, r in gv["numstat"]:
        if a is None:
            binary_paths.add(p)
            continue
        c = git_counts.setdefault(p, [0, 0])
        c[0] += a
        c[1] += r
    text_counts: dict[str, list[int]] = {}
    for f in tfiles:
        if f["binary"]:
            continue
        key = _final_path(f, gv)
        c = text_counts.setdefault(key, [0, 0])
        c[0] += len(f["added"])
        c[1] += len(f["removed"])
    for p in set(git_counts) | set(text_counts):
        if git_counts.get(p, [0, 0]) != text_counts.get(p, [0, 0]) and p in git_paths:
            v(p, "parser_mismatch", "git and text parsers disagree on changed line counts")

    all_paths = sorted(git_paths | text_paths)
    for p in all_paths:
        why = _unsafe_reason(p)
        if why:
            v(p, "unsafe_path", why)
        if not path_allowed(p):
            v(p, "path_not_allowed", "path is not an allowed test file location")
    for f in tfiles:
        if f["quoted"]:
            v(sorted(f["paths"])[0] if f["paths"] else "", "unsafe_path", "git-quoted path")

    for p, mode in gv["creates"].items():
        if mode != "100644":
            v(p, "file_mode", f"new file mode {mode} (only regular non-executable files allowed)")
    for p in gv["bad_modes"]:
        v(p, "file_mode", "deleted file with non-regular mode")
    if gv["mode_changes"]:
        v("", "file_mode", "mode change present")
    for f in tfiles:
        name = _final_path(f, gv)
        for m in f["modes"]:
            mm = re.search(r"(\d{6})$", m)
            if m.startswith(("old mode", "new mode")):
                v(name, "file_mode", "mode change present")
            elif mm and mm.group(1) != "100644":
                v(name, "file_mode", f"unexpected file mode {mm.group(1)}")
    for p in sorted(binary_paths):
        v(p, "binary", "binary change")
    for f in tfiles:
        if f["binary"] and not binary_paths:
            v(_final_path(f, gv), "binary", "binary change")
    for p in sorted(gv["deletes"]):
        v(p, "deleted_test_file", "whole-file deletion")
    for f in tfiles:
        if f["deleted"] and _final_path(f, gv) not in gv["deletes"]:
            v(_final_path(f, gv), "deleted_test_file", "whole-file deletion")

    a_added = a_removed = 0
    for f in tfiles:
        name = _final_path(f, gv)
        file_assert = 0
        for lineno, text in f["added"]:
            n = len(ASSERT_RE.findall(text))
            if any(r.search(text) for r in TRIVIAL_RES):
                v(name, "trivial_assertion", "added line asserts a constant", lineno)
                n = 0
            a_added += n
            file_assert += n
            for r in DISABLED_RES:
                if r.search(text):
                    v(name, "disabled_test", f"added line matches {r.pattern!r}", lineno)
                    break
            for r, label in DANGEROUS_RES:
                if r.search(text):
                    v(name, "dangerous_call", f"added line uses {label}", lineno)
                    break
            else:
                if HTTPX_RE.search(text) and not HTTPX_OK_RE.search(text):
                    v(name, "dangerous_call", "added line uses httpx request/client", lineno)
                elif "os.environ" in text and ENV_WRITE_RE.search(text) \
                        and "monkeypatch" not in text:
                    v(name, "dangerous_call",
                      "os.environ write outside monkeypatch", lineno)
        removed_funcs = 0
        for text in f["removed"]:
            a_removed += len(ASSERT_RE.findall(text))
            if REMOVED_TEST_RE.search(text):
                removed_funcs += 1
        if removed_funcs:
            warns.append({"file": name, "rule": "removed_test", "line": None,
                          "detail": f"{removed_funcs} test definition line(s) removed"})
        if f["created"] and file_assert == 0 and not f["binary"] \
                and any(r.fullmatch(name) for r in TEST_FILE_RES):
            v(name, "no_assertions", "new test file contains no assertions")
        if path_allowed(name) and _is_support(name):
            warns.append({"file": name, "rule": "support_file_changed", "line": None,
                          "detail": "conftest/setup/fixture file changed"})
    if a_removed > a_added:
        v("", "assertions_reduced",
          f"{a_removed} assertions removed but only {a_added} added")

    stats = {"files": len(gv["numstat"]),
             "added": sum(a or 0 for _, a, _ in gv["numstat"]),
             "removed": sum(r or 0 for _, _, r in gv["numstat"]),
             "assertions_added": a_added, "assertions_removed": a_removed}
    if stats["files"] > max_files:
        v("", "too_large", f"{stats['files']} files exceeds limit {max_files}")
    if stats["added"] > max_added:
        v("", "too_large", f"{stats['added']} added lines exceeds limit {max_added}")

    files_out = [{"path": p, "change": "binary" if a is None else "text"}
                 for p, a, _ in gv["numstat"]]
    return {"ok": not viol, "violations": viol, "warnings": warns,
            "files": files_out, "stats": stats}


def _final_path(f: dict[str, Any], gv: dict[str, Any]) -> str:
    """Pick the path git reports for this section (new side of a rename)."""
    paths = f["paths"]
    renamed_new = {new for _, new in gv["renames"]}
    cands = sorted(paths & renamed_new) or sorted(paths)
    return cands[0] if cands else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("patch")
    ap.add_argument("--output")
    ap.add_argument("--max-files", type=int, default=30)
    ap.add_argument("--max-added-lines", type=int, default=1500)
    args = ap.parse_args(argv)

    code = 0
    try:
        with open(args.patch, "rb") as f:
            data = f.read()
        result = analyze(data, args.max_files, args.max_added_lines)
        code = 0 if result["ok"] else 1
    except (PatchError, OSError, ValueError, RecursionError) as e:
        result = {"ok": False, "error": str(e)[:500], "violations": [], "warnings": [],
                  "files": [], "stats": {"files": 0, "added": 0, "removed": 0,
                                         "assertions_added": 0, "assertions_removed": 0}}
        code = 2
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
                f.write("\n")
        except OSError as e:
            print(f"cannot write output: {e}", file=sys.stderr)
            return 2
    for item in result["violations"]:
        print(f"VIOLATION {item['rule']}: {item['file']!r} {item['detail']}", file=sys.stderr)
    if code == 2:
        print(f"ERROR: {result['error']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
