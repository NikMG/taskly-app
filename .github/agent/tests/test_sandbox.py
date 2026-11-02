"""Integration tests for run_tests.sh. They need Docker and network (to install deps)."""

import os
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

REPO = Path(__file__).resolve().parents[3]
CANARY = "canary-value"

BACKEND_OK = '''
import os, socket


def test_env_has_no_key():
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "{canary}" not in repr(dict(os.environ))
    with open("/proc/self/environ", "rb") as f:
        assert b"{canary}" not in f.read()


def test_network_blocked():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3)
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 443)


def test_workspace_read_only():
    for target in ("/workspace/probe.txt", "/workspace/backend/probe.txt", "/deps/probe.txt"):
        with pytest.raises(OSError):
            open(target, "w")


def test_not_root():
    assert os.getuid() == 10001
    assert os.geteuid() == 10001
'''.format(canary=CANARY).replace("import os, socket", "import os, socket\n\nimport pytest", 1)

BACKEND_FAIL = "def test_fails():\n    assert False\n"

FRONTEND_OK = '''// @vitest-environment node
import { test, expect } from 'vitest'
import fs from 'node:fs'
import net from 'node:net'

test('env has no key', () => {
  expect(process.env.ANTHROPIC_API_KEY).toBeUndefined()
  expect(JSON.stringify(process.env)).not.toContain('CANARY')
  expect(fs.readFileSync('/proc/self/environ').toString()).not.toContain('CANARY')
})

test('network blocked', async () => {
  await new Promise<void>((resolve, reject) => {
    const s = net.connect({ host: '1.1.1.1', port: 443 })
    s.setTimeout(3000, () => { s.destroy(); reject(new Error('connected or hung')) })
    s.on('connect', () => { s.destroy(); reject(new Error('connected')) })
    s.on('error', () => resolve())
  })
  await expect(fetch('https://example.com', { signal: AbortSignal.timeout(2000) })).rejects.toThrow()
})

test('workspace read only', () => {
  for (const p of ['/workspace/probe.txt', '/workspace/frontend/probe.txt', '/deps/probe.txt']) {
    expect(() => fs.writeFileSync(p, 'x')).toThrow()
  }
})

test('not root', () => {
  expect(process.getuid?.()).toBe(10001)
  expect(process.geteuid?.()).toBe(10001)
})
'''.replace("CANARY", CANARY)

FRONTEND_FAIL = '''// @vitest-environment node
import { test, expect } from 'vitest'
test('fails', () => { expect(1).toBe(2) })
'''

def _plant_py(victim):
    return '''import os


def test_plant():
    victim = "{victim}"
    os.symlink(victim, "/out/junit.xml")
    os.symlink(victim, "/out/test.log")
    os.makedirs("/out/sub")
    os.symlink(victim, "/out/sub/link")
    os.mkfifo("/out/sub/fifo")
    open("/out/suid", "w").close()
    os.chmod("/out/suid", 0o4755)
    os.makedirs("/out/sgid")
    os.chmod("/out/sgid", 0o2755)
    os.makedirs("/out/locked")
    open("/out/locked/f", "w").close()
    os.chmod("/out/locked", 0)
'''.format(victim=victim)


def _plant_ts(victim):
    return '''// @vitest-environment node
import { test } from 'vitest'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'

test('plant', () => {
  const victim = 'VICTIM'
  fs.symlinkSync(victim, '/out/junit.xml')
  fs.symlinkSync(victim, '/out/test.log')
  fs.mkdirSync('/out/sub')
  fs.symlinkSync(victim, '/out/sub/link')
  execFileSync('mkfifo', ['/out/sub/fifo'])
  fs.writeFileSync('/out/suid', '')
  fs.chmodSync('/out/suid', 0o4755)
  fs.mkdirSync('/out/sgid')
  fs.chmodSync('/out/sgid', 0o2755)
  fs.mkdirSync('/out/locked')
  fs.writeFileSync('/out/locked/f', '')
  fs.chmodSync('/out/locked', 0)
})
'''.replace('VICTIM', victim)


BIG_PY = (
    "def test_big():\n"
    "    with open('/out/big.bin', 'wb') as f:\n"
    "        f.write(b'x' * 3_000_000)\n"
)
BIG_TS = '''// @vitest-environment node
import { test } from 'vitest'
import fs from 'node:fs'
test('big', () => { fs.writeFileSync('/out/big.bin', Buffer.alloc(3_000_000)) })
'''

HIDDEN_PY = '''import os


def test_hidden():
    for p in ("/workspace/.env", "/workspace/.git", "/workspace/.github"):
        assert not os.path.exists(p), p
    assert os.listdir("/workspace") == ["backend"]
'''
HIDDEN_TS = '''// @vitest-environment node
import { test, expect } from 'vitest'
import fs from 'node:fs'
test('hidden', () => {
  for (const p of ['/workspace/.env', '/workspace/.git', '/workspace/.github']) {
    expect(fs.existsSync(p)).toBe(false)
  }
  expect(fs.readdirSync('/workspace')).toEqual(['frontend'])
})
'''

PROBES = {
    "backend/tests/test_probe_ok.py": BACKEND_OK,
    "backend/tests/test_probe_fail.py": BACKEND_FAIL,
    "frontend/src/probe_ok.test.ts": FRONTEND_OK,
    "frontend/src/probe_fail.test.ts": FRONTEND_FAIL,
    "backend/tests/test_probe_big.py": BIG_PY,
    "frontend/src/probe_big.test.ts": BIG_TS,
    "backend/tests/test_probe_hidden.py": HIDDEN_PY,
    "frontend/src/probe_hidden.test.ts": HIDDEN_TS,
}
VICTIM_NAME = "victim.txt"
VICTIM_TEXT = "original"


def _tree_files():
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p and (REPO / p).is_file()]


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists():  # files created by uid 10001 inside the container
        subprocess.run(
            ["docker", "run", "--rm", "--user", "0", "-v", f"{path}:/x",
             "--entrypoint", "sh", "taskly-agent-sandbox:backend", "-c", "rm -rf /x/* /x/.[!.]*"],
            capture_output=True,
        )
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def env_dirs(tmp_path_factory):
    root = Path(tmp_path_factory.mktemp("sandbox")).resolve()
    ws = root / "repo"
    for rel in _tree_files():
        dest = ws / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    victim = root / VICTIM_NAME
    victim.write_text(VICTIM_TEXT)
    # Secrets and VCS data at the workspace root must stay invisible to tests.
    (ws / ".env").write_text("SECRET=" + CANARY)
    (ws / ".git").mkdir()
    (ws / ".git/config").write_text("x")
    probes = dict(PROBES)
    probes["backend/tests/test_probe_plant.py"] = _plant_py(victim)
    probes["frontend/src/probe_plant.test.ts"] = _plant_ts(str(victim))
    for rel, body in probes.items():
        (ws / rel).write_text(body)
    deps = root / "deps"
    deps.mkdir()
    out = root / "out"
    env = dict(
        os.environ,
        ANTHROPIC_API_KEY=CANARY,
        AGENT_WORKSPACE=str(ws),
        AGENT_DEPS_DIR=str(deps),
        AGENT_OUTPUT_DIR=str(out),
    )
    for stack in ("backend", "frontend"):
        subprocess.run([str(ws / ".github/agent/prepare_sandbox.sh"), stack],
                       env=env, check=True, capture_output=True, timeout=900)
    yield ws, out, env, victim
    _rmtree(str(deps))
    _rmtree(str(out))
    shutil.rmtree(root, ignore_errors=True)


def run(env_dirs, *args):
    ws, _, env, _ = env_dirs
    return subprocess.run([str(ws / ".github/agent/run_tests.sh"), *args],
                          env=env, capture_output=True, text=True, timeout=600)


def test_scripts_are_executable():
    for name in ("run_tests.sh", "prepare_sandbox.sh"):
        assert (REPO / ".github/agent" / name).stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("stack,probe", [
    ("backend", "backend/tests/test_probe_ok.py"),
    ("frontend", "frontend/src/probe_ok.test.ts"),
])
def test_sandbox_probes_pass(env_dirs, stack, probe):
    # Covers: no key in env, no network, read-only workspace, uid 10001.
    result = run(env_dirs, stack, "--", probe)
    assert result.returncode == 0, result.stdout + result.stderr
    junit = env_dirs[1] / stack / "results" / "junit.xml"
    root = ET.parse(junit).getroot()
    assert root.iter("testcase")
    assert not list(root.iter("failure"))
    assert len(list(root.iter("testcase"))) == 4
    assert CANARY not in result.stdout


@pytest.mark.parametrize("stack,probe", [
    ("backend", "backend/tests/test_probe_fail.py"),
    ("frontend", "frontend/src/probe_fail.test.ts"),
])
def test_failing_test_exits_1_with_junit(env_dirs, stack, probe):
    junit = env_dirs[1] / stack / "results" / "junit.xml"
    result = run(env_dirs, stack, probe)
    assert result.returncode == 1, result.stdout + result.stderr
    assert junit.is_file()
    assert list(ET.parse(junit).getroot().iter("failure"))
    assert (env_dirs[1] / stack / "test.log").is_file()


@pytest.mark.parametrize("args", [
    ["backend", "../x"],
    ["backend", "-x"],
    ["backend", "--", "-x"],
    ["backend", "a;b"],
    ["backend", "$(id)"],
    ["backend", "backend/tests/../../x.py"],
    ["backend", "backend/tests/a b.py"],
    ["backend", "backend/tests/x.py;id"],
    ["backend", "frontend/src/a.test.ts"],
    ["frontend", "frontend/src/a.ts"],
    ["frontend", "frontend/src/a.test.ts`id`"],
    ["nope"],
    ["backend", "--bogus"],
    ["frontend", "--coverage", "--watch"],
    [],
])
def test_bad_arguments_start_no_container(env_dirs, tmp_path, args):
    marker = tmp_path / "docker-called"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "docker").write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    (shim / "docker").chmod(0o755)
    ws, _, env, _ = env_dirs
    env = dict(env, PATH=f"{shim}:{env['PATH']}")
    result = subprocess.run([str(ws / ".github/agent/run_tests.sh"), *args],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not marker.exists()


PLANT = {
    "backend": "backend/tests/test_probe_plant.py",
    "frontend": "frontend/src/probe_plant.test.ts",
}
HIDDEN = {
    "backend": "backend/tests/test_probe_hidden.py",
    "frontend": "frontend/src/probe_hidden.test.ts",
}


@pytest.mark.parametrize("stack", ["backend", "frontend"])
def test_planted_links_fifos_and_modes_are_sanitized(env_dirs, stack):
    _, out, _, victim = env_dirs
    result = run(env_dirs, stack, PLANT[stack])
    assert result.returncode == 0, result.stdout + result.stderr
    assert victim.read_text() == VICTIM_TEXT
    results = out / stack / "results"
    entries = [results] + list(results.rglob("*"))
    for p in entries:
        mode = p.lstat().st_mode
        assert stat.S_ISREG(mode) or stat.S_ISDIR(mode), p
        assert not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX), p
    assert not (results / "junit.xml").is_symlink()
    assert (results / "suid").is_file()
    assert (results / "suid").stat().st_mode & 0o777 == 0o644
    assert (results / "locked" / "f").is_file()
    assert (results / "sgid").is_dir()
    assert not (results / "sub" / "link").exists()
    assert not (results / "sub" / "fifo").exists()


@pytest.mark.parametrize("stack", ["backend", "frontend"])
def test_test_log_is_host_owned_regular_file(env_dirs, stack):
    _, out, _, victim = env_dirs
    result = run(env_dirs, stack, PLANT[stack])
    assert result.returncode == 0, result.stdout + result.stderr
    log = out / stack / "test.log"
    assert log.is_file() and not log.is_symlink()
    assert "plant" in log.read_text()
    assert victim.read_text() == VICTIM_TEXT
    assert not (out / stack).stat().st_mode & 0o002  # not world-writable


@pytest.mark.parametrize("stack", ["backend", "frontend"])
def test_secrets_and_git_not_visible_in_workspace(env_dirs, stack):
    result = run(env_dirs, stack, HIDDEN[stack])
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("stack,probe", [
    ("backend", "backend/tests/test_probe_big.py"),
    ("frontend", "frontend/src/probe_big.test.ts"),
])
def test_oversize_results_are_wiped(env_dirs, stack, probe):
    # Shrink the limit in the temp copy of the script (1 MB instead of 256 MB).
    script = env_dirs[0] / ".github/agent/run_tests.sh"
    original = script.read_text()
    assert "max_results_kb=262144" in original
    script.write_text(original.replace("max_results_kb=262144", "max_results_kb=1024"))
    try:
        result = run(env_dirs, stack, probe)
    finally:
        script.write_text(original)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "wiped" in result.stderr
    assert list((env_dirs[1] / stack / "results").iterdir()) == []


def test_stale_results_are_wiped_before_run(env_dirs):
    stale = env_dirs[1] / "backend" / "results" / "stale.txt"
    stale.write_text("old")
    result = run(env_dirs, "backend", "backend/tests/test_probe_fail.py")
    assert result.returncode == 1
    assert not stale.exists()
