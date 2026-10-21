#!/bin/bash
set -euo pipefail
mkdir -p .tmp
trap 'rm -rf .tmp' EXIT
mkdir -p .tmp/module-cache
CLANG_MODULE_CACHE_PATH="$PWD/.tmp/module-cache" \
SWIFT_MODULE_CACHE_PATH="$PWD/.tmp/module-cache" \
xcrun swiftc -O ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift ios/Shared/PairingLink.swift ios/Shared/TailnetPin.swift ios/RSBroadcast/ReconnectPolicy.swift ios/Tests/NodeSelftest.swift ios/Tests/PairingSelftest.swift ios/Tests/TailnetPinSelftest.swift ios/Tests/ReconnectSelftest.swift -o .tmp/nodeselftest
.tmp/nodeselftest
# 路由 / P5 / 自动回退 / 字幕存档：共享逻辑不依赖 ReplayKit，
# 单独编一个入口（RoutingSelftest 是 @main），这样不用改 NodeSelftest 的入口。
xcrun swiftc -O ios/Shared/Routing.swift ios/Shared/LineStore.swift ios/Shared/AutoFallback.swift ios/RSiOS/TranscriptStore.swift ios/Tests/RoutingSelftest.swift ios/Tests/LineStoreSelftest.swift ios/Tests/AutoFallbackSelftest.swift ios/Tests/TranscriptStoreSelftest.swift -o .tmp/parityselftest
.tmp/parityselftest
