#!/bin/bash
set -euo pipefail
mkdir -p .tmp
trap 'rm -rf .tmp' EXIT
xcrun swiftc -O ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift ios/Tests/NodeSelftest.swift -o .tmp/nodeselftest
.tmp/nodeselftest
