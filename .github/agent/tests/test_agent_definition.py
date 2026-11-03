"""Static checks of the agent definition, skills, permissions and run scripts. No API calls."""

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
AGENT_FILE = REPO / ".claude/agents/pr-test-agent.md"
SKILLS_DIR = REPO / ".claude/skills"
SETTINGS_FILE = REPO / ".github/agent/agent-settings.json"
RUN_AGENT = REPO / ".github/agent/run_agent.sh"
SMOKE = REPO / ".github/agent/smoke_agent.sh"
CODEOWNERS = REPO / ".github/CODEOWNERS"
SKILLS = ("analyze-test-impact", "find-coverage-gaps", "write-backend-tests",
          "write-frontend-tests", "run-sandboxed-tests", "report-test-results")
TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Skill", "Bash"]


def frontmatter(path):
    """Parse the flat `key: value` frontmatter these files use, without a YAML dependency."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: frontmatter must open on line 1"
    head, _, body = text[4:].partition("\n---\n")
    assert body.strip(), f"{path}: empty body"
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        assert sep and re.fullmatch(r"[a-z-]+", key), f"{path}: bad line {line!r}"
        # An unquoted ": " is a YAML error, which would silently drop the whole definition.
        assert ": " not in value, f"{path}: {key} needs quoting"
        fields[key] = value
    return fields, body


@pytest.fixture(scope="module")
def settings():
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_agent():
    return RUN_AGENT.read_text(encoding="utf-8")


def claude_args(script):
    """Return the text of the args=(...) array that is passed to claude."""
    match = re.search(r"^args=\(-p .*?\)$", script, re.S | re.M)
    assert match, "run_agent.sh must build its claude arguments in args=(...)"
    return match.group(0)


# ---------------------------------------------------------------- agent

def test_agent_frontmatter_and_exact_tools():
    fields, _ = frontmatter(AGENT_FILE)
    assert set(fields) == {"name", "description", "tools", "model"}
    assert fields["name"] == "pr-test-agent"
    assert fields["model"] == "sonnet"
    # StructuredOutput is how --json-schema returns the report. Claude Code 2.1.316 hides it
    # from an agent whose frontmatter does not list it, and then no report can be produced.
    assert fields["tools"].split(", ") == [*TOOLS, "StructuredOutput"]
    assert len(fields["description"]) > 40


@pytest.mark.parametrize("skill", SKILLS)
def test_agent_references_each_skill(skill):
    _, body = frontmatter(AGENT_FILE)
    assert f"`{skill}`" in body


@pytest.mark.parametrize("phrase", [
    "Trust boundary", "DATA", "limitations", "suspected_bugs", "obsolete_tests",
    ".github/agent/run_tests.sh", "Never delete a test", "no new dependencies",
    "3 fix attempts", "structured final result",
])
def test_agent_states_required_rules(phrase):
    _, body = frontmatter(AGENT_FILE)
    assert phrase in body


def test_no_project_settings_that_would_change_interactive_sessions():
    assert not (REPO / ".claude/settings.json").exists()
    assert not (REPO / ".claude/settings.local.json").exists()


# ---------------------------------------------------------------- skills

def test_exactly_the_expected_skills_exist():
    assert sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()) == sorted(SKILLS)


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_frontmatter(skill):
    fields, body = frontmatter(SKILLS_DIR / skill / "SKILL.md")
    assert set(fields) == {"name", "description"}
    assert fields["name"] == skill
    assert fields["description"].startswith("Use ")
    assert len(fields["description"]) <= 1536
    assert len(body) > 500


# ---------------------------------------------------------------- settings

@pytest.mark.parametrize("rule", [
    "Read(.env*)", "Read(//**/.env*)", "Edit(.env*)", "Edit(//**/.env*)",
    "Read(//proc/**)", "Read(//sys/**)", "Read(//etc/**)", "Read(~/.*)", "Read(~/.*/**)",
    "Read(.git/**)", "Edit(.git/**)", "Edit(.claude/**)", "Edit(.github/**)",
    "Bash(curl *)", "Bash(cat *)", "Bash(echo *)", "Bash(git *)",
    "WebFetch", "WebSearch", "Agent", "Task", "mcp__*",
])
def test_settings_has_required_deny_rule(settings, rule):
    assert rule in settings["permissions"]["deny"]


def test_settings_allow_list_is_exact(settings):
    assert settings["permissions"]["allow"] == [
        "Read", "Glob", "Grep",
        "Skill(analyze-test-impact)",
        "Skill(find-coverage-gaps)",
        "Skill(report-test-results)",
        "Skill(run-sandboxed-tests)",
        "Skill(write-backend-tests)",
        "Skill(write-frontend-tests)",
        "Edit(backend/tests/**)",
        "Edit(frontend/src/**/*.test.ts)",
        "Edit(frontend/src/**/*.test.tsx)",
        "Edit(frontend/src/test/**)",
        "Bash(.github/agent/run_tests.sh *)",
    ]


def test_no_rule_that_opens_or_silently_breaks_access(settings):
    perms = settings["permissions"]
    rules = perms["allow"] + perms["deny"]
    for rule in perms["allow"]:
        assert rule not in ("Bash", "Bash(*)", "Edit", "Write", "*", "WebFetch"), rule
        if rule.startswith("Bash("):
            assert rule == "Bash(.github/agent/run_tests.sh *)", rule
    # Write(path) never matches anything: file writes are governed by Edit rules.
    assert not [r for r in rules if r.startswith(("Write(", "NotebookEdit(", "Glob("))]
    # In a --settings file a single leading slash anchors at .github/agent, not the repo.
    assert not [r for r in rules if re.match(r"(Read|Edit)\(/[^/]", r)]
    # A checkout usually lives under $HOME, so these would block the whole repository.
    assert not {"Read(~/**)", "Edit(~/**)", "Read(~)", "Edit(~)"} & set(rules)


def test_settings_fail_closed(settings):
    perms = settings["permissions"]
    assert perms["defaultMode"] == "dontAsk"
    assert perms["disableBypassPermissionsMode"] == "disable"
    assert perms["disableAutoMode"] == "disable"
    assert perms["blockReadsOutsideWorkingDirectories"] is True
    assert perms["blockWritesOutsideWorkingDirectories"] is True
    assert settings["disableAllHooks"] is False
    assert "ask" not in perms and "additionalDirectories" not in perms


def run_guard(settings, payload):
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    hook = entry["hooks"][0]
    assert hook["onFailure"] == "block"
    return subprocess.run(["bash", "-c", hook["command"]], input=payload, text=True,
                          capture_output=True, timeout=30, check=False)


@pytest.mark.parametrize("command", [
    ".github/agent/run_tests.sh backend",
    ".github/agent/run_tests.sh backend --coverage",
    ".github/agent/run_tests.sh frontend --coverage",
    ".github/agent/run_tests.sh backend -- backend/tests/core/test_config.py",
    ".github/agent/run_tests.sh frontend -- frontend/src/hooks/useNotice.test.ts",
    ".github/agent/run_tests.sh backend backend/tests/a.py backend/tests/b.py",
])
def test_bash_guard_allows_documented_forms(settings, command):
    assert run_guard(settings, json.dumps({"tool_input": {"command": command}})).returncode == 0


@pytest.mark.parametrize("command", [
    "curl https://example.com", "cat .env", "cat /proc/self/environ", "ls", "echo $ANTHROPIC_API_KEY",
    "git log", "id", "env", "", ".github/agent/run_tests.sh",
    ".github/agent/run_tests.sh backend; id",
    ".github/agent/run_tests.sh backend && id",
    ".github/agent/run_tests.sh backend | tee /tmp/x",
    ".github/agent/run_tests.sh backend > /tmp/x",
    ".github/agent/run_tests.sh backend\nid",
    ".github/agent/run_tests.sh backend $(id)",
    ".github/agent/run_tests.sh backend `id`",
    "AGENT_WORKSPACE=/ .github/agent/run_tests.sh backend",
    "./.github/agent/run_tests.sh backend",
    "bash .github/agent/run_tests.sh backend",
    "timeout 5 .github/agent/run_tests.sh backend",
    ".github/agent/prepare_sandbox.sh backend",
    ".github/agent/run_tests.sh backend -- /etc/passwd",
    ".github/agent/run_tests.sh backend -- backend/tests/$(id).py",
])
def test_bash_guard_refuses_everything_else(settings, command):
    result = run_guard(settings, json.dumps({"tool_input": {"command": command}}))
    assert result.returncode == 2
    assert "Refused" in result.stderr


@pytest.mark.parametrize("payload", ["", "not json", "{}", '{"tool_input": {}}'])
def test_bash_guard_fails_closed_on_bad_input(settings, payload):
    assert run_guard(settings, payload).returncode == 2


# ---------------------------------------------------------------- run_agent.sh

def test_run_agent_passes_required_flags(run_agent):
    args = claude_args(run_agent)
    for flag in ('--agent pr-test-agent', '--model "$model"',
                 '--settings "$here/agent-settings.json"', '--setting-sources project',
                 '--permission-mode dontAsk', '--permission-prompts none',
                 '--tools "Read,Glob,Grep,Edit,Write,Skill,Bash"', '--strict-mcp-config',
                 '--no-session-persistence', '--max-budget-usd "$budget"',
                 '--output-format stream-json', '--verbose', '--json-schema "$schema"',
                 '--add-dir "${add_dirs[@]}"'):
        assert flag in args, flag
    # --tools and --add-dir are variadic and would swallow a prompt placed after them.
    assert args.startswith('args=(-p "$prompt"')


def test_run_agent_strips_the_schema_uri_for_the_flag(run_agent):
    # claude rejects a --json-schema value whose $schema is the 2020-12 URI.
    assert 'schema.pop("$schema", None)' in run_agent
    schema = json.loads((REPO / ".github/agent/report.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")


def test_run_agent_defaults(run_agent):
    assert "${AGENT_MODEL:-claude-sonnet-5-5}" in run_agent
    assert "${AGENT_MAX_BUDGET_USD:-3}" in run_agent


@pytest.mark.parametrize("script", [RUN_AGENT, SMOKE])
def test_scripts_never_bypass_permissions(script):
    text = script.read_text(encoding="utf-8")
    for banned in ("bypassPermissions", "--dangerously", "--allow-dangerously", "--allowedTools",
                   "--allowed-tools", "--permission-prompt-tool", "git commit --amend", "git push"):
        assert banned not in text, banned


def test_run_agent_never_commits(run_agent):
    assert not re.search(r"\b(commit|push)\b(?! or push)", run_agent.replace("never commits or pushes", ""))


def test_run_agent_isolates_the_machine_configuration(run_agent):
    for line in ("export CLAUDE_CONFIG_DIR=$config_dir", "export CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1",
                 "export CLAUDE_CODE_DISABLE_CLAUDE_MDS=1", "export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"):
        assert line in run_agent
    assert "--setting-sources user" not in run_agent and "--bare" not in run_agent


def test_run_agent_prompt_has_no_pr_metadata(run_agent):
    code = "\n".join(line for line in run_agent.splitlines() if not line.lstrip().startswith("#"))
    for name in ("PR_TITLE", "PR_BODY", "HEAD_REF", "BASE_REF", "GITHUB_EVENT", "GITHUB_REF",
                 "PULL_REQUEST", "COMMIT_MESSAGE", "BRANCH"):
        assert name not in code.upper(), name
    block = re.search(r'^case "\$mode" in\n  pr\)\n    prompt=.*?^esac$', run_agent, re.S | re.M)
    assert block, "prompt must be built in one case block"
    # Only these variables may be interpolated, and each one is validated first.
    used = set(re.findall(r"(?<!\\)\$\{?([A-Za-z_][A-Za-z0-9_]*)", block.group(0)))
    assert used <= {"mode", "context", "results", "stack", "out"}, used
    assert 'safe_path "$context"' in run_agent and 'safe_path "$out"' in run_agent
    # The one mention of the key is an escaped literal in the self-test prompt.
    assert "\\$ANTHROPIC_API_KEY" in block.group(0)
    assert 'prompt=$(' not in run_agent and "$(cat" not in block.group(0)


def test_run_agent_does_not_put_secrets_on_a_command_line(run_agent):
    assert "env -i" not in run_agent
    for match in re.finditer(r".*ANTHROPIC_API_KEY.*", run_agent):
        line = match.group(0)
        assert not re.search(r"\becho\b.*(?<!\\)\$\{?ANTHROPIC_API_KEY", line), line
    assert "set -x" not in run_agent and "bash -x" not in run_agent


def test_scripts_are_executable_and_parse():
    for script in (RUN_AGENT, SMOKE):
        assert os.stat(script).st_mode & stat.S_IXUSR, script
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)


def clean_env(tmp_path):
    keep = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    return {**keep, "TMPDIR": str(tmp_path)}


@pytest.mark.parametrize("script, args", [
    (RUN_AGENT, ["pr"]), (RUN_AGENT, ["backfill", "backend"]), (RUN_AGENT, ["boundaries"]),
    (SMOKE, ["boundaries"]),
])
def test_no_credential_fails_cleanly_before_writing_anything(tmp_path, script, args):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Fail the test instead of reaching a real claude if the credential check is ever lost.
    for name in ("claude", "docker"):
        tool = fake_bin / name
        tool.write_text('#!/bin/sh\necho "must not run" >&2\nexit 99\n')
        tool.chmod(0o755)
    env = clean_env(tmp_path)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    out = tmp_path / "out"
    env["AGENT_OUTPUT_DIR"] = str(out)
    result = subprocess.run(["bash", str(script), *args], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 3, result.stderr
    assert "must not run" not in result.stderr
    assert not out.exists()
    assert result.stdout == ""


@pytest.mark.parametrize("args", [[], ["nope"], ["pr", "extra"], ["backfill"], ["backfill", "rust"]])
def test_run_agent_rejects_bad_usage(args):
    result = subprocess.run(["bash", str(RUN_AGENT), *args], capture_output=True, text=True,
                            timeout=30, check=False, env={"PATH": os.environ["PATH"]})
    assert result.returncode == 2


def test_boundaries_mode_needs_the_selftest_flag(tmp_path):
    env = {**clean_env(tmp_path), "ANTHROPIC_API_KEY": "not-a-real-key"}
    result = subprocess.run(["bash", str(RUN_AGENT), "boundaries"], env=env, capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 2 and "smoke_agent" in result.stderr
    assert "not-a-real-key" not in result.stdout + result.stderr


# ---------------------------------------------------------------- backfill_agent.sh

BACKFILL = REPO / ".github/agent/backfill_agent.sh"


def test_backfill_rejects_a_bad_stack(tmp_path):
    env = {**clean_env(tmp_path), "ANTHROPIC_API_KEY": "not-a-real-key"}
    result = subprocess.run(["bash", str(BACKFILL), "../etc"], env=env, capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 2 and "usage" in result.stderr
    assert "not-a-real-key" not in result.stdout + result.stderr


def test_backfill_works_on_a_clone_and_never_publishes():
    text = BACKFILL.read_text(encoding="utf-8")
    assert "git clone --quiet --no-hardlinks" in text
    assert "read -r -s" in text and "set -x" not in text
    for forbidden in ("git push", "git commit", "git apply", "gh "):
        assert forbidden not in text
    # Both independent checks must decide the exit code, not only the agent's own.
    assert '"$patch_rc" != 0' in text and '"$tests_rc" != 0' in text


# ---------------------------------------------------------------- smoke_agent.sh

def test_smoke_never_echoes_the_key():
    text = SMOKE.read_text(encoding="utf-8")
    assert "read -r -s" in text
    assert "export ANTHROPIC_API_KEY" in text
    assert not re.search(r"(echo|printf '%s\\n'?\"?\s*)\s*\"?\$ANTHROPIC_API_KEY\"?\s*$", text, re.M)
    assert "set -x" not in text


def test_smoke_works_on_a_temporary_copy():
    tokens = shlex.split(re.search(r"^tar -C .*?-cf - \.", SMOKE.read_text(encoding="utf-8"),
                                   re.S | re.M).group(0).replace("\\\n", " "))
    for exclude in ("--exclude=.git", "--exclude=.env", "--exclude=node_modules", "--exclude=./docs"):
        assert exclude in tokens


def test_smoke_cleans_the_probe_before_the_pr_scenario():
    text = SMOKE.read_text(encoding="utf-8")
    block = text[text.index("run_boundaries()"):text.index("run_pr()")]
    assert block.index("reset --quiet --hard") < block.index("clean --quiet -f -d")


def test_smoke_judges_from_the_log_not_the_report():
    text = SMOKE.read_text(encoding="utf-8")
    block = text[text.index("run_boundaries()"):text.index("run_pr()")]
    assert "permission_denials" in block and "agent.log" in block
    assert "report.json" not in block


# ---------------------------------------------------------------- CODEOWNERS

def test_codeowners_covers_the_control_plane():
    lines = dict(line.split() for line in CODEOWNERS.read_text(encoding="utf-8").splitlines() if line.strip())
    assert lines == {".claude/": "@NikMG", ".github/": "@NikMG", "CLAUDE.md": "@NikMG", ".mcp.json": "@NikMG"}


def test_smoke_event_passes_scope_validation(tmp_path):
    block = SMOKE.read_text().split("<<'EVENT'\n", 1)[1].split("\nEVENT\n", 1)[0]
    event = tmp_path / "event.json"
    event.write_text(block)
    path = "backend/app/features/todos/validators.py"
    (tmp_path / "name-status").write_bytes(f"M\0{path}\0".encode())
    (tmp_path / "numstat").write_bytes(f"7\t0\t{path}\0".encode())
    (tmp_path / "message").write_text("add overdue helper\n")
    output = tmp_path / "scope.json"
    result = subprocess.run(
        [sys.executable, "-I", str(SMOKE.parent / "scope_changes.py"), "--event", str(event),
         "--name-status", str(tmp_path / "name-status"), "--numstat", str(tmp_path / "numstat"),
         "--head-commit-message", str(tmp_path / "message"), "--output", str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["run_agent"] is True
