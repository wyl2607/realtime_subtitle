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
# DesignTokens.swift 要一起编：自动回退的状态文案改成从设计令牌取（spec 010 C3），
# 不带上它这个二进制编不过。
xcrun swiftc -O ios/Shared/Routing.swift ios/Shared/LineStore.swift ios/Shared/AutoFallback.swift ios/Shared/DesignTokens.swift ios/RSiOS/TranscriptStore.swift ios/Tests/RoutingSelftest.swift ios/Tests/LineStoreSelftest.swift ios/Tests/AutoFallbackSelftest.swift ios/Tests/TranscriptStoreSelftest.swift -o .tmp/parityselftest
.tmp/parityselftest
# 云端档纯逻辑（Groq 请求/响应、额度账本、分段器）：第三个独立入口，
# 只编这三个 Shared 文件 + QuotaLedger 需要的 Routing.swift（滞回判定在路由状态机里）。
# 不发网络请求，也不读任何真实 key。
xcrun swiftc -O ios/Shared/Routing.swift ios/Shared/CloudClient.swift ios/Shared/QuotaLedger.swift ios/Shared/CloudSegmenter.swift ios/Tests/CloudSelftestMain.swift ios/Tests/CloudClientSelftest.swift ios/Tests/QuotaLedgerSelftest.swift ios/Tests/CloudSegmenterSelftest.swift -o .tmp/cloudselftest
.tmp/cloudselftest
# I5：SampleHandler 抽出来的纯判定（切回时机、按会话回填译文、广播结束闸门、换 key 作废）。
xcrun swiftc -O ios/Shared/RecentLines.swift ios/Tests/RecentLinesSelftest.swift -o .tmp/handoffselftest
.tmp/handoffselftest
