#!/bin/bash
# v3.2.0 Groq key 验收（MBA）。只使用 fakehome；不触碰真实 rslite 配置。
# macOS /bin/bash 3.2 compatible.  The key is read only through --key-file stdin.

set -u

REAL_HOME="$HOME"
DATA="$REAL_HOME/projects/rs-mac-native-data"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PYTHON="$REPO/venv/bin/python"
RSLITE="$REPO/macos-native/.build/release/rslite"

KEY_FILE=""
OUT_DIR=""
ONLY=""
FAKE=""
SUMMARY_FILE=""
REAL_SNAPSHOT=""

usage() {
    printf '%s\n' '用法: run_mac_key.sh --key-file PATH [--out DIR] [--only cloud|latency|recover]' >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --key-file)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            KEY_FILE="$2"
            shift 2
            ;;
        --out)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            OUT_DIR="$2"
            shift 2
            ;;
        --only)
            [ "$#" -ge 2 ] || { usage; exit 1; }
            ONLY="$2"
            shift 2
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

case "$ONLY" in
    ""|cloud|latency|recover) ;;
    *) usage; exit 1 ;;
esac

[ -n "$KEY_FILE" ] && [ -f "$KEY_FILE" ] && [ -s "$KEY_FILE" ] || {
    printf '%s\n' '--key-file 必须指向非空文件' >&2
    exit 1
}

if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$DATA/accept-v320-key/$(date +'%Y%m%d-%H%M%S')"
fi

log_result() {
    local name="$1"
    local result="$2"
    local reason="$3"
    printf '%s %s %s\n' "$result" "$name" "$reason"
    printf '%s\t%s\t%s\n' "$name" "$result" "$reason" >> "$SUMMARY_FILE"
}

real_config_snapshot() {
    # 文件名 + 内容哈希；若隔离失效，任何真实配置写入都会改变此快照。
    (cd "$REAL_HOME/.config/rslite" 2>/dev/null && find . -type f | sort | while read -r file; do shasum "$file"; done)
}

setup_fake_home() {
    FAKE="$OUT_DIR/fakehome"
    rm -rf "$FAKE"
    mkdir -m 700 "$FAKE" || return 1
    mkdir -m 700 "$FAKE/.config" || return 1
    mkdir -m 700 "$FAKE/.config/rslite" || return 1
}

cleanup_fake_home() {
    # 清除密钥，但留下非敏感账本和 cloud.json 供验收取证。
    if [ -n "$FAKE" ]; then
        rm -f "$FAKE/.config/rslite/groq.key"
    fi
}

write_key() {
    local config_log="$1"
    HOME="$FAKE" RSLITE_CONFIG_DIR="$FAKE/.config/rslite" "$RSLITE" --set-groq-key < "$KEY_FILE" > "$config_log" 2>&1
}

set_cloud() {
    local state="$1"
    local config_log="$2"
    HOME="$FAKE" RSLITE_CONFIG_DIR="$FAKE/.config/rslite" "$RSLITE" --cloud "$state" >> "$config_log" 2>&1
}

run_playback() {
    local events_file="$1"
    local stderr_file="$2"
    local timeout_sec="$3"
    local pid
    local elapsed=0

    HOME="$FAKE" RSLITE_CONFIG_DIR="$FAKE/.config/rslite" "$RSLITE" \
        --mode hybrid --source "file:$DATA/concat5.wav" --headless > "$events_file" 2> "$stderr_file" &
    pid=$!
    while [ "$elapsed" -lt "$timeout_sec" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        return 124
    fi
    wait "$pid"
}

score_concat5() {
    local events_file="$1"
    local score_file="$2"
    local score_log="$3"
    "$PYTHON" "$REPO/scripts/bench/hybrid_score.py" "$events_file" \
        --refs "$DATA/refs.jsonl" --n 5 --json > "$score_file" 2> "$score_log"
}

check_cloud() {
    local events_file="$1"
    local score_file="$2"
    "$PYTHON" - "$events_file" "$score_file" <<'PY'
import json
import sys

events_path, score_path = sys.argv[1:]
events = []
with open(events_path, encoding="utf-8") as handle:
    for line in handle:
        if line.strip().startswith("{"):
            events.append(json.loads(line))
mode_statuses = [e.get("text") for e in events if e.get("ev") == "status" and str(e.get("text", "")).startswith("模式：")]
if not mode_statuses:
    raise SystemExit("未找到模式 status 事件")
if mode_statuses[0] != "模式：云端·Groq":
    raise SystemExit("首个模式 status 不是 模式：云端·Groq: %r" % mode_statuses[0])
with open(score_path, encoding="utf-8") as handle:
    score = json.load(handle)
if score.get("problems") != []:
    raise SystemExit("hybrid_score problems != []: %r" % score.get("problems"))
wer = score.get("final_wer")
if not isinstance(wer, (int, float)):
    raise SystemExit("hybrid_score 缺少数值 final_wer")
if wer > 0.0263:
    raise SystemExit("final_wer=%.6f > 0.0263" % wer)
print("first_mode=%s; problems=[]; final_wer=%.6f" % (mode_statuses[0], wer))
PY
}

check_recover() {
    local events_file="$1"
    local score_file="$2"
    "$PYTHON" - "$events_file" "$score_file" <<'PY'
import json
import sys

events_path, score_path = sys.argv[1:]
events = []
with open(events_path, encoding="utf-8") as handle:
    for line in handle:
        if line.strip().startswith("{"):
            events.append(json.loads(line))
statuses = [e.get("text") for e in events if e.get("ev") == "status"]
if "模式：云端·Groq" not in statuses:
    raise SystemExit("--cloud on 后未见 模式：云端·Groq")
with open(score_path, encoding="utf-8") as handle:
    score = json.load(handle)
if score.get("problems") != []:
    raise SystemExit("hybrid_score problems != []: %r" % score.get("problems"))
print("recovery_status=模式：云端·Groq; problems=[]")
PY
}

check_latency_clock_contract() {
    # main.swift:203,336-345,347-359: t is runHeadless-started monotonic elapsed time.
    # AudioFanout.swift:4-13/130: t1 is a sample clock.  The headless clock event
    # (ev=clock) records the shared zero point between the wall clock (t) and the
    # sample clock (t0).  latency = t_final - (clock.t + (t1 - clock.t0)).
    # 没有 clock 事件就仍是 NOT_EVALUABLE，绝不硬算 t - t1。
    local events_file="$1"
    local latency_file="$2"
    "$PYTHON" - "$events_file" "$latency_file" <<'PY'
import json
import statistics
import sys

events_path, out_path = sys.argv[1:]
events = []
with open(events_path, encoding="utf-8") as handle:
    for line in handle:
        if line.strip().startswith("{"):
            events.append(json.loads(line))

criterion = "cloud final (id null, text [cloud:groq]) median latency <= 2.0s"
clock = next((e for e in events if e.get("ev") == "clock"), None)

def write_result(result, extra):
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"result": result, "criterion": criterion, **extra}, handle,
                  ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

if clock is None or not isinstance(clock.get("t"), (int, float)) or not isinstance(clock.get("t0"), (int, float)):
    write_result("NOT_EVALUABLE", {
        "reason": "events 中没有有效的 clock 事件（第一样本共同零点），无法计算严格延迟",
    })
    print("NOT_EVALUABLE: 缺少 clock 事件（共同零点）")
    sys.exit(0)

cloud_finals = [
    e for e in events
    if e.get("ev") == "final" and e.get("id") is None
    and isinstance(e.get("text"), str) and str(e["text"]).startswith("[cloud:groq]")
    and isinstance(e.get("t"), (int, float)) and isinstance(e.get("t1"), (int, float))
]
latencies = [e["t"] - (clock["t"] + (e["t1"] - clock["t0"])) for e in cloud_finals]
median = statistics.median(latencies) if latencies else None
passed = median is not None and median <= 2.0
extra = {
    "clock_t": clock["t"],
    "clock_t0": clock["t0"],
    "n_cloud_finals": len(cloud_finals),
    "latencies_s": [round(v, 3) for v in latencies],
    "median_latency_s": round(median, 3) if median is not None else None,
}
if passed:
    write_result("PASS", extra)
    print("PASS: n_cloud_finals=%d median_latency=%.3fs" % (len(cloud_finals), median))
else:
    write_result("FAIL", {**extra, "reason": "median_latency > 2.0s" if median is not None else "没有云端 final 样本"})
    print("FAIL: median_latency=%s" % ("%.3fs" % median if median is not None else "n/a"))
sys.exit(0)
PY
}

check_key_absent() {
    # grep reads the key only through KEY_FILE and never writes it to stdout.
    local events_file="$1"
    local stderr_file="$2"
    if grep -F -q -f "$KEY_FILE" "$events_file" "$stderr_file" "$SUMMARY_FILE"; then
        return 1
    fi
    return 0
}

run_cloud() {
    local work="$OUT_DIR/cloud"
    local events="$work/events.jsonl"
    local stderr="$work/stderr.log"
    local config="$work/config.log"
    local score="$work/score.json"
    local score_log="$work/score.stderr.log"
    local detail
    local rc

    mkdir -p "$work" || return 1
    write_key "$config"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "cloud" "FAIL" "--set-groq-key 退出码 $rc；见 $config"
        return 1
    fi
    set_cloud on "$config"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "cloud" "FAIL" "--cloud on 退出码 $rc；见 $config"
        return 1
    fi
    run_playback "$events" "$stderr" 240
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "cloud" "FAIL" "rslite 回放退出码 $rc；见 $stderr"
        return 1
    fi
    score_concat5 "$events" "$score" "$score_log"
    rc=$?
    # hybrid_score's own RFC checks can return 1 while still writing usable JSON.
    if [ "$rc" -gt 1 ] || [ ! -s "$score" ]; then
        log_result "cloud" "FAIL" "hybrid_score.py 退出码 $rc；见 $score_log"
        return 1
    fi
    detail="$(check_cloud "$events" "$score" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "cloud" "FAIL" "$detail"
        return 1
    fi
    if check_key_absent "$events" "$stderr"; then
        log_result "cloud" "PASS" "$detail；key 未出现在 events/stderr/summary"
    else
        log_result "cloud" "FAIL" "key 出现在 events、stderr 或 summary；停止查看并轮换该 key"
        return 1
    fi
}

run_latency() {
    local events="$OUT_DIR/cloud/events.jsonl"
    local latency="$OUT_DIR/latency.json"
    local detail
    if [ ! -s "$events" ]; then
        log_result "latency" "FAIL" "缺少 cloud events；先运行 cloud（--only latency 会自动补跑）"
        return 1
    fi
    detail="$(check_latency_clock_contract "$events" "$latency" 2>&1)"
    case "$detail" in
        PASS*) log_result "latency" "PASS" "$detail；见 $latency" ;;
        NOT_EVALUABLE*) log_result "latency" "NOT_EVALUABLE" "$detail；见 $latency" ;;
        *) log_result "latency" "FAIL" "$detail；见 $latency" ;;
    esac
}

run_recover() {
    local work="$OUT_DIR/recover"
    local events="$work/events.jsonl"
    local stderr="$work/stderr.log"
    local config="$work/config.log"
    local score="$work/score.json"
    local score_log="$work/score.stderr.log"
    local pid
    local elapsed=0
    local rc
    local detail

    mkdir -p "$work" || return 1
    write_key "$config"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "recover" "FAIL" "--set-groq-key 退出码 $rc；见 $config"
        return 1
    fi
    set_cloud off "$config"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "recover" "FAIL" "--cloud off 退出码 $rc；见 $config"
        return 1
    fi
    HOME="$FAKE" RSLITE_CONFIG_DIR="$FAKE/.config/rslite" "$RSLITE" \
        --mode hybrid --source "file:$DATA/concat5.wav" --headless > "$events" 2> "$stderr" &
    pid=$!
    while [ "$elapsed" -lt 15 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid"
            log_result "recover" "FAIL" "回放在第 15 秒开云端前已退出"
            return 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    set_cloud on "$config"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        log_result "recover" "FAIL" "第 15 秒 --cloud on 退出码 $rc；见 $config"
        return 1
    fi
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [ "$elapsed" -ge 240 ]; then
            kill "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            log_result "recover" "FAIL" "回放超时（240s）"
            return 1
        fi
    done
    wait "$pid"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "recover" "FAIL" "rslite 回放退出码 $rc；见 $stderr"
        return 1
    fi
    score_concat5 "$events" "$score" "$score_log"
    rc=$?
    if [ "$rc" -gt 1 ] || [ ! -s "$score" ]; then
        log_result "recover" "FAIL" "hybrid_score.py 退出码 $rc；见 $score_log"
        return 1
    fi
    detail="$(check_recover "$events" "$score" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "recover" "FAIL" "$detail"
        return 1
    fi
    if check_key_absent "$events" "$stderr"; then
        log_result "recover" "PASS" "$detail；第 15 秒执行 --cloud on；key 未出现在 events/stderr/summary"
    else
        log_result "recover" "FAIL" "key 出现在 events、stderr 或 summary；停止查看并轮换该 key"
        return 1
    fi
}

print_summary() {
    local name
    local result
    local reason
    printf '\n%s\n' '=== 验收汇总 ==='
    while IFS="$(printf '\t')" read -r name result reason; do
        printf '%-16s %-15s %s\n' "$name" "$result" "$reason"
    done < "$SUMMARY_FILE"
}

main() {
    mkdir -p "$OUT_DIR" || exit 1
    SUMMARY_FILE="$OUT_DIR/summary.tsv"
    printf '项目名\t结果\t原因\n' > "$SUMMARY_FILE"
    cd "$REPO" || exit 1
    [ -x "$RSLITE" ] || { log_result "preflight" "FAIL" "缺少 release rslite: $RSLITE"; print_summary; exit 1; }
    [ -x "$PYTHON" ] || { log_result "preflight" "FAIL" "缺少 Python: $PYTHON"; print_summary; exit 1; }
    [ -f "$DATA/concat5.wav" ] && [ -f "$DATA/refs.jsonl" ] || {
        log_result "preflight" "FAIL" "缺少 $DATA/concat5.wav 或 refs.jsonl"
        print_summary
        exit 1
    }
    REAL_SNAPSHOT="$(real_config_snapshot)"
    setup_fake_home || { log_result "preflight" "FAIL" "无法创建 fakehome"; print_summary; exit 1; }
    trap 'cleanup_fake_home' EXIT HUP INT TERM

    if [ -z "$ONLY" ] || [ "$ONLY" = cloud ] || [ "$ONLY" = latency ]; then
        run_cloud
    fi
    if [ -z "$ONLY" ] || [ "$ONLY" = latency ]; then
        run_latency
    fi
    if [ -z "$ONLY" ] || [ "$ONLY" = recover ]; then
        run_recover
    fi

    cleanup_fake_home
    if [ "$(real_config_snapshot)" = "$REAL_SNAPSHOT" ]; then
        log_result "real_config_untouched" "PASS" "~/.config/rslite 快照未变"
    else
        log_result "real_config_untouched" "FAIL" "~/.config/rslite 快照改变；立即人工核对"
    fi
    print_summary
    if grep -Fq "$(printf '\t')FAIL$(printf '\t')" "$SUMMARY_FILE"; then
        exit 1
    fi
    # NOT_EVALUABLE（时钟原点未对齐）不是误报为通过；调用者须补共同时间锚点后再验收 Done 3。
    if grep -Fq "$(printf '\t')NOT_EVALUABLE$(printf '\t')" "$SUMMARY_FILE"; then
        exit 2
    fi
}

main
