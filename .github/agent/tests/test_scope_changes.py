import json
import subprocess

import pytest

import scope_changes as sc


def event(**over):
    pr = {
        "draft": False,
        "head": {"repo": {"full_name": "o/r"}},
        "base": {"repo": {"full_name": "o/r"}},
        "labels": [],
        "user": {"login": "alice", "type": "User"},
    }
    pr.update(over)
    return {"pull_request": pr}


def ns(*entries):
    """Build name-status -z bytes. entry = (status, path[, newpath])."""
    out = []
    for e in entries:
        out += list(e)
    return ("\0".join(out) + "\0").encode() if out else b""


def numstat(*rows):
    out = []
    for r in rows:
        out.append("\t".join(r[:3]) if len(r) == 3 else "\t".join(r[:2]) + "\t")
        if len(r) == 4:
            out += [r[2], r[3]]
    return ("\0".join(out) + "\0").encode() if out else b""


def run(tmp_path, ev=None, name_status=b"", ns_numstat=b"", msg="", extra=(), raw_event=None):
    e = tmp_path / "event.json"
    e.write_text(raw_event if raw_event is not None else json.dumps(ev if ev is not None else event()))
    (tmp_path / "ns").write_bytes(name_status)
    (tmp_path / "num").write_bytes(ns_numstat)
    (tmp_path / "msg").write_text(msg)
    out = tmp_path / "scope.json"
    code = sc.main(["--event", str(e), "--name-status", str(tmp_path / "ns"),
                    "--numstat", str(tmp_path / "num"),
                    "--head-commit-message", str(tmp_path / "msg"),
                    "--output", str(out), *extra])
    return code, json.loads(out.read_text())


BACKEND = ("M", "backend/app/services/tasks.py")
NUM1 = numstat(("3", "1", "backend/app/services/tasks.py"))


def test_happy_path_backend(tmp_path):
    code, r = run(tmp_path, name_status=ns(BACKEND), ns_numstat=NUM1)
    assert code == 0
    assert r["run_agent"] is True and r["skip_reason"] is None
    assert r["stacks"] == ["backend"]
    assert r["source_files"] == {"backend": ["backend/app/services/tasks.py"], "frontend": []}
    assert r["totals"] == {"files": 1, "lines": 4}


def test_both_stacks_and_test_files(tmp_path):
    names = ns(BACKEND, ("M", "frontend/src/api/client.ts"), ("A", "frontend/src/a.test.tsx"),
               ("M", "backend/tests/test_x.py"), ("M", "frontend/src/test/setup.ts"),
               ("M", "README.md"))
    code, r = run(tmp_path, name_status=names, ns_numstat=b"")
    assert code == 0 and r["stacks"] == ["backend", "frontend"]
    assert r["test_files"] == ["frontend/src/a.test.tsx", "backend/tests/test_x.py",
                               "frontend/src/test/setup.ts"]
    assert r["other_files"] == ["README.md"]


@pytest.mark.parametrize("path,kind", [
    ("backend/app/__init__.py", "other"),
    ("backend/app/sub/__init__.py", "other"),
    ("backend/app/main.py", "other"),
    ("backend/app/sub/main.py", "backend"),
    ("backend/app/readme.txt", "other"),
    ("frontend/src/main.tsx", "other"),
    ("frontend/src/x.d.ts", "other"),
    ("frontend/src/a/types.ts", "other"),
    ("frontend/src/a/task.types.ts", "other"),
    ("frontend/src/a/index.ts", "other"),
    ("frontend/src/a/Button.stories.tsx", "other"),
    ("frontend/src/design-system/Button.tsx", "other"),
    ("frontend/src/test/utils.tsx", "test"),
    ("frontend/src/a/b.test.ts", "test"),
    ("frontend/src/a/b.css", "other"),
    ("frontend/src/a/index.tsx", "frontend"),
    ("frontend/src/a/useThing.ts", "frontend"),
    ("backend/tests/conftest.py", "test"),
])
def test_classify(path, kind):
    assert sc.classify(path) == kind


def test_deleted_source_not_testable(tmp_path):
    code, r = run(tmp_path, name_status=ns(("D", "backend/app/old.py")))
    assert r["skip_reason"] == "no_testable_changes" and r["other_files"] == ["backend/app/old.py"]
    assert code == 0


def test_no_testable_changes(tmp_path):
    code, r = run(tmp_path, name_status=ns(("M", "README.md")))
    assert (code, r["run_agent"], r["skip_reason"]) == (0, False, "no_testable_changes")


def test_fork(tmp_path):
    ev = event(head={"repo": {"full_name": "evil/r"}})
    code, r = run(tmp_path, ev, ns(BACKEND), NUM1)
    assert code == 0 and r["skip_reason"] == "fork"


def test_deleted_fork_repo_is_fork(tmp_path):
    code, r = run(tmp_path, event(head={"repo": None}), ns(BACKEND), NUM1)
    assert r["skip_reason"] == "fork"


def test_draft(tmp_path):
    assert run(tmp_path, event(draft=True), ns(BACKEND), NUM1)[1]["skip_reason"] == "draft"


@pytest.mark.parametrize("user", [{"login": "dependabot[bot]", "type": "User"},
                                  {"login": "x", "type": "Bot"}])
def test_bot_author(tmp_path, user):
    assert run(tmp_path, event(user=user), ns(BACKEND), NUM1)[1]["skip_reason"] == "bot_author"


def test_opt_out_label(tmp_path):
    ev = event(labels=[{"name": "bug"}, {"name": "skip-test-agent"}])
    assert run(tmp_path, ev, ns(BACKEND), NUM1)[1]["skip_reason"] == "opt_out_label"


@pytest.mark.parametrize("entry", [
    ("M", ".claude/settings.json"), ("A", ".github/workflows/x.yml"),
    ("M", "CLAUDE.md"), ("M", "backend/CLAUDE.md"), ("M", "frontend/src/.claude/skills/x.md"),
    ("A", ".mcp.json"), ("M", "backend/.mcp.json"), ("M", "backend/.CLAUDE/x"),
    ("M", ".GitHub/actions/a.yml"), ("M", ".github/agent/check_patch.py"),
])
def test_control_plane(tmp_path, entry):
    code, r = run(tmp_path, name_status=ns(BACKEND, entry), ns_numstat=NUM1)
    assert code == 0 and r["skip_reason"] == "control_plane_changed"


def test_rename_out_of_claude_dir_counts_old_path(tmp_path):
    names = ns(BACKEND, ("R100", ".claude/commands/x.md", "docs/x.md"))
    assert run(tmp_path, name_status=names)[1]["skip_reason"] == "control_plane_changed"


def test_rename_into_control_plane_counts_new_path(tmp_path):
    names = ns(BACKEND, ("R090", "docs/x.md", ".github/x.md"))
    assert run(tmp_path, name_status=names)[1]["skip_reason"] == "control_plane_changed"


def test_not_control_plane_lookalikes(tmp_path):
    names = ns(BACKEND, ("M", "docs/CLAUDE.md.txt"), ("M", "x.github/y"), ("M", "backend/app/github/a.py"),
               ("M", "claude.md.bak"))
    code, r = run(tmp_path, name_status=names)
    assert r["run_agent"] is True


def test_agent_commit(tmp_path):
    msg = "feat\n\nTest-Agent-Commit: true\n"
    assert run(tmp_path, name_status=ns(BACKEND), ns_numstat=NUM1, msg=msg)[1]["skip_reason"] == "agent_commit"
    msg = "feat\r\n\r\nTest-Agent-Commit: true\r\n"
    assert run(tmp_path, name_status=ns(BACKEND), ns_numstat=NUM1, msg=msg)[1]["skip_reason"] == "agent_commit"


@pytest.mark.parametrize("msg", ["Test-Agent-Commit: true extra", "test-agent-commit: true",
                                 "mention Test-Agent-Commit: true here", "Test-Agent-Commit: false"])
def test_agent_commit_requires_exact_line(tmp_path, msg):
    assert run(tmp_path, name_status=ns(BACKEND), ns_numstat=NUM1, msg=msg)[1]["run_agent"] is True


def test_too_large_lines(tmp_path):
    n = numstat(("2000", "1", "backend/app/a.py"))
    code, r = run(tmp_path, name_status=ns(("M", "backend/app/a.py")), ns_numstat=n)
    assert code == 0 and r["skip_reason"] == "diff_too_large" and r["totals"]["lines"] == 2001


def test_too_large_files_and_custom_limits(tmp_path):
    names = ns(*[("M", f"backend/app/m{i}.py") for i in range(4)])
    assert run(tmp_path, name_status=names, extra=["--max-files", "3"])[1]["skip_reason"] == "diff_too_large"
    assert run(tmp_path, name_status=names, extra=["--max-files", "4"])[1]["run_agent"] is True


def test_at_limit_is_ok(tmp_path):
    n = numstat(("1999", "1", "backend/app/a.py"))
    assert run(tmp_path, name_status=ns(("M", "backend/app/a.py")), ns_numstat=n)[1]["run_agent"] is True


def test_binary_counts_zero(tmp_path):
    n = numstat(("-", "-", "frontend/src/logo.png"), ("1", "1", "backend/app/a.py"))
    code, r = run(tmp_path, name_status=ns(("M", "backend/app/a.py"), ("M", "frontend/src/logo.png")), ns_numstat=n)
    assert r["totals"]["lines"] == 2 and r["run_agent"] is True


def test_numstat_rename_record_parsed(tmp_path):
    n = numstat(("1", "2", "old.py", "backend/app/new.py"), ("5", "5", "backend/app/b.py"))
    assert sc.parse_numstat(n) == 13


def test_precedence_order():
    # every gate true at once; peel them off from the top
    ev = {"draft": True, "head": "evil/r", "base": "o/r", "labels": ["skip-test-agent"],
          "login": "x[bot]", "type": "Bot"}
    entries = [{"status": "M", "old": ".github/x", "new": ".github/x"}]
    msg = "Test-Agent-Commit: true"
    order = ["fork", "draft", "bot_author", "opt_out_label", "control_plane_changed",
             "agent_commit", "no_testable_changes", "diff_too_large"]
    got = []
    assert sc.decide(ev, entries, 99999, msg, 50, 2000)["skip_reason"] == "fork"
    ev["head"] = "o/r"
    for step in order[1:]:
        r = sc.decide(ev, entries, 99999, msg, 50, 2000)
        got.append(r["skip_reason"])
        assert r["skip_reason"] == step
        if step == "draft": ev["draft"] = False
        elif step == "bot_author": ev["type"], ev["login"] = "User", "x"
        elif step == "opt_out_label": ev["labels"] = []
        elif step == "control_plane_changed": entries = [{"status": "M", "old": "README.md", "new": "README.md"}]
        elif step == "agent_commit": msg = ""
        elif step == "no_testable_changes":
            entries = [{"status": "M", "old": "backend/app/a.py", "new": "backend/app/a.py"}]
    assert got == order[1:]


def test_z_parsing_with_spaces_and_newlines(tmp_path):
    weird = "backend/app/my file\nwith newline.py"
    names = ns(("M", weird), ("R100", "frontend/src/a b.ts", "frontend/src/c d.ts"))
    code, r = run(tmp_path, name_status=names)
    assert code == 0 and r["source_files"]["backend"] == [weird]
    assert r["source_files"]["frontend"] == ["frontend/src/c d.ts"]
    assert r["totals"]["files"] == 2


def test_real_git_output_roundtrip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True).stdout
    git("init", "-q", "-b", "main")
    git("config", "user.email", "a@b"); git("config", "user.name", "n")
    (repo / "backend/app").mkdir(parents=True)
    (repo / ".claude").mkdir()
    (repo / ".claude/x.md").write_text("hello\nworld\nfoo\nbar\n")
    (repo / "backend/app/a.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    git("mv", ".claude/x.md", "moved.md")
    (repo / "backend/app/a b.py").write_text("y = 2\n")
    git("add", "-A"); git("commit", "-qm", "head")
    nsb = git("diff", "--name-status", "-z", "--find-renames", "HEAD~1...HEAD")
    nub = git("diff", "--numstat", "-z", "--find-renames", "HEAD~1...HEAD")
    code, r = run(tmp_path, name_status=nsb, ns_numstat=nub)
    assert code == 0 and r["skip_reason"] == "control_plane_changed"
    assert r["totals"]["files"] == 2


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"pull_request": 3}',
                                 json.dumps({"pull_request": {"draft": "no"}})])
def test_malformed_event(tmp_path, raw):
    code, r = run(tmp_path, name_status=ns(BACKEND), raw_event=raw)
    assert code == 1 and r["run_agent"] is False and r["skip_reason"] == "scope_error"


def test_missing_user_type_is_error(tmp_path):
    ev = event(user={"login": "a"})
    code, r = run(tmp_path, ev, ns(BACKEND))
    assert code == 1 and r["skip_reason"] == "scope_error"


@pytest.mark.parametrize("bad_ns", [b"Z\0a\0", b"M\0", b"R100\0only\0", b"M\0\0", b"garbage"])
def test_malformed_name_status(tmp_path, bad_ns):
    code, r = run(tmp_path, name_status=bad_ns)
    assert code == 1 and r["skip_reason"] == "scope_error" and r["run_agent"] is False


@pytest.mark.parametrize("bad", [b"1\t2\0", b"x\ty\tz\0", b"1\t1\t\0onlyone\0"])
def test_malformed_numstat(tmp_path, bad):
    code, r = run(tmp_path, name_status=ns(BACKEND), ns_numstat=bad)
    assert code == 1 and r["skip_reason"] == "scope_error"


def test_missing_input_file_fails_closed(tmp_path):
    out = tmp_path / "o.json"
    code = sc.main(["--event", str(tmp_path / "nope"), "--name-status", str(tmp_path / "nope"),
                    "--numstat", str(tmp_path / "nope"), "--head-commit-message", str(tmp_path / "nope"),
                    "--output", str(out)])
    assert code == 1 and json.loads(out.read_text())["skip_reason"] == "scope_error"
