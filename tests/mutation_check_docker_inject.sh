#!/usr/bin/env bash
# Mutation check for the F7/R2 docker-install shell-injection fix.
#
#   bash tests/mutation_check_docker_inject.sh
#
# WHAT THIS IS FOR
# ----------------
# tests/test_docker_install.mjs claims two independent defences against a
# `usermod` shell injection. A test file that only ever runs green proves
# nothing about itself, so this script re-introduces each defect in a *scratch
# copy* of the module and requires the suite to notice.
#
#   control    — unmutated scratch copy: the suite must be GREEN, otherwise
#                "it went red below" would mean nothing.
#   shell      — the R2 regression itself: `usermod` is interpolated into a
#                `bash -c` string again. The "no username in a shell string"
#                half of the suite must go red.
#   novalidate — defence 1 removed: isValidUsername() accepts anything. Only
#                the validation half of the suite may go red, because the
#                argument-vector form is still intact.
#
# That the two mutations break *disjoint* sets of tests is the point: it shows
# each defence is pinned by its own assertions rather than by one of them
# happening to catch the other's regression.
#
# The repo is never modified: everything happens in a mktemp directory that is
# removed on exit. Every patch anchor must match exactly once, so an edit to
# docker_install.js that invalidates a fixture fails loudly here instead of
# silently skipping the negative pass.
#
# Exit: 0 all expectations met · 1 an expectation was not met · 77 skipped
# (no node or no python3 — reported, never silently passed).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NODE="$(command -v node || true)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$NODE" ]; then
  echo "SKIP: node not found — cannot run tests/test_docker_install.mjs" >&2
  exit 77
fi
if [ -z "$PY" ]; then
  echo "SKIP: python3 not found — cannot apply the mutation patches" >&2
  exit 77
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SUITE="$TMP/repo/tests/test_docker_install.mjs"
MODULE="$TMP/repo/electron/docker_install.js"
MAIN="$TMP/repo/electron/main.js"
mkdir -p "$TMP/repo/tests" "$TMP/repo/electron"
cp "$ROOT/tests/test_docker_install.mjs" "$SUITE"
cp "$ROOT/electron/docker_install.js" "$MODULE"
cp "$ROOT/electron/main.js" "$MAIN"

PRISTINE="$TMP/docker_install.js.pristine"
cp "$MODULE" "$PRISTINE"

rc_total=0

# Restore the shipped module before every case, so cases cannot contaminate
# each other.
reset_module() { cp "$PRISTINE" "$MODULE"; }

# patch <from-file> <to-file> — apply to $MODULE, refusing a stale anchor.
patch() {
  "$PY" - "$MODULE" "$1" "$2" <<'PYEOF'
import sys
path, frm_path, to_path = sys.argv[1:4]
src = open(path).read()
old = open(frm_path).read()
new = open(to_path).read()
n = src.count(old)
if n != 1:
    sys.exit('mutation anchor matched %d times in %s (need exactly 1) — '
             'the fixture in tests/mutation_check_docker_inject.sh is stale'
             % (n, path))
open(path, 'w').write(src.replace(old, new))
PYEOF
}

# run_suite — run the suite in the scratch copy, print it, return its exit code.
run_suite() {
  ( cd "$TMP/repo" && "$NODE" tests/test_docker_install.mjs 2>&1 ) || true
}

fail() { echo "FAIL: $*" >&2; rc_total=1; }
pass() { echo "PASS: $*"; }

# expect_green <label> — the suite must be entirely green.
expect_green() {
  local label="$1" out rc
  out="$( ( cd "$TMP/repo" && "$NODE" tests/test_docker_install.mjs 2>&1 ) )" && rc=0 || rc=$?
  printf '%s\n' "$out" | sed 's/^/    /'
  if [ "$rc" -ne 0 ]; then
    fail "$label: the unmutated scratch copy is not green (rc=$rc), so a red result below would prove nothing"
  else
    pass "$label: unmutated scratch copy is green"
  fi
}

# expect_red <label> <test name that must fail>...
expect_red() {
  local label="$1"; shift
  local out rc failing
  out="$( ( cd "$TMP/repo" && "$NODE" tests/test_docker_install.mjs 2>&1 ) )" && rc=0 || rc=$?
  printf '%s\n' "$out" | sed 's/^/    /'
  if [ "$rc" -eq 0 ]; then
    fail "$label: the suite is VACUOUS — it passes against the re-broken module"
  else
    pass "$label: the re-broken module makes the suite red (rc=$rc)"
  fi
  # Reported either way: a vacuous suite fails every one of these.
  for want in "$@"; do
    failing="$(printf '%s\n' "$out" | grep -c "^FAIL ${want}" || true)"
    if [ "$failing" -lt 1 ]; then
      fail "$label: expected test to fail but it did not: ${want}"
    fi
  done
}

# ── control ──────────────────────────────────────────────────────────────────
reset_module
expect_green "control"

# ── mutation 1: the R2 shell injection, restored ─────────────────────────────
# `usermod` goes back inside a `bash -c` string with the username interpolated,
# which is arbitrary root code execution via process.env.USER.
reset_module
cat > "$TMP/from.shell" <<'EOF'
  const addToDockerGroup = ['pkexec', ['usermod', '-aG', 'docker', user]];
EOF
cat > "$TMP/to.shell" <<'EOF'
  const addToDockerGroup = ['pkexec', ['bash', '-c', `usermod -aG docker ${user}`]];
EOF
patch "$TMP/from.shell" "$TMP/to.shell"
expect_red "mutation: usermod inside a shell script string" \
  "no recipe puts usermod inside a shell script string" \
  "a username can never appear inside a shell script string" \
  "usermod runs as its own argument vector in every recipe" \
  "the username only ever appears as a standalone argv element"

# ── mutation 2: the validation, removed ──────────────────────────────────────
# Defence 1 gone. The argument vector is untouched, so only the tests that
# assert the refusal may fail — if the shell-string tests fail here too, the two
# mutations are not actually independent and one of them is doing the other's
# job.
reset_module
cat > "$TMP/from.validate" <<'EOF'
function isValidUsername(name) {
  return typeof name === 'string' && USERNAME_RE.test(name);
}
EOF
cat > "$TMP/to.validate" <<'EOF'
function isValidUsername(name) {
  return true;
}
EOF
patch "$TMP/from.validate" "$TMP/to.validate"
out="$( ( cd "$TMP/repo" && "$NODE" tests/test_docker_install.mjs 2>&1 ) )" && rc=0 || rc=$?
printf '%s\n' "$out" | sed 's/^/    /'
if [ "$rc" -eq 0 ]; then
  fail "mutation: validation removed — the suite is VACUOUS, it passes against a module that accepts any user name"
else
  pass "mutation: validation removed — the suite goes red (rc=$rc)"
fi
for want in \
  "a metacharacter username is refused before any step is built" \
  "buildInstallSteps refuses an invalid user name (defence in depth)" \
  "isValidUsername rejects shell metacharacters and injection payloads" \
  "readUsername THROWS on a malicious or missing value"
do
  if ! printf '%s\n' "$out" | grep -q "^FAIL ${want}"; then
    fail "mutation: validation removed — expected test to fail but it did not: ${want}"
  fi
done
# Independence: the shell-string property is still intact under this mutation, so
# none of its tests may fail. A failure here would mean the two defences are
# pinned by the same assertion and one could regress unnoticed.
for must_stay_green in \
  "no recipe puts usermod inside a shell script string" \
  "a username can never appear inside a shell script string" \
  "usermod runs as its own argument vector in every recipe"
do
  if printf '%s\n' "$out" | grep -q "^FAIL ${must_stay_green}"; then
    fail "mutation: validation removed — the shell-string defence is not independently pinned: ${must_stay_green}"
  fi
done

reset_module
if [ "$rc_total" -eq 0 ]; then
  echo
  echo "PASS: the docker-install tests are non-vacuous — the control is green and both defences are pinned."
else
  echo
  echo "FAIL: see the failures above." >&2
fi
exit "$rc_total"
