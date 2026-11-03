#!/usr/bin/env python3
"""Re-run the tests with and without the agent's patch and write verify.json.

Runs on the host from the trusted checkout. Test code only ever runs inside
run_tests.sh. Everything read back from results/ was produced by PR-controlled code,
so junit and coverage are treated as hostile claims: size-capped, parsed defensively,
and a missing or malformed report never counts as a pass. Test output is never echoed,
because the runner would parse workflow commands out of the job log.

verify.json: status (passed|failed|skipped|error), reason, preexisting_failures,
patch_sha256, stacks.<stack>.{baseline,with_patch,flaky_rerun,coverage,diff_coverage,static_checks}.
Exit: 0 when verify.json was written, 1 on an internal error (it is still written).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from xml.parsers import expat

HERE = Path(__file__).resolve().parent
STACKS = ("backend", "frontend")
INFRA_EXITS = (2, 124)
FLAKY_RUNS = 2
RUN_TIMEOUT_SECONDS = 600
MAX_XML_BYTES = 20_000_000
MAX_REPORT_BYTES = 50_000_000
RESULT_FILES = ("junit.xml", "coverage/coverage.json", "coverage/coverage-summary.json",
                "coverage/lcov.info")
# Same shapes run_tests.sh accepts, limited to files that hold tests themselves.
RERUN_RES = {
    "backend": re.compile(r"backend/tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]*\.py"),
    "frontend": re.compile(r"frontend/src/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.test\.tsx?"),
}
# Files prettier checks: everything the patch may touch in the frontend.
FORMAT_RE = re.compile(r"frontend/src/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.tsx?")
HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

Runner = Callable[[str, list[str], Path], int]


# ---------------------------------------------------------------- hostile input

class UnsafeXml(Exception):
    pass


def _reject_dtd(data: bytes) -> None:
    def reject(*_: Any) -> None:
        raise UnsafeXml("DTD or entity declaration")
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.Parse(data, True)


def _read_capped(path: Path, cap: int) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > cap:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _int_attr(element: ET.Element, name: str) -> int:
    value = element.get(name, "0")
    return int(value) if value.isascii() and value.isdigit() and len(value) < 10 else 0


def parse_junit(path: Path) -> dict[str, int] | None:
    """Return executed test and failure counts, or None if the report is unusable.

    Skipped tests do not count, so a patch that skips the suite shows up as fewer tests.
    """
    data = _read_capped(path, MAX_XML_BYTES)
    if data is None:
        return None
    try:
        _reject_dtd(data)
        root = ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError, UnsafeXml, ValueError, LookupError, RecursionError):
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    tests = failures = 0
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            continue
        tests += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failures += 1
    declared = sum(_int_attr(s, "failures") + _int_attr(s, "errors") for s in root.iter("testsuite"))
    return {"tests": tests, "failures": max(failures, declared)}


def _load_json(path: Path) -> Any:
    data = _read_capped(path, MAX_REPORT_BYTES)
    if data is None:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2) if math.isfinite(value) and 0 <= value <= 100 else None


def total_coverage(results: Path, stack: str) -> float | None:
    if stack == "backend":
        doc = _load_json(results / "coverage" / "coverage.json")
        totals = doc.get("totals") if isinstance(doc, dict) else None
        return _percent(totals.get("percent_covered")) if isinstance(totals, dict) else None
    doc = _load_json(results / "coverage" / "coverage-summary.json")
    total = doc.get("total") if isinstance(doc, dict) else None
    lines = total.get("lines") if isinstance(total, dict) else None
    return _percent(lines.get("pct")) if isinstance(lines, dict) else None


# ---------------------------------------------------------------- diff coverage

def added_lines(diff: str) -> dict[str, set[int]]:
    """Map each file in a unified diff to its added line numbers. Hunk-aware."""
    added: dict[str, set[int]] = {}
    path: str | None = None
    old_left = new_left = new_no = 0
    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            kind = line[:1]
            if kind == "+":
                if path is not None:
                    added.setdefault(path, set()).add(new_no)
                new_no += 1
                new_left -= 1
            elif kind == "-":
                old_left -= 1
            elif kind != "\\":
                new_no += 1
                new_left -= 1
                old_left -= 1
            continue
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
            # Quoted names are not source files run_tests.sh could cover.
            path = raw[2:] if raw.startswith("b/") else None
        elif line.startswith("@@ "):
            match = HUNK_RE.match(line)
            if match:
                old_left = int(match.group(1)) if match.group(1) is not None else 1
                new_left = int(match.group(3)) if match.group(3) is not None else 1
                new_no = int(match.group(2))
    return added


def _stack_path(key: str, stack: str) -> str:
    key = key.replace("\\", "/")
    prefix = f"/tmp/work/{stack}/"
    if key.startswith(prefix):
        key = key[len(prefix):]
    elif key.startswith("./"):
        key = key[2:]
    return f"{stack}/{key}"


def _int_lines(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {n for n in value if isinstance(n, int) and not isinstance(n, bool)}


def line_coverage(results: Path, stack: str) -> dict[str, tuple[set[int], set[int]]]:
    """Map repo-relative paths to (executable lines, covered lines)."""
    out: dict[str, tuple[set[int], set[int]]] = {}
    doc = _load_json(results / "coverage" / "coverage.json")
    files = doc.get("files") if isinstance(doc, dict) else None
    for key, entry in files.items() if isinstance(files, dict) else ():
        if isinstance(entry, dict):
            executed = _int_lines(entry.get("executed_lines"))
            out[_stack_path(key, stack)] = (executed | _int_lines(entry.get("missing_lines")), executed)
    data = _read_capped(results / "coverage" / "lcov.info", MAX_REPORT_BYTES)
    current: tuple[set[int], set[int]] | None = None
    for line in (data or b"").decode("utf-8", "replace").split("\n"):
        if line.startswith("SF:"):
            current = out.setdefault(_stack_path(line[3:].strip(), stack), (set(), set()))
        elif line.startswith("DA:") and current is not None:
            fields = line[3:].strip().split(",")
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                current[0].add(int(fields[0]))
                if int(fields[1]) > 0:
                    current[1].add(int(fields[0]))
    return out


def diff_coverage(results: Path, stack: str, added: dict[str, set[int]],
                  sources: list[str]) -> float | None:
    """Percent of added executable lines in the PR's source files that ran; None if none."""
    per_file = line_coverage(results, stack)
    executable = covered = 0
    for path in sources:
        lines = added.get(path, set())
        file_exec, file_cov = per_file.get(path, (set(), set()))
        executable += len(lines & file_exec)
        covered += len(lines & file_cov)
    return round(100.0 * covered / executable, 2) if executable else None


# ---------------------------------------------------------------- judging

def run_summary(exit_code: int, junit: dict[str, int] | None) -> dict[str, Any]:
    return {"exit_code": exit_code,
            "tests": junit["tests"] if junit else None,
            "failures": junit["failures"] if junit else None}


def has_preexisting_failures(base: dict[str, Any]) -> bool:
    return bool(base["failures"])


def judge_full_run(base: dict[str, Any], after: dict[str, Any]) -> tuple[str, str | None]:
    """Compare the patched run with PR head before the patch. Returns (status, reason)."""
    if after["exit_code"] in INFRA_EXITS:
        return "error", "with_patch_infrastructure"
    if base["tests"] is None:
        return "failed", "baseline_unusable"
    if after["tests"] is None:
        return "failed", "no_junit"  # includes os._exit(0) from a test
    if after["tests"] == 0:
        return "failed", "no_tests"
    if after["tests"] < base["tests"]:
        return "failed", "fewer_tests"
    if has_preexisting_failures(base):
        if after["failures"] > base["failures"]:
            return "failed", "tests_failed"
        if after["exit_code"] != 0 and not after["failures"]:
            return "failed", "tests_failed"  # nonzero exit that no test failure explains
    elif after["exit_code"] != 0 or after["failures"]:
        return "failed", "tests_failed"
    return "passed", None


def judge_rerun(run: dict[str, Any]) -> bool:
    return run["exit_code"] == 0 and bool(run["tests"]) and run["failures"] == 0


def rerun_paths(stack: str, patch_files: list[str]) -> list[str]:
    return [p for p in patch_files if RERUN_RES[stack].fullmatch(p) and ".." not in p]


def judge_static(runner: Runner, workspace: Path, patch_files: list[str],
                 types_ok_before: bool) -> tuple[str, str | None, dict[str, Any]]:
    """Run CI's frontend checks on the patched tree. Returns (status, reason, details).

    Formatting is judged on the patch's own files. Types are judged on the whole project,
    so a type error that was already in the pull request does not count against the patch.
    """
    files = [p for p in patch_files
             if p.startswith("frontend/") and FORMAT_RE.fullmatch(p) and ".." not in p
             and (workspace / p).is_file()]
    details: dict[str, Any] = {"format": None, "typecheck": None, "typecheck_before": types_ok_before}
    if files:
        rc = runner("frontend", ["--format", "--", *files], Path("frontend-format.log"))
        if rc in INFRA_EXITS:
            return "error", "static_infrastructure", details
        details["format"] = rc == 0
    rc = runner("frontend", ["--typecheck"], Path("frontend-typecheck.log"))
    if rc in INFRA_EXITS:
        return "error", "static_infrastructure", details
    details["typecheck"] = rc == 0
    if details["format"] is False:
        return "failed", "format_failed", details
    if rc != 0 and types_ok_before:
        return "failed", "typecheck_failed", details
    return "passed", None, details


def patch_stacks(patch_files: list[str]) -> set[str]:
    return {p.split("/", 1)[0] for p in patch_files}


# ---------------------------------------------------------------- running

def default_runner(env: dict[str, str], logs: Path) -> Runner:
    script = HERE / "run_tests.sh"

    def run(stack: str, args: list[str], log_name: Path) -> int:
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / log_name, "wb") as log:
            try:
                return subprocess.run([str(script), stack, *args], env=env, stdout=log,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      timeout=RUN_TIMEOUT_SECONDS, check=False).returncode
            except subprocess.TimeoutExpired:
                return 124
    return run


def snapshot(results: Path, dest: Path) -> Path:
    """Keep the reports aside: the next run wipes results/."""
    for rel in RESULT_FILES:
        src = results / rel
        if src.is_file() and not src.is_symlink():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def run_patch_check(patch: Path, output: Path) -> tuple[int, dict[str, Any]]:
    try:
        rc = subprocess.run([sys.executable, "-I", str(HERE / "check_patch.py"), str(patch),
                             "--output", str(output)], capture_output=True, timeout=120,
                            check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 2, {}
    doc = _load_json(output)
    return rc, doc if isinstance(doc, dict) else {}


def apply_patch(workspace: Path, patch: Path) -> bool:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workspace.parent),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "apply", "--whitespace=nowarn", str(patch)],
            cwd=workspace, env=env, capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _empty_stack() -> dict[str, Any]:
    return {"baseline": None, "with_patch": None,
            "flaky_rerun": {"runs": 0, "all_passed": None},
            "coverage": {"before": None, "after": None},
            "diff_coverage": {"before": None, "after": None},
            "static_checks": None}


def _result(status: str, reason: str | None, stacks: dict[str, Any], digest: str | None,
            preexisting: bool = False) -> dict[str, Any]:
    return {"status": status, "reason": reason, "preexisting_failures": preexisting,
            "patch_sha256": digest, "stacks": stacks}


def verify(scope: dict[str, Any], pr_diff: str, patch: Path, workspace: Path, out_root: Path,
           work: Path, runner: Runner) -> dict[str, Any]:
    wanted = scope.get("stacks")
    stacks = [s for s in STACKS if isinstance(wanted, list) and s in wanted]
    if not stacks:
        return _result("error", "no_stacks", {}, None)
    if not patch.is_file():
        return _result("error", "no_patch", {}, None)
    patch_bytes = patch.read_bytes()
    digest = hashlib.sha256(patch_bytes).hexdigest()

    report: dict[str, Any] = {s: _empty_stack() for s in stacks}
    added = added_lines(pr_diff)
    sources = scope.get("source_files") if isinstance(scope.get("source_files"), dict) else {}

    def measure(stack: str, label: str, side: str, exit_code: int) -> dict[str, Any]:
        results = snapshot(out_root / stack / "results", work / label / stack)
        files = sources.get(stack) if isinstance(sources.get(stack), list) else []
        report[stack]["coverage"][side] = total_coverage(results, stack)
        report[stack]["diff_coverage"][side] = diff_coverage(results, stack, added, files)
        return run_summary(exit_code, parse_junit(results / "junit.xml"))

    for stack in stacks:
        rc = runner(stack, ["--coverage"], Path(f"{stack}-baseline.log"))
        report[stack]["baseline"] = measure(stack, "baseline", "before", rc)
    preexisting = any(has_preexisting_failures(report[s]["baseline"]) for s in stacks)
    if any(report[s]["baseline"]["exit_code"] in INFRA_EXITS for s in stacks):
        return _result("error", "baseline_infrastructure", report, digest, preexisting)

    rc, check = run_patch_check(patch, work / "patch_check.json")
    if rc != 0:
        return _result("failed", "patch_rejected", report, digest, preexisting)
    if not patch_bytes:
        return _result("skipped", "empty_patch", report, digest, preexisting)
    entries = check.get("files") if isinstance(check.get("files"), list) else []
    patch_files = [f["path"] for f in entries if isinstance(f, dict) and isinstance(f.get("path"), str)]
    if not patch_files:
        return _result("failed", "patch_rejected", report, digest, preexisting)
    if patch_stacks(patch_files) - set(stacks):
        # No dependencies were prepared for that stack, so it could not be verified.
        return _result("failed", "patch_outside_scope", report, digest, preexisting)
    types_ok_before = True
    if "frontend" in patch_stacks(patch_files):
        rc = runner("frontend", ["--typecheck"], Path("frontend-typecheck-baseline.log"))
        if rc in INFRA_EXITS:
            return _result("error", "baseline_infrastructure", report, digest, preexisting)
        types_ok_before = rc == 0
    if not apply_patch(workspace, patch):
        return _result("failed", "apply_failed", report, digest, preexisting)

    status, reason = "passed", None
    for stack in stacks:
        stack_report = report[stack]
        rc = runner(stack, ["--coverage"], Path(f"{stack}-with-patch.log"))
        stack_report["with_patch"] = measure(stack, "with_patch", "after", rc)
        stack_status, stack_reason = judge_full_run(stack_report["baseline"], stack_report["with_patch"])
        if stack_status == "passed":
            paths = rerun_paths(stack, patch_files)
            passes = runs = 0
            for attempt in range(FLAKY_RUNS if paths else 0):
                rc = runner(stack, ["--", *paths], Path(f"{stack}-rerun-{attempt + 1}.log"))
                runs += 1
                rerun = run_summary(rc, parse_junit(out_root / stack / "results" / "junit.xml"))
                if rc in INFRA_EXITS:
                    stack_status, stack_reason = "error", "rerun_infrastructure"
                elif judge_rerun(rerun):
                    passes += 1
            stack_report["flaky_rerun"] = {"runs": runs, "all_passed": passes == runs if runs else None}
            if stack_status == "passed" and passes != runs:
                stack_status, stack_reason = "failed", "flaky"
        if stack_status == "passed" and stack == "frontend" and "frontend" in patch_stacks(patch_files):
            stack_status, stack_reason, stack_report["static_checks"] = judge_static(
                runner, workspace, patch_files, types_ok_before)
        if stack_status == "error" or (stack_status == "failed" and status == "passed"):
            status, reason = stack_status, stack_reason
    return _result(status, reason, report, digest, preexisting)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--pr-diff", required=True)
    ap.add_argument("--patch", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--deps", required=True)
    ap.add_argument("--work-dir", required=True, help="host-owned scratch space, outside the workspace")
    ap.add_argument("--output-dir", required=True, help="gets verify.json, patch_check.json and logs/")
    args = ap.parse_args(argv)

    output = Path(args.output_dir)
    work = Path(args.work_dir)
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    code = 0
    try:
        scope = _load_json(Path(args.scope))
        if not isinstance(scope, dict):
            result = _result("error", "no_scope", {}, None)
        else:
            diff = (_read_capped(Path(args.pr_diff), MAX_REPORT_BYTES) or b"").decode("utf-8", "replace")
            env = {**os.environ, "AGENT_WORKSPACE": args.workspace, "AGENT_DEPS_DIR": args.deps,
                   "AGENT_OUTPUT_DIR": str(work / "out")}
            result = verify(scope, diff, Path(args.patch), Path(args.workspace), work / "out", work,
                            default_runner(env, output / "logs"))
    except Exception as e:  # noqa: BLE001 - a broken verifier must never read as a pass
        print(f"verify_patch.py: internal error: {type(e).__name__}", file=sys.stderr)
        result, code = _result("error", "internal_error", {}, None), 1
    if (work / "patch_check.json").is_file():
        shutil.copyfile(work / "patch_check.json", output / "patch_check.json")
    (output / "verify.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"verify_patch.py: status={result['status']} reason={result['reason']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
