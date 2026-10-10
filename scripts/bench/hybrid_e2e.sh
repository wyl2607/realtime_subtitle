#!/usr/bin/env bash
# 混合档端到端验收脚本：依次跑 B、C、混合档，评估 RFC Done criteria 4
# 用法：scripts/bench/hybrid_e2e.sh [wav文件路径]
# 默认使用 ~/projects/rs-mac-native-data/concat5.wav

set -euo pipefail

# 默认wav文件
DEFAULT_WAV="${HOME}/projects/rs-mac-native-data/concat5.wav"
WAV="${1:-$DEFAULT_WAV}"

# 检查wav文件是否存在
if [ ! -f "$WAV" ]; then
    echo "错误：找不到wav文件 $WAV" >&2
    exit 1
fi

# 获取项目根目录
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="${RSLITE_BIN:-$ROOT/macos-native/.build/release/rslite}"
PY="${RS_PY:-$HOME/projects/rs-mac-venv/bin/python}"
REFS="${RS_REFS:-$HOME/projects/rs-mac-native-data/refs.jsonl}"

# 检查rslite二进制是否存在
if [ ! -x "$BIN" ]; then
    echo "错误：找不到rslite二进制文件 $BIN" >&2
    echo "请先构建：swift build -c release --product rslite" >&2
    exit 1
fi
if [ ! -x "$PY" ]; then
    echo "错误：找不到可执行 Python $PY（可用 RS_PY 覆盖）" >&2
    exit 1
fi
if [ ! -f "$REFS" ]; then
    echo "错误：找不到 refs 文件 $REFS（可用 RS_REFS 覆盖）" >&2
    exit 1
fi

# 输出目录（使用时间戳避免冲突）
TIMESTAMP="${HYBRID_E2E_TIMESTAMP:-$(date +"%Y%m%d_%H%M%S")}"
RESULTS_DIR="${HYBRID_E2E_RESULTS_DIR:-$ROOT/scripts/bench/results}"
OUT_DIR="$RESULTS_DIR/hybrid_e2e_$TIMESTAMP"
mkdir -p "$OUT_DIR"

# 组间冷却时间
COOLDOWN_S="${HYBRID_E2E_COOLDOWN_S:-5}"

# 音频时长（用于采样时长估算）
SECS=$("$PY" -c "import wave,sys; w=wave.open(sys.argv[1]); print(int(w.getnframes()/w.getframerate())+8)" "$WAV")

echo "wav文件: $WAV"
echo "音频时长: ${SECS}秒（含缓冲）"
echo "输出目录: $OUT_DIR"
echo "Python: $PY"
echo "refs: $REFS"
echo ""

run_mode() {
    local label="$1"
    local mode="$2"
    local stdout_file="$3"
    local stderr_file="$4"
    echo "[$label] rslite --mode $mode"
    set +e
    "$BIN" --mode "$mode" --source "file:$WAV" --headless > "$stdout_file" 2> "$stderr_file"
    local rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        echo "  失败：rslite --mode $mode 退出 $rc" >&2
        echo "  stdout: $stdout_file" >&2
        echo "  stderr: $stderr_file" >&2
        return "$rc"
    fi
    echo "  完成，事件存至: $stdout_file"
    echo "  stderr 存至: $stderr_file"
}

# 运行B模式（本机）
run_mode "B" "local" "$OUT_DIR/B.jsonl" "$OUT_DIR/B.stderr.log"
sleep "$COOLDOWN_S"

# 运行C基线。rslite 当前只有 auto/local/hybrid，没有“全部交给节点”的纯 C 模式。
# 因此这里使用 auto 作为现有能力中最接近的 C 基线，并在汇总 JSON 里显式记录。
C_MODE_NOTE="rslite supports --mode auto|local|hybrid; no pure all-node C mode is available, so --mode auto is used as the closest available C baseline."
run_mode "C" "auto" "$OUT_DIR/C.jsonl" "$OUT_DIR/C.stderr.log"
sleep "$COOLDOWN_S"

# 运行混合档模式
run_mode "混合" "hybrid" "$OUT_DIR/hybrid.jsonl" "$OUT_DIR/hybrid.stderr.log"
sleep "$COOLDOWN_S"

echo ""
echo "=== 汇总结果（JSON）==="
set +e
"$PY" "$ROOT/scripts/bench/hybrid_score.py" \
    --offline-dir "$OUT_DIR" \
    --refs "$REFS" \
    --json \
    --c-mode-note "$C_MODE_NOTE" > "$OUT_DIR/summary.json"
SCORE_RC=$?
set -e
cat "$OUT_DIR/summary.json"
echo ""
if [ "$SCORE_RC" -eq 0 ]; then
    echo "RFC判定: PASS"
else
    echo "RFC判定: FAIL"
fi
exit "$SCORE_RC"
