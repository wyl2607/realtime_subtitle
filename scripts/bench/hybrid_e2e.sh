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
BIN="$ROOT/macos-native/.build/release/rslite"

# 检查rslite二进制是否存在
if [ ! -x "$BIN" ]; then
    echo "错误：找不到rslite二进制文件 $BIN" >&2
    echo "请先构建：swift build -c release --product rslite" >&2
    exit 1
fi

# 输出目录（使用时间戳避免冲突）
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUT_DIR="$ROOT/scripts/bench/results/hybrid_e2e_$TIMESTAMP"
mkdir -p "$OUT_DIR"

# 组间冷却时间
COOLDOWN_S=5

# 音频时长（用于采样时长估算）
SECS=$(python3 -c "import wave,sys; w=wave.open(sys.argv[1]); print(int(w.getnframes()/w.getframerate())+8)" "$WAV")

echo "wav文件: $WAV"
echo "音频时长: ${SECS}秒（含缓冲）"
echo "输出目录: $OUT_DIR"
echo ""

# 运行B模式（本机）
echo "[B] rslite 本机模式"
"$BIN" --mode local --source "file:$WAV" --headless > "$OUT_DIR/B.jsonl" 2>&1 || true
echo "  完成，事件存至: $OUT_DIR/B.jsonl"
sleep "$COOLDOWN_S"

# 运行C模式（远程精修）
echo "[C] rslite 远程精修模式（auto）"
"$BIN" --mode auto --source "file:$WAV" --headless > "$OUT_DIR/C.jsonl" 2>&1 || true
echo "  完成，事件存至: $OUT_DIR/C.jsonl"
sleep "$COOLDOWN_S"

# 运行混合档模式
echo "[混合] rslite 混合档模式"
"$BIN" --mode hybrid --source "file:$WAV" --headless > "$OUT_DIR/hybrid.jsonl" 2>&1 || true
echo "  完成，事件存至: $OUT_DIR/hybrid.jsonl"
sleep "$COOLDOWN_S"

# 评估B模式（作为基线）
echo ""
echo "=== B模式评估（基线）==="
B_RESULT=$(python3 "$ROOT/scripts/bench/hybrid_score.py" "$OUT_DIR/B.jsonl" \
    --refs "$HOME/projects/rs-mac-native-data/refs.jsonl" \
    --b-events "$OUT_DIR/B.jsonl" 2>/dev/null || true)
echo "$B_RESULT"

# 提取B模式的首字时间用于比较
B_FIRST_TEXT_S=$(echo "$B_RESULT" | grep "首字时间" | awk '{print $3}' || echo "n/a")

# 评估C模式（获取C的WER作为基线）
echo ""
echo "=== C模式评估（获取基线WER）==="
C_RESULT=$(python3 "$ROOT/scripts/bench/hybrid_score.py" "$OUT_DIR/C.jsonl" \
    --refs "$HOME/projects/rs-mac-native-data/refs.jsonl" 2>/dev/null || true)
echo "$C_RESULT"

# 提取C模式的最终WER
C_FINAL_WER=$(echo "$C_RESULT" | grep "最终文本 WER" | awk '{print $4}' || echo "n/a")

# 评估混合档模式（使用B和C的基线）
echo ""
echo "=== 混合档模式评估==="
HYBRID_RESULT=$(python3 "$ROOT/scripts/bench/hybrid_score.py" "$OUT_DIR/hybrid.jsonl" \
    --refs "$HOME/projects/rs-mac-native-data/refs.jsonl" \
    --b-events "$OUT_DIR/B.jsonl" \
    --c-wer "$C_FINAL_WER" 2>/dev/null || true)
echo "$HYBRID_RESULT"

# 汇总结果为JSON
echo ""
echo "=== 汇总结果 ==="
cat << EOF
{
  "timestamp": "$TIMESTAMP",
  "wav_file": "$WAV",
  "b_mode": {
    "events_file": "$OUT_DIR/B.jsonl",
    "first_text_s": $B_FIRST_TEXT_S
  },
  "c_mode": {
    "events_file": "$OUT_DIR/C.jsonl",
    "final_wer": $C_FINAL_WER
  },
  "hybrid_mode": {
    "events_file": "$OUT_DIR/hybrid.jsonl"
  }
}
EOF