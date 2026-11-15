#!/bin/bash
# Archive Room Scanner and upload it to App Store Connect for TestFlight.
# Usage:  BUILD=2 scanner/scripts/build_testflight.sh
# Needs: Xcode signed in to team Z9Q392PD67, xcodegen, and an App Store Connect app record
# for me.dlewis.roomscanner.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
SCANNER_DIR="${SCANNER_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BUILD="${BUILD:-1}"
OUT=/tmp/roomscanner-upload
rm -rf "$OUT"; mkdir -p "$OUT"
cd "$SCANNER_DIR"
xcodegen generate --spec project.yml
SIGN=(DEVELOPMENT_TEAM=Z9Q392PD67 CODE_SIGN_STYLE=Automatic CODE_SIGN_IDENTITY="Apple Development"
      PROVISIONING_PROFILE_SPECIFIER="" PROVISIONING_PROFILE=""
      CURRENT_PROJECT_VERSION="$BUILD" MARKETING_VERSION=0.1.0)
xcodebuild -project RoomScanner.xcodeproj -scheme RoomScanner -configuration Release \
  -destination generic/platform=iOS -archivePath "$OUT/RoomScanner.xcarchive" \
  -allowProvisioningUpdates "${SIGN[@]}" archive
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
  -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" -allowProvisioningUpdates
echo "UPLOAD_DONE"
