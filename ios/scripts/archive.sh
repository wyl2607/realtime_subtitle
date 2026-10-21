#!/usr/bin/env bash
# TestFlight 打包：Release archive → 导出 app-store-connect 用的 ipa（可选直接上传）。
# 用法：bash ios/scripts/archive.sh <TEAM_ID> [--upload]
#   Team ID 不入库，由命令行传入。--upload 用本机 Xcode 已登录的账号直接传到 App Store Connect
#   （需先在 App Store Connect 建好同 bundle id 的 App 记录）。
# build number = UTC 时间戳 yyMMddHHmm，每次打包自动递增，不用手改 project.yml。
set -euo pipefail
TEAM=${1:?用法: archive.sh <TEAM_ID> [--upload]}
DEST=export; [ "${2:-}" = "--upload" ] && DEST=upload
cd "$(dirname "$0")/.."
BUILD=$(date -u +%y%m%d%H%M)
OUT=build/release; rm -rf "$OUT"; mkdir -p "$OUT"
xcodegen generate -q
xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$OUT/RSiOS.xcarchive" \
  DEVELOPMENT_TEAM="$TEAM" CURRENT_PROJECT_VERSION="$BUILD" \
  -allowProvisioningUpdates archive | tail -3
cat > "$OUT/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>$DEST</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>automatic</string>
  <key>uploadSymbols</key><true/>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST
xcodebuild -exportArchive -archivePath "$OUT/RSiOS.xcarchive" -exportPath "$OUT/export" \
  -exportOptionsPlist "$OUT/ExportOptions.plist" -allowProvisioningUpdates | tail -3
echo "build=$BUILD dest=$DEST out=$OUT/export"
