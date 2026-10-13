#!/usr/bin/env bash
# ============================================================
# Tailscale 保活守护（TSKA-001）
#
# 行为：
#   - 以 TAILSCALE_BE_CLI=1 运行 tailscale status --json（App Store 版二进制在无 SHLVL
#     环境下会按 GUI 启动，打印报错且 rc=0；该环境变量强制 CLI 模式）
#   - PATH 含 /Applications/Tailscale.app/Contents/MacOS 与 /opt/homebrew/bin
#   - 解析 BackendState：
#       "Stopped"    -> 执行 tailscale up（不加任何会改配置的参数），记一行带时间的日志
#       "NeedsLogin" -> 只记日志，不动作
#       其他状态     -> 不动作
#   - 日志：追加到 ~/Library/Logs/rs-node/ts_keepalive.log
#   - 日志截断：超过 1MB 时保留最后 200 行
# ============================================================
# macOS 自带 bash 3.2：不用关联数组等 4.0+ 特性
# ============================================================

set -euo pipefail

# PATH：launchd 的默认 PATH 不含 Tailscale.app 与 Homebrew
# 兼容测试桩：RS_TOOL_PATH_PREFIX 允许测试注入优先路径
export PATH="${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH"

LOG_DIR="$HOME/Library/Logs/rs-node"
LOG_FILE="$LOG_DIR/ts_keepalive.log"
MAX_LOG_BYTES=1048576   # 1MB
KEEP_LINES=200

mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true

# 截断日志：文件超过 1MB 只保留最后 200 行
maybe_truncate_log() {
    if [ ! -f "$LOG_FILE" ]; then
        return 0
    fi
    local size
    size=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
        tail -n "$KEEP_LINES" "$LOG_FILE" > "$LOG_FILE.tmp.$$" 2>/dev/null || return 0
        mv "$LOG_FILE.tmp.$$" "$LOG_FILE"
    fi
}

log() {
    local level="$1"
    shift
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    printf '[%s] [%s] %s\n' "$ts" "$level" "$*" >> "$LOG_FILE"
    maybe_truncate_log
}

# 取 Tailscale 状态 JSON
get_ts_status() {
    # 停止状态下 status 可能返回非零但仍输出 JSON：不看退出码，只看有没有拿到内容
    local out
    out=$(TAILSCALE_BE_CLI=1 tailscale status --json 2>/dev/null || true)
    [ -n "$out" ] || return 1
    printf '%s\n' "$out"
}

main() {
    local status_json
    status_json=$(get_ts_status) || {
        log "ERROR" "tailscale status --json 失败（未安装、未登录或命令不可用）"
        exit 1
    }

    local backend_state
    backend_state=$(printf '%s\n' "$status_json" | python3 -I -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("BackendState", ""))
except Exception:
    print("")
' 2>/dev/null || echo "")

    case "$backend_state" in
        "Stopped")
            log "WARN" "Tailscale BackendState=Stopped -> 执行 tailscale up"
            TAILSCALE_BE_CLI=1 tailscale up 2>&1 | while IFS= read -r line; do
                log "INFO" "tailscale up: $line"
            done
            ;;
        "NeedsLogin")
            log "WARN" "Tailscale BackendState=NeedsLogin -> 需要人工登录，不自动 up"
            ;;
        "Running")
            # 正常，静默
            ;;
        *)
            log "INFO" "Tailscale BackendState=$backend_state（未处理）"
            ;;
    esac
}

main "$@"