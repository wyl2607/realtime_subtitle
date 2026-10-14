#!/bin/bash
set -euo pipefail
mkdir -p .tmp
trap 'rm -rf .tmp' EXIT
mkdir -p .tmp/module-cache
CLANG_MODULE_CACHE_PATH="$PWD/.tmp/module-cache" \
SWIFT_MODULE_CACHE_PATH="$PWD/.tmp/module-cache" \
xcrun swiftc -O ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift ios/Shared/PairingLink.swift ios/Shared/TailnetPin.swift ios/RSBroadcast/ReconnectPolicy.swift ios/Tests/NodeSelftest.swift ios/Tests/PairingSelftest.swift ios/Tests/TailnetPinSelftest.swift ios/Tests/ReconnectSelftest.swift -o .tmp/nodeselftest
.tmp/nodeselftest
