import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "check_backfill.py"


def backend_cov(**missing):
    files = {path.replace("__", "/") + ".py": {"summary": {"missing_lines": lines, "missing_branches": 0}}
             for path, lines in missing.items()}
    return {"files": files, "totals": {"percent_covered": 50}}


def decision(path, needs_tests, reason="a specific reason", stack="backend"):
    return {"path": path, "stack": stack, "needs_tests": needs_tests, "reason": reason}


def run(tmp_path, stack, coverage, files_analyzed):
    (tmp_path / "coverage").mkdir(exist_ok=True)
    name = "coverage.json" if stack == "backend" else "coverage-summary.json"
    (tmp_path / "coverage" / name).write_text(json.dumps(coverage), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"files_analyzed": files_analyzed}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), stack, "--results", str(tmp_path), "--report", str(report)],
        capture_output=True, text=True, timeout=30, check=False)


def test_fully_covered_stack_passes_with_an_empty_report(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=0), [])
    assert result.returncode == 0 and "Files with untested code: 0" in result.stdout


def test_gap_missing_from_the_report_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [])
    assert result.returncode == 1
    assert "backend/app/a.py" in result.stdout and "not in the report" in result.stdout


def test_gap_marked_as_needing_tests_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py", True)])
    assert result.returncode == 1 and "still needs tests" in result.stdout


def test_gap_left_on_purpose_passes_and_shows_the_reason(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py", False, "only wiring")])
    assert result.returncode == 0 and "only wiring" in result.stdout


def test_gap_without_a_reason_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py", False, "  ")])
    assert result.returncode == 1


def test_decision_for_the_other_stack_does_not_count(tmp_path):
    entry = decision("backend/app/a.py", False, stack="frontend")
    assert run(tmp_path, "backend", backend_cov(app__a=3), [entry]).returncode == 1


def test_missing_branches_count_as_a_gap(tmp_path):
    coverage = {"files": {"app/a.py": {"summary": {"missing_lines": 0, "missing_branches": 2}}}}
    assert run(tmp_path, "backend", coverage, []).returncode == 1


def frontend_entry(covered, total=10):
    block = {"covered": covered, "total": total}
    return {"lines": block, "statements": block, "functions": {"covered": 1, "total": 1}, "branches": block}


def test_frontend_sandbox_paths_are_matched_to_repository_paths(tmp_path):
    coverage = {"total": frontend_entry(5), "/tmp/work/frontend/src/a.ts": frontend_entry(5)}
    missing = run(tmp_path, "frontend", coverage, [])
    assert missing.returncode == 1 and "frontend/src/a.ts" in missing.stdout
    explained = run(tmp_path, "frontend", coverage, [decision("frontend/src/a.ts", False, stack="frontend")])
    assert explained.returncode == 0


def test_frontend_total_is_not_treated_as_a_file(tmp_path):
    coverage = {"total": frontend_entry(5), "/tmp/work/frontend/src/a.ts": frontend_entry(10)}
    assert run(tmp_path, "frontend", coverage, []).returncode == 0


def test_control_characters_in_a_reason_do_not_reach_the_terminal(tmp_path):
    entry = decision("backend/app/a.py", False, "ok\x1b[2Jclear")
    result = run(tmp_path, "backend", backend_cov(app__a=3), [entry])
    assert result.returncode == 0 and "\x1b" not in result.stdout


@pytest.mark.parametrize("coverage", [{}, {"files": 3}, {"files": {"app/a.py": {}}}])
def test_malformed_coverage_fails_closed(tmp_path, coverage):
    assert run(tmp_path, "backend", coverage, []).returncode == 2


def test_missing_coverage_file_fails_closed(tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"files_analyzed": []}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "backend", "--results", str(tmp_path), "--report", str(report)],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2
