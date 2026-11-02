#!/usr/bin/env bash
# Builds the sandbox image and installs dependencies. Needs network, so run it
# in a job that holds no secrets. The install runs as uid 10001, with the
# workspace read-only and no environment passed in.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
stack=${1:-}
if [ "$#" -ne 1 ] || { [ "$stack" != backend ] && [ "$stack" != frontend ]; }; then
  echo "usage: prepare_sandbox.sh <backend|frontend>" >&2
  exit 2
fi

workspace=${AGENT_WORKSPACE:-$(git -C "$here" rev-parse --show-toplevel)}
workspace=$(cd "$workspace" && pwd -P)
base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}/taskly-agent
deps_root=${AGENT_DEPS_DIR:-$base/deps}
mkdir -p "$deps_root"
deps_root=$(cd "$deps_root" && pwd -P)
case "$deps_root/" in
  "$workspace"/*) echo "AGENT_DEPS_DIR must not be inside the workspace" >&2; exit 2 ;;
esac

[ -d "$workspace/$stack" ] || { echo "no $stack directory in workspace" >&2; exit 2; }
image=taskly-agent-sandbox:$stack
docker build --quiet --target "$stack" -t "$image" - < "$here/sandbox.Dockerfile" >/dev/null

# Dirs are made world-writable so uid 10001 can write regardless of the host uid.
rm -rf "${deps_root:?}/$stack" 2>/dev/null || true
mkdir -p "$deps_root/$stack"
chmod 0777 "$deps_root" "$deps_root/$stack"

run() {
  docker run --rm --pull never --cap-drop ALL --security-opt no-new-privileges \
    --read-only --user 10001:10001 --memory 2g --pids-limit 512 \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
    -v "$workspace/$stack:/workspace/$stack:ro" -v "$deps_root/$stack:/deps:rw" \
    -e HOME=/tmp -w /tmp "$image" "$@"
}

if [ "$stack" = backend ]; then
  run pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: \
    --target /deps -r /workspace/backend/requirements.txt
else
  run sh -c 'cp /workspace/frontend/package.json /workspace/frontend/package-lock.json /deps/ &&
    { [ ! -f /workspace/frontend/.npmrc ] || cp /workspace/frontend/.npmrc /deps/; } &&
    cd /deps && npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache'
fi
echo "dependencies for $stack installed in $deps_root/$stack"
