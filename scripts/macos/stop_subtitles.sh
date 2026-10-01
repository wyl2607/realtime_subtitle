#!/usr/bin/env bash
# 停止实时字幕程序（优先优雅退出，超时再强杀）
#
# 用法：bash scripts/macos/stop_subtitles.sh
#
# 对应 scripts/windows/stop_subtitles.ps1，逐条照抄它的语义：
#   写 .stop → 主程序 QTimer 看到后走 app.quit → stop() 关线程/模型
#   等 5 秒 → 还不退就**只杀那一个 PID**
# ☠️ 优雅退出正常 1-2 秒（积压任务直接丢弃、在飞流式翻译会被打断），但 .stop
# 轮询 + 在飞识别 + Ollama 卸载 HTTP 叠加时会顶到 3 秒边缘，所以留 5 秒。
# 退得快照样立即返回（250ms 一查）。
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"
# shellcheck source=scripts/macos/_identity.sh
. "$RTS_SCRIPT_DIR/_identity.sh"

GRACE_SECONDS=5
stopped=0

# RTS_QUIET=1 是给 deps_release_for_pip 用的：它已经在自己那段话里解释过
# 为什么要停了，这里再吵一遍只会把 pip 失败的真实原因埋掉。
say()  { [ -n "${RTS_QUIET:-}" ] || printf '%s\n' "$*"; }
ok()   { [ -n "${RTS_QUIET:-}" ] || printf '%s\n' "${_RTS_GRN}$*${_RTS_OFF}"; }
warn() { [ -n "${RTS_QUIET:-}" ] || printf '%s\n' "${_RTS_YEL}$*${_RTS_OFF}" >&2; }

# 250ms 一查：程序退完立刻返回，不多等
wait_process_exit() {
    local pid="$1" limit="$2" i
    for ((i = 0; i < limit * 4; i++)); do
        if ! proc_alive "$pid"; then return 0; fi
        sleep 0.25
    done
    ! proc_alive "$pid"
}

# 请求优雅退出，等宽限期，还不走就强杀那一个 PID。
# ☠️ 强杀前再确认一次身份：PID 会被系统回收复用，宽限期里这个 PID 可能已经
# 变成别的进程了（Windows 那边的创建时间校验对应这里）。
stop_one_instance() {
    local pid="$1" recorded="${2:-}"
    : > "$STOP_FLAG"
    say "正在请求优雅退出 (PID ${pid}，最多等 ${GRACE_SECONDS}s)..."
    if wait_process_exit "$pid" "$GRACE_SECONDS"; then
        ok "已优雅停止实时字幕程序 (PID $pid)"
        return 0
    fi
    if identity_is_ours "$pid" "$recorded"; then
        kill -9 "$pid" 2>/dev/null
        say "优雅退出超时，已强制停止 (PID $pid)"
    else
        say "等待期间进程身份已变化，放弃强杀 (PID $pid)"
    fi
    return 0
}

if [ -f "$PID_FILE" ]; then
    pid="$(read_identity_pid "$PID_FILE" || true)"
    recorded="$(read_identity_field "$PID_FILE" start_time || true)"
    if [ -n "$pid" ] && identity_is_ours "$pid" "$recorded"; then
        stop_one_instance "$pid" "$recorded"
        stopped=1
    elif [ -n "$pid" ] && proc_alive "$pid"; then
        say "subtitle.pid 里的 PID $pid 不是当前实时字幕实例（可能已被系统复用，或是离线任务），忽略并清理。"
    elif [ -n "$pid" ]; then
        say "subtitle.pid 里的 PID $pid 已不存在，忽略并清理。"
    fi
    rm -f "$PID_FILE"
fi

if [ "$stopped" -eq 0 ]; then
    # pid 文件丢了/对不上：按进程身份找（解释器 = 本仓库 venv + 入口 = main.py）。
    # ☠️ 千万别退化成"按名字杀"——同一个 venv 里可能还有用户的离线任务在跑。
    while read -r pid; do
        [ -n "$pid" ] || continue
        say "subtitle.pid 缺失，按进程身份找到实时字幕 (PID $pid)，正在请求优雅退出..."
        stop_one_instance "$pid" ""
        stopped=1
    done < <(find_realtime_instances)
fi

if [ "$stopped" -eq 0 ]; then
    say "没有找到正在运行的实时字幕程序"
fi

# 卸载 Ollama 里常驻的翻译模型：光停 python 不会通知 Ollama，模型会按
# keep_alive（默认可达数小时）继续占着统一内存，"关了字幕但内存没释放"。
# 只卸载「确实加载着」的本项目模型（/api/ps），不碰其它程序的模型。
# ☠️ 不能用 `ollama stop` CLI：服务没在运行时它会自己拉起服务并无限期等待，
# 开机后程序没启动就点停止脚本，窗口就永远关不掉。走 HTTP：不可达 2 秒即知。
if curl -fsS --max-time 2 "$(ollama_url)/api/version" >/dev/null 2>&1; then
    if venv_python_exists; then
        # ☠️ 别用 mapfile 往数组里灌：macOS 自带的是 bash 3.2，那儿没有它
        # （`/bin/bash --version` → 3.2.57）。下面这套 while + 变量才是 3.2 版的写法。
        ours="$(rts_read_config OLLAMA_MODEL GAME_MODE_OLLAMA_MODEL)"
        while IFS= read -r hit; do
            [ -n "$hit" ] || continue
            matched=""
            # 名字精确匹配 + 前缀匹配：config 写 "qwen3.5" 时 /api/ps 可能报
            # "qwen3.5:latest"，只做相等会漏卸
            while IFS= read -r model; do
                [ -n "$model" ] || continue
                case "$hit" in
                    "$model"|"$model":*) matched="$model"; break ;;
                esac
            done <<EOF_MODELS
$ours
EOF_MODELS
            [ -n "$matched" ] || continue
            if ollama_unload "$hit"; then
                ok "已卸载 Ollama 常驻模型 $hit"
            else
                warn "卸载 $hit 失败（Ollama 正忙？内存最多占用到 keep_alive 到期）"
            fi
        done <<EOF_LOADED
$(ollama_loaded_models)
EOF_LOADED
    fi
else
    info "Ollama 服务没在运行，跳过卸载（它本来就没占着内存）"
fi

# 清掉暂停/停止标记，避免下次启动误判
rm -f "$STOP_FLAG" "$PAUSE_FLAG"

# 验收：确认本项目 venv 下没有残留的 python 进程。
# ☠️ 要给宽限期，不能查一次就报。优雅退出真正生效之后进程有一小段收尾尾巴，
# 查一次必然撞上，于是每次正常停止都打一条「这不该发生」吓用户。
# 轮询 8 秒兜住这个抖动；退干净就立刻返回，所以这 8 秒只有**真残留**时才会付。
leftover=""
i=0
while [ "$i" -lt 32 ]; do
    leftover="$(find_realtime_instances | tr '\n' ' ')"
    [ -z "$leftover" ] && break
    sleep 0.25
    i=$((i + 1))
done
if [ -n "$leftover" ]; then
    say ""
    warn "⚠️  停止后仍有本项目的 python 进程残留（PID: ${leftover// /,}）。"
    warn "   这不该发生。请把这行连同 subtitle.log 发给 AI（可能占着音频设备，"
    warn "   下次启动会提示「已经在运行」）。手动结束: kill -9 ${leftover// /,}"
fi

exit 0

