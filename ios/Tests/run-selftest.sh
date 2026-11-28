#!/bin/bash
set -euo pipefail
mkdir -p .tmp
trap 'rm -rf .tmp' EXIT
mkdir -p .tmp/module-cache
export CLANG_MODULE_CACHE_PATH="$PWD/.tmp/module-cache"
export SWIFT_MODULE_CACHE_PATH="$PWD/.tmp/module-cache"
xcrun swiftc -O ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift ios/Shared/PairingLink.swift ios/Shared/TailnetPin.swift ios/Engine/ReconnectPolicy.swift ios/Tests/NodeSelftest.swift ios/Tests/PCMConverterSelftest.swift ios/Tests/PairingSelftest.swift ios/Tests/TailnetPinSelftest.swift ios/Tests/ReconnectSelftest.swift -o .tmp/nodeselftest
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
xcrun swiftc -O ios/Shared/Routing.swift ios/Shared/CloudClient.swift ios/Shared/QuotaLedger.swift ios/Shared/CloudSegmenter.swift ios/Shared/NetworkPaths.swift ios/Shared/CellularHTTP.swift ios/Tests/CloudSelftestMain.swift ios/Tests/CellularHTTPSelftest.swift ios/Tests/CloudClientSelftest.swift ios/Tests/QuotaLedgerSelftest.swift ios/Tests/CloudSegmenterSelftest.swift -o .tmp/cloudselftest
.tmp/cloudselftest
# I5：SampleHandler 抽出来的纯判定（切回时机、按会话回填译文、广播结束闸门、换 key 作废）。
xcrun swiftc -O ios/Shared/RecentLines.swift ios/Tests/RecentLinesSelftest.swift -o .tmp/handoffselftest
.tmp/handoffselftest
# I8-B：广播/麦克风互斥和 PiP 会话回退必须可在命令行稳定复现，
# 不把 Keychain、音频硬件或 ReplayKit 带进单元自测。
xcrun swiftc -O ios/Shared/CaptureArbiter.swift ios/Tests/CaptureArbiterSelftest.swift -o .tmp/capturearbiterselftest
.tmp/capturearbiterselftest
# 引擎轮次闸门要先在命令行锁住：旧异步回调不能跨 begin/end 进入新一轮，
# local 档也绝不能意外建立节点连接。
xcrun swiftc -O ios/Shared/EngineRunGate.swift ios/Tests/EngineRunGateSelftest.swift -o .tmp/enginerungateselftest
.tmp/enginerungateselftest
# 本机引擎状态解析：纯逻辑、无依赖，单独编译验证。
xcrun swiftc -O ios/Shared/LocalEngineStatus.swift ios/Shared/DesignTokens.swift ios/Tests/LocalEngineStatusSelftest.swift -o .tmp/localenginestatusselftest
.tmp/localenginestatusselftest

# App 前后台通知的状态映射是纯逻辑；通知回调不得再额外排 DispatchQueue.main.async。
xcrun swiftc -O -parse-as-library ios/Tests/AppStateMonitorSelftest.swift -o .tmp/appstatemonitorselftest
.tmp/appstatemonitorselftest

# 界面状态词和字幕样式模型：只依赖 Foundation + DesignTokens，
# 单独编一组验证解析规则和持久化夹值，不把 SwiftUI 带进自测。
xcrun swiftc -O ios/Shared/StatusSummary.swift ios/Shared/SubtitleStyle.swift ios/Shared/LocalEngineStatus.swift ios/Shared/DesignTokens.swift ios/Tests/StatusSummarySelftest.swift ios/Tests/SubtitleStyleSelftest.swift ios/Tests/UISelftestMain.swift -o .tmp/uiselftest
.tmp/uiselftest
# 定稿分句是纯逻辑：本机档十几秒才定稿一次，切句规则错一点就会退回「一条字幕塞好几句」。
xcrun swiftc -O ios/Shared/SentenceSplitter.swift ios/Tests/SentenceSplitterSelftest.swift -o .tmp/sentencesplitterselftest
.tmp/sentencesplitterselftest
# HC-U18：App Group 文件存储（注入临时目录和内存旧 Keychain 闭包，不碰真实 Keychain）。
xcrun swiftc -O ios/Shared/SharedStore.swift ios/Shared/SharedKeychain.swift ios/Shared/Routing.swift ios/Tests/SharedStoreSelftest.swift -o .tmp/sharedstoreselftest
.tmp/sharedstoreselftest
# HC-U19：回看页单句重新翻译纯逻辑（可重试判定、同句并发去重、失败文案映射），不依赖 Translation/SwiftUI。
xcrun swiftc -O ios/RSiOS/RetryTranslationPolicy.swift ios/Shared/DesignTokens.swift ios/Tests/RetryTranslationSelftest.swift -o .tmp/retrytranslationselftest
.tmp/retrytranslationselftest
# HC-P01：主 App 的运行态只允许有一个轮询源。LiveFeed 要求内容没变不发布属性，
# 否则合并了读取次数、SwiftUI 还是每次整树重绘。SharedStore / SharedKeychain 一起编是为了
# 走真实默认读取闭包（自测里注入的是内存闭包，不碰真实磁盘/Keychain）；
# Routing.swift 提供 SharedStore.readEngineMode 需要的 EngineMode。
xcrun swiftc -O ios/RSiOS/LiveFeed.swift ios/Shared/SharedStore.swift ios/Shared/SharedKeychain.swift ios/Shared/Routing.swift ios/Tests/LiveFeedSelftest.swift -o .tmp/livefeedselftest
.tmp/livefeedselftest
# HC-U20：快捷指令 / 控制中心开始与停止的纯状态判定（重复触发忽略、广播互斥）。
xcrun swiftc -O ios/Shared/CaptureArbiter.swift ios/Shared/MicCaptionIntentPolicy.swift ios/Tests/MicCaptionIntentPolicySelftest.swift -o .tmp/miccaptionintentselftest
.tmp/miccaptionintentselftest
# HC-B01：NetworkProbe 可取消闸门——保证 continuation 只恢复一次、取消即关闭连接不再等 5 秒。
xcrun swiftc -swift-version 6 -O ios/Shared/NetworkProbeGate.swift ios/Tests/NetworkProbeSelftest.swift -o .tmp/networkprobeselftest
.tmp/networkprobeselftest
