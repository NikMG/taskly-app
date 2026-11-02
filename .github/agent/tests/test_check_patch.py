import json
import os
import subprocess

import pytest

import check_patch as cp

BASE_TEST = """def test_a():
    assert a() == 1


def test_b():
    assert b() == 2
    assert c() == 3
"""
BASE_FRONT = """import { it, expect } from 'vitest'
it('a', () => {
  expect(f()).toBe(1)
})
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "backend/tests").mkdir(parents=True)
    (root / "backend/app").mkdir(parents=True)
    (root / "frontend/src/test").mkdir(parents=True)
    (root / "backend/tests/test_a.py").write_text(BASE_TEST)
    (root / "backend/tests/conftest.py").write_text("import pytest\n")
    (root / "backend/app/x_old.py").write_text("def f():\n    return 1\n")
    (root / "frontend/src/a.test.ts").write_text(BASE_FRONT)
    r = Repo(root)
    r.git("init", "-q", "-b", "main")
    r.git("config", "user.email", "a@b")
    r.git("config", "user.name", "n")
    r.git("add", "-A")
    r.git("commit", "-qm", "base")
    return r


class Repo:
    def __init__(self, root):
        self.root = root

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True).stdout

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def patch(self):
        self.git("add", "-A")
        return self.git("diff", "--cached", "-M", "--binary", "--no-color")


def check(tmp_path, patch, *extra):
    pf = tmp_path / "p.patch"
    pf.write_bytes(patch)
    out = tmp_path / "out.json"
    code = cp.main([str(pf), "--output", str(out), *extra])
    return code, json.loads(out.read_text())


def rules(res):
    return {v["rule"] for v in res["violations"]}


def test_clean_new_backend_test(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert f() == 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0 and res["ok"] and res["violations"] == []
    assert res["stats"] == {"files": 1, "added": 2, "removed": 0,
                            "assertions_added": 1, "assertions_removed": 0}
    assert res["files"] == [{"path": "backend/tests/test_new.py", "change": "text"}]


def test_clean_frontend_update_and_new(repo, tmp_path):
    repo.write("frontend/src/a.test.ts", BASE_FRONT + "it('b', () => { expect(g()).toBe(2) })\n")
    repo.write("frontend/src/ui/B.test.tsx", "it('c', () => { expect(f()).toBe(1) })\n")
    repo.write("frontend/src/test/helpers.ts", "export const h = 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


def test_empty_patch_ok(tmp_path):
    code, res = check(tmp_path, b"")
    assert code == 0 and res["ok"] and res["stats"]["files"] == 0


def test_production_file_edit(repo, tmp_path):
    repo.write("backend/app/x_old.py", "def f():\n    return 2\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and rules(res) == {"path_not_allowed"}
    assert res["violations"][0]["file"] == "backend/app/x_old.py"


def test_mixed_allowed_and_production(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert f() == 1\n")
    repo.write("backend/app/x_old.py", "def f():\n    return 3\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and [v["file"] for v in res["violations"]] == ["backend/app/x_old.py"]


@pytest.mark.parametrize("path", [
    "backend/tests/helper.py", "backend/tests/data.json", "frontend/src/a.ts",
    "frontend/src/test.ts", "frontend/package.json", ".github/workflows/x.yml", "CLAUDE.md",
    "backend/tests/mocks.pyc", "backend/tests/sub/mocks.py", "frontend/src/test/x.js",
])
def test_path_not_allowed_table(repo, tmp_path, path):
    repo.write(path, "x = 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "path_not_allowed" in rules(res)


@pytest.mark.parametrize("path", [
    "backend/tests/test_z.py", "backend/tests/sub/deep/test_z.py", "backend/tests/sub/conftest.py",
    "backend/tests/sub/__init__.py", "backend/tests/mocks.py", "frontend/src/x/y.test.tsx",
    "frontend/src/test/sub/util.tsx",
])
def test_allowed_paths(path):
    assert cp.path_allowed(path)


def hand_patch(path, body="+assert f() == 1\n", header=None, mode="100644"):
    header = header or f"diff --git a/{path} b/{path}\nnew file mode {mode}\nindex 0000000..1111111\n"
    return (header + f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n" + body).encode()


def test_dotdot_traversal(tmp_path):
    code, res = check(tmp_path, hand_patch("backend/tests/../../.github/x.py"))
    assert code in (1, 2)
    if code == 1:
        assert "unsafe_path" in rules(res)


def test_dotdot_traversal_at_start(tmp_path):
    code, res = check(tmp_path, hand_patch("../outside.py"))
    assert code in (1, 2)
    if code == 1:
        assert "unsafe_path" in rules(res)


def test_git_dir_segment(tmp_path):
    code, res = check(tmp_path, hand_patch("backend/tests/.git/test_hooks.py"))
    assert code == 1 and "unsafe_path" in rules(res)


def test_unsafe_reasons_unit():
    assert cp._unsafe_reason("/abs/test_x.py")
    assert cp._unsafe_reason("a\\b")
    assert cp._unsafe_reason("a\x01b")
    assert cp._unsafe_reason('"quoted"')
    assert cp._unsafe_reason("a/./b")
    assert cp._unsafe_reason("a/.GIT/b")
    assert cp._unsafe_reason("backend/tests/test_ok.py") is None


def test_quoted_path_detected(tmp_path):
    p = (b'diff --git "a/backend/tests/test_\\001.py" "b/backend/tests/test_\\001.py"\n'
         b'new file mode 100644\nindex 0000000..1111111\n--- /dev/null\n'
         b'+++ "b/backend/tests/test_\\001.py"\n@@ -0,0 +1 @@\n+assert f() == 1\n')
    code, res = check(tmp_path, p)
    assert code == 1 and "unsafe_path" in rules(res)


def test_symlink(repo, tmp_path):
    os.symlink("../../.env", repo.root / "backend/tests/test_link.py")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "file_mode" in rules(res)


def test_chmod_plus_x(repo, tmp_path):
    (repo.root / "backend/tests/test_a.py").chmod(0o755)
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "file_mode" in rules(res)


def test_new_executable_file(repo, tmp_path):
    repo.write("backend/tests/test_exe.py", "assert f() == 1\n")
    (repo.root / "backend/tests/test_exe.py").chmod(0o755)
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "file_mode" in rules(res)


def test_submodule_mode(tmp_path):
    p = (b"diff --git a/backend/tests/test_sub.py b/backend/tests/test_sub.py\n"
         b"new file mode 160000\nindex 0000000..1111111\n--- /dev/null\n"
         b"+++ b/backend/tests/test_sub.py\n@@ -0,0 +1 @@\n+Subproject commit abc\n")
    code, res = check(tmp_path, p)
    assert code == 1 and "file_mode" in rules(res)


@pytest.mark.parametrize("line", [
    "it.only('x', () => {})", "describe.skip('x')", "it.skipIf(true)('x')", "it.runIf(a)('x')",
    "it.todo('x')", "it.fails('x')", "xit('x')", "xdescribe('x')", "fit('x')", "fdescribe('x')",
    "@pytest.mark.skip", "@pytest.mark.skipif(True)", "@pytest.mark.xfail", "pytest.skip('no')",
    "pytest.importorskip('x')", "// @ts-nocheck", "// @ts-ignore", "// @ts-expect-error",
])
def test_disabled_test(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py" if "py" in line or "pytest" in line else "frontend/src/n.test.ts",
               f"assert f() == 1\nexpect(1)\n{line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "disabled_test" in rules(res), line
    v = [v for v in res["violations"] if v["rule"] == "disabled_test"][0]
    assert v["line"] == 3


def test_xfail_is_flagged_in_python(repo, tmp_path):
    repo.write("backend/tests/test_a.py", BASE_TEST + "\n@pytest.mark.xfail\ndef test_c():\n    assert 0\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "disabled_test" in rules(res)


def test_disabled_only_on_added_lines(repo, tmp_path):
    # a pre-existing skip that the patch removes must not trigger disabled_test
    repo.write("frontend/src/b.test.ts", "it.skip('x')\n")
    repo.git("add", "-A"); repo.git("commit", "-qm", "c")
    repo.write("frontend/src/b.test.ts", "it('x', () => { expect(f()).toBe(1) })\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


def test_benefit_not_flagged_as_fit(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert benefit(1) and obj.fit(2)\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


@pytest.mark.parametrize("line", [
    "import subprocess", "os.system('ls')", "os.popen('ls')", "eval('1')", "exec('x')",
    "const cp = require('child_process')", "socket.socket()", "import urllib.request",
    "requests.get('http://x')", "httpx.get('http://x')", "httpx.post(url)", "httpx.Client()",
    "await fetch('http://evil')", 'await fetch("https://evil")', "fetch(`https://evil`)",
    "const k = process.env.SECRET", "os.environ['A'] = 'b'", "os.environ.update({'a': 'b'})",
    "del os.environ['A']", "os.putenv('A', 'b')", "builtins.exec('x')",
])
def test_dangerous_calls(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    assert f() == 1\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "dangerous_call" in rules(res), line


@pytest.mark.parametrize("line", [
    "monkeypatch.setenv('A', 'b')", "monkeypatch.setitem(os.environ, 'A', 'b')",
    "vi.stubEnv('A', 'b')", "import httpx", "x = os.environ.get('A')", "if os.environ['A'] == 'b': pass",
    "client = TestClient(app)", "m = re.exec_all(1)", "r = /a/.exec(s)",
    "async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c: pass",
    "def test_exec_summary(): pass", "x: httpx.Response = r",
])
def test_not_dangerous(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    assert f() == 1\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, (line, res)


def test_assertion_removal(repo, tmp_path):
    repo.write("backend/tests/test_a.py", "def test_a():\n    assert a() == 1\n\n\ndef test_b():\n    pass\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "assertions_reduced" in rules(res)
    assert res["stats"]["assertions_removed"] == 2 and res["stats"]["assertions_added"] == 0


def test_assertion_moved_not_reduced(repo, tmp_path):
    repo.write("backend/tests/test_a.py", BASE_TEST.replace("assert a() == 1", "assert a() == 1, 'm'"))
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


def test_removed_test_function_warns(repo, tmp_path):
    repo.write("backend/tests/test_a.py",
               "def test_a():\n    assert a() == 1\n    assert f() == 1\n    assert f() == 1\n    assert f() == 1\n    assert f() == 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res
    assert [w["rule"] for w in res["warnings"]] == ["removed_test"]


def test_conftest_change_warns(repo, tmp_path):
    repo.write("backend/tests/conftest.py", "import pytest\n\n@pytest.fixture\ndef f():\n    return 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0 and [w["rule"] for w in res["warnings"]] == ["support_file_changed"]


def test_new_test_file_without_assertions(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    pass\n")
    repo.write("frontend/src/z.test.ts", "it('x', () => {})\n")
    repo.write("backend/tests/sub/conftest.py", "x = 1\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1
    na = [v["file"] for v in res["violations"] if v["rule"] == "no_assertions"]
    assert sorted(na) == ["backend/tests/test_new.py", "frontend/src/z.test.ts"]


def test_deleted_test_file(repo, tmp_path):
    (repo.root / "backend/tests/test_a.py").unlink()
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and {"deleted_test_file"} <= rules(res)


def test_rename_test_to_production(repo, tmp_path):
    repo.git("mv", "backend/tests/test_a.py", "backend/app/x.py")
    code, res = check(tmp_path, repo.patch())
    assert code == 1
    assert any(v["rule"] == "path_not_allowed" and v["file"] == "backend/app/x.py" for v in res["violations"])


def test_rename_production_to_test(repo, tmp_path):
    repo.git("mv", "backend/app/x_old.py", "backend/tests/test_moved.py")
    code, res = check(tmp_path, repo.patch())
    assert code == 1
    assert any(v["rule"] == "path_not_allowed" and v["file"] == "backend/app/x_old.py" for v in res["violations"])


def test_rename_test_within_tests_ok(repo, tmp_path):
    repo.git("mv", "backend/tests/test_a.py", "backend/tests/test_renamed.py")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


def test_rename_with_changes_both_sides_checked(repo, tmp_path):
    repo.git("mv", "backend/app/x_old.py", "backend/app/x_new.py")
    repo.write("backend/app/x_new.py", "def f():\n    return 1\n# more\n")
    code, res = check(tmp_path, repo.patch())
    files = {v["file"] for v in res["violations"] if v["rule"] == "path_not_allowed"}
    assert files == {"backend/app/x_old.py", "backend/app/x_new.py"}


def test_binary_patch(repo, tmp_path):
    (repo.root / "backend/tests/test_blob.py").write_bytes(bytes(range(256)) * 4)
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "binary" in rules(res)


def test_too_large(repo, tmp_path):
    for i in range(4):
        repo.write(f"backend/tests/test_n{i}.py", "assert f() == 1\n" * 5)
    p = repo.patch()
    code, res = check(tmp_path, p, "--max-files", "3")
    assert code == 1 and "too_large" in rules(res)
    code, res = check(tmp_path, p, "--max-added-lines", "19")
    assert code == 1 and "too_large" in rules(res)
    code, res = check(tmp_path, p, "--max-files", "4", "--max-added-lines", "20")
    assert code == 0


def test_parser_mismatch_hidden_second_file(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert f() == 1\n")
    legit = repo.patch().decode()
    # Text that looks like a second file's header sitting outside any hunk: our
    # parser and git must agree on it, else the patch is rejected.
    evil = ("diff --git a/backend/app/main.py b/backend/app/main.py\n"
            "--- a/backend/app/main.py\n+++ b/backend/app/main.py\n")
    # git itself sees these two as (empty) file sections, so they are flagged as such
    for patch in (evil + legit, legit + evil):
        code, res = check(tmp_path, patch.encode())
        assert code == 1 and "path_not_allowed" in rules(res)
    # a bare ---/+++ pair with no hunk after the last section: only the text parser sees it
    code, res = check(tmp_path, (legit + "--- a/backend/app/main.py\n+++ b/backend/app/main.py\n").encode())
    assert code == 1 and "parser_mismatch" in rules(res)


def test_parser_mismatch_detected_directly(monkeypatch):
    legit = hand_patch("backend/tests/test_new.py")
    real = cp.git_view

    def lying(patch):
        gv = real(patch)
        gv["numstat"].append(("backend/app/hidden.py", 1, 0))
        return gv
    monkeypatch.setattr(cp, "git_view", lying)
    res = cp.analyze(legit, 30, 1500)
    assert "parser_mismatch" in {v["rule"] for v in res["violations"]}
    assert not res["ok"]


def test_parser_mismatch_on_counts(monkeypatch):
    real = cp.git_view

    def lying(patch):
        gv = real(patch)
        gv["numstat"] = [(p, 99, 0) for p, _, _ in gv["numstat"]]
        return gv
    monkeypatch.setattr(cp, "git_view", lying)
    res = cp.analyze(hand_patch("backend/tests/test_new.py"), 30, 1500)
    assert "parser_mismatch" in {v["rule"] for v in res["violations"]}


def test_added_diff_header_inside_hunk_is_content(repo, tmp_path):
    body = "def test_x():\n    assert f() == 1\n    s = '''\ndiff --git a/backend/app/main.py b/backend/app/main.py\n'''\n"
    repo.write("backend/tests/test_new.py", body)
    code, res = check(tmp_path, repo.patch())
    assert code == 0, res


@pytest.mark.parametrize("garbage", [b"hello world\n", b"\x00\x01\x02", b"diff --git a/x b/x\n",
                                     b"--- a/x\n+++ b/x\n@@ -1 +1 @@\n"])
def test_garbage_exit_2(tmp_path, garbage):
    code, res = check(tmp_path, garbage)
    assert code == 2 and res["ok"] is False and "error" in res


def test_truncated_patch_exit_2(repo, tmp_path):
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert f() == 1\n    assert f() == 1\n")
    p = repo.patch()
    code, _ = check(tmp_path, p[: len(p) - 10])
    assert code == 2


def test_missing_patch_file(tmp_path):
    assert cp.main([str(tmp_path / "nope")]) == 2


def test_assertion_regex_counts():
    f = lambda s: len(cp.ASSERT_RE.findall(s))
    assert f("assert x") == 1 and f("self.assertEqual(a, b)") == 0
    assert f("expect(a).toBe(1); expect(b).toBe(2)") == 2
    assert f("with pytest.raises(ValueError):") == 1 and f("expect(fn).toHaveBeenCalledWith(1)") == 2


@pytest.mark.parametrize("line", [
    'collect_ignore = ["test_a.py"]', 'collect_ignore_glob = ["*"]',
    "def pytest_collection_modifyitems(items): items.clear()", "def pytest_ignore_collect(p): return True",
    "def pytest_collect_file(p): pass", "def pytest_runtest_call(item): pass",
    "def pytest_pycollect_makeitem(c): pass", "__test__ = False",
    "it.skip.each([1])('a', () => {})", "it['skip']('a', () => {})", 'it["only"]("a")',
    "describe.only.each([1])('a')", "test.concurrent.skip('a')", "it.todo('a')", "it.skipIf(c)('a')",
    "testPathIgnorePatterns: ['x']", "passWithNoTests: true",
])
def test_disabled_test_bypasses_closed(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    assert x\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "disabled_test" in rules(res), line


@pytest.mark.parametrize("line", [
    "skipped_count = 3", "todos = []", "const t = items.todos", "x.skipWhile(f)", "todoKeys = list(k)",
    "todoFixture = make()", "const TodoItem = 1", "def test_todo_create(): pass", "todo = Todo()",
    "client.post('/todos')", "result = it.each_todo", "my_collect_ignored = 1", "obj.only_one(1)",
    "task.todo.title",
])
def test_disabled_test_no_false_positives(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    assert x\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0, (line, res)


@pytest.mark.parametrize("line", [
    "importlib.import_module('sub' + 'process')", "getattr(os, 'sys' + 'tem')('ls')",
    "getattr(subprocess, 'run')",
])
def test_dangerous_additions(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    assert x\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "dangerous_call" in rules(res)


@pytest.mark.parametrize("line", [
    "assert True", "assert 1", "assert 1 == 1", 'assert "x"', "assert 'x' == 'x'", "assert (True)",
    "assert True, 'msg'", "expect(true).toBe(true)", "expect(1).toBe(1)", "expect(true).toBeTruthy()",
    "expect('a').toEqual('a')",
])
def test_trivial_assertion(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1 and "trivial_assertion" in rules(res), line
    assert res["stats"]["assertions_added"] == 0
    assert "no_assertions" in rules(res)


@pytest.mark.parametrize("line", [
    "assert result == 1", "assert x is not None", "assert len(r) == 1", "expect(result).toBe(1)",
    "expect(screen.getByText('a')).toBeTruthy()", "assert r.status_code == 200",
])
def test_nontrivial_assertion_ok(repo, tmp_path, line):
    repo.write("backend/tests/test_new.py", f"def test_x():\n    {line}\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 0 and res["stats"]["assertions_added"] == 1, (line, res)


def test_production_line_edit_next_to_new_test(repo, tmp_path):
    repo.write("backend/app/a.py", "def f():\n    return 1\n")
    repo.git("add", "-A"); repo.git("commit", "-qm", "a")
    repo.write("backend/app/a.py", "def f():\n    return 99\n")
    repo.write("backend/tests/test_new.py", "def test_x():\n    assert f() == 99\n")
    code, res = check(tmp_path, repo.patch())
    assert code == 1
    assert [v["file"] for v in res["violations"]] == ["backend/app/a.py"]
    assert rules(res) == {"path_not_allowed"}


def test_git_dir_hand_written_patch(tmp_path):
    code, res = check(tmp_path, hand_patch("backend/tests/.git/test_f.py"))
    assert code == 1 and "unsafe_path" in rules(res)
