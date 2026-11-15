#!/usr/bin/env bash
# Archive Room Scanner and upload it to App Store Connect / TestFlight using an
# App Store Connect API key (same scheme as ios/scripts/upload.sh):
#   ~/.appstoreconnect/config with ASC_KEY_ID=... and ASC_ISSUER_ID=...
#   ~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8
# The key needs the Admin role: App Manager keys can't use cloud-managed distribution signing.
# Usage: BUILD=4 scanner/scripts/upload_appstore.sh
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
cd "$(dirname "$0")/.."
BUILD="${BUILD:?set BUILD}"
VERSION="${VERSION:-0.2.0}"
[ -f "$HOME/.appstoreconnect/config" ] && . "$HOME/.appstoreconnect/config"
: "${ASC_KEY_ID:?}"; : "${ASC_ISSUER_ID:?}"
KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
[ -f "$KEY" ] || { echo "missing $KEY" >&2; exit 1; }

OUT="${TMPDIR:-/tmp}/roomscanner-upload"
rm -rf "$OUT" && mkdir -p "$OUT"
AUTH=(-allowProvisioningUpdates
      -authenticationKeyPath "$KEY"
      -authenticationKeyID "$ASC_KEY_ID"
      -authenticationKeyIssuerID "$ASC_ISSUER_ID")

xcodegen generate --spec project.yml
xcodebuild -project RoomScanner.xcodeproj -scheme RoomScanner -configuration Release \
  -destination generic/platform=iOS -archivePath "$OUT/RoomScanner.xcarchive" \
  DEVELOPMENT_TEAM=Z9Q392PD67 CODE_SIGN_STYLE=Automatic \
  CURRENT_PROJECT_VERSION="$BUILD" MARKETING_VERSION="$VERSION" \
  "${AUTH[@]}" archive

cat > "$OUT/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>app-store-connect</string>
<key>destination</key><string>upload</string>
<key>teamID</key><string>Z9Q392PD67</string>
<key>signingStyle</key><string>automatic</string>
<key>uploadSymbols</key><true/>
<key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST

xcodebuild -exportArchive -archivePath "$OUT/RoomScanner.xcarchive" \
  -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" "${AUTH[@]}"
echo "Uploaded Room Scanner $VERSION ($(/usr/libexec/PlistBuddy -c 'Print :ApplicationProperties:CFBundleVersion' "$OUT/RoomScanner.xcarchive/Info.plist"))"
