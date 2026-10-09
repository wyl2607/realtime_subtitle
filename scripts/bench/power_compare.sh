#!/usr/bin/env bash
# 整机功耗对比，五组：空闲 / B（rslite 本机）/ C（远程精修，v2 节点）/ A（Python 桌面字幕）/ 混合（rslite 混合档）。
# 每组都回放同一段音频（实时速度），用 powermetrics 采 CPU+GPU+ANE 合计功耗。
# rslite 本机模式的识别跑在系统 daemon 里，进程级 CPU 看不到——只能看整机。
#
# 用法（需要 sudo，powermetrics 只能 root 跑）：
#   sudo bash scripts/bench/power_compare.sh <wav>
# rslite 的 --mode 由 TK-005 实现（待 TK-005 合并后实跑）；节点从 ~/.config/rslite/nodes.json 读，
# 所以 C 组与混合组都要求 v2 节点已装好且在线（scripts/node/install_node.sh）。
#
# 测之前（脚本只检查、提示，不替你关任何进程）：
#   1. 停掉 A 版 Python 字幕、关掉浏览器里正在播的视频；
#   2. 接不接电源都行，但五组必须一致；
#   3. 噪声：2s 一个采样，每组约一分钟，组间要冷却；实测 B 与空闲只差 120mW，
#      在噪声以内——只有差距明显大于重复测量的波动时才下结论，建议整套重复 ≥2 轮。
set -euo pipefail

WAV=${1:?usage: power_compare.sh <wav>}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/macos-native/.build/release/rslite"
RUN_USER=${SUDO_USER:-$USER}
# 组间冷却：让 SoC 温度和后台活动回落，避免上一组的余热抬高下一组
COOLDOWN_S=30
# A 版启动后先等模型加载完再采样，否则测到的是加载而不是稳态
A_WARMUP_S=30
OUT=$(mktemp -d)
SECS=$(python3 -c "import wave,sys; w=wave.open(sys.argv[1]); print(int(w.getnframes()/w.getframerate())+8)" "$WAV")

# 只检查、不 kill：别人的进程不归脚本管
check_quiet() {
    local busy=0
    if pgrep -f "realtime_subtitle|python.*main\.py" > /dev/null; then
        echo "错误：检测到 A 版 Python 字幕（或同类进程）在跑，请先手动退出：" >&2
        pgrep -fl "realtime_subtitle|python.*main\.py" >&2 || true
        busy=1
    fi
    if pgrep -x rslite > /dev/null; then
        echo "错误：检测到 rslite 已在运行，请先退出。" >&2
        busy=1
    fi
    [ "$busy" -eq 0 ] || exit 1
    if pgrep -x "Google Chrome" > /dev/null || pgrep -x Safari > /dev/null || pgrep -x firefox > /dev/null; then
        echo "提示：浏览器在运行。请确认没有标签页在播视频/音频，否则会抬高所有组的功耗。"
    fi
}

as_user() { sudo -u "$RUN_USER" "$@"; }

# $1=名字 $2...=采样期间要跑的命令（空=只测空闲）
measure() {
    local name=$1; shift
    powermetrics --samplers cpu_power,gpu_power,ane_power -i 2000 -n $((SECS / 2)) > "$OUT/$name.txt" 2>/dev/null &
    local pm=$!
    if [ $# -gt 0 ]; then
        as_user "$@" > "$OUT/$name.events" 2>&1 || true
    fi
    wait $pm
    awk -v n="$name" '/Combined Power/ {s+=$(NF-1); c++} END {if (c) printf "%-8s 平均 %6.0f mW  (%d 个采样)\n", n, s/c, c; else printf "%-8s 无采样\n", n}' "$OUT/$name.txt"
}

cool() { echo "  冷却 ${COOLDOWN_S}s……"; sleep "$COOLDOWN_S"; }

# A 版是 GUI 常驻程序，只能抓系统声音：用 afplay 放同一段音频，采样结束后只停我们自己起的这个进程。
# 注意 afplay 本身有少量播放功耗，其它组走 rslite 的 file: 源、不出声——这个差异要写进结论。
measure_a() {
    as_user "$ROOT/venv/bin/python" "$ROOT/main.py" > "$OUT/a.log" 2>&1 &
    local apid=$!
    echo "  A 版已启动，等待 ${A_WARMUP_S}s 加载模型……"
    sleep "$A_WARMUP_S"
    measure a afplay "$WAV"
    kill "$apid" 2>/dev/null || true
    wait "$apid" 2>/dev/null || true
}

[ "$(id -u)" -eq 0 ] || { echo "需要 sudo 执行（powermetrics）" >&2; exit 1; }
[ -x "$BIN" ] || { echo "找不到 ${BIN}，先 swift build -c release --product rslite" >&2; exit 1; }
check_quiet

echo "每组约 ${SECS} 秒，共五组（含冷却）……"
measure idle
cool
echo "[B] rslite 本机"
measure B "$BIN" --mode local --source "file:$WAV" --headless
cool
echo "[C] 远程精修（v2 节点）"
# TK-005 的 --mode 只有 auto|local|hybrid：auto 在不满足 P8 门槛的机器上选节点。
# 待 TK-005 合并后核对：若客户端上 C 与混合无法区分，C 改为到节点侧采样。
measure C "$BIN" --mode auto --source "file:$WAV" --headless
cool
echo "[A] Python 桌面字幕"
measure_a
cool
echo "[混合] rslite 混合档"
measure hybrid "$BIN" --mode hybrid --source "file:$WAV" --headless
# 数据是 root 写的，交还给发起 sudo 的用户，否则事后读不了
chown -R "$RUN_USER" "$OUT"
echo "原始数据: ${OUT}（hybrid.events 可交给 hybrid_score.py 评分）"
