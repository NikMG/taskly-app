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


def decision(path, left_untested=None, stack="backend"):
    entry = {"path": path, "stack": stack, "needs_tests": True, "reason": "about the file"}
    if left_untested is not None:
        entry["left_untested"] = left_untested
    return entry


ALL_SKILLS = ("run-sandboxed-tests", "find-coverage-gaps", "report-test-results",
              "write-backend-tests", "write-frontend-tests")


def agent_log(path, skills=ALL_SKILLS):
    content = [{"type": "tool_use", "name": "Skill", "input": {"skill": skill}} for skill in skills]
    path.write_text(json.dumps({"type": "assistant", "message": {"content": content}}) + "\n", encoding="utf-8")
    return path


def run(tmp_path, stack, coverage, files_analyzed, skills=ALL_SKILLS, tests_written=()):
    (tmp_path / "coverage").mkdir(exist_ok=True)
    name = "coverage.json" if stack == "backend" else "coverage-summary.json"
    (tmp_path / "coverage" / name).write_text(json.dumps(coverage), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"files_analyzed": files_analyzed, "tests_written": list(tests_written)}),
                      encoding="utf-8")
    log = agent_log(tmp_path / "agent.log", skills)
    return subprocess.run(
        [sys.executable, str(SCRIPT), stack, "--results", str(tmp_path), "--report", str(report),
         "--agent-log", str(log)],
        capture_output=True, text=True, timeout=30, check=False)


def test_fully_covered_stack_passes_with_an_empty_report(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=0), [])
    assert result.returncode == 0 and "Files with untested code: 0" in result.stdout


def test_gap_missing_from_the_report_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [])
    assert result.returncode == 1
    assert "backend/app/a.py" in result.stdout and "not in the report" in result.stdout


def test_file_listed_without_a_left_untested_reason_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py")])
    assert result.returncode == 1 and "no reason given" in result.stdout


def test_needs_tests_does_not_excuse_a_gap(tmp_path):
    entry = {"path": "backend/app/a.py", "stack": "backend", "needs_tests": False, "reason": "looks fine"}
    assert run(tmp_path, "backend", backend_cov(app__a=3), [entry]).returncode == 1


def test_run_that_skipped_the_report_skill_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=0), [], skills=("run-sandboxed-tests", "find-coverage-gaps"))
    assert result.returncode == 1 and "report-test-results" in result.stdout


def test_writing_skill_is_required_only_if_tests_were_written(tmp_path):
    skills = ("run-sandboxed-tests", "find-coverage-gaps", "report-test-results")
    assert run(tmp_path, "backend", backend_cov(app__a=0), [], skills=skills).returncode == 0
    wrote = run(tmp_path, "backend", backend_cov(app__a=0), [], skills=skills, tests_written=[{"path": "x"}])
    assert wrote.returncode == 1 and "write-backend-tests" in wrote.stdout


def test_unreadable_agent_log_fails_closed(tmp_path):
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage" / "coverage.json").write_text(json.dumps(backend_cov(app__a=0)), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text('{"files_analyzed": []}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "backend", "--results", str(tmp_path), "--report", str(report),
         "--agent-log", str(tmp_path / "missing.log")],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2


def test_gap_left_on_purpose_passes_and_shows_the_reason(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py", "line 3 has no effect")])
    assert result.returncode == 0 and "line 3 has no effect" in result.stdout


def test_gap_without_a_reason_fails(tmp_path):
    result = run(tmp_path, "backend", backend_cov(app__a=3), [decision("backend/app/a.py", "  ")])
    assert result.returncode == 1


def test_decision_for_the_other_stack_does_not_count(tmp_path):
    entry = decision("backend/app/a.py", "reason", stack="frontend")
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
    explained = run(tmp_path, "frontend", coverage, [decision("frontend/src/a.ts", "reason", stack="frontend")])
    assert explained.returncode == 0


def test_frontend_total_is_not_treated_as_a_file(tmp_path):
    coverage = {"total": frontend_entry(5), "/tmp/work/frontend/src/a.ts": frontend_entry(10)}
    assert run(tmp_path, "frontend", coverage, []).returncode == 0


def test_control_characters_in_a_reason_do_not_reach_the_terminal(tmp_path):
    entry = decision("backend/app/a.py", "ok\x1b[2Jclear")
    result = run(tmp_path, "backend", backend_cov(app__a=3), [entry])
    assert result.returncode == 0 and "\x1b" not in result.stdout


@pytest.mark.parametrize("coverage", [{}, {"files": 3}, {"files": {"app/a.py": {}}}])
def test_malformed_coverage_fails_closed(tmp_path, coverage):
    assert run(tmp_path, "backend", coverage, []).returncode == 2


def test_missing_coverage_file_fails_closed(tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"files_analyzed": []}', encoding="utf-8")
    log = agent_log(tmp_path / "agent.log")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "backend", "--results", str(tmp_path), "--report", str(report),
         "--agent-log", str(log)],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2
