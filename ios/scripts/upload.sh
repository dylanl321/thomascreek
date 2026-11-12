#!/usr/bin/env bash
# Archive the ThomasCreek app and upload it to App Store Connect / TestFlight.
# Uses an App Store Connect API key kept outside the repo:
#   ~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8
#   ~/.appstoreconnect/config with ASC_KEY_ID=... and ASC_ISSUER_ID=...
# (or export ASC_KEY_ID / ASC_ISSUER_ID). Bump CURRENT_PROJECT_VERSION in
# project.yml before each upload. Builds land in the internal "Family" group.
# SPEC picks the XcodeGen spec:
#   project-vpn-appstore.yml (default): the app with the WireGuard tunnel, signed manually with the
#     "Thomas Creek <app|widgets|tunnel|notifications> App Store" profiles and the iPhone Distribution identity. Needs Go.
#   project-watch.yml: the above plus the Apple Watch app (needs the "Thomas Creek watch App Store" profile).
#   project.yml: the app without the tunnel, automatic signing.
set -euo pipefail

cd "$(dirname "$0")/.."
SPEC="${SPEC:-project-vpn-appstore.yml}"
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

xcodegen generate --spec "$SPEC"
xcodebuild -project ThomasCreek.xcodeproj -scheme ThomasCreek \
  -configuration Release -destination generic/platform=iOS \
  -archivePath "$OUT/ThomasCreek.xcarchive" "${AUTH[@]}" archive

if [ "$SPEC" = project-vpn-appstore.yml ] || [ "$SPEC" = project-watch.yml ]; then
  WATCH=""
  [ "$SPEC" = project-watch.yml ] && WATCH="<key>me.dlewis.thomascreek.watchkitapp</key><string>Thomas Creek watch App Store</string>"
  SIGNING="<key>signingStyle</key><string>manual</string>
<key>signingCertificate</key><string>iPhone Distribution</string>
<key>provisioningProfiles</key><dict>
$WATCH
<key>me.dlewis.thomascreek</key><string>Thomas Creek app App Store</string>
<key>me.dlewis.thomascreek.widgets</key><string>Thomas Creek widgets App Store</string>
<key>me.dlewis.thomascreek.tunnel</key><string>Thomas Creek tunnel App Store</string>
<key>me.dlewis.thomascreek.notifications</key><string>Thomas Creek notifications App Store</string>
</dict>"
else
  SIGNING="<key>signingStyle</key><string>automatic</string>"
fi
cat > "$OUT/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>app-store-connect</string>
<key>destination</key><string>upload</string>
$SIGNING
<key>uploadSymbols</key><true/>
<key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST

xcodebuild -exportArchive -archivePath "$OUT/ThomasCreek.xcarchive" \
  -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" "${AUTH[@]}"
echo "Uploaded build $(/usr/libexec/PlistBuddy -c 'Print :ApplicationProperties:CFBundleVersion' "$OUT/ThomasCreek.xcarchive/Info.plist")"
