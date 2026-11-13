#!/usr/bin/env bash
# Tell the hub a new build is out, so phones show "New version ready" without anyone installing it first.
#   announce-build.sh <build> [version]
# upload.sh runs this in the background after each upload. It:
#   1. waits until App Store Connect says TestFlight has finished processing the build (up to 45 min),
#      using the same API key upload.sh uses (~/.appstoreconnect); the key never leaves the Mac,
#   2. posts the build to the hub (POST /v1/app/release), signed with the Mac's test key
#      (~/.thomascreek/test-key, made on the hub web page under Test simulators).
# THOMASCREEK_HUB overrides the hub address (default https://10.0.0.10:8443).
# Log: ~/Library/Logs/thomascreek-announce.log
set -uo pipefail

BUILD="${1:?usage: announce-build.sh <build> [version]}"
VERSION="${2:-}"
HUB="${THOMASCREEK_HUB:-https://10.0.0.10:8443}"
APPLE_ID="${THOMASCREEK_APPLE_ID:-6833735579}"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') build $BUILD: $*"; }

[ -f "$HOME/.appstoreconnect/config" ] && . "$HOME/.appstoreconnect/config"
KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID:-none}.p8"
TEST_KEY="${THOMASCREEK_TEST_KEY:-$(cat "$HOME/.thomascreek/test-key" 2>/dev/null)}"
TEST_KEY="$(printf '%s' "$TEST_KEY" | tr -d '[:space:]')"
if [ -z "$TEST_KEY" ]; then
  log "no ~/.thomascreek/test-key, so the hub can't be told (make one on the hub web page, Test simulators)"
  exit 1
fi

# A 20-minute App Store Connect token (ES256), signed with openssl; python3 only reshapes the signature.
asc_token() {
  python3 - "$KEY" "$ASC_KEY_ID" "$ASC_ISSUER_ID" <<'PY'
import base64, json, subprocess, sys, time
key, kid, iss = sys.argv[1:4]
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
now = int(time.time())
head = b64(json.dumps({"alg": "ES256", "kid": kid, "typ": "JWT"}).encode())
body = b64(json.dumps({"iss": iss, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"}).encode())
der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key], input=f"{head}.{body}".encode(),
                     capture_output=True, check=True).stdout
# DER SEQUENCE { INTEGER r, INTEGER s } -> 64 raw bytes
i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
def integer(i):
    n = der[i + 1]
    return der[i + 2:i + 2 + n], i + 2 + n
r, i = integer(i)
s, _ = integer(i)
sig = r.lstrip(b"\0").rjust(32, b"\0") + s.lstrip(b"\0").rjust(32, b"\0")
print(f"{head}.{body}.{b64(sig)}")
PY
}

# VALID, PROCESSING, FAILED, INVALID, or empty when App Store Connect doesn't list it yet.
build_state() {
  local tok
  tok="$(asc_token)" || return 1
  curl -sf -m 30 -H "Authorization: Bearer $tok" \
    "https://api.appstoreconnect.apple.com/v1/builds?filter%5Bapp%5D=$APPLE_ID&filter%5Bversion%5D=$BUILD&fields%5Bbuilds%5D=processingState&limit=1" |
    python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d[0]["attributes"]["processingState"] if d else "")'
}

if [ -n "${ASC_KEY_ID:-}" ] && [ -n "${ASC_ISSUER_ID:-}" ] && [ -f "$KEY" ]; then
  state=""
  for _ in $(seq 1 90); do
    state="$(build_state 2>/dev/null || true)"
    [ "$state" = VALID ] && break
    if [ "$state" = FAILED ] || [ "$state" = INVALID ]; then
      log "TestFlight says $state, not telling the hub"
      exit 1
    fi
    sleep 30
  done
  if [ "$state" = VALID ]; then
    log "TestFlight finished processing"
  else
    log "still not processed after 45 min (last: ${state:-not listed}); telling the hub anyway"
  fi
else
  log "no App Store Connect key in ~/.appstoreconnect; waiting 20 min for TestFlight to process instead"
  sleep 1200
fi

body="{\"build\": $BUILD$([ -n "$VERSION" ] && printf ', "version": "%s"' "$VERSION")}"
for attempt in 1 2 3 4 5; do
  if out="$(curl -sk -m 20 -X POST "$HUB/v1/app/release" -H "X-Test-Key: $TEST_KEY" \
              -H 'Content-Type: application/json' -d "$body" -w ' HTTP %{http_code}')"; then
    case "$out" in
      *"HTTP 200") log "hub told: $out"; exit 0 ;;
      *"HTTP 401") log "hub refused the test key: $out"; exit 1 ;;
    esac
  fi
  log "hub not reachable (attempt $attempt): ${out:-no answer}"
  sleep $((attempt * 30))
done
exit 1
