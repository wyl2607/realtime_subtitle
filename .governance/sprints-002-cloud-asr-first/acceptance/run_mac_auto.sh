#!/bin/bash
# macOS 自动验收脚本 for v3.2.0。
# 兼容 macOS 自带 bash 3.2；不运行时不修改用户的 rslite 配置。

set -u

REAL_HOME="$HOME"
DATA="$REAL_HOME/projects/rs-mac-native-data"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PYTHON="$REPO/venv/bin/python"

OUT_DIR=""
ONLY=""
SKIP_PYTEST=false
FAKE=""

usage() {
    printf '%s\n' '用法: run_mac_auto.sh [--out DIR] [--only gates|docs|fallback] [--skip-pytest]' >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
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
        --skip-pytest)
            SKIP_PYTEST=true
            shift
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

case "$ONLY" in
    ""|gates|docs|fallback) ;;
    *) usage; exit 1 ;;
esac

if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$DATA/accept-v320/$(date +'%Y%m%d-%H%M%S')"
fi

cd "$REPO" || exit 1
mkdir -p "$OUT_DIR" || exit 1
SUMMARY_FILE="$OUT_DIR/summary.tsv"
printf '项目名\t结果\t原因\n' > "$SUMMARY_FILE"

log_result() {
    local name="$1"
    local result="$2"
    local reason="$3"
    printf '%s %s %s\n' "$result" "$name" "$reason"
    printf '%s\t%s\t%s\n' "$name" "$result" "$reason" >> "$SUMMARY_FILE"
}

cleanup_fake_home() {
    if [ -n "$FAKE" ] && [ -d "$FAKE" ]; then
        rm -f "$FAKE/.config/rslite/groq.key"
        rm -rf "$FAKE"
    fi
    FAKE=""
}

# $1=yes 时才复制真实节点资料；每个 fallback 场景均会先重建这个目录。
setup_fake_home() {
    local copy_nodes="$1"
    local token_source
    local token_target

    cleanup_fake_home
    FAKE="$OUT_DIR/fakehome"
    mkdir -m 700 "$FAKE" || return 1
    mkdir -p "$FAKE/.config"
    chmod 700 "$FAKE/.config"
    mkdir -m 700 "$FAKE/.config/rslite" || return 1

    if [ "$copy_nodes" != "yes" ]; then
        return 0
    fi

    if [ -f "$REAL_HOME/.config/rslite/nodes.json" ]; then
        cp -p "$REAL_HOME/.config/rslite/nodes.json" "$FAKE/.config/rslite/nodes.json" || return 1
        chmod 600 "$FAKE/.config/rslite/nodes.json"
    fi

    if [ -d "$REAL_HOME/.config/rslite/tokens" ]; then
        mkdir -m 700 "$FAKE/.config/rslite/tokens" || return 1
        for token_source in "$REAL_HOME/.config/rslite/tokens/"*; do
            [ -f "$token_source" ] || continue
            token_target="$FAKE/.config/rslite/tokens/$(basename "$token_source")"
            cp -p "$token_source" "$token_target" || return 1
            chmod 600 "$token_target"
        done
    fi
}

run_quality_gates() {
    local log_file
    local rc

    if [ "$SKIP_PYTEST" = true ]; then
        log_result "gates_pytest" "SKIP" "--skip-pytest"
    else
        log_file="$OUT_DIR/gates_pytest.log"
        TMPDIR=/tmp "$PYTHON" -m pytest -q -p no:cacheprovider --basetemp=/tmp/rs-accept-pytest > "$log_file" 2>&1
        rc=$?
        if [ "$rc" -eq 0 ]; then
            log_result "gates_pytest" "PASS" "pytest 退出码 0"
        else
            log_result "gates_pytest" "FAIL" "pytest 退出码 $rc；见 $log_file"
        fi
    fi

    log_file="$OUT_DIR/gates_ruff.log"
    "$REPO/venv/bin/ruff" check . > "$log_file" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log_result "gates_ruff" "PASS" "ruff check 退出码 0"
    else
        log_result "gates_ruff" "FAIL" "ruff 退出码 $rc；见 $log_file"
    fi

    log_file="$OUT_DIR/gates_swift_build.log"
    (cd "$REPO/macos-native" && swift build -c release) > "$log_file" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log_result "gates_swift_build" "PASS" "swift build -c release 退出码 0"
    else
        log_result "gates_swift_build" "FAIL" "Swift build 退出码 $rc；见 $log_file"
    fi

    log_file="$OUT_DIR/gates_selftest.log"
    HOME="$FAKE" "$REPO/macos-native/.build/release/rslite" --selftest > "$log_file" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "gates_selftest" "FAIL" "rslite --selftest 退出码 $rc；见 $log_file"
    elif grep -Fq 'SELFTEST OK' "$log_file"; then
        log_result "gates_selftest" "PASS" "输出含 SELFTEST OK"
    else
        log_result "gates_selftest" "FAIL" "输出不含 SELFTEST OK；见 $log_file"
    fi

    log_file="$OUT_DIR/gates_tokens.log"
    python3 scripts/design/gen_tokens.py --check > "$log_file" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log_result "gates_tokens" "PASS" "设计令牌同步检查退出码 0"
    else
        log_result "gates_tokens" "FAIL" "设计令牌检查退出码 $rc；见 $log_file"
    fi
}

DOC_ERROR=""
require_readme_text() {
    local file="$1"
    local label="$2"
    local needle="$3"
    if ! grep -Fq -- "$needle" "$file"; then
        DOC_ERROR="$file 缺少 $label（$needle）"
        return 1
    fi
}

check_documentation() {
    if ! grep -Fq '__version__ = "3.2.0"' realtime_subtitle/version.py; then
        log_result "docs" "FAIL" "realtime_subtitle/version.py 不是 3.2.0"
        return 1
    fi

    require_readme_text README.md 'Groq' 'Groq' &&
        require_readme_text README.md '云端关闭命令' '--cloud on|off' &&
        require_readme_text README.md '隐私说明' '隐私承诺' &&
        require_readme_text README.md '数据保留说明' '数据保留' &&
        require_readme_text README.md 'key 配置命令' '--set-groq-key' &&
        require_readme_text README.en.md 'Groq' 'Groq' &&
        require_readme_text README.en.md 'cloud-off command' '--cloud on|off' &&
        require_readme_text README.en.md 'privacy statement' 'Privacy commitment' &&
        require_readme_text README.en.md 'data-retention statement' 'data retention' &&
        require_readme_text README.en.md 'key command' '--set-groq-key' &&
        require_readme_text README.de.md 'Groq' 'Groq' &&
        require_readme_text README.de.md 'Cloud-off-Befehl' '--cloud on|off' &&
        require_readme_text README.de.md 'Datenschutzangabe' 'Datenschutzversprechen' &&
        require_readme_text README.de.md 'Datenspeicherungsangabe' 'Datenspeicherung' &&
        require_readme_text README.de.md 'Schlüsselbefehl' '--set-groq-key'
    if [ "$?" -ne 0 ]; then
        log_result "docs" "FAIL" "$DOC_ERROR"
        return 1
    fi

    log_result "docs" "PASS" "version 与三种语言 README 的 Groq、关闭、隐私/保留、key 判据均存在"
}

check_events() {
    local scenario="$1"
    local events_file="$2"
    local stderr_file="$3"
    local fake_key="$4"

    "$PYTHON" - "$scenario" "$events_file" "$stderr_file" "$fake_key" <<'PY'
import json
import sys

scenario, events_path, stderr_path, fake_key = sys.argv[1:]
try:
    with open(events_path, encoding="utf-8") as fh:
        raw_events = fh.read()
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    with open(stderr_path, encoding="utf-8") as fh:
        stderr = fh.read()
except (OSError, ValueError) as exc:
    raise SystemExit("cannot parse events/stderr: %s" % exc)

statuses = [event.get("text", "") for event in events if event.get("ev") == "status"]
node_final = any(
    event.get("ev") == "final" and event.get("text", "").startswith("[")
    for event in events
)
any_final = any(event.get("ev") == "final" for event in events)

if scenario == "bad-key":
    if "模式：云端 key 无效" not in statuses:
        raise SystemExit("missing status: 模式：云端 key 无效")
    if not node_final:
        raise SystemExit("missing node final event")
    if fake_key and (fake_key in raw_events or fake_key in stderr):
        raise SystemExit("fake key appears in events or stderr")
elif scenario == "cloud-off":
    if "模式：云端·Groq" in statuses:
        raise SystemExit("cloud-off emitted 模式：云端·Groq")
    if not node_final and "rslite.route select node=" not in stderr:
        raise SystemExit("missing node final and select node route log")
elif scenario == "node-down":
    if "rslite.route select local reason=no_available_node" not in stderr:
        raise SystemExit("missing no_available_node route log")
    if not any_final:
        raise SystemExit("missing final event")
    if any(text.startswith("模式：混合·") for text in statuses):
        raise SystemExit("node-down emitted 模式：混合·")
else:
    raise SystemExit("unknown scenario: %s" % scenario)

print("events validated")
PY
}

check_score() {
    local score_file="$1"
    "$PYTHON" - "$score_file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        result = json.load(fh)
except (OSError, ValueError) as exc:
    raise SystemExit("cannot parse score JSON: %s" % exc)

if not isinstance(result, dict) or result.get("problems") != []:
    raise SystemExit("score problems is not an empty list: %r" % result.get("problems"))
final_wer = result.get("final_wer")
if not isinstance(final_wer, (int, float)):
    raise SystemExit("score final_wer is missing or non-numeric")
print("final_wer=%s" % final_wer)
PY
}

run_fallback_test() {
    local scenario="$1"
    local scenario_dir="$OUT_DIR/$scenario"
    local events_file="$scenario_dir/events.jsonl"
    local stderr_file="$scenario_dir/stderr.log"
    local config_log="$scenario_dir/config.log"
    local score_file="$scenario_dir/score.json"
    local score_log="$scenario_dir/score.stderr.log"
    local timeout_sec=240
    local pid
    local elapsed=0
    local rc
    local random_string=""
    local fake_key=""
    local event_result
    local score_result

    mkdir -p "$scenario_dir" || {
        log_result "fallback_$scenario" "FAIL" "无法创建 $scenario_dir"
        return 1
    }

    case "$scenario" in
        bad-key|cloud-off) setup_fake_home yes ;;
        node-down) setup_fake_home no ;;
        *) log_result "fallback_$scenario" "FAIL" "未知场景"; return 1 ;;
    esac
    if [ "$?" -ne 0 ]; then
        log_result "fallback_$scenario" "FAIL" "无法重建 fakehome"
        return 1
    fi

    # 配置命令会在 main.swift 中优先执行并退出，必须和回放命令分开运行。
    case "$scenario" in
        bad-key)
            random_string="$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 40)"
            fake_key="gsk_$random_string"
            printf '%s\n' "$fake_key" | HOME="$FAKE" "$REPO/macos-native/.build/release/rslite" --set-groq-key > "$config_log" 2>&1
            rc=$?
            if [ "$rc" -ne 0 ]; then
                log_result "fallback_bad-key" "FAIL" "--set-groq-key 退出码 $rc；见 $config_log"
                return 1
            fi
            HOME="$FAKE" "$REPO/macos-native/.build/release/rslite" --cloud on >> "$config_log" 2>&1
            rc=$?
            if [ "$rc" -ne 0 ]; then
                log_result "fallback_bad-key" "FAIL" "--cloud on 退出码 $rc；见 $config_log"
                return 1
            fi
            ;;
        cloud-off)
            HOME="$FAKE" "$REPO/macos-native/.build/release/rslite" --cloud off > "$config_log" 2>&1
            rc=$?
            if [ "$rc" -ne 0 ]; then
                log_result "fallback_cloud-off" "FAIL" "--cloud off 退出码 $rc；见 $config_log"
                return 1
            fi
            ;;
    esac

    HOME="$FAKE" "$REPO/macos-native/.build/release/rslite" --mode hybrid --source "file:$DATA/concat5.wav" --headless > "$events_file" 2> "$stderr_file" &
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
        log_result "fallback_$scenario" "FAIL" "回放超时（${timeout_sec}s）"
        return 1
    fi
    wait "$pid"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "fallback_$scenario" "FAIL" "rslite 回放退出码 $rc；见 $stderr_file"
        return 1
    fi

    event_result="$(check_events "$scenario" "$events_file" "$stderr_file" "$fake_key" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "fallback_$scenario" "FAIL" "$event_result"
        return 1
    fi

    "$PYTHON" "$REPO/scripts/bench/hybrid_score.py" "$events_file" --refs "$DATA/refs.jsonl" --json > "$score_file" 2> "$score_log"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "fallback_$scenario" "FAIL" "hybrid_score.py 退出码 $rc；见 $score_log"
        return 1
    fi
    score_result="$(check_score "$score_file" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log_result "fallback_$scenario" "FAIL" "$score_result"
        return 1
    fi

    log_result "fallback_$scenario" "PASS" "$event_result；$score_result"
}

print_summary() {
    local name
    local result
    local reason
    printf '\n%s\n' '=== 验收汇总 ==='
    while IFS="$(printf '\t')" read -r name result reason; do
        printf '%-24s %-4s %s\n' "$name" "$result" "$reason"
    done < "$SUMMARY_FILE"
}

main() {
    # 单实例锁：rslite 与 Python 主程序不能和验收回放并行运行。
    if pgrep -f 'macos-native/.build/release/rslite' >/dev/null 2>&1 || \
       pgrep -f 'python.*main.py' >/dev/null 2>&1; then
        log_result "preflight" "FAIL" "检测到正在运行的 rslite 或 Python 主程序"
        print_summary
        exit 2
    fi

    trap 'cleanup_fake_home' EXIT HUP INT TERM
    setup_fake_home no || {
        log_result "preflight" "FAIL" "无法创建 fakehome"
        print_summary
        exit 1
    }

    if [ -z "$ONLY" ] || [ "$ONLY" = gates ]; then
        run_quality_gates
    fi
    if [ -z "$ONLY" ] || [ "$ONLY" = docs ]; then
        check_documentation
    fi
    if [ -z "$ONLY" ] || [ "$ONLY" = fallback ]; then
        run_fallback_test bad-key
        run_fallback_test cloud-off
        run_fallback_test node-down
    fi

    print_summary
    if grep -Fq "$(printf '\t')FAIL$(printf '\t')" "$SUMMARY_FILE"; then
        printf '%s\n' '验收失败：存在不通过的检查项'
        exit 1
    fi
    printf '%s\n' '所有检查项均通过'
}

main
