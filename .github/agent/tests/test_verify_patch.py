import json
import subprocess
import time
from pathlib import Path

import pytest

import verify_patch as vp

GOOD_JUNIT = '<testsuites><testsuite name="s" tests="{n}" failures="0" errors="0">{cases}</testsuite></testsuites>'


def junit(passed=2, failed=0, skipped=0):
    cases = ['<testcase name="p"/>'] * passed
    cases += ['<testcase name="f"><failure message="x"/></testcase>'] * failed
    cases += ['<testcase name="s"><skipped/></testcase>'] * skipped
    return (f'<testsuites><testsuite tests="{passed + failed + skipped}" failures="{failed}" errors="0">'
            + "".join(cases) + "</testsuite></testsuites>")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


# ---------------------------------------------------------------- junit

def test_junit_counts(tmp_path):
    p = write(tmp_path / "j.xml", junit(3, 1, 2))
    assert vp.parse_junit(p) == {"tests": 4, "failures": 1}


def test_junit_single_testsuite_root_and_error_child(tmp_path):
    p = write(tmp_path / "j.xml", '<testsuite><testcase name="a"><error/></testcase><testcase name="b"/></testsuite>')
    assert vp.parse_junit(p) == {"tests": 2, "failures": 1}


def test_junit_declared_failures_win_over_counted(tmp_path):
    p = write(tmp_path / "j.xml", '<testsuite failures="3" errors="1"><testcase name="a"/></testsuite>')
    assert vp.parse_junit(p) == {"tests": 1, "failures": 4}


def test_junit_all_skipped_counts_zero(tmp_path):
    assert vp.parse_junit(write(tmp_path / "j.xml", junit(0, 0, 5))) == {"tests": 0, "failures": 0}


@pytest.mark.parametrize("doc", [
    '<!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><testsuite><testcase name="&b;"/></testsuite>',
    '<!DOCTYPE t SYSTEM "file:///etc/passwd"><testsuite><testcase name="a"/></testsuite>',
    '<!DOCTYPE testsuite><testsuite><testcase name="a"/></testsuite>',
    "not xml", "", "<testsuite>", '<html><testcase name="a"/></html>',
    '<?xml version="1.0" encoding="no-such"?><testsuite/>',
])
def test_junit_hostile_or_malformed_is_unusable(tmp_path, doc):
    assert vp.parse_junit(write(tmp_path / "j.xml", doc)) is None


def test_junit_doctype_hidden_in_utf16_is_rejected(tmp_path):
    doc = '<!DOCTYPE t [<!ENTITY a "x">]><testsuite><testcase name="&a;"/></testsuite>'
    assert vp.parse_junit(write(tmp_path / "j.xml", doc.encode("utf-16"))) is None


def test_junit_size_cap(tmp_path, monkeypatch):
    p = write(tmp_path / "j.xml", junit())
    monkeypatch.setattr(vp, "MAX_XML_BYTES", 10)
    assert vp.parse_junit(p) is None


def test_junit_deep_nesting_is_survivable(tmp_path):
    n = 200_000
    p = write(tmp_path / "j.xml", "<testsuite>" + "<a>" * n + "</a>" * n + "</testsuite>")
    assert vp.parse_junit(p) == {"tests": 0, "failures": 0}


def test_junit_symlink_and_missing(tmp_path):
    target = write(tmp_path / "real.xml", junit())
    link = tmp_path / "link.xml"
    link.symlink_to(target)
    assert vp.parse_junit(link) is None and vp.parse_junit(tmp_path / "none.xml") is None


# ---------------------------------------------------------------- coverage

def test_total_coverage(tmp_path):
    write(tmp_path / "coverage/coverage.json", json.dumps({"totals": {"percent_covered": 81.234}}))
    write(tmp_path / "coverage/coverage-summary.json", json.dumps({"total": {"lines": {"pct": 55.5}}}))
    assert vp.total_coverage(tmp_path, "backend") == 81.23
    assert vp.total_coverage(tmp_path, "frontend") == 55.5


@pytest.mark.parametrize("doc", ['{"totals": {"percent_covered": "90"}}', '{"totals": {"percent_covered": 900}}',
                                 '{"totals": {"percent_covered": true}}', '{"totals": 3}', "[]", "{nope",
                                 '{"totals": {"percent_covered": NaN}}', '{"totals": {"percent_covered": -1}}'])
def test_total_coverage_bad_claims(tmp_path, doc):
    write(tmp_path / "coverage/coverage.json", doc)
    assert vp.total_coverage(tmp_path, "backend") is None


def test_total_coverage_missing(tmp_path):
    assert vp.total_coverage(tmp_path, "backend") is None and vp.total_coverage(tmp_path, "frontend") is None


DIFF = """diff --git a/backend/app/a.py b/backend/app/a.py
index 111..222 100644
--- a/backend/app/a.py
+++ b/backend/app/a.py
@@ -1,3 +1,4 @@
 one
+two
 three
 four
@@ -10 +11,2 @@ def x():
-old
+new1
+new2
diff --git a/backend/app/b c.py b/backend/app/b c.py
new file mode 100644
--- /dev/null
+++ b/backend/app/b c.py\t
@@ -0,0 +1 @@
++++ b/backend/app/fake.py
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def test_added_lines_is_hunk_aware():
    assert vp.added_lines(DIFF) == {"backend/app/a.py": {2, 11, 12}, "backend/app/b c.py": {1}}


def test_diff_coverage_from_coverage_json(tmp_path):
    write(tmp_path / "coverage/coverage.json", json.dumps({"files": {
        "app/a.py": {"executed_lines": [1, 2, 11], "missing_lines": [12, 13]},
        "/tmp/work/backend/app/b c.py": {"executed_lines": [], "missing_lines": []},
    }}))
    added = vp.added_lines(DIFF)
    assert vp.diff_coverage(tmp_path, "backend", added, ["backend/app/a.py", "backend/app/b c.py"]) == 66.67


def test_diff_coverage_from_lcov(tmp_path):
    write(tmp_path / "coverage/lcov.info", "TN:\nSF:src/a.ts\nDA:1,3\nDA:2,0\nDA:bad\nend_of_record\n"
                                           "SF:/tmp/work/frontend/src/b.ts\nDA:5,1\nend_of_record\n")
    added = {"frontend/src/a.ts": {1, 2, 9}, "frontend/src/b.ts": {5}, "frontend/src/other.ts": {1}}
    files = ["frontend/src/a.ts", "frontend/src/b.ts", "frontend/src/other.ts"]
    assert vp.diff_coverage(tmp_path, "frontend", added, files) == 66.67


def test_diff_coverage_null_when_no_executable_changed_line(tmp_path):
    write(tmp_path / "coverage/coverage.json", json.dumps({"files": {"app/a.py": {"executed_lines": [1], "missing_lines": []}}}))
    assert vp.diff_coverage(tmp_path, "backend", {"backend/app/a.py": {9}}, ["backend/app/a.py"]) is None
    assert vp.diff_coverage(tmp_path, "backend", {}, []) is None
    assert vp.diff_coverage(tmp_path / "nothing", "backend", {"backend/app/a.py": {1}}, ["backend/app/a.py"]) is None


def test_diff_coverage_only_counts_scope_source_files(tmp_path):
    write(tmp_path / "coverage/coverage.json", json.dumps({"files": {"app/a.py": {"executed_lines": [], "missing_lines": [1]}}}))
    assert vp.diff_coverage(tmp_path, "backend", {"backend/app/a.py": {1}}, []) is None


@pytest.mark.parametrize("doc", ['{"files": 3}', '{"files": {"app/a.py": 1}}',
                                 '{"files": {"app/a.py": {"executed_lines": "x", "missing_lines": [true, "1"]}}}'])
def test_diff_coverage_tolerates_garbage(tmp_path, doc):
    write(tmp_path / "coverage/coverage.json", doc)
    assert vp.diff_coverage(tmp_path, "backend", {"backend/app/a.py": {1}}, ["backend/app/a.py"]) is None


# ---------------------------------------------------------------- judging

def s(code=0, tests=5, failures=0):
    return {"exit_code": code, "tests": tests, "failures": failures}


@pytest.mark.parametrize("base,after,expected", [
    (s(), s(tests=7), ("passed", None)),
    (s(), s(tests=5), ("passed", None)),
    (s(), s(1, 6, 1), ("failed", "tests_failed")),
    (s(), s(tests=4), ("failed", "fewer_tests")),
    (s(), s(0, None, None), ("failed", "no_junit")),
    (s(), s(1, None, None), ("failed", "no_junit")),
    (s(), s(0, 0, 0), ("failed", "no_tests")),
    (s(tests=0), s(0, 0, 0), ("failed", "no_tests")),
    (s(), s(1, 6, 0), ("failed", "tests_failed")),
    (s(), s(0, 6, 1), ("failed", "tests_failed")),
    (s(), s(2, None, None), ("error", "with_patch_infrastructure")),
    (s(), s(124, None, None), ("error", "with_patch_infrastructure")),
    (s(0, None, None), s(tests=9), ("failed", "baseline_unusable")),
    (s(1, None, None), s(tests=9), ("failed", "baseline_unusable")),
    # pre-existing failures
    (s(1, 5, 2), s(1, 8, 2), ("passed", None)),
    (s(1, 5, 2), s(0, 8, 0), ("passed", None)),
    (s(1, 5, 2), s(1, 8, 1), ("passed", None)),
    (s(1, 5, 2), s(1, 8, 3), ("failed", "tests_failed")),
    (s(1, 5, 2), s(1, 4, 1), ("failed", "fewer_tests")),
    (s(1, 5, 2), s(1, 8, 0), ("failed", "tests_failed")),
    (s(1, 5, 2), s(1, None, None), ("failed", "no_junit")),
    # exit 1 without failing tests is not a pre-existing failure, so it earns no leniency
    (s(1, 0, 0), s(1, 3, 0), ("failed", "tests_failed")),
    (s(1, 0, 0), s(0, 3, 0), ("passed", None)),
])
def test_judge_full_run(base, after, expected):
    assert vp.judge_full_run(base, after) == expected


@pytest.mark.parametrize("run,ok", [(s(), True), (s(1), False), (s(0, 0, 0), False),
                                    (s(0, None, None), False), (s(0, 5, 1), False)])
def test_judge_rerun(run, ok):
    assert vp.judge_rerun(run) is ok


def test_rerun_paths():
    files = ["backend/tests/test_a.py", "backend/tests/x/test_b.py", "backend/tests/conftest.py",
             "backend/tests/mocks.py", "backend/tests/test_with space.py", "frontend/src/a.test.ts",
             "frontend/src/x/b.test.tsx", "frontend/src/test/setup.ts"]
    assert vp.rerun_paths("backend", files) == ["backend/tests/test_a.py", "backend/tests/x/test_b.py"]
    assert vp.rerun_paths("frontend", files) == ["frontend/src/a.test.ts", "frontend/src/x/b.test.tsx"]


# ---------------------------------------------------------------- whole flow, real git and check_patch

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout


GOOD_TEST = "def test_new():\n    from app.a import f\n    assert f(1) == 2\n"


class Flow:
    """A real repo and real check_patch; only run_tests.sh is replaced by a script of results."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        (self.repo / "backend/tests").mkdir(parents=True)
        (self.repo / "backend/app").mkdir(parents=True)
        (self.repo / "backend/app/a.py").write_text("def f(x):\n    return x + 1\n")
        (self.repo / "backend/tests/test_a.py").write_text("def test_a():\n    assert 1 + 1 == 2 or True\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.out = tmp_path / "out"
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.calls = []
        self.plan = []
        self.scope = {"stacks": ["backend"], "source_files": {"backend": ["backend/app/a.py"], "frontend": []}}

    def patch_adding(self, name="backend/tests/test_new.py", content=GOOD_TEST):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(self.repo, "add", "-N", ".")
        text = git(self.repo, "diff", "--binary", "--no-color")
        git(self.repo, "reset", "-q")
        target.unlink()
        return write(self.tmp / "agent.patch", text)

    def runner(self, stack, args, log):
        self.calls.append((stack, args))
        step = self.plan.pop(0)
        results = self.out / stack / "results"
        if results.exists():
            for f in sorted(results.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
        results.mkdir(parents=True, exist_ok=True)
        if step.get("junit") is not None:
            write(results / "junit.xml", step["junit"])
        if step.get("coverage") is not None:
            write(results / "coverage/coverage.json", json.dumps(step["coverage"]))
        return step.get("rc", 0)

    def run(self, patch):
        return vp.verify(self.scope, "", patch, self.repo, self.out, self.work, self.runner)


@pytest.fixture
def flow(tmp_path):
    return Flow(tmp_path)


def test_flow_pass_keeps_baseline_and_applies(flow):
    cov = lambda pct: {"totals": {"percent_covered": pct}, "files": {}}
    flow.plan = [{"junit": junit(2), "coverage": cov(50)}, {"junit": junit(3), "coverage": cov(80)},
                 {"junit": junit(1)}, {"junit": junit(1)}]
    r = flow.run(flow.patch_adding())
    assert r["status"] == "passed" and r["reason"] is None and r["preexisting_failures"] is False
    stack = r["stacks"]["backend"]
    assert stack["baseline"] == {"exit_code": 0, "tests": 2, "failures": 0}
    assert stack["with_patch"] == {"exit_code": 0, "tests": 3, "failures": 0}
    assert stack["flaky_rerun"] == {"runs": 2, "all_passed": True}
    assert stack["coverage"] == {"before": 50.0, "after": 80.0}
    assert (flow.repo / "backend/tests/test_new.py").is_file()
    assert flow.calls == [("backend", ["--coverage"]), ("backend", ["--coverage"]),
                          ("backend", ["--", "backend/tests/test_new.py"]),
                          ("backend", ["--", "backend/tests/test_new.py"])]
    assert len(r["patch_sha256"]) == 64


def test_flow_empty_patch_is_skipped_and_nothing_applied(flow):
    flow.plan = [{"junit": junit(2)}]
    r = flow.run(write(flow.tmp / "agent.patch", b""))
    assert (r["status"], r["reason"]) == ("skipped", "empty_patch")
    assert r["stacks"]["backend"]["with_patch"] is None and len(flow.calls) == 1


def test_flow_violation_is_not_applied(flow):
    flow.plan = [{"junit": junit(2)}]
    r = flow.run(flow.patch_adding("backend/app/evil.py", "x = 1\n"))
    assert (r["status"], r["reason"]) == ("failed", "patch_rejected")
    assert not (flow.repo / "backend/app/evil.py").exists() and len(flow.calls) == 1
    assert json.loads((flow.work / "patch_check.json").read_text())["ok"] is False


def test_flow_unparseable_patch_is_rejected(flow):
    flow.plan = [{"junit": junit(2)}]
    r = flow.run(write(flow.tmp / "agent.patch", "garbage that is not a patch\n"))
    assert (r["status"], r["reason"]) == ("failed", "patch_rejected")


def test_flow_missing_patch_is_error_without_running(flow):
    r = flow.run(flow.tmp / "absent.patch")
    assert (r["status"], r["reason"]) == ("error", "no_patch") and flow.calls == []


def test_flow_patch_that_does_not_apply(flow):
    flow.plan = [{"junit": junit(2)}]
    patch = flow.patch_adding()
    (flow.repo / "backend/tests/test_new.py").write_text("already here\n")
    r = flow.run(patch)
    assert (r["status"], r["reason"]) == ("failed", "apply_failed") and len(flow.calls) == 1


def test_flow_patch_for_a_stack_outside_scope(flow):
    flow.plan = [{"junit": junit(2)}]
    r = flow.run(flow.patch_adding("frontend/src/a.test.ts", "it('x', () => { expect(f()).toBe(1) })\n"))
    assert (r["status"], r["reason"]) == ("failed", "patch_outside_scope")


def test_flow_failing_new_test_skips_reruns(flow):
    flow.plan = [{"junit": junit(2)}, {"rc": 1, "junit": junit(2, 1)}]
    r = flow.run(flow.patch_adding())
    assert (r["status"], r["reason"]) == ("failed", "tests_failed") and len(flow.calls) == 2
    assert r["stacks"]["backend"]["flaky_rerun"]["runs"] == 0


def test_flow_os_exit_zero_gives_no_junit_and_fails(flow):
    flow.plan = [{"junit": junit(2)}, {"rc": 0}]
    r = flow.run(flow.patch_adding())
    assert (r["status"], r["reason"]) == ("failed", "no_junit")
    assert r["stacks"]["backend"]["with_patch"] == {"exit_code": 0, "tests": None, "failures": None}


def test_flow_stale_junit_from_baseline_is_not_reused(flow):
    # The wipe in the runner is what run_tests.sh does; the snapshot must not leak back.
    flow.plan = [{"junit": junit(2)}, {"rc": 0, "junit": None}]
    assert flow.run(flow.patch_adding())["status"] == "failed"


def test_flow_flaky_rerun_fails_the_patch(flow):
    flow.plan = [{"junit": junit(2)}, {"junit": junit(3)}, {"junit": junit(1)}, {"rc": 1, "junit": junit(0, 1)}]
    r = flow.run(flow.patch_adding())
    assert (r["status"], r["reason"]) == ("failed", "flaky")
    assert r["stacks"]["backend"]["flaky_rerun"] == {"runs": 2, "all_passed": False}


FRONTEND_TEST = "import { expect, it } from 'vitest'\n\nit('adds', () => {\n  expect(1 + 2).toBe(3)\n})\n"


def frontend_flow(flow):
    flow.scope = {"stacks": ["frontend"], "source_files": {"backend": [], "frontend": []}}
    return flow.patch_adding("frontend/src/a.test.ts", FRONTEND_TEST)


def static_plan(format_rc=0, types_rc=0, types_before=0):
    # baseline, types before, with patch, two re-runs, format, types after
    return [{"junit": junit(2)}, {"rc": types_before}, {"junit": junit(3)}, {"junit": junit(1)}, {"junit": junit(1)},
            {"rc": format_rc}, {"rc": types_rc}]


def test_flow_frontend_patch_runs_the_static_checks_on_its_own_files(flow):
    patch = frontend_flow(flow)
    flow.plan = static_plan()
    r = flow.run(patch)
    assert r["status"] == "passed"
    assert ("frontend", ["--format", "--", "frontend/src/a.test.ts"]) in flow.calls
    assert flow.calls.count(("frontend", ["--typecheck"])) == 2
    assert r["stacks"]["frontend"]["static_checks"]["format"] is True


def test_flow_badly_formatted_patch_fails(flow):
    patch = frontend_flow(flow)
    flow.plan = static_plan(format_rc=1)
    r = flow.run(patch)
    assert r["status"] == "failed" and r["reason"] == "format_failed"


def test_flow_patch_that_adds_a_type_error_fails(flow):
    patch = frontend_flow(flow)
    flow.plan = static_plan(types_rc=1)
    r = flow.run(patch)
    assert r["status"] == "failed" and r["reason"] == "typecheck_failed"


def test_flow_type_error_already_in_the_pull_request_does_not_count(flow):
    patch = frontend_flow(flow)
    flow.plan = static_plan(types_rc=1, types_before=1)
    r = flow.run(patch)
    assert r["status"] == "passed" and r["stacks"]["frontend"]["static_checks"]["typecheck_before"] is False


def test_flow_sandbox_failure_in_static_checks_is_an_error_not_a_pass(flow):
    patch = frontend_flow(flow)
    flow.plan = static_plan(format_rc=2)
    r = flow.run(patch)
    assert r["status"] == "error" and r["reason"] == "static_infrastructure"


def test_flow_backend_patch_runs_no_static_checks(flow):
    flow.plan = [{"junit": junit(2)}, {"junit": junit(3)}, {"junit": junit(1)}, {"junit": junit(1)}]
    r = flow.run(flow.patch_adding())
    assert r["status"] == "passed" and r["stacks"]["backend"]["static_checks"] is None
    assert not any("--typecheck" in args for _, args in flow.calls)


def test_patch_that_repairs_an_outdated_test_passes():
    base = {"exit_code": 1, "tests": 10, "failures": 1}
    after = {"exit_code": 0, "tests": 10, "failures": 0}
    assert vp.judge_full_run(base, after) == ("passed", None)


def test_flow_preexisting_failures_pass_when_new_tests_pass_alone(flow):
    flow.plan = [{"rc": 1, "junit": junit(4, 1)}, {"rc": 1, "junit": junit(5, 1)}, {"junit": junit(1)}, {"junit": junit(1)}]
    r = flow.run(flow.patch_adding())
    assert r["status"] == "passed" and r["preexisting_failures"] is True


def test_flow_preexisting_failures_new_test_fails_alone(flow):
    flow.plan = [{"rc": 1, "junit": junit(4, 1)}, {"rc": 1, "junit": junit(5, 1)}, {"rc": 1, "junit": junit(0, 1)}, {"junit": junit(1)}]
    r = flow.run(flow.patch_adding())
    assert r["status"] == "failed" and r["preexisting_failures"] is True


@pytest.mark.parametrize("rc", [2, 124])
def test_flow_baseline_infrastructure_error(flow, rc):
    flow.plan = [{"rc": rc}]
    r = flow.run(flow.patch_adding())
    assert (r["status"], r["reason"]) == ("error", "baseline_infrastructure") and len(flow.calls) == 1


@pytest.mark.parametrize("rc", [2, 124])
def test_flow_with_patch_infrastructure_error(flow, rc):
    flow.plan = [{"junit": junit(2)}, {"rc": rc}]
    assert flow.run(flow.patch_adding())["status"] == "error"


@pytest.mark.parametrize("rc", [2, 124])
def test_flow_rerun_infrastructure_error(flow, rc):
    flow.plan = [{"junit": junit(2)}, {"junit": junit(3)}, {"rc": rc}, {"junit": junit(1)}]
    assert flow.run(flow.patch_adding())["status"] == "error"


def test_flow_refactor_deleting_tests_compares_against_head_not_base(flow):
    # PR head itself has few tests, so the baseline is 1. That is not the patch's fault.
    flow.plan = [{"junit": junit(1)}, {"junit": junit(2)}, {"junit": junit(1)}, {"junit": junit(1)}]
    assert flow.run(flow.patch_adding())["status"] == "passed"


def test_flow_no_stacks_is_error(flow):
    flow.scope = {"stacks": ["python"]}
    assert flow.run(flow.patch_adding())["reason"] == "no_stacks"


def test_flow_one_stack_error_beats_another_failure(flow):
    flow.scope = {"stacks": ["backend", "frontend"], "source_files": {}}
    flow.plan = [{"junit": junit(2)}, {"junit": junit(2)}, {"rc": 1, "junit": junit(2, 1)}, {"rc": 2}]
    assert flow.run(flow.patch_adding())["status"] == "error"


def test_main_writes_error_for_bad_scope(tmp_path):
    scope = write(tmp_path / "scope.json", "{not json")
    rc = vp.main(["--scope", str(scope), "--pr-diff", str(tmp_path / "d"), "--patch", str(tmp_path / "p"),
                  "--workspace", str(tmp_path), "--deps", str(tmp_path), "--work-dir", str(tmp_path / "w"),
                  "--output-dir", str(tmp_path / "o")])
    assert rc == 0 and json.loads((tmp_path / "o/verify.json").read_text())["reason"] == "no_scope"


def test_main_internal_error_is_never_a_pass(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(vp, "verify", boom)
    scope = write(tmp_path / "scope.json", "{}")
    rc = vp.main(["--scope", str(scope), "--pr-diff", str(tmp_path / "d"), "--patch", str(tmp_path / "p"),
                  "--workspace", str(tmp_path), "--deps", str(tmp_path), "--work-dir", str(tmp_path / "w"),
                  "--output-dir", str(tmp_path / "o")])
    result = json.loads((tmp_path / "o/verify.json").read_text())
    assert rc == 1 and (result["status"], result["reason"]) == ("error", "internal_error")


def test_contract_renders(tmp_path, flow):
    import render_report as rr
    flow.plan = [{"junit": junit(2)}, {"junit": junit(3)}, {"junit": junit(1)}, {"junit": junit(1)}]
    verify = write(tmp_path / "v.json", json.dumps(flow.run(flow.patch_adding())))
    scope = write(tmp_path / "s.json", json.dumps({"run_agent": True}))
    assert rr.main(["--scope", str(scope), "--verify", str(verify), "--output", str(tmp_path / "o.md")]) == 0
    text = (tmp_path / "o.md").read_text()
    assert "Verification status: **passed**" in text and "exit 0, 3 tests, 0 failures" in text
    assert "2 run(s), all passed: yes" in text
