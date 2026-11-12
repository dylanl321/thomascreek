#!/usr/bin/env bash
# Archive the ThomasCreek app and upload it to App Store Connect / TestFlight.
# Uses an App Store Connect API key kept outside the repo:
#   ~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8
#   ~/.appstoreconnect/config with ASC_KEY_ID=... and ASC_ISSUER_ID=...
# (or export ASC_KEY_ID / ASC_ISSUER_ID). Bump CURRENT_PROJECT_VERSION in
# project.yml before each upload. Builds land in the internal "Family" group.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f "$HOME/.appstoreconnect/config" ] && . "$HOME/.appstoreconnect/config"
: "${ASC_KEY_ID:?set ASC_KEY_ID or add it to ~/.appstoreconnect/config}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID or add it to ~/.appstoreconnect/config}"
KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
[ -f "$KEY" ] || { echo "missing $KEY" >&2; exit 1; }

OUT="${TMPDIR:-/tmp}/thomascreek-upload"
rm -rf "$OUT" && mkdir -p "$OUT"
AUTH=(-allowProvisioningUpdates
      -authenticationKeyPath "$KEY"
      -authenticationKeyID "$ASC_KEY_ID"
      -authenticationKeyIssuerID "$ASC_ISSUER_ID")

xcodegen generate
xcodebuild -project ThomasCreek.xcodeproj -scheme ThomasCreek \
  -configuration Release -destination generic/platform=iOS \
  -archivePath "$OUT/ThomasCreek.xcarchive" "${AUTH[@]}" archive

cat > "$OUT/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>app-store-connect</string>
<key>destination</key><string>upload</string>
<key>signingStyle</key><string>automatic</string>
<key>uploadSymbols</key><true/>
<key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST

xcodebuild -exportArchive -archivePath "$OUT/ThomasCreek.xcarchive" \
  -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" "${AUTH[@]}"
echo "Uploaded build $(/usr/libexec/PlistBuddy -c 'Print :ApplicationProperties:CFBundleVersion' "$OUT/ThomasCreek.xcarchive/Info.plist")"
