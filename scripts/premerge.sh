#!/bin/bash
# Pre-merge check for the hub and the iPhone app, run on the Mac in place of GitHub Actions.
#
#   scripts/premerge.sh            # check this checkout's HEAD commit (not uncommitted changes), merged with origin/main
#   scripts/premerge.sh 212        # check pull request #212, merged with origin/main
#   scripts/premerge.sh my-branch  # check a branch or commit, merged with origin/main
#
# What it runs, in a private worktree so your checkout, its Xcode project and any open simulator are not touched:
#   1. the hub test suite (server/tests, pytest)
#   2. the TVPhone Swift package tests (ios/TVPhone)
#   3. the iPhone app build for the simulator (xcodegen project.yml, generic simulator destination, no signing).
#      No simulator is booted.
#
# It ends with one line, PREMERGE PASS or PREMERGE FAIL. Paste that line into the pull request; a pull request
# without a PASS line for its head commit is not merged (AGENTS.md). Exit code 0 only on PASS.
#
# Only one pre-merge check runs on the Mac at a time, for this repository and the TV repository together: a
# second one waits for the first (lock in ~/.thomascreek/premerge.lock). Everything runs at low priority (nice).
# No keys, tokens, addresses or signing are needed. Needs: git, Python 3.12+, Xcode, xcodegen; gh only for a
# pull request number.
#
# Environment: PREMERGE_WAIT minutes to wait for the lock (default 60), PREMERGE_SKIP_IOS=1 to skip steps 2 and 3
# (the line then says so, and it does not count as a full pass for an iPhone change).
set -uo pipefail

REPO=thomascreek
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CACHE="$HOME/Library/Caches/thomascreek-premerge/$REPO"
WT="$CACHE/worktree"
LOG="$CACHE/last.log"
LOCK="$HOME/.thomascreek/premerge.lock"
START=$(date +%s)
mkdir -p "$CACHE" "$(dirname "$LOCK")"
: > "$LOG"

TARGET="${1:-HEAD}"
SUMMARY=()
HEAD_SHA="?"
MAIN_SHA="?"

elapsed() { local s=$(( $(date +%s) - START )); printf '%dm%02ds' $((s / 60)) $((s % 60)); }
stamp() { date -u +%Y-%m-%dT%H:%MZ; }

fail() {
  echo
  echo "---- last lines of the log ($LOG) ----"
  tail -n 40 "$LOG"
  echo "----"
  echo "PREMERGE FAIL $REPO $HEAD_SHA (+ main $MAIN_SHA) · $1 · $(stamp) · $(elapsed)"
  exit 1
}

# Run a step quietly into the log; name it on screen.
step() {
  local name=$1; shift
  printf '== %s ... ' "$name"
  echo "==== $name: $*" >> "$LOG"
  local t=$(date +%s)
  if nice -n 10 "$@" >> "$LOG" 2>&1; then
    echo "ok ($(( $(date +%s) - t ))s)"
    return 0
  fi
  echo "FAILED"
  return 1
}

# ---- one check at a time on this Mac
release_lock() { [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK"; }
waited=0
limit=$(( ${PREMERGE_WAIT:-60} * 60 ))
until mkdir "$LOCK" 2>/dev/null; do
  holder=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    echo "removing a stale lock from process $holder"
    rm -rf "$LOCK"
    continue
  fi
  [ $waited -ge $limit ] && { echo "PREMERGE FAIL $REPO · another pre-merge check held the Mac for ${PREMERGE_WAIT:-60} minutes ($(cat "$LOCK/what" 2>/dev/null)) · $(stamp)"; exit 1; }
  [ $(( waited % 60 )) -eq 0 ] && echo "waiting for another pre-merge check to finish: $(cat "$LOCK/what" 2>/dev/null)"
  sleep 10; waited=$(( waited + 10 ))
done
echo $$ > "$LOCK/pid"
echo "$REPO $TARGET, started $(stamp)" > "$LOCK/what"
trap release_lock EXIT INT TERM

# ---- source: the target merged with origin/main, in a private worktree
cd "$ROOT"
echo "==== source" >> "$LOG"
git fetch -q origin main >> "$LOG" 2>&1 || fail "git fetch origin main failed"
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  git fetch -q origin "pull/$TARGET/head" >> "$LOG" 2>&1 || fail "pull request #$TARGET not found on origin"
  REF=$(git rev-parse FETCH_HEAD)
  LABEL="#$TARGET"
else
  REF=$(git rev-parse --verify -q "$TARGET^{commit}" || git rev-parse --verify -q "origin/$TARGET^{commit}") \
    || fail "no commit or branch named $TARGET"
  LABEL="$TARGET"
fi
HEAD_SHA=$(git rev-parse --short "$REF")
MAIN_SHA=$(git rev-parse --short origin/main)

if [ ! -e "$WT/.git" ]; then
  git worktree prune >> "$LOG" 2>&1
  rm -rf "$WT"
  git worktree add --detach "$WT" "$REF" >> "$LOG" 2>&1 || fail "could not create the check worktree"
fi
(
  cd "$WT" || exit 1
  git merge --abort >/dev/null 2>&1
  git checkout -q --detach --force "$REF" &&
  git clean -q -fd &&
  git -c user.name=premerge -c user.email=premerge@localhost merge -q --no-edit --no-ff origin/main
) >> "$LOG" 2>&1 || fail "$LABEL does not merge cleanly with main; update the branch"
echo "checking $LABEL ($HEAD_SHA) merged with main ($MAIN_SHA)"

# ---- 1. hub tests
PY=""
for p in python3.13 python3.12 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    PY=$p; break
  fi
done
[ -n "$PY" ] || fail "Python 3.12 or newer not found (brew install python@3.12)"
VENV="$CACHE/venv"
[ -x "$VENV/bin/python" ] || step "hub: make venv" "$PY" -m venv "$VENV" || fail "could not make the hub venv"
step "hub: install" "$VENV/bin/pip" install -q -e "$WT/server[test]" || fail "hub: pip install failed"
printf '== hub: tests ... '
echo "==== hub tests" >> "$LOG"
t=$(date +%s)
( cd "$WT/server" && nice -n 10 "$VENV/bin/python" -m pytest -q -p no:cacheprovider ) > "$CACHE/pytest.log" 2>&1
rc=$?
cat "$CACHE/pytest.log" >> "$LOG"
RESULT=$(grep -E '[0-9]+ (passed|failed|errors?)' "$CACHE/pytest.log" | tail -1 | sed 's/=//g; s/^ *//; s/ in [0-9.]*s.*//; s/ *$//')
if [ $rc -ne 0 ]; then
  echo "FAILED"
  fail "hub tests: ${RESULT:-pytest exited $rc}"
fi
echo "ok ($(( $(date +%s) - t ))s)"
SUMMARY+=("hub ${RESULT:-passed}")

# ---- 2 and 3. iPhone
if [ "${PREMERGE_SKIP_IOS:-0}" = "1" ]; then
  SUMMARY+=("iPhone SKIPPED")
else
  command -v xcodebuild >/dev/null || fail "Xcode not found"
  command -v xcodegen >/dev/null || fail "xcodegen not found (brew install xcodegen)"
  ( cd "$WT/ios/TVPhone" && step "iPhone: TVPhone tests" swift test --scratch-path "$CACHE/swiftpm" ) \
    || fail "iPhone: TVPhone package tests failed"
  SUMMARY+=("TVPhone tests ok")
  ( cd "$WT/ios" && step "iPhone: xcodegen" xcodegen generate --spec project.yml --quiet ) \
    || fail "iPhone: xcodegen failed"
  ( cd "$WT/ios" && step "iPhone: simulator build" xcodebuild -quiet \
      -project ThomasCreek.xcodeproj -scheme ThomasCreek -configuration Debug \
      -destination 'generic/platform=iOS Simulator' -derivedDataPath "$CACHE/DerivedData" \
      CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build ) \
    || fail "iPhone: simulator build failed (grep 'error:' $LOG)"
  SUMMARY+=("iPhone simulator build ok")
fi

LINE="PREMERGE PASS $REPO $HEAD_SHA (+ main $MAIN_SHA)"
for s in "${SUMMARY[@]}"; do LINE="$LINE · $s"; done
echo
echo "$LINE · $(stamp) · $(elapsed)"
