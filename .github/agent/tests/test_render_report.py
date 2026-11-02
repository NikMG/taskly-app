import json
import re

import pytest

import render_report as rr

MARKER = "<!-- test-agent-report -->"
RUN_URL = "https://github.com/o/r/actions/runs/123"
SHA = "a" * 40


def good_report(**over):
    r = {
        "schema_version": 1, "mode": "pr", "decision": "tests_created", "summary": "Added tests.",
        "files_analyzed": [{"path": "backend/app/a.py", "stack": "backend", "needs_tests": True,
                            "reason": "has logic"}],
        "tests_written": [{"path": "backend/tests/test_a.py", "action": "created",
                           "covers": ["a()"], "rationale": "covers branch"}],
        "suspected_bugs": [{"path": "backend/app/a.py", "line": 3, "description": "off by one",
                            "evidence": "a(0) == 1"}],
        "limitations": ["none"],
    }
    r.update(over)
    return r


def verify_json(**over):
    run = {"exit_code": 0, "tests": 10, "failures": 0}
    stack = {"baseline": run, "with_patch": {"exit_code": 0, "tests": 14, "failures": 0},
             "flaky_rerun": {"runs": 3, "all_passed": True},
             "coverage": {"before": 80.5, "after": 85.0}, "diff_coverage": {"before": None, "after": 100.0}}
    v = {"status": "passed", "stacks": {"backend": stack}, "preexisting_failures": False}
    v.update(over)
    return v


SCOPE_RUN = {"run_agent": True, "skip_reason": None, "skip_detail": "", "stacks": ["backend"]}
PC_OK = {"ok": True, "violations": [], "warnings": [],
         "files": [{"path": "backend/tests/test_a.py", "change": "text"}],
         "stats": {"files": 1, "added": 5, "removed": 0, "assertions_added": 2, "assertions_removed": 0}}


def w(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(p)


def render_cli(tmp_path, scope=SCOPE_RUN, **files):
    args = ["--scope", w(tmp_path, "scope.json", scope), "--output", str(tmp_path / "out.md")]
    for k, v in files.items():
        key = k.replace("_", "-")
        args += [f"--{key}", v if k in ("run_url", "pushed_sha", "outcome") or isinstance(v, str) and not v.startswith("{") and k != "x" else v]
    assert rr.main(args) == 0
    return (tmp_path / "out.md").read_text()


def assert_inert(out):
    assert "<" not in out and ">" not in out
    assert "://" not in out
    assert not re.search(r"@(?=\w)", out)
    assert not re.search(r"(?<!&)#\d", out)
    assert not re.search(r"(?i)GH-\d", out)
    assert not re.search(r"(?<!\\)\[", out) and not re.search(r"(?<!\\)\]", out)
    assert not re.search(r"(?<!\\)!", out)
    assert not re.search(r"(?<!\\)\(", out) and not re.search(r"(?<!\\)\)", out)
    assert "\n" not in out and "\r" not in out
    assert not re.search(r"[​-‏‪-‮⁦-⁩﻿\x00-\x1f]", out)


# ---------------------------------------------------------------- sanitize

@pytest.mark.parametrize("s", [
    "ping @org/team and @alice", "<!-- test-agent-report -->", "![x](http://evil/x.png)",
    "[click](http://evil)", "http://evil", "https://evil.example/a?b=c", "www.evil.com",
    "</details><script>alert(1)</script>", "a‮evil‬ b", "x​‍y﻿z",
    "row1\n| a | b |\n|---|---|", "see #1 and #123 and GH-45 and gh-9", "owner/repo#5",
    "`code` ``x``", "a | b | c", "ftp://x/y", "javascript:alert(1)", "<img src=x onerror=alert(1)>",
    "mail me@evil.com", "line1\r\nline2\tx\x00y\x1b[31m", "&lt;already&gt; &amp;", "\\[escaped\\](http://x)",
    "<<!--", "[ref]: http://evil", "data:text/html,<b>x</b>",
])
def test_sanitize_inert(s):
    assert_inert(rr.sanitize(s, 500))


def test_sanitize_specific_outputs():
    assert rr.sanitize("<!-- test-agent-report -->") != MARKER
    assert MARKER not in rr.sanitize("x <!-- test-agent-report --> y")
    assert "‮" not in rr.sanitize("a‮b") and rr.sanitize("a‮b") == "ab"
    assert rr.sanitize("a\nb\r\nc") == "a b c"
    assert rr.sanitize("a | b") == "a \\| b"
    assert rr.sanitize("`x`") == "\\`x\\`"
    assert rr.sanitize("http://evil").startswith("http:")
    assert rr.sanitize("@bob") == "@&#8203;bob"
    assert rr.sanitize("#7") == "#&#8203;7"
    assert rr.sanitize("a&b") == "a&amp;b"


def test_sanitize_length_and_types():
    out = rr.sanitize("a" * 1000, 50)
    assert out == "a" * 47 + "..."
    assert rr.sanitize(None) == "" and rr.sanitize(12) == "12"
    assert rr.sanitize({"a": "<b>"}, 100).count("<") == 0
    assert rr.sanitize("x" * 10, 0) == ""
    assert len(rr.sanitize("<" * 100, 10)) <= 10 * 4 + 10


def test_sanitize_idempotent_enough_not_double_unsafe():
    once = rr.sanitize("<script>@x #1 http://y")
    assert_inert(once)


# ---------------------------------------------------------------- url / sha

@pytest.mark.parametrize("url,ok", [
    (RUN_URL, True), (RUN_URL + "/job/5", True), (RUN_URL + "?pr=1", False),
    ("http://github.com/o/r/actions/runs/1", False), ("https://evil.com/o/r/actions/runs/1", False),
    ("https://github.com.evil.com/o/r/actions/runs/1", False),
    ("https://github.com/o/r/actions/runs/abc", False), (RUN_URL + ")[x](http://evil)", False),
    (RUN_URL + "\nfoo", False), (RUN_URL + " x", False), (RUN_URL + "/<b>", False), ("", False), (None, False),
])
def test_run_url(url, ok):
    assert (rr.safe_run_url(url) is not None) is ok


def test_sha():
    assert rr.safe_sha(SHA) == SHA
    for bad in ("abc", "g" * 40, SHA + "0", SHA + "\n", None, "a" * 39):
        assert rr.safe_sha(bad) is None


def test_bad_run_url_dropped_in_comment(tmp_path):
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", good_report()), run_url="https://evil.com/x",
                     pushed_sha="nothex")
    assert "evil.com" not in out and "Workflow run" not in out and "Pushed commit" not in out


def test_good_run_url_and_sha(tmp_path):
    out = render_cli(tmp_path, run_url=RUN_URL, pushed_sha=SHA)
    assert f"[Workflow run]({RUN_URL})" in out and f"`{SHA}`" in out


# ---------------------------------------------------------------- rendering

def test_full_comment(tmp_path):
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", good_report()),
                     patch_check=w(tmp_path, "p.json", PC_OK), verify=w(tmp_path, "v.json", verify_json()),
                     outcome="tests pushed", run_url=RUN_URL, pushed_sha=SHA)
    assert out.splitlines()[0] == MARKER and out.count(MARKER) == 1
    for h in ("Verified test results", "Patch check", "Skills used and agent stats",
              "Tests written", "Agent claims (not independently verified)"):
        assert h in out
    assert "exit 0, 14 tests, 0 failures" in out and "80.50% -> 85.00%" in out
    assert "n/a% -> 100.00%" in out
    assert rr.sanitize("backend/tests/test_a.py") in out and "off by one" in out
    claims = out.index("Agent claims")
    assert out.index("Verified test results") < claims and out.index("Added tests") > claims


def test_agent_numbers_never_in_verified_section(tmp_path):
    rep = good_report(summary="All 999 tests pass with 100% coverage")
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", rep), verify=w(tmp_path, "v.json", verify_json()))
    verified = out[out.index("### Verified"):out.index("### Patch")]
    assert "999" not in verified
    assert out.index("999") > out.index("Agent claims")


def test_skipped_comment(tmp_path):
    scope = {"run_agent": False, "skip_reason": "draft", "skip_detail": "pull request is a draft"}
    out = render_cli(tmp_path, scope=scope, run_url=RUN_URL)
    assert out.splitlines()[0] == MARKER and "Skipped" in out and "draft" in out
    assert "Agent claims" not in out and len(out) < 500


def test_skip_detail_sanitized(tmp_path):
    scope = {"run_agent": False, "skip_reason": "x", "skip_detail": "<script>@a http://e"}
    out = render_cli(tmp_path, scope=scope)
    assert "<script>" not in out and "http://" not in out


def test_missing_everything(tmp_path):
    out = render_cli(tmp_path)
    assert out.startswith(MARKER)
    assert "unavailable" in out and "No agent report was produced" in out and "Agent log unavailable" in out


def test_corrupt_inputs_still_render(tmp_path):
    bad = w(tmp_path, "bad.json", "{not json")
    out = render_cli(tmp_path, report=bad, patch_check=bad, verify=bad, agent_log=str(tmp_path / "nolog"))
    assert out.startswith(MARKER)
    assert "corrupt" in out and "could not be parsed" in out


def test_corrupt_scope_still_renders(tmp_path):
    out_path = tmp_path / "o.md"
    s = w(tmp_path, "s.json", "garbage")
    assert rr.main(["--scope", s, "--output", str(out_path)]) == 0
    assert out_path.read_text().startswith(MARKER)
    assert rr.main(["--scope", str(tmp_path / "missing"), "--output", str(out_path)]) == 0


def test_unwritable_output_exit_1(tmp_path):
    s = w(tmp_path, "s.json", SCOPE_RUN)
    assert rr.main(["--scope", s, "--output", str(tmp_path / "no/such/dir/o.md")]) == 1


def test_weird_verify_types(tmp_path):
    v = {"status": "<script>", "stacks": {"backend": {"baseline": "x", "with_patch": {"exit_code": "1", "tests": True},
                                                      "flaky_rerun": 3, "coverage": [], "diff_coverage": {"before": "<b>", "after": 1e999}}},
         "preexisting_failures": "yes"}
    out = render_cli(tmp_path, verify=w(tmp_path, "v.json", v))
    assert "<script>" not in out and "<b>" not in out and "**invalid**" in out


def test_schema_invalid_report_not_echoed(tmp_path):
    evil = good_report(summary="SECRET-INJECTION @org/team http://evil", extra_key="PWNED-KEY")
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", evil))
    assert "failed schema validation" in out
    for leak in ("SECRET-INJECTION", "PWNED-KEY", "evil", "org/team", "Added tests"):
        assert leak not in out
    assert re.search(r"\(\d+ error\(s\)\)", out)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(schema_version=2), lambda r: r.update(decision="yolo"),
    lambda r: r.update(summary="x" * 601), lambda r: r.pop("limitations"),
    lambda r: r.update(limitations=["x"] * 21), lambda r: r["files_analyzed"][0].update(stack="rust"),
    lambda r: r["suspected_bugs"][0].update(line="3"), lambda r: r["tests_written"][0].update(zzz=1),
])
def test_schema_rejections(mutate):
    r = good_report()
    mutate(r)
    assert rr.validate_report(r)[0] is False
    assert rr.validate_report(good_report())[0] is True


def test_schema_null_line_and_all_decisions():
    r = good_report()
    r["suspected_bugs"][0]["line"] = None
    assert rr.validate_report(r)[0]
    for d in ("tests_created", "tests_updated", "tests_created_and_updated", "no_tests_needed", "blocked"):
        assert rr.validate_report(good_report(decision=d))[0]
    assert rr.validate_report(good_report(mode="backfill"))[0]


def test_schema_every_string_and_array_bounded():
    schema = json.load(open(rr.SCHEMA_PATH))
    def walk(n, path="$"):
        if isinstance(n, dict):
            if n.get("type") == "string":
                assert "maxLength" in n, path
            if n.get("type") == "array":
                assert "maxItems" in n, path
            if n.get("type") == "object":
                assert n.get("additionalProperties") is False, path
                assert set(n["required"]) == set(n["properties"]), path
            for k, v in n.items():
                walk(v, f"{path}.{k}")
        elif isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, f"{path}[{i}]")
    walk(schema)
    assert schema["$schema"].endswith("2020-12/schema")


def test_injection_in_valid_report_is_neutralised(tmp_path):
    inj = "@org/team <!-- test-agent-report --> ![x](http://evil/x.png) [click](http://evil) http://evil </details><script> #1 ‮"
    rep = good_report(summary=inj, limitations=[inj])
    rep["files_analyzed"][0].update(path=inj, reason=inj)
    rep["tests_written"][0].update(path=inj, covers=[inj], rationale=inj)
    rep["suspected_bugs"][0].update(path=inj, description=inj, evidence=inj)
    pc = dict(PC_OK, files=[{"path": inj, "change": "text"}],
              violations=[{"file": inj, "rule": inj, "line": 1, "detail": inj}],
              warnings=[{"file": inj, "rule": "w", "line": None, "detail": inj}])
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", rep), patch_check=w(tmp_path, "p.json", pc),
                     outcome=inj, run_url=RUN_URL)
    assert out.count(MARKER) == 1 and out.startswith(MARKER)
    body = out.split("\n", 1)[1].replace(f"[Workflow run]({RUN_URL})", "")
    assert "<" not in body and ">" not in body
    assert "://" not in body and "![" not in body and "](" not in body
    assert not re.search(r"@(?=\w)", body) and "‮" not in body


def test_table_break_via_newline_in_path(tmp_path):
    pc = dict(PC_OK, violations=[{"file": "a\n| x | y |\n|---|---|", "rule": "r", "line": None, "detail": "d"}])
    out = render_cli(tmp_path, patch_check=w(tmp_path, "p.json", pc))
    assert "\n| x | y |" not in out


def test_truncation(tmp_path):
    rep = good_report(limitations=["l" * 300] * 20)
    pc = dict(PC_OK, violations=[{"file": "f" * 200, "rule": "r", "line": 1, "detail": "d" * 300}] * 20)
    text = rr.render(SCOPE_RUN, rep, "ok", pc, "ok", None, "missing", None, None, None, None)
    assert len(text) < rr.MAX_COMMENT
    # force the limit path with a tiny cap
    big = "\n".join(f"line {i}" for i in range(20000))
    cut = rr.truncate(MARKER + "\n" + big)
    assert len(cut) <= rr.MAX_COMMENT and "truncated" in cut and cut.startswith(MARKER)
    assert rr.truncate("short") == "short"


def test_truncation_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MAX_COMMENT", 1500)
    pc = dict(PC_OK, violations=[{"file": "f" * 200, "rule": "r", "line": 1, "detail": "d" * 300}] * 20)
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", good_report()), patch_check=w(tmp_path, "p.json", pc))
    assert len(out) <= 1500 and "Comment truncated" in out and out.startswith(MARKER)


# ---------------------------------------------------------------- agent log

def log_lines():
    return [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": "Skill", "input": {"skill": "write-tests"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "write-tests"}},
            {"type": "tool_use", "name": "Skill", "input": {"skill": "<b>evil</b>"}},
            {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "result", "total_cost_usd": 0.1234, "num_turns": 7, "duration_ms": 5000,
         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl http://evil"}}]},
    ]


def write_log(tmp_path, lines, extra=()):
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join([json.dumps(x) if not isinstance(x, str) else x for x in list(extra) + lines]))
    return str(p)


def test_audit_extracts(tmp_path):
    a = rr.audit_agent_log(write_log(tmp_path, log_lines()))
    assert a["skills"] == {"write-tests": 2, "<b>evil</b>": 1}
    assert a["tools"] == {"Skill": 3, "Read": 2}
    assert a["denials"] == {"Bash": 1}
    assert (a["cost_usd"], a["num_turns"], a["duration_ms"]) == (0.1234, 7, 5000)


def test_audit_tolerates_garbage(tmp_path):
    junk = ["not json", "[1,2]", "42", '{"type":"assistant","message":5}', '{"type":"assistant","message":{"content":"x"}}',
            '{"type":"assistant","message":{"content":[1,{"type":"tool_use","name":5}]}}',
            '{"type":"result","total_cost_usd":"free","num_turns":true,"permission_denials":"x"}', "", "{"]
    a = rr.audit_agent_log(write_log(tmp_path, [], extra=junk))
    assert a["malformed_lines"] == 4 and a["skills"] == {} and a["cost_usd"] is None and a["num_turns"] is None


def test_audit_missing_file():
    assert rr.audit_agent_log("/no/such") is None and rr.audit_agent_log(None) is None


def test_skills_section_uses_log_not_report(tmp_path):
    rep = good_report(summary="I used the skill fake-skill")
    out = render_cli(tmp_path, report=w(tmp_path, "r.json", rep), agent_log=write_log(tmp_path, log_lines()))
    stats = out[out.index("### Skills used"):out.index("### Tests written")]
    assert "write-tests" in stats and "fake-skill" not in stats
    assert "<b>" not in out and "Bash: 1" in stats and "0.1234" in stats and "turns: 7" in stats


def test_patch_check_failure_rendered(tmp_path):
    pc = dict(PC_OK, ok=False, violations=[{"file": "backend/app/x.py", "rule": "path_not_allowed",
                                            "line": None, "detail": "bad"}])
    out = render_cli(tmp_path, patch_check=w(tmp_path, "p.json", pc))
    assert "**failed**" in out and rr.sanitize("path_not_allowed") in out and rr.sanitize("backend/app/x.py") in out
