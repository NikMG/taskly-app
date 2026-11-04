import hashlib
import json
import subprocess

import pytest

import publish_results as pr
import render_report as rr

SHA = "a" * 40
PATCH = b"patch"
PATCH_SHA = hashlib.sha256(PATCH).hexdigest()
BOT = "github-actions[bot]"


def needs(prepare="success", generate="success", verify="success", exit_code="0"):
    return {"scope": {"result": "success", "outputs": {}},
            "prepare": {"result": prepare, "outputs": {}},
            "generate": {"result": generate, "outputs": {"agent_exit": exit_code}},
            "verify": {"result": verify, "outputs": {}}}


RUN = {"run_agent": True}
PASSED = {"status": "passed", "patch_sha256": PATCH_SHA}


@pytest.mark.parametrize("args,expected", [
    ((needs(), None, None, None, None, None), "pipeline_error"),
    ((needs(), "garbage", None, None, None, None), "pipeline_error"),
    ((needs(), {"run_agent": False, "skip_reason": "draft"}, None, None, None, None), "skipped"),
    ((needs(), {"run_agent": False, "skip_reason": "scope_error"}, None, None, None, None), "pipeline_error"),
    ((needs(prepare="failure"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(generate="failure"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(generate="skipped"), RUN, None, None, None, None), "pipeline_error"),
    ((needs(exit_code="2"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(exit_code="3"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(exit_code="4"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(exit_code="x"), RUN, PASSED, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, None, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, "corrupt", None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, {"status": "error"}, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, {"status": "weird"}, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, {"status": "failed"}, None, PATCH, 0), "needs_human"),
    ((needs(), RUN, PASSED, None, PATCH, 0), "tests_proposed"),
    ((needs(exit_code="1"), RUN, PASSED, None, PATCH, 0), "tests_proposed"),
    ((needs(exit_code="124"), RUN, PASSED, None, PATCH, 0), "tests_proposed"),
    ((needs(), RUN, PASSED, None, PATCH, 1), "needs_human"),
    ((needs(), RUN, PASSED, None, PATCH, 2), "needs_human"),
    ((needs(), RUN, {"status": "passed", "patch_sha256": "0" * 64}, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, {"status": "passed"}, None, PATCH, 0), "pipeline_error"),
    ((needs(), RUN, PASSED, None, None, None), "pipeline_error"),
    ((needs(), RUN, {"status": "passed", "patch_sha256": hashlib.sha256(b"").hexdigest()}, None, b"", 0), "needs_human"),
    ((needs(), RUN, {"status": "skipped"}, {"decision": "no_tests_needed"}, b"", 0), "no_tests_needed"),
    ((needs(), RUN, {"status": "skipped"}, {"decision": "needs_human"}, b"", 0), "needs_human"),
    ((needs(), RUN, {"status": "skipped"}, {"decision": "tests_created"}, b"", 0), "needs_human"),
    ((needs(), RUN, {"status": "skipped"}, None, b"", 0), "needs_human"),
    ((needs(exit_code="1"), RUN, {"status": "skipped"}, {"decision": "no_tests_needed"}, b"", 0), "pipeline_error"),
])
def test_decide(args, expected):
    assert pr.decide(*args) == expected


def test_conclusions_cover_every_outcome_and_only_errors_fail():
    assert set(pr.CONCLUSIONS) == set(rr.OUTCOME_TEXT)
    assert [k for k, v in pr.CONCLUSIONS.items() if v == "failure"] == ["pipeline_error"]
    assert pr.CONCLUSIONS["needs_human"] == "neutral"


@pytest.mark.parametrize("n,expected", [(needs(), False), (needs(verify="cancelled"), True),
                                         (needs(generate="cancelled"), True), (None, False), ("x", False)])
def test_is_superseded(n, expected):
    assert pr.is_superseded(n) is expected


@pytest.mark.parametrize("ref,ok", [("feature/a", True), ("main", True), ("-x", False), ("", False),
                                    ("a..b", False), ("a b", False), ("a~1", False), ("x;id", True),
                                    ("$(id)", True)])
def test_valid_ref(ref, ok):
    # `x;id` and `$(id)` are legal git refs, and safe: a ref is only ever a JSON string value.
    assert pr.valid_ref(ref) is ok


# ---------------------------------------------------------------- fake API

class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.head_sha = SHA
        self.state = "open"
        self.comments = []
        self.pulls = []
        self.fail = set()

    def __call__(self, method, path, body=None, paginate=False):
        self.calls.append((method, path, body))
        for pattern in self.fail:
            if pattern in f"{method} {path}":
                raise pr.PublishError(f"refused {pattern}")
        if method == "GET" and path.endswith("/pulls/7"):
            return {"head": {"sha": self.head_sha}, "state": self.state}
        if method == "GET" and "/comments" in path:
            return self.comments
        if method == "GET" and "/pulls?" in path:
            return self.pulls
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/o/r/pull/99", "number": 99}
        if method == "PATCH" and "/pulls/" in path:
            return {"html_url": f"https://github.com/o/r/pull/{path.rsplit('/', 1)[1]}"}
        return {}

    def named(self, method, part):
        return [c for c in self.calls if c[0] == method and part in c[1]]


@pytest.fixture
def hub(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(pr, "gh_api", fake)
    return fake


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    def fake(remote, head, patch, branch, token):
        calls.append((remote, head, branch))
        return "b" * 40
    monkeypatch.setattr(pr, "build_and_push", fake)
    return calls


def artifacts(tmp_path, scope=RUN, verify=None, report=None, patch=None):
    root = tmp_path / "artifacts"
    for name in ("report-scope", "report-agent", "report-verify"):
        (root / name).mkdir(parents=True)
    if scope is not None:
        (root / "report-scope/scope.json").write_text(json.dumps(scope))
    if verify is not None:
        (root / "report-verify/verify.json").write_text(json.dumps(verify))
    if report is not None:
        (root / "report-agent/report.json").write_text(json.dumps(report))
    if patch is not None:
        (root / "report-agent/agent.patch").write_bytes(patch)
    return root


GOOD_PATCH = (b"diff --git a/backend/tests/test_new.py b/backend/tests/test_new.py\n"
              b"new file mode 100644\n--- /dev/null\n+++ b/backend/tests/test_new.py\n"
              b"@@ -0,0 +1,2 @@\n+def test_new(thing):\n+    assert thing() == 2\n")
GOOD_VERIFY = {"status": "passed", "patch_sha256": hashlib.sha256(GOOD_PATCH).hexdigest()}


def env_for(root, tmp_path, n=None, head_ref="feature/x"):
    return {"PR_NUMBER": "7", "HEAD_SHA": SHA, "HEAD_REF": head_ref, "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "5", "ARTIFACTS_DIR": str(root), "GH_TOKEN": "fake",
            "NEEDS_JSON": json.dumps(needs() if n is None else n)}


@pytest.fixture(autouse=True)
def summary(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


def check_runs(hub):
    return [(c[2]["head_sha"], c[2]["conclusion"], c[2]["name"]) for c in hub.named("POST", "/check-runs")]


def test_proposed_flow(tmp_path, hub, pushed, summary):
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert pushed == [("https://github.com/o/r.git", SHA, "test-agent/pr-7")]
    created = hub.named("POST", "/pulls")
    assert len(created) == 1
    body = created[0][2]
    assert (body["base"], body["head"]) == ("feature/x", "test-agent/pr-7")
    assert body["title"] == "Test agent: proposed tests for PR 7"
    assert ("b" * 40) in body["body"]
    comment = hub.named("POST", "/issues/7/comments")[0][2]["body"]
    assert comment.startswith(rr.MARKER) and "https://github.com/o/r/pull/99" in comment
    assert check_runs(hub) == [(SHA, "success", "Test agent"), ("b" * 40, "success", "Test agent")]
    assert "Tests proposed" in summary.read_text()


def test_existing_bot_pr_is_updated_not_duplicated(tmp_path, hub, pushed):
    hub.pulls = [{"number": 41, "user": {"login": BOT}}, {"number": 42, "user": {"login": BOT}},
                 {"number": 43, "user": {"login": "mallory"}}]
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert not hub.named("POST", "repos/o/r/pulls")
    updates = hub.named("PATCH", "/pulls/")
    assert [c[1] for c in updates] == ["repos/o/r/pulls/42", "repos/o/r/pulls/41"]
    assert updates[0][2] == {"state": "closed"} and updates[1][2]["base"] == "feature/x"


def test_sticky_comment_updates_only_our_own(tmp_path, hub):
    hub.comments = [{"id": 1, "user": {"login": "mallory"}, "body": rr.MARKER + " spoof"},
                    {"id": 2, "user": {"login": BOT}, "body": "other"},
                    {"id": 3, "user": {"login": BOT}, "body": rr.MARKER + "\nold"}]
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": "scope_error"})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert [c[1] for c in hub.named("PATCH", "/issues/comments/")] == ["repos/o/r/issues/comments/3"]
    assert not hub.named("POST", "/issues/7/comments")


def test_skipped_run_keeps_the_earlier_report(tmp_path, hub):
    hub.comments = [{"id": 3, "user": {"login": BOT}, "body": rr.MARKER + "\nreal results"}]
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": "no_testable_changes"})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert not hub.named("PATCH", "/issues/comments/")
    assert not hub.named("POST", "/issues/7/comments")


@pytest.mark.parametrize("reason", ["bot_author", "agent_branch", "agent_commit"])
def test_agents_own_activity_gets_no_comment_but_keeps_the_check(tmp_path, hub, reason):
    hub.comments = []
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": reason})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert not hub.named("POST", "/issues/7/comments")
    assert hub.named("POST", "/check-runs")


def test_workflow_downloads_report_artifacts_by_name():
    # A pattern matching a single artifact unpacks it without its subdirectory, which made
    # every skipped run report a pipeline error.
    workflow = (pr.HERE.parent / "workflows" / "test-agent.yml").read_text()
    assert "pattern:" not in workflow
    for name in ("report-scope", "report-agent", "report-verify"):
        assert f"name: {name}\n          path: ${{{{ runner.temp }}}}/artifacts/{name}\n" in workflow


def test_skipped_run_comments_when_there_is_no_report_yet(tmp_path, hub):
    hub.comments = []
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": "no_testable_changes"})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert hub.named("POST", "/issues/7/comments")


def test_spoofed_marker_gets_a_new_comment(tmp_path, hub):
    hub.comments = [{"id": 1, "user": {"login": "mallory"}, "body": rr.MARKER}]
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": "draft"})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert hub.named("POST", "/issues/7/comments") and not hub.named("PATCH", "/issues/comments/")


@pytest.mark.parametrize("change", ["head", "closed"])
def test_stale_publishes_nothing(tmp_path, hub, pushed, summary, change):
    if change == "head":
        hub.head_sha = "c" * 40
    else:
        hub.state = "closed"
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert pushed == [] and not hub.named("POST", "/comments") and not hub.named("POST", "/pulls")
    assert not hub.named("PATCH", "/")
    assert check_runs(hub) == [(SHA, "neutral", "Test agent")]
    assert "Stale" in summary.read_text()


def test_cancelled_job_is_treated_as_superseded(tmp_path, hub, pushed):
    root = artifacts(tmp_path)
    assert pr.publish(env_for(root, tmp_path, n=needs(verify="cancelled"))) == 0
    assert not hub.named("POST", "/comments") and check_runs(hub) == [(SHA, "neutral", "Test agent")]


def test_needs_human_for_rejected_patch(tmp_path, hub, pushed):
    root = artifacts(tmp_path, verify={"status": "failed", "reason": "patch_rejected"}, patch=b"x")
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert pushed == [] and check_runs(hub) == [(SHA, "neutral", "Test agent")]
    assert "Needs human review" in hub.named("POST", "/issues/7/comments")[0][2]["body"]


def test_tampered_patch_is_not_published(tmp_path, hub, pushed):
    root = artifacts(tmp_path, verify=dict(GOOD_VERIFY), patch=GOOD_PATCH + b"+more\n")
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert pushed == [] and check_runs(hub) == [(SHA, "failure", "Test agent")]


def test_patch_that_publisher_rejects_is_not_published(tmp_path, hub, pushed):
    evil = (b"diff --git a/backend/app/x.py b/backend/app/x.py\nnew file mode 100644\n--- /dev/null\n"
            b"+++ b/backend/app/x.py\n@@ -0,0 +1 @@\n+x = 1\n")
    root = artifacts(tmp_path, verify={"status": "passed", "patch_sha256": hashlib.sha256(evil).hexdigest()}, patch=evil)
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert pushed == [] and check_runs(hub) == [(SHA, "neutral", "Test agent")]


def test_comment_rendered_when_every_artifact_is_missing(tmp_path, hub, pushed):
    root = tmp_path / "nothing"
    n = needs(prepare="failure", generate="skipped", verify="skipped")
    assert pr.publish(env_for(root, tmp_path, n=n)) == 0
    body = hub.named("POST", "/issues/7/comments")[0][2]["body"]
    assert body.startswith(rr.MARKER) and "Pipeline error" in body
    assert "https://github.com/o/r/actions/runs/5" in body
    assert check_runs(hub) == [(SHA, "failure", "Test agent")] and pushed == []


def test_bad_needs_json_still_renders(tmp_path, hub):
    env = env_for(artifacts(tmp_path), tmp_path)
    env["NEEDS_JSON"] = "{nope"
    assert pr.publish(env) == 0
    assert "Pipeline error" in hub.named("POST", "/issues/7/comments")[0][2]["body"]


def test_api_outage_on_head_lookup_pushes_nothing(tmp_path, hub, pushed):
    hub.fail = {"GET repos/o/r/pulls/7"}
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path)) == 1
    assert pushed == [] and check_runs(hub) == [(SHA, "failure", "Test agent")]


def test_failed_pr_creation_is_a_pipeline_error_and_still_reports(tmp_path, hub, pushed):
    hub.fail = {"POST repos/o/r/pulls"}
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path)) == 1
    assert "Pipeline error" in hub.named("POST", "/issues/7/comments")[0][2]["body"]
    assert check_runs(hub) == [(SHA, "failure", "Test agent")]


def test_invalid_head_ref_pushes_nothing(tmp_path, hub, pushed):
    root = artifacts(tmp_path, verify=GOOD_VERIFY, patch=GOOD_PATCH)
    assert pr.publish(env_for(root, tmp_path, head_ref="-bad")) == 1
    assert pushed == []


@pytest.mark.parametrize("key,value", [("PR_NUMBER", "7; id"), ("HEAD_SHA", "abc"), ("GITHUB_REPOSITORY", "o/r/../x"),
                                       ("GITHUB_RUN_ID", "1 2")])
def test_invalid_event_values_stop_before_any_call(tmp_path, hub, key, value):
    env = env_for(artifacts(tmp_path), tmp_path)
    env[key] = value
    assert pr.publish(env) == 1 and hub.calls == []


def test_skipped_has_no_bot_pr_and_is_neutral(tmp_path, hub, pushed):
    root = artifacts(tmp_path, scope={"run_agent": False, "skip_reason": "draft", "skip_detail": "pull request is a draft"})
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert check_runs(hub) == [(SHA, "neutral", "Test agent")] and pushed == []


def test_no_tests_needed_is_success(tmp_path, hub, pushed):
    root = artifacts(tmp_path, verify={"status": "skipped", "reason": "empty_patch"},
                     report={"decision": "no_tests_needed"}, patch=b"")
    assert pr.publish(env_for(root, tmp_path)) == 0
    assert check_runs(hub) == [(SHA, "success", "Test agent")] and pushed == []


# ---------------------------------------------------------------- the commit, with real git

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_build_and_push_real_git(tmp_path, capfd):
    source = tmp_path / "source"
    (source / "backend/tests").mkdir(parents=True)
    (source / "backend/tests/test_old.py").write_text("x = 1\n")
    (source / ".gitattributes").write_text("* filter=evil\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    # A filter or hook that ran would leave this file behind.
    canary = tmp_path / "canary"
    git(source, "config", "filter.evil.smudge", f"touch {canary}; cat")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "head")
    head = git(source, "rev-parse", "HEAD")
    patch = tmp_path / "agent.patch"
    patch.write_bytes(GOOD_PATCH)

    commit = pr.build_and_push(f"file://{source}", head, patch, "test-agent/pr-7", None)

    assert git(source, "rev-parse", "test-agent/pr-7") == commit
    assert git(source, "rev-parse", "main") == head, "the source branch must not move"
    assert git(source, "log", "-1", "--format=%an|%ae|%P", commit) == f"{BOT}|{pr.BOT_EMAIL}|{head}"
    assert git(source, "log", "-1", "--format=%B", commit).splitlines()[-1] == "Test-Agent-Commit: true"
    assert git(source, "diff", "--name-only", head, commit) == "backend/tests/test_new.py"
    assert not canary.exists()

    # A second run on a moved head replaces the branch.
    (source / "z.txt").write_text("z\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "next")
    head2 = git(source, "rev-parse", "HEAD")
    commit2 = pr.build_and_push(f"file://{source}", head2, patch, "test-agent/pr-7", None)
    assert git(source, "rev-parse", "test-agent/pr-7") == commit2 and commit2 != commit


def test_build_and_push_patch_that_does_not_apply(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "head")
    patch = tmp_path / "agent.patch"
    patch.write_bytes(b"diff --git a/none.txt b/none.txt\n--- a/none.txt\n+++ b/none.txt\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(pr.PublishError):
        pr.build_and_push(f"file://{source}", git(source, "rev-parse", "HEAD"), patch, "test-agent/pr-7", None)
    assert git(source, "branch", "--list", "test-agent/*") == ""


def test_token_never_reaches_argv_or_url(tmp_path, monkeypatch, capfd):
    seen = []
    real = subprocess.run

    def spy(args, **kwargs):
        seen.append((list(args), kwargs.get("env") or {}))
        return real(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", spy)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "head")
    patch = tmp_path / "agent.patch"
    patch.write_bytes(GOOD_PATCH)
    pr.build_and_push(f"file://{source}", git(source, "rev-parse", "HEAD"), patch, "test-agent/pr-7", "s3cret-token")
    for args, env in seen:
        assert not any("s3cret-token" in a for a in args)
        assert "s3cret-token" not in env.get("GIT_CONFIG_VALUE_0", "")
    with_header = [args for args, env in seen if "GIT_CONFIG_VALUE_0" in env]
    assert sorted(a[a.index("core.fsmonitor=false") + 1] for a in with_header) == ["fetch", "push"]
    assert "s3cret-token" not in capfd.readouterr().out


# ---------------------------------------------------------------- GitHub outages

@pytest.fixture
def gh_cli(monkeypatch):
    """Scripted `gh` results; records calls and never sleeps."""
    class Cli:
        script: list = []
        calls: list = []

    cli = Cli()
    cli.calls = []
    monkeypatch.setattr(pr.time, "sleep", lambda _: None)

    def run(args, **_):
        cli.calls.append(args)
        rc, out, err = cli.script.pop(0)
        return subprocess.CompletedProcess(args, rc, out.encode(), err.encode())

    monkeypatch.setattr(pr.subprocess, "run", run)
    return cli


def test_server_error_is_retried(gh_cli):
    gh_cli.script = [(1, "", "gh: Server Error (HTTP 502)"), (0, '{"ok": true}', "")]
    assert pr.gh_api("POST", "x", {"a": 1}) == {"ok": True}
    assert len(gh_cli.calls) == 2


def test_lasting_server_error_gives_up(gh_cli):
    gh_cli.script = [(1, "", "gh: Server Error (HTTP 502)")] * 3
    with pytest.raises(pr.PublishError, match="502"):
        pr.gh_api("GET", "x")
    assert len(gh_cli.calls) == 3


def test_client_error_is_not_retried(gh_cli):
    gh_cli.script = [(1, "", "gh: Validation Failed (HTTP 422)")]
    with pytest.raises(pr.PublishError, match="422"):
        pr.gh_api("POST", "x", {})
    assert len(gh_cli.calls) == 1


def test_bot_pr_created_by_a_request_that_reported_an_error_is_found(monkeypatch):
    found = [{"number": 9, "html_url": "https://example.test/9", "user": {"login": pr.BOT_LOGIN}}]
    answers = [[], pr.PublishError("POST: A pull request already exists (HTTP 422)"), found]

    def fake(*_, **__):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(pr, "gh_api", fake)
    assert pr.upsert_bot_pr("o/r", "1", "main", "test-agent/pr-1", "body") == "https://example.test/9"


def test_other_errors_creating_the_bot_pr_still_fail(monkeypatch):
    def fake(method, *_, **__):
        if method == "POST":
            raise pr.PublishError("POST: Forbidden (HTTP 403)")
        return []

    monkeypatch.setattr(pr, "gh_api", fake)
    with pytest.raises(pr.PublishError, match="403"):
        pr.upsert_bot_pr("o/r", "1", "main", "test-agent/pr-1", "body")
