#!/usr/bin/env bash
# iOS spike 构建校验（在 worktree 根目录跑）
set -eu
bash ios/Tests/run-selftest.sh | tail -1 | grep -q 'SELFTEST OK'
cd ios && rm -rf RSiOS.xcodeproj build && xcodegen generate -q
xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -sdk iphonesimulator -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build 2>&1 | grep -q '\*\* BUILD SUCCEEDED \*\*'
APPEX=build/Build/Products/Debug-iphonesimulator/RSiOS.app/PlugIns/RSBroadcast.appex
test -d "$APPEX"
plutil -p "$APPEX/Info.plist" | grep -q RPBroadcastProcessModeSampleBuffer
plutil -p "$APPEX/Info.plist" | grep -q AppIdentifierPrefix
plutil -p build/Build/Products/Debug-iphonesimulator/RSiOS.app/Info.plist | grep -q AppIdentifierPrefix
# 扫配对码要能拉起 App：rsnode 链接必须注册在主 App 上
plutil -p build/Build/Products/Debug-iphonesimulator/RSiOS.app/Info.plist | grep -q '"rsnode"'
! grep -rq "import Shared" RSiOS RSBroadcast Shared
! grep -q "type: framework" project.yml
rm -rf build
echo BUILD-CHECK OK
