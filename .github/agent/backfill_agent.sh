#!/usr/bin/env bash
# Usage: backfill_agent.sh <backend|frontend>
#
# For a human, in their own terminal, with their own key: this spends real money.
# The agent works on a fresh clone of HEAD, so it never sees .env, docs/ or uncommitted
# files. The real tree is not touched: the result is a patch that a human reviews and
# applies. The patch is then checked by check_patch.py, the stack is re-run, and
# check_backfill.py compares the resulting coverage with the agent's decisions. Those
# results come from here rather than from what the agent says.
#
# Exit: 0 patch ready, 1 agent, patch check, tests or gap check failed, 2 usage or
# setup error, 3 no key, 4 the key appeared in the outputs.
set -euo pipefail

die() { echo "backfill_agent.sh: $*" >&2; exit 2; }

here=$(cd "$(dirname "$0")" && pwd -P)
[ "$#" -eq 1 ] || die "usage: backfill_agent.sh <backend|frontend>"
stack=$1
case "$stack" in
  backend|frontend) ;;
  *) die "usage: backfill_agent.sh <backend|frontend>" ;;
esac
command -v claude >/dev/null 2>&1 || die "claude not found"
command -v docker >/dev/null 2>&1 || die "docker not found"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ ! -t 0 ]; then
    echo "backfill_agent.sh: ANTHROPIC_API_KEY is unset and there is no terminal to ask on" >&2
    exit 3
  fi
  read -r -s -p "Anthropic API key (input hidden): " ANTHROPIC_API_KEY
  echo
  [ -n "$ANTHROPIC_API_KEY" ] || { echo "backfill_agent.sh: no key given" >&2; exit 3; }
fi
# Only the agent step gets the key, as in CI: the sandbox setup, the guards and the
# re-run of the tests have no use for it.
api_key=$ANTHROPIC_API_KEY
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN

repo=$(git -C "$here" rev-parse --show-toplevel)
tmp=$(mktemp -d)
tmp=$(cd "$tmp" && pwd -P)
[[ "$tmp" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "temp dir path has unsupported characters: $tmp"
copy=$tmp/repo
out=$tmp/out
echo "Everything is written under: $tmp"
git clone --quiet --no-hardlinks "$repo" "$copy"
agent_dir=$copy/.github/agent

export AGENT_DEPS_DIR=${AGENT_DEPS_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/taskly-agent/deps}
unset AGENT_WORKSPACE
if [ ! -d "$AGENT_DEPS_DIR/$stack" ] || ! docker image inspect "taskly-agent-sandbox:$stack" >/dev/null 2>&1; then
  echo "Preparing the $stack sandbox (needs network)..."
  "$agent_dir/prepare_sandbox.sh" "$stack"
fi

system_python=$(command -v python3.12 || command -v python3) || die "python3 not found"
"$system_python" -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes \
  -r "$agent_dir/requirements.txt"
export AGENT_PYTHON=$tmp/venv/bin/python
export PATH=$tmp/venv/bin:$PATH

agent_rc=0 patch_rc=0 tests_rc=0 gaps_rc=0
set +e
echo "Running the agent. This usually takes several minutes..."
ANTHROPIC_API_KEY=$api_key AGENT_OUTPUT_DIR=$out "$agent_dir/run_agent.sh" backfill "$stack"
agent_rc=$?
"$AGENT_PYTHON" -I "$agent_dir/check_patch.py" "$out/agent.patch" --output "$out/patch_check.json"
patch_rc=$?
AGENT_OUTPUT_DIR=$tmp/verify "$agent_dir/run_tests.sh" "$stack" --coverage >"$tmp/verify.log" 2>&1
tests_rc=$?
set -e

tail -n 25 "$tmp/verify.log"
echo
set +e
"$AGENT_PYTHON" "$agent_dir/check_backfill.py" "$stack" \
  --results "$tmp/verify/$stack/results" --report "$out/report.json"
gaps_rc=$?
set -e
echo
# run_agent.sh already scanned its own outputs; this covers the independent re-run.
if grep -rlF -f <(printf '%s\n' "$api_key") "$tmp/verify" "$tmp/verify.log" >/dev/null 2>&1; then
  echo "backfill_agent.sh: the key appeared in the test outputs" >&2
  exit 4
fi
echo "Patch:  $out/agent.patch"
echo "Report: $out/report.json"
echo "Tests:  $tmp/verify.log"
echo "agent exit: $agent_rc, check_patch exit: $patch_rc, run_tests exit: $tests_rc, check_backfill exit: $gaps_rc"
if [ "$agent_rc" != 0 ] || [ "$patch_rc" != 0 ] || [ "$tests_rc" != 0 ] || [ "$gaps_rc" != 0 ]; then
  echo "backfill: FAIL"
  exit 1
fi
echo "backfill: PASS"
