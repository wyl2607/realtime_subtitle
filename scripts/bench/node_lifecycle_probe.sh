#!/usr/bin/env bash
# TK-007 Done criteria 2 探针：从控制端只读观察节点 worker 的按需加载/回收。
#
# 用法：
#   bash scripts/bench/node_lifecycle_probe.sh <ssh-host> <wav>
#
# 远端只执行 ps，以及 tail/grep 节点日志；不 launchctl、不 kill、不改文件。
# rslite 的 JSONL stdout 会被即时脱敏，只保留事件类型、时间戳和是否节点结果。
set -euo pipefail

usage() {
    echo "usage: node_lifecycle_probe.sh <ssh-host> <wav>" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage

SSH_HOST=$1
WAV=$2
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN=${RSLITE_BIN:-"$ROOT/macos-native/.build/release/rslite"}
TMP_ROOT=/tmp/tk007
RUN_DIR="$TMP_ROOT/node_lifecycle_probe.$(date +%Y%m%d-%H%M%S).$$"
SANITIZER="$RUN_DIR/sanitize_events.py"
RSLITE_PID=
RSLITE_SANITIZER_PID=
RSLITE_EVENTS=
RSLITE_ERR=

GATEWAY_RSS_LIMIT_MB=50
COLD_REFINE_LIMIT_S=15
IDLE_REAP_MIN_S=100
IDLE_REAP_MAX_S=150
DISCONNECT_LIMIT_S=30
DISCONNECT_TOTAL_REAP_LIMIT_S=150
POLL_S=1

mkdir -p "$RUN_DIR"

cleanup() {
    if [ -n "${RSLITE_PID:-}" ]; then
        kill "$RSLITE_PID" 2>/dev/null || true
    fi
    if [ -n "${RSLITE_SANITIZER_PID:-}" ]; then
        kill "$RSLITE_SANITIZER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

case "$SSH_HOST" in
    *[!A-Za-z0-9._@-]* | "" )
        echo "错误：ssh-host 含空白或特殊字符，拒绝执行" >&2
        exit 2
        ;;
esac

[ -f "$WAV" ] || { echo "错误：找不到 wav：${WAV}" >&2; exit 2; }
[ -x "$BIN" ] || { echo "错误：找不到可执行 rslite：${BIN}" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "错误：需要 python3 解析 JSONL 事件" >&2; exit 2; }

cat > "$SANITIZER" <<'PY'
import json
import sys


def is_node_final(rec):
    if rec.get("ev") != "final":
        return False
    source = str(rec.get("source", ""))
    if source.startswith("node"):
        return True
    # 当前 headless 会把节点精修写成 "[nodeID] ..."；只用前缀判断，不输出正文。
    text = rec.get("text")
    return isinstance(text, str) and text.startswith("[") and "]" in text[:80]


def is_node_replace(rec):
    if rec.get("ev") != "replace":
        return False
    text = rec.get("text")
    return isinstance(text, str) and text.startswith("P5 node=")


for line in sys.stdin:
    try:
        rec = json.loads(line)
    except Exception:
        continue
    if not isinstance(rec, dict) or "ev" not in rec:
        continue
    out = {"ev": rec.get("ev"), "t": rec.get("t")}
    if "t0" in rec:
        out["t0"] = rec.get("t0")
    if "t1" in rec:
        out["t1"] = rec.get("t1")
    if is_node_final(rec):
        out["node_result"] = True
    if is_node_replace(rec):
        out["node_replace"] = True
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True)
PY

now_s() {
    python3 -c 'import time; print("%.3f" % time.monotonic())'
}

calc() {
    python3 - "$@" <<'PY'
import sys
expr = sys.argv[1]
try:
    print("%.3f" % eval(expr, {"__builtins__": {}}, {}))
except Exception:
    print("null")
PY
}

float_le() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a <= b) }'
}

float_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a >= b) }'
}

json_num() {
    case "${1:-}" in
        "" | null | n/a) printf 'null' ;;
        *) printf '%s' "$1" ;;
    esac
}

json_bool() {
    if [ "$1" = "PASS" ]; then
        printf 'true'
    else
        printf 'false'
    fi
}

status_of() {
    if [ "$1" -eq 0 ]; then
        printf 'PASS'
    else
        printf 'FAIL'
    fi
}

line_result() {
    local status=$1
    local name=$2
    shift 2
    printf '[%s] %s' "$status" "$name"
    if [ "$#" -gt 0 ]; then
        printf ' %s' "$*"
    fi
    printf '\n'
}

remote_ps_file() {
    local out=$1
    ssh -- "$SSH_HOST" ps -axo pid,rss,command > "$out"
}

proc_stats() {
    local ps_file=$1
    python3 - "$ps_file" <<'PY'
import sys

worker = 0
gateway = 0
gateway_rss_kb = 0
worker_pids = []
gateway_pids = []

with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
    for line in f:
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, rss, cmd = parts
        try:
            rss_i = int(rss)
        except ValueError:
            rss_i = 0
        if "realtime_subtitle.node.worker" in cmd:
            worker += 1
            worker_pids.append(pid)
        if "realtime_subtitle.node.gateway" in cmd:
            gateway += 1
            gateway_pids.append(pid)
            gateway_rss_kb = max(gateway_rss_kb, rss_i)

print("%d %d %.1f %s %s" % (
    worker,
    gateway,
    gateway_rss_kb / 1024.0,
    ",".join(worker_pids) or "-",
    ",".join(gateway_pids) or "-",
))
PY
}

get_proc_stats() {
    local label=$1
    local ps_file="$RUN_DIR/${label}.ps"
    remote_ps_file "$ps_file"
    proc_stats "$ps_file"
}

worker_count_from_stats() {
    printf '%s\n' "$1" | awk '{print $1}'
}

wait_worker_present() {
    local label=$1
    local limit_s=$2
    local start
    local now
    local elapsed
    local stats
    local workers
    start=$(now_s)
    while :; do
        stats=$(get_proc_stats "${label}_$(date +%s)")
        workers=$(worker_count_from_stats "$stats")
        now=$(now_s)
        elapsed=$(calc "${now} - ${start}")
        if [ "$workers" -gt 0 ]; then
            printf '%s %s\n' "$elapsed" "$stats"
            return 0
        fi
        if ! float_le "$elapsed" "$limit_s"; then
            printf 'null %s\n' "$stats"
            return 1
        fi
        sleep "$POLL_S"
    done
}

wait_worker_gone() {
    local label=$1
    local limit_s=$2
    local start
    local now
    local elapsed
    local stats
    local workers
    start=$(now_s)
    while :; do
        stats=$(get_proc_stats "${label}_$(date +%s)")
        workers=$(worker_count_from_stats "$stats")
        now=$(now_s)
        elapsed=$(calc "${now} - ${start}")
        if [ "$workers" -eq 0 ]; then
            printf '%s %s\n' "$elapsed" "$stats"
            return 0
        fi
        if ! float_le "$elapsed" "$limit_s"; then
            printf 'null %s\n' "$stats"
            return 1
        fi
        sleep "$POLL_S"
    done
}

first_refine_s() {
    local events=$1
    python3 - "$events" <<'PY'
import json
import sys

first = None
fallback = None
try:
    lines = open(sys.argv[1], encoding="utf-8", errors="replace")
except FileNotFoundError:
    print("null")
    raise SystemExit

with lines:
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = rec.get("t")
        if not isinstance(t, (int, float)):
            continue
        if rec.get("node_result"):
            first = t
            break
        if fallback is None and rec.get("node_replace"):
            fallback = t
print("null" if first is None and fallback is None else "%.3f" % (first if first is not None else fallback))
PY
}

wav_timeout_s() {
    python3 - "$WAV" <<'PY'
import sys
import wave

try:
    with wave.open(sys.argv[1]) as w:
        dur = w.getnframes() / float(w.getframerate())
    print(int(dur + 90))
except Exception:
    print(240)
PY
}

start_rslite() {
    local name=$1
    local fifo="$RUN_DIR/${name}.fifo"
    RSLITE_EVENTS="$RUN_DIR/${name}.events.meta.jsonl"
    RSLITE_ERR="$RUN_DIR/${name}.stderr.log"
    mkfifo "$fifo"
    python3 -u "$SANITIZER" < "$fifo" > "$RSLITE_EVENTS" &
    RSLITE_SANITIZER_PID=$!
    "$BIN" --headless --source "file:${WAV}" --mode hybrid > "$fifo" 2> "$RSLITE_ERR" &
    RSLITE_PID=$!
}

pid_alive() {
    local pid=$1
    local state
    kill -0 "$pid" 2>/dev/null || return 1
    state=$(ps -p "$pid" -o stat= 2>/dev/null | tr -d ' ')
    [ -n "$state" ] || return 1
    case "$state" in
        Z*) return 1 ;;
        *) return 0 ;;
    esac
}

wait_local_pid() {
    local pid=$1
    local limit_s=$2
    local start
    local now
    local elapsed
    start=$(now_s)
    while pid_alive "$pid"; do
        now=$(now_s)
        elapsed=$(calc "${now} - ${start}")
        if ! float_le "$elapsed" "$limit_s"; then
            kill "$pid" 2>/dev/null || true
            sleep 2
            kill -9 "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            return 124
        fi
        sleep 1
    done
    wait "$pid"
}

remote_gateway_logs() {
    ssh -- "$SSH_HOST" 'tail -n 2000 "$HOME"/Library/Logs/rs-node/*.log 2>/dev/null | grep -E "session_(start|end)|worker_reaped" || true'
}

session_end_count() {
    grep -c 'session_end ' "$1" 2>/dev/null || true
}

last_session_end_dur() {
    python3 - "$1" <<'PY'
import re
import sys

last = None
try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except FileNotFoundError:
    text = ""
for m in re.finditer(r"session_end .*?dur_s=([0-9.]+)", text):
    last = m.group(1)
print(last or "null")
PY
}

read -r r0_workers r0_gateways r0_gateway_rss r0_worker_pids r0_gateway_pids <<EOF
$(get_proc_stats baseline)
EOF

if [ "$r0_workers" -eq 0 ]; then
    idle_worker_status=PASS
else
    idle_worker_status=FAIL
fi
line_result "$idle_worker_status" "idle_no_worker" "worker_count=${r0_workers} worker_pids=${r0_worker_pids}"

if [ "$r0_gateways" -gt 0 ] && float_le "$r0_gateway_rss" "$GATEWAY_RSS_LIMIT_MB"; then
    idle_gateway_status=PASS
else
    idle_gateway_status=FAIL
fi
line_result "$idle_gateway_status" "idle_gateway_rss" "gateway_count=${r0_gateways} rss_mb=${r0_gateway_rss} limit_mb=${GATEWAY_RSS_LIMIT_MB} gateway_pids=${r0_gateway_pids}"

run_timeout=$(wav_timeout_s)
start_rslite cold
cold_pid=$RSLITE_PID
cold_sanitizer=$RSLITE_SANITIZER_PID
cold_events=$RSLITE_EVENTS
cold_start=$(now_s)
worker_seen_s=null
cold_worker_seen_status=FAIL
if present_out=$(wait_worker_present cold_present "$COLD_REFINE_LIMIT_S"); then
    worker_seen_s=$(printf '%s\n' "$present_out" | awk '{print $1}')
    cold_worker_seen_status=PASS
fi

first_refine=null
while pid_alive "$cold_pid"; do
    first_refine=$(first_refine_s "$cold_events")
    [ "$first_refine" != "null" ] && break
    elapsed=$(calc "$(now_s) - ${cold_start}")
    float_le "$elapsed" "$COLD_REFINE_LIMIT_S" || break
    sleep "$POLL_S"
done

if [ "$first_refine" != "null" ] && float_le "$first_refine" "$COLD_REFINE_LIMIT_S"; then
    cold_refine_status=PASS
else
    cold_refine_status=FAIL
fi
line_result "$cold_refine_status" "cold_first_refine" "seconds=${first_refine} limit_s=${COLD_REFINE_LIMIT_S} worker_seen_s=${worker_seen_s} worker_seen=${cold_worker_seen_status}"

cold_rc=0
wait_local_pid "$cold_pid" "$run_timeout" || cold_rc=$?
wait "$cold_sanitizer" 2>/dev/null || true
RSLITE_PID=
RSLITE_SANITIZER_PID=
cold_end=$(now_s)

if [ "$cold_rc" -eq 0 ]; then
    cold_exit_status=PASS
else
    cold_exit_status=FAIL
fi
line_result "$cold_exit_status" "cold_rslite_exit" "exit_code=${cold_rc} timeout_s=${run_timeout}"

idle_exit_s=null
idle_status=FAIL
if gone_out=$(wait_worker_gone idle_reap "$IDLE_REAP_MAX_S"); then
    idle_exit_s=$(printf '%s\n' "$gone_out" | awk '{print $1}')
    if [ "$cold_worker_seen_status" = "PASS" ] && float_ge "$idle_exit_s" "$IDLE_REAP_MIN_S" && float_le "$idle_exit_s" "$IDLE_REAP_MAX_S"; then
        idle_status=PASS
    fi
fi
idle_since_cold_end=$(calc "$(now_s) - ${cold_end}")
line_result "$idle_status" "idle_reap_after_playback" "seconds=${idle_exit_s} expected_window_s=${IDLE_REAP_MIN_S}-${IDLE_REAP_MAX_S} observed_since_cold_end_s=${idle_since_cold_end}"

logs_before="$RUN_DIR/logs_before_disconnect.txt"
remote_gateway_logs > "$logs_before" || true
session_end_before=$(session_end_count "$logs_before")

start_rslite disconnect
disc_pid=$RSLITE_PID
disc_sanitizer=$RSLITE_SANITIZER_PID
disc_start=$(now_s)
disc_worker_seen_s=null
disc_worker_status=FAIL
if disc_present_out=$(wait_worker_present disconnect_present "$COLD_REFINE_LIMIT_S"); then
    disc_worker_seen_s=$(printf '%s\n' "$disc_present_out" | awk '{print $1}')
    disc_worker_status=PASS
fi

kill_ts=$(now_s)
kill -9 "$disc_pid" 2>/dev/null || true
wait "$disc_pid" 2>/dev/null || true
wait "$disc_sanitizer" 2>/dev/null || true
RSLITE_PID=
RSLITE_SANITIZER_PID=
kill_after_start_s=$(calc "${kill_ts} - ${disc_start}")

disconnect_detect_s=null
disconnect_status=FAIL
i=0
while [ "$i" -le 45 ]; do
    logs_after="$RUN_DIR/logs_after_disconnect_${i}.txt"
    remote_gateway_logs > "$logs_after" || true
    session_end_after=$(session_end_count "$logs_after")
    if [ "$session_end_after" -gt "$session_end_before" ]; then
        session_dur=$(last_session_end_dur "$logs_after")
        if [ "$session_dur" != "null" ]; then
            disconnect_detect_s=$(calc "${session_dur} - ${kill_after_start_s}")
            if float_ge "$disconnect_detect_s" 0 && float_le "$disconnect_detect_s" "$DISCONNECT_LIMIT_S"; then
                disconnect_status=PASS
            fi
        fi
        break
    fi
    i=$((i + 1))
    sleep "$POLL_S"
done
line_result "$disconnect_status" "disconnect_detected" "seconds_after_kill=${disconnect_detect_s} limit_s=${DISCONNECT_LIMIT_S} worker_seen_s=${disc_worker_seen_s} worker_seen=${disc_worker_status}"

disconnect_worker_exit_s=null
disconnect_reap_status=FAIL
if disc_gone_out=$(wait_worker_gone disconnect_reap "$DISCONNECT_TOTAL_REAP_LIMIT_S"); then
    disconnect_worker_exit_s=$(printf '%s\n' "$disc_gone_out" | awk '{print $1}')
    if [ "$disc_worker_status" = "PASS" ] && float_le "$disconnect_worker_exit_s" "$DISCONNECT_TOTAL_REAP_LIMIT_S"; then
        disconnect_reap_status=PASS
    fi
fi
line_result "$disconnect_reap_status" "disconnect_worker_reap" "seconds_after_kill=${disconnect_worker_exit_s} total_limit_s=${DISCONNECT_TOTAL_REAP_LIMIT_S}"

overall=PASS
for s in "$idle_worker_status" "$idle_gateway_status" "$cold_worker_seen_status" "$cold_refine_status" "$cold_exit_status" "$idle_status" "$disconnect_status" "$disconnect_reap_status" "$disc_worker_status"; do
    if [ "$s" != "PASS" ]; then
        overall=FAIL
    fi
done

printf '{"ok":%s,"idle_no_worker":%s,"idle_worker_count":%s,"gateway_rss_ok":%s,"gateway_rss_mb":%s,"cold_refine_ok":%s,"cold_refine_s":%s,"cold_worker_seen_s":%s,"idle_reap_ok":%s,"idle_reap_s":%s,"disconnect_detect_ok":%s,"disconnect_detect_s":%s,"disconnect_worker_seen_s":%s,"disconnect_reap_ok":%s,"disconnect_worker_exit_s":%s,"run_dir":"%s"}\n' \
    "$(json_bool "$overall")" \
    "$(json_bool "$idle_worker_status")" \
    "$r0_workers" \
    "$(json_bool "$idle_gateway_status")" \
    "$(json_num "$r0_gateway_rss")" \
    "$(json_bool "$cold_refine_status")" \
    "$(json_num "$first_refine")" \
    "$(json_num "$worker_seen_s")" \
    "$(json_bool "$idle_status")" \
    "$(json_num "$idle_exit_s")" \
    "$(json_bool "$disconnect_status")" \
    "$(json_num "$disconnect_detect_s")" \
    "$(json_num "$disc_worker_seen_s")" \
    "$(json_bool "$disconnect_reap_status")" \
    "$(json_num "$disconnect_worker_exit_s")" \
    "$RUN_DIR"
