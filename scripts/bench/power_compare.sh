#!/usr/bin/env bash
# 整机功耗对比：空闲 / rslite 本机（系统识别+系统翻译）/ rslite 远程（mini2 识别）。
# 三段都回放同一段音频（实时速度），用 powermetrics 采 CPU+GPU+ANE 合计功耗。
# rslite 本机模式的识别跑在系统 daemon 里，进程级 CPU 看不到——只能看整机。
#
# 用法（需要 sudo，powermetrics 只能 root 跑）：
#   sudo bash scripts/bench/power_compare.sh <wav> [ws://mini2:8791/v1]
# 测之前：停掉 Python 字幕、关掉看视频的浏览器标签，接不接电源都行但三段要一致。
set -euo pipefail

WAV=${1:?usage: power_compare.sh <wav> [remote-url]}
REMOTE=${2:-ws://100.105.163.59:8791/v1}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/macos-native/.build/release/rslite"
# sudo 下 HOME 是 root 的：token 要用发起 sudo 的那个用户的
USER_HOME=$(eval echo "~${SUDO_USER:-$USER}")
TOKEN="$USER_HOME/.config/rslite/remote-token"
OUT=$(mktemp -d)
SECS=$(python3 -c "import wave,sys; w=wave.open(sys.argv[1]); print(int(w.getnframes()/w.getframerate())+8)" "$WAV")

measure() {  # $1=名字 $2...=要跑的命令（空=只测空闲）
    local name=$1; shift
    powermetrics --samplers cpu_power,gpu_power,ane_power -i 2000 -n $((SECS / 2)) > "$OUT/$name.txt" 2>/dev/null &
    local pm=$!
    if [ $# -gt 0 ]; then
        sudo -u "${SUDO_USER:-$USER}" "$@" > "$OUT/$name.events" 2>&1 || true
    fi
    wait $pm
    awk -v n="$name" '/Combined Power/ {s+=$(NF-1); c++} END {printf "%-8s 平均 %6.0f mW  (%d 个采样)\n", n, s/c, c}' "$OUT/$name.txt"
}

echo "每段约 ${SECS} 秒，共三段……"
measure idle
measure local "$BIN" --source "file:$WAV" --headless
measure remote "$BIN" --remote "$REMOTE" --token-file "$TOKEN" --source "file:$WAV" --headless
# 数据是 root 写的，交还给发起 sudo 的用户，否则事后读不了
chown -R "${SUDO_USER:-$USER}" "$OUT"
echo "原始数据: $OUT"
