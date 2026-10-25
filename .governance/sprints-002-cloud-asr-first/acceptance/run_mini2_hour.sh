#!/bin/bash
# v3.2.0 Done 6 Groq long-run acceptance driver.  Run on the MBA only.
# It creates/uses only ~/rs-accept-v320 on the remote host.

set -u

REAL_HOME="$HOME"
DATA="$REAL_HOME/projects/rs-mac-native-data"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PYTHON="$REPO/venv/bin/python"
RSLITE="$REPO/macos-native/.build/release/rslite"

KEY_FILE=""
HOST="mini2"
MINUTES="60"
OUT_DIR=""
SUMMARY_FILE=""
REMOTE_DIR="~/rs-accept-v320"
CLEANED_REMOTE=false

usage() {
    printf '%s\n' '用法: run_mini2_hour.sh --key-file PATH [--host mini2] [--minutes 60]' >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --key-file)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            KEY_FILE="$2"
            shift 2
            ;;
        --host)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            HOST="$2"
            shift 2
            ;;
        --minutes)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            MINUTES="$2"
            shift 2
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

[ -n "$KEY_FILE" ] && [ -f "$KEY_FILE" ] && [ -s "$KEY_FILE" ] || {
    printf '%s\n' '--key-file 必须指向非空文件' >&2
    exit 1
}
case "$MINUTES" in
    *[!0-9]*|"") printf '%s\n' '--minutes 必须是正整数' >&2; exit 1 ;;
esac
[ "$MINUTES" -gt 0 ] || { printf '%s\n' '--minutes 必须大于 0' >&2; exit 1; }

OUT_DIR="$DATA/accept-v320-mini2/$(date +'%Y%m%d-%H%M%S')"
LONG_WAV="$OUT_DIR/long.wav"

log_result() {
    local name="$1"
    local result="$2"
    local reason="$3"
    printf '%s %s %s\n' "$result" "$name" "$reason"
    printf '%s\t%s\t%s\n' "$name" "$result" "$reason" >> "$SUMMARY_FILE"
}

cleanup_remote() {
    [ "$CLEANED_REMOTE" = true ] && return
    CLEANED_REMOTE=true
    # --clear-groq-key and the explicit rm both target only the test fakehome.
    ssh "$HOST" 'cd "$HOME/rs-accept-v320" 2>/dev/null || exit 0; env HOME="$HOME/rs-accept-v320/fakehome" RSLITE_CONFIG_DIR="$HOME/rs-accept-v320/fakehome/.config/rslite" ./rslite --clear-groq-key >/dev/null 2>&1; rm -f "$HOME/rs-accept-v320/fakehome/.config/rslite/groq.key" "$HOME/rs-accept-v320/k"' \
        >> "$OUT_DIR/remote-cleanup.log" 2>&1
}

make_long_wav() {
    "$PYTHON" - "$DATA/wav" "$LONG_WAV" "$MINUTES" <<'PY'
import glob
import os
import sys
import wave

wav_dir, output, minutes = sys.argv[1], sys.argv[2], int(sys.argv[3])
target_frames = minutes * 60 * 16_000
paths = sorted(glob.glob(os.path.join(wav_dir, "*.wav")))
if len(paths) != 60:
    raise SystemExit("expected exactly 60 FLEURS wav files in %s, found %d" % (wav_dir, len(paths)))
source = []
for path in paths:
    with wave.open(path, "rb") as reader:
        if reader.getframerate() != 16_000 or reader.getnchannels() != 1:
            raise SystemExit("not 16k mono: %s (rate=%s channels=%s)" % (path, reader.getframerate(), reader.getnchannels()))
        if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
            raise SystemExit("not 16-bit uncompressed PCM: %s" % path)
        source.append(reader.readframes(reader.getnframes()))
if not source:
    raise SystemExit("no source audio")
silence = b"\x00\x00" * 16_000
written = 0
index = 0
with wave.open(output, "wb") as writer:
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(16_000)
    writer.setcomptype("NONE", "not compressed")
    while written < target_frames:
        chunk = source[index % len(source)]
        frames = len(chunk) // 2
        writer.writeframes(chunk)
        written += frames
        if written >= target_frames:
            break
        writer.writeframes(silence)
        written += 16_000
        index += 1
with wave.open(output, "rb") as check:
    if check.getframerate() != 16_000 or check.getnchannels() != 1 or check.getnframes() < target_frames:
        raise SystemExit("generated long.wav failed verification")
print("long.wav %.3f seconds from %d FLEURS files" % (check.getnframes() / 16_000.0, len(paths)))
PY
}

write_report() {
    "$PYTHON" - "$OUT_DIR" <<'PY'
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
events_path = out / "events.jsonl"
stderr_path = out / "stderr.log"
ledger_path = out / "quota-ledger.json"
summary_path = out / "summary.tsv"
report_path = out / "report.md"
stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
http_429 = stderr.count("Groq HTTP 429")
ledger_error = None
ledger = None
try:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
except Exception as exc:
    ledger_error = str(exc)
events = ledger.get("events", []) if isinstance(ledger, dict) else []
corrections = ledger.get("request_corrections", []) if isinstance(ledger, dict) else []
n_local = len(events)
try:
    s_local = sum(float(event["billed_s"]) for event in events)
    billable_shape = all(float(event["billed_s"]) >= 10 for event in events)
except Exception:
    s_local = None
    billable_shape = False

# CloudClient 成功响应读到剩余请求数时写 stderr `rslite.cloud remaining_requests=<n>`。
# 只有真实的 start/end 成对值才能构成 Delta_R；单个值不算。
remaining = [int(value) for value in re.findall(r"rslite\.cloud remaining_requests=(\d+)", stderr)]
if len(remaining) >= 2:
    # 第一个 remaining 是第一次请求扣完之后读到的，所以首尾差只覆盖 N-1 次请求，要 +1。
    # 2026-10-19 mini2 实测：390 条 remaining 逐次恰好减 1，首尾差 389、账本 390。
    delta_r = remaining[0] - remaining[-1] + 1
    header_result = "PASS" if delta_r == n_local else "FAIL"
    header_reason = "Delta_R=%d, N_local=%d" % (delta_r, n_local)
else:
    delta_r = None
    header_result = "NOT_EVALUABLE"
    header_reason = "stderr 的 rslite.cloud remaining_requests 记录不足 2 个，无法构成 Delta_R"

# 账本现在写 dur_s（round 3 位实际音频秒）。逐段重算 billed_s == max(10, dur_s)，
# 容差 0.001 覆盖 dur_s 的 3 位舍入；没有 dur_s 的旧账本仍是 NOT_EVALUABLE。
durations = [event.get("dur_s") for event in events]
if events and all(d is not None for d in durations):
    per_segment_ok = all(
        abs(float(event["billed_s"]) - max(10.0, float(event["dur_s"]))) <= 0.001
        for event in events
    )
    duration_result = "PASS" if per_segment_ok else "FAIL"
    duration_reason = "逐段 billed_s == max(10, dur_s)（dur_s 3 位，容差 0.001）: %s" % ("全部成立" if per_segment_ok else "存在不成立")
else:
    duration_result = "NOT_EVALUABLE"
    duration_reason = "账本事件缺 dur_s（旧格式），无法逐段重算；仅可见 billed_s>=10: %s" % billable_shape
rows = [
    ("http_429", "PASS" if http_429 == 0 else "FAIL", "count=%d; detector literal Groq HTTP 429" % http_429),
    ("ledger", "PASS" if ledger_error is None else "FAIL", "N_local=%d; S_local=%s" % (n_local, "n/a" if s_local is None else "%.3f" % s_local) if ledger_error is None else ledger_error),
    ("response_header", header_result, header_reason),
    ("billed_formula", duration_result, duration_reason),
]
prior_rows = []
if summary_path.exists():
    for line in summary_path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t", 2)
        if len(parts) == 3:
            prior_rows.append(tuple(parts))
with summary_path.open("w", encoding="utf-8") as handle:
    handle.write("项目名\t结果\t原因\n")
    for name, result, reason in prior_rows + rows:
        handle.write("%s\t%s\t%s\n" % (name, result, reason))
with report_path.open("w", encoding="utf-8") as handle:
    handle.write("# v3.2.0 Done 6 — mini2 长测报告\n\n")
    handle.write("- 429 次数：`%d`（只数 stderr 中源码错误文案 `Groq HTTP 429`；`macos-native/Sources/rslite/CloudClient.swift:34-39,309-319`）。\n" % http_429)
    if ledger_error:
        handle.write("- 账本：无法读取：`%s`。\n" % ledger_error)
    else:
        handle.write("- 本地账本：`N_local=%d`、`S_local=%.3f`，来自 `events[].billed_s`；账本文件名与字段见 `macos-native/Sources/rslite/QuotaLedger.swift:19-36,101-113`。\n" % (n_local, s_local))
        handle.write("- 账本的响应头校正记录数：`%d`（`request_corrections[].used_requests`）。这不是开始/结束的剩余请求数，不能独自构成 Delta_R。\n" % len(corrections))
    handle.write("- 响应头比对：`%s` — %s。`CloudClient` 成功响应读到 `x-ratelimit-remaining-requests` 时往 stderr 写 `rslite.cloud remaining_requests=<n>`（`CloudClient.swift:170-178`），只写整数，不写 key / URL / 正文 / 其它响应头。\n" % (header_result, header_reason))
    handle.write("- 逐段计费公式：`%s` — %s。源码实际在发送前将 `segment.duration` 传给 `recordRequest`（`CloudSegmenter.swift:646-675`），再写入 `max(10, duration)` 与 round 3 位的 `dur_s`（`QuotaLedger.swift:105-113`）。\n" % (duration_result, duration_reason))
    handle.write("- 自动云端路径：`--mode auto` 是合法非 local 模式（`HybridEngine.swift:42-46,753-772,797-799`）；有 key 且开关开启时控制器初始化为 cloud available（`CloudSegmenter.swift:588-603`）。\n")
PY
}

main() {
    mkdir -p "$OUT_DIR" || exit 1
    SUMMARY_FILE="$OUT_DIR/summary.tsv"
    printf '项目名\t结果\t原因\n' > "$SUMMARY_FILE"
    cd "$REPO" || exit 1
    [ -x "$RSLITE" ] || { log_result "preflight" "FAIL" "缺少 release rslite: $RSLITE"; exit 1; }
    [ -x "$PYTHON" ] || { log_result "preflight" "FAIL" "缺少 Python: $PYTHON"; exit 1; }
    [ -d "$DATA/wav" ] || { log_result "preflight" "FAIL" "缺少 $DATA/wav"; exit 1; }
    trap 'cleanup_remote' EXIT HUP INT TERM

    make_long_wav > "$OUT_DIR/build-long.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "long_wav" "FAIL" "长音频拼接或 16k 单声道核验失败；见 build-long.log"
        return 1
    fi
    log_result "long_wav" "PASS" "已生成至少 ${MINUTES} 分钟的 16k 单声道 long.wav"

    ssh "$HOST" 'mkdir -p "$HOME/rs-accept-v320" && chmod 700 "$HOME/rs-accept-v320"' > "$OUT_DIR/remote-setup.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "remote_setup" "FAIL" "无法创建 ~/rs-accept-v320；见 remote-setup.log"
        return 1
    fi
    scp "$RSLITE" "$LONG_WAV" "$HOST:$REMOTE_DIR/" > "$OUT_DIR/scp.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "copy" "FAIL" "scp rslite/long.wav 失败；见 scp.log"
        return 1
    fi
    ssh "$HOST" 'chmod 700 "$HOME/rs-accept-v320/rslite"' >> "$OUT_DIR/remote-setup.log" 2>&1

    # Deliberately no cat/echo locally: ssh copies stdin directly into a 0600 temp file.
    ssh "$HOST" "cat > ~/rs-accept-v320/k && chmod 600 ~/rs-accept-v320/k" < "$KEY_FILE" > "$OUT_DIR/key-transfer.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "key_transfer" "FAIL" "无法写入远端临时 key 文件；见 key-transfer.log"
        return 1
    fi
    ssh "$HOST" 'cd "$HOME/rs-accept-v320" && env HOME="$HOME/rs-accept-v320/fakehome" RSLITE_CONFIG_DIR="$HOME/rs-accept-v320/fakehome/.config/rslite" ./rslite --set-groq-key < k; rc=$?; rm -f "$HOME/rs-accept-v320/k"; exit "$rc"' > "$OUT_DIR/key-config.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "key_config" "FAIL" "远端 --set-groq-key 失败；临时 k 已清理；见 key-config.log"
        return 1
    fi
    log_result "key_config" "PASS" "key 仅写入 mini2 测试 fakehome，远端临时 k 已删除"

    ssh "$HOST" 'cd "$HOME/rs-accept-v320" && caffeinate -i env HOME="$HOME/rs-accept-v320/fakehome" RSLITE_CONFIG_DIR="$HOME/rs-accept-v320/fakehome/.config/rslite" ./rslite --mode auto --source "file:$HOME/rs-accept-v320/long.wav" --headless > events.jsonl 2> stderr.log' > "$OUT_DIR/remote-run.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "remote_run" "FAIL" "mini2 rslite --mode auto 退出非零；见 remote-run.log"
    else
        log_result "remote_run" "PASS" "mini2 headless 长测已正常退出"
    fi

    scp "$HOST:$REMOTE_DIR/events.jsonl" "$HOST:$REMOTE_DIR/stderr.log" "$OUT_DIR/" > "$OUT_DIR/collect.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "collect_events" "FAIL" "无法 scp events/stderr；见 collect.log"
    fi
    scp "$HOST:$REMOTE_DIR/fakehome/.config/rslite/quota-ledger.json" "$OUT_DIR/quota-ledger.json" >> "$OUT_DIR/collect.log" 2>&1
    if [ "$?" -ne 0 ]; then
        log_result "collect_ledger" "FAIL" "无法 scp quota-ledger.json；见 collect.log"
    fi
    write_report
    if [ "$?" -ne 0 ]; then
        log_result "report" "FAIL" "无法生成 summary.tsv/report.md"
        return 1
    fi
    # write_report intentionally replaces the provisional summary with metric rows.
    cat "$SUMMARY_FILE"
    if grep -Fq "$(printf '\t')FAIL$(printf '\t')" "$SUMMARY_FILE"; then
        return 1
    fi
    # Incomplete observable evidence remains non-passing, never silently green.
    if grep -Fq "$(printf '\t')NOT_EVALUABLE$(printf '\t')" "$SUMMARY_FILE"; then
        return 2
    fi
}

main
