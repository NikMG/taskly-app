#!/usr/bin/env python3
"""Publish the outcome of a test agent run. Runs from the trusted checkout only.

PR code is never checked out or executed: the head commit is fetched into an empty bare
repository, and the patch is applied to a private index, so no worktree, hooks or filters
exist. The token is only in this process's environment and is handed to git for one
fetch and one push through GIT_CONFIG_* variables, never argv, a URL or a file.

Environment: PR_NUMBER, HEAD_SHA, HEAD_REF, GITHUB_REPOSITORY, GITHUB_RUN_ID, GH_TOKEN,
ARTIFACTS_DIR (report-scope, report-agent, report-verify), NEEDS_JSON, GITHUB_STEP_SUMMARY.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import render_report as rr

HERE = Path(__file__).resolve().parent
BOT_LOGIN = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
CHECK_NAME = "Test agent"
COMMIT_MESSAGE = "test: add tests proposed by the test agent\n\nTest-Agent-Commit: true\n"
CONCLUSIONS = {"tests_proposed": "success", "no_tests_needed": "success", "skipped": "neutral",
               "stale": "neutral", "needs_human": "neutral", "pipeline_error": "failure"}
# 1 agent failed and 124 timed out can still leave a complete patch; verify judges it.
# 4 means a credential reached the outputs, so nothing from that run may be published.
AGENT_UNUSABLE_EXITS = (2, 3, 4)
SILENT_SKIPS = ("bot_author", "agent_branch", "agent_commit")


class PublishError(Exception):
    pass


# ---------------------------------------------------------------- deciding

def is_superseded(needs: Any) -> bool:
    """A cancelled job means a newer run took over. Job timeouts look the same; the job
    limits are set above the inner limits so that those are reported by the inner steps."""
    if not isinstance(needs, dict):
        return False
    return any(isinstance(n, dict) and n.get("result") == "cancelled" for n in needs.values())


def _job(needs: Any, name: str) -> dict[str, Any]:
    job = needs.get(name) if isinstance(needs, dict) else None
    return job if isinstance(job, dict) else {}


def agent_exit_code(needs: Any) -> int | None:
    outputs = _job(needs, "generate").get("outputs")
    value = outputs.get("agent_exit") if isinstance(outputs, dict) else None
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value) else None


def decide(needs: Any, scope: Any, verify: Any, report: Any, patch: bytes | None,
           patch_check_rc: int | None) -> str:
    if not isinstance(scope, dict):
        return "pipeline_error"
    if scope.get("run_agent") is not True:
        return "pipeline_error" if scope.get("skip_reason") == "scope_error" else "skipped"
    if any(_job(needs, n).get("result") != "success" for n in ("prepare", "generate")):
        return "pipeline_error"
    code = agent_exit_code(needs)
    if code is None or code in AGENT_UNUSABLE_EXITS or not isinstance(verify, dict):
        return "pipeline_error"
    status = verify.get("status")
    if status == "failed":
        return "needs_human"
    if status == "skipped":
        if code != 0:
            return "pipeline_error"
        decision = report.get("decision") if isinstance(report, dict) else None
        return "no_tests_needed" if decision == "no_tests_needed" else "needs_human"
    if status != "passed":
        return "pipeline_error"
    if patch is None or verify.get("patch_sha256") != hashlib.sha256(patch).hexdigest():
        return "pipeline_error"
    return "tests_proposed" if patch and patch_check_rc == 0 else "needs_human"


# ---------------------------------------------------------------- GitHub API

def gh_api(method: str, path: str, body: Any = None, paginate: bool = False) -> Any:
    args = ["gh", "api", "--method", method, path]
    if paginate:
        args += ["--paginate", "--slurp"]
    if body is not None:
        args += ["--input", "-"]
    proc = subprocess.run(args, input=None if body is None else json.dumps(body).encode(),
                          capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise PublishError(f"{method} {path}: " + proc.stderr.decode("utf-8", "replace").strip()[:500])
    if paginate:
        return [item for page in json.loads(proc.stdout) for item in page]
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def upsert_comment(repo: str, number: str, text: str, overwrite: bool = True) -> None:
    # Anyone can write the marker; only the login proves the comment is ours.
    comments = gh_api("GET", f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
    for comment in comments:
        if comment.get("user", {}).get("login") == BOT_LOGIN \
                and str(comment.get("body", "")).startswith(rr.MARKER):
            if overwrite:
                gh_api("PATCH", f"repos/{repo}/issues/comments/{comment['id']}", {"body": text})
            return
    gh_api("POST", f"repos/{repo}/issues/{number}/comments", {"body": text})


def upsert_bot_pr(repo: str, number: str, base_ref: str, branch: str, body: str) -> str:
    owner = repo.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}", "per_page": 100})
    pulls = [p for p in gh_api("GET", f"repos/{repo}/pulls?{query}", paginate=True)
             if p.get("user", {}).get("login") == BOT_LOGIN]
    title = f"Test agent: proposed tests for PR {number}"
    for extra in pulls[1:]:
        gh_api("PATCH", f"repos/{repo}/pulls/{extra['number']}", {"state": "closed"})
    if pulls:
        return gh_api("PATCH", f"repos/{repo}/pulls/{pulls[0]['number']}",
                      {"title": title, "body": body, "base": base_ref})["html_url"]
    return gh_api("POST", f"repos/{repo}/pulls",
                  {"title": title, "head": branch, "base": base_ref, "body": body})["html_url"]


def post_check(repo: str, sha: str, outcome: str, run_url: str) -> None:
    gh_api("POST", f"repos/{repo}/check-runs", {
        "name": CHECK_NAME, "head_sha": sha, "status": "completed",
        "conclusion": CONCLUSIONS[outcome], "details_url": run_url,
        "output": {"title": rr.OUTCOME_TEXT[outcome].split(".", 1)[0],
                   "summary": rr.OUTCOME_TEXT[outcome]}})


# ---------------------------------------------------------------- the commit

def _git_env(token: str | None, home: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        secret = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        print(f"::add-mask::{secret}")
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {secret}")
    return env


def valid_ref(ref: str) -> bool:
    if not ref or ref.startswith("-"):
        return False
    return subprocess.run(["git", "check-ref-format", f"refs/heads/{ref}"], capture_output=True,
                          timeout=30, check=False).returncode == 0


def build_and_push(remote: str, head_sha: str, patch_path: Path, branch: str,
                   token: str | None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo.git"

        def git(*args: str, auth: bool = False, index: bool = False, stdin: bytes | None = None,
                identity: bool = False) -> str:
            env = _git_env(token if auth else None, tmp)
            if index:
                env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            if identity:
                env.update(GIT_AUTHOR_NAME=BOT_LOGIN, GIT_AUTHOR_EMAIL=BOT_EMAIL,
                           GIT_COMMITTER_NAME=BOT_LOGIN, GIT_COMMITTER_EMAIL=BOT_EMAIL)
            proc = subprocess.run(
                ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                 *args], env=env, input=stdin, capture_output=True, timeout=180, check=False)
            if proc.returncode != 0:
                raise PublishError(f"git {args[0]}: " + proc.stderr.decode("utf-8", "replace").strip()[:500])
            return proc.stdout.decode().strip()

        subprocess.run(["git", "init", "--quiet", "--bare", "--template=", str(repo)], check=True,
                       env=_git_env(None, tmp), capture_output=True, timeout=30)
        git("fetch", "--quiet", "--no-tags", "--depth=1", remote, head_sha, auth=True)
        git("read-tree", head_sha, index=True)
        git("apply", "--cached", "--whitespace=nowarn", str(patch_path), index=True)
        tree = git("write-tree", index=True)
        commit = git("commit-tree", tree, "-p", head_sha, stdin=COMMIT_MESSAGE.encode(), identity=True)
        git("push", "--quiet", "--force", remote, f"{commit}:refs/heads/{branch}", auth=True)
        return commit


# ---------------------------------------------------------------- main flow

def _load(path: Path) -> Any:
    return rr.load_json(str(path))[0]


def append_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def render(artifacts: Path, patch_check: Path, run_url: str, outcome: str, sha: str | None = None,
           bot_url: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.md"
        args = ["--scope", str(artifacts / "report-scope" / "scope.json"), "--output", str(out),
                "--report", str(artifacts / "report-agent" / "report.json"),
                "--patch-check", str(patch_check),
                "--verify", str(artifacts / "report-verify" / "verify.json"),
                "--agent-log", str(artifacts / "report-agent" / "agent.log"),
                "--run-url", run_url, "--outcome", outcome]
        if sha:
            args += ["--pushed-sha", sha]
        if bot_url:
            args += ["--bot-pr-url", bot_url]
        if rr.main(args) != 0:
            raise PublishError("could not render the report")
        return out.read_text(encoding="utf-8")


def run_patch_check(patch_path: Path, output: Path) -> int:
    return subprocess.run([sys.executable, "-I", str(HERE / "check_patch.py"), str(patch_path),
                           "--output", str(output)], capture_output=True, timeout=120,
                          check=False).returncode


def publish(env: dict[str, str]) -> int:
    number, head_sha, head_ref = env["PR_NUMBER"], env["HEAD_SHA"], env["HEAD_REF"]
    repo, run_id = env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[0-9]+", number) or not re.fullmatch(r"[0-9a-f]{40}", head_sha) \
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) \
            or not re.fullmatch(r"[0-9]+", run_id):
        print("publish_results.py: invalid event values", file=sys.stderr)
        return 1
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    artifacts = Path(env["ARTIFACTS_DIR"])
    try:
        needs = json.loads(env.get("NEEDS_JSON") or "{}")
    except ValueError:
        needs = None

    errors: list[str] = []
    try:
        live = gh_api("GET", f"repos/{repo}/pulls/{number}")
        fresh = live["head"]["sha"] == head_sha and live["state"] == "open"
    except (PublishError, KeyError, TypeError, ValueError) as e:
        errors.append(str(e))
        fresh = True  # cannot tell; the outcome below is pipeline_error and pushes nothing

    if not fresh or is_superseded(needs):
        append_summary(f"## Test agent\n\n{rr.OUTCOME_TEXT['stale']}")
        try:
            post_check(repo, head_sha, "stale", run_url)
        except (PublishError, OSError, subprocess.SubprocessError) as e:
            print(f"::warning::{e!r}")
        return 0

    patch_path = artifacts / "report-agent" / "agent.patch"
    patch = patch_path.read_bytes() if patch_path.is_file() else None
    with tempfile.TemporaryDirectory() as tmp:
        patch_check = Path(tmp) / "patch_check.json"
        rc = run_patch_check(patch_path, patch_check) if patch is not None else None
        if rc is None and (artifacts / "report-verify" / "patch_check.json").is_file():
            patch_check = artifacts / "report-verify" / "patch_check.json"
        outcome = "pipeline_error" if errors else decide(
            needs, _load(artifacts / "report-scope" / "scope.json"),
            _load(artifacts / "report-verify" / "verify.json"),
            _load(artifacts / "report-agent" / "report.json"), patch, rc)

        commit = bot_url = None
        if outcome == "tests_proposed":
            branch = f"test-agent/pr-{number}"
            try:
                if not valid_ref(head_ref):
                    raise PublishError("the source branch name is not a valid ref")
                commit = build_and_push(f"https://github.com/{repo}.git", head_sha, patch_path, branch,
                                        env.get("GH_TOKEN"))
                body = render(artifacts, patch_check, run_url, outcome, sha=commit)
                bot_url = upsert_bot_pr(repo, number, head_ref, branch, body)
            except (PublishError, OSError, subprocess.SubprocessError, KeyError) as e:
                errors.append(str(e))
                outcome = "pipeline_error"

        text = render(artifacts, patch_check, run_url, outcome, sha=commit, bot_url=bot_url)

    append_summary(text)
    # A skipped run has no results, so it must not erase the report of an earlier run.
    # The agent's own PRs and merges are not worth a comment at all.
    scope = _load(artifacts / "report-scope" / "scope.json")
    skip_reason = scope.get("skip_reason") if isinstance(scope, dict) else None
    actions = [lambda: post_check(repo, head_sha, outcome, run_url)]
    if not (outcome == "skipped" and skip_reason in SILENT_SKIPS):
        actions.insert(0, lambda: upsert_comment(repo, number, text, overwrite=outcome != "skipped"))
    if bot_url and commit:
        actions.append(lambda: post_check(repo, commit, outcome, run_url))
    for action in actions:
        try:
            action()
        except (PublishError, OSError, subprocess.SubprocessError) as e:
            errors.append(str(e))

    print(f"publish_results.py: outcome={outcome}")
    for error in errors:
        print(f"::error::{error!r}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(publish(dict(os.environ)))
