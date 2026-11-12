#!/bin/sh
# Builds wireguard-go (libwg-go.a) with the Makefile inside the vendored WireGuardKit package.
set -e
dir="$(cd "$(dirname "$0")/.." && pwd)/Vendor/WireGuardKit/Sources/WireGuardKitGo"
[ -d "$dir" ] || { echo "error: WireGuardKitGo not found at $dir" >&2; exit 1; }
cd "$dir"
exec /usr/bin/make "$@"
