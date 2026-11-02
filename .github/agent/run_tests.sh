#!/usr/bin/env bash
# Usage: run_tests.sh <backend|frontend> [--coverage] [--] [test paths...]
# Every argument is untrusted. Exit: 0 pass, 1 tests failed, 124 timeout,
# 2 bad usage or sandbox error.
set -euo pipefail

die() { echo "run_tests.sh: $*" >&2; exit 2; }

here=$(cd "$(dirname "$0")" && pwd -P)
[ "$#" -ge 1 ] || die "usage: run_tests.sh <backend|frontend> [--coverage] [--] [test paths...]"
stack=$1
shift
case "$stack" in
  backend)  path_re='^backend/tests/[A-Za-z0-9_/.-]+\.py$' ;;
  frontend) path_re='^frontend/src/[A-Za-z0-9_/.-]+\.test\.(ts|tsx)$' ;;
  *) die "unknown stack" ;;
esac

coverage=0
paths=()
while [ "$#" -gt 0 ]; do
  arg=$1
  shift
  if [ "$arg" = "--coverage" ]; then
    coverage=1
  elif [ "$arg" = "--" ]; then
    break
  else
    case "$arg" in
      -*) die "unknown flag" ;;
    esac
    set -- "$arg" "$@"
    break
  fi
done
for arg in "$@"; do
  case "$arg" in
    *..*) die "invalid test path" ;;
  esac
  [[ "$arg" =~ $path_re ]] || die "invalid test path"
  paths+=("${arg#"$stack"/}")
done

command -v docker >/dev/null 2>&1 || die "docker not found"

workspace=${AGENT_WORKSPACE:-$(git -C "$here" rev-parse --show-toplevel)}
workspace=$(cd "$workspace" && pwd -P)
base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}/taskly-agent
deps_root=${AGENT_DEPS_DIR:-$base/deps}
out_root=${AGENT_OUTPUT_DIR:-$base/out}
mkdir -p "$out_root"
out_root=$(cd "$out_root" && pwd -P)
[ -d "$deps_root/$stack" ] || die "no dependencies for $stack; run prepare_sandbox.sh first"
deps_root=$(cd "$deps_root" && pwd -P)
for d in "$deps_root" "$out_root"; do
  case "$d/" in
    "$workspace"/*) die "deps and output dirs must not be inside the workspace" ;;
  esac
done

image=taskly-agent-sandbox:$stack
docker image inspect "$image" >/dev/null 2>&1 || die "image $image missing; run prepare_sandbox.sh first"

[ -d "$workspace/$stack" ] || die "no $stack directory in workspace"

# $out is host-owned and never mounted. Only $out/results is shared with the
# container, so test code cannot plant anything next to test.log.
out=$out_root/$stack
results=$out/results
mkdir -p "$results"
chmod 0755 "$out"
chmod 0777 "$results"

# Fixed script; user-controlled values only ever arrive as positional args.
if [ "$stack" = backend ]; then
  read -r -d '' inner <<'INNER' || true
mkdir -p /tmp/work && cd /tmp/work || exit 125
tar -C /workspace --exclude=.venv --exclude=venv --exclude=__pycache__ --exclude=coverage \
  --exclude=.pytest_cache -cf - backend | tar -xf - || exit 125
cd backend || exit 125
cov=$1
shift
set -- --junitxml=/tmp/work/junit.xml "$@"
[ "$cov" = 1 ] && set -- --cov=app --cov-report=term-missing --cov-report=xml --cov-report=json "$@"
export PYTHONPATH=/deps PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=/tmp/work/.coverage
timeout 300 python -m pytest "$@"
rc=$?
[ -f /tmp/work/junit.xml ] && cp /tmp/work/junit.xml /out/junit.xml
[ -d coverage ] && cp -R coverage /out/coverage
exit $rc
INNER
else
  read -r -d '' inner <<'INNER' || true
mkdir -p /tmp/work && cd /tmp/work || exit 125
tar -C /workspace --exclude=node_modules --exclude=coverage -cf - frontend | tar -xf - || exit 125
cd frontend || exit 125
# node_modules is a real tmpfs dir of symlinks so vite/vitest caches stay writable.
mkdir node_modules
for e in /deps/node_modules/* /deps/node_modules/.bin; do
  ln -s "$e" "node_modules/$(basename "$e")"
done
cov=$1
shift
set -- --reporter=default --reporter=junit --outputFile.junit=/tmp/work/junit.xml "$@"
[ "$cov" = 1 ] && set -- --coverage "$@"
export XDG_CACHE_HOME=/tmp/cache
timeout 300 node_modules/.bin/vitest run "$@"
rc=$?
# busybox timeout exits 143 (SIGTERM) instead of 124
[ "$rc" = 143 ] && rc=124
[ -f /tmp/work/junit.xml ] && cp /tmp/work/junit.xml /out/junit.xml
[ -d coverage ] && cp -R coverage /out/coverage
exit $rc
INNER
fi

max_results_kb=262144

# Same hardening for every container; $workspace/$stack is the only project code mounted.
harden=(--rm --pull never --network none --cap-drop ALL --security-opt no-new-privileges
  --read-only --user 10001:10001 --memory 2g --memory-swap 2g --pids-limit 512
  --tmpfs /tmp:rw,nosuid,nodev,size=1g,mode=1777 -e HOME=/tmp -w /tmp)

# Trusted fixed scripts that run before and after the test code touches results/.
# They run as uid 10001 because the host may not be able to delete files it owns.
wipe='find /out -mindepth 1 -delete'
sanitize='for e in /out/* /out/.[!.]*; do
  { [ -e "$e" ] || [ -L "$e" ]; } && chmod -R u+rwx "$e" 2>/dev/null
done
find /out -mindepth 1 ! -type f ! -type d -delete || exit 125
find /out -mindepth 1 -type d -exec chmod 0755 {} + || exit 125
find /out -mindepth 1 -type d -exec chmod u-s,g-s,a-t {} + || exit 125
find /out -mindepth 1 -type f -exec chmod 0644 {} + || exit 125
kb=$(du -sk /out | cut -f1) || exit 125
if [ "$kb" -gt "$1" ]; then
  echo "run_tests.sh: warning: results exceeded $1 KB, wiped" >&2
  find /out -mindepth 1 -delete || exit 125
fi
exit 0'

docker run "${harden[@]}" -v "$results:/out:rw" "$image" sh -c "$wipe" \
  || die "could not wipe results"

log=$(mktemp)
trap 'rm -f "$log"' EXIT

set +e
docker run "${harden[@]}" \
  -v "$workspace/$stack:/workspace/$stack:ro" -v "$deps_root/$stack:/deps:ro" -v "$results:/out:rw" \
  -e CI=true \
  "$image" sh -c "$inner" sh "$coverage" ${paths[@]+"${paths[@]}"} >"$log" 2>&1
rc=$?
set -e

rm -f "$out/test.log"
cat "$log" > "$out/test.log"
tail -n 200 "$log"

if ! docker run "${harden[@]}" -v "$results:/out:rw" "$image" sh -c "$sanitize" sh "$max_results_kb"; then
  rm -rf "$results" 2>/dev/null
  die "could not sanitize results"
fi

case "$rc" in
  0) exit 0 ;;
  124) exit 124 ;;
  125|126|127) exit 2 ;;
  *) exit 1 ;;
esac
