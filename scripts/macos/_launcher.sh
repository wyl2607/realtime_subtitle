#!/usr/bin/env bash
# 桌面启动器（launchers/*.command）的共用逻辑。
#
# ☠️ 为什么要有这么一层：五个 .command 文件必须**完全一致地**只做三件事
# （找仓库 → 调脚本 → 收尾），而"找仓库"这一步在两种位置下答案不同：
#   - 还在仓库里（scripts/macos/launchers/）：按自己的位置往上找两级
#   - 被拷到桌面（安装脚本干的）：旁边的 .repo-root 记着绝对路径
# 把这段逻辑写在每个 .command 里就是五份副本，改一处漏四处——所以收在这里，
# .command 本身只有几行。
#
# 对应 Windows 那侧的桌面 .bat：成功≈3 秒自动关，失败保留窗口让人看报错。
# 这里关窗口走 osascript（Terminal.app），关之前会核对"前一个窗口是不是我们
# 启动的这个 tty"——关错窗口比不关更糟。

# 用法： rts_launcher <脚本名> [auto|none|always] [额外参数...]
#   auto    成功就关窗口，失败时停一下等用户看完报错（默认）
#   none    一律不干预（脚本自己已经"按回车关闭"了，比如下载加字幕）
#   always  永远停一下（卸载：要让用户看到删了什么/还剩什么）
rts_launcher() {
    local script="$1" mode="${2:-auto}"
    shift 2 2>/dev/null || shift $#
    local code=0

    bash "$RTS_REPO_ROOT/scripts/macos/$script" "$@" || code=$?

    case "$mode" in
        none) return "$code" ;;
        always)
            say ""
            printf '按回车关闭'
            IFS= read -r _ || true
            return "$code"
            ;;
    esac

    if [ "$code" -eq 0 ]; then
        # Windows 那边成功是 ping -n 4（约 3 秒），留给人读完"已启动 (PID …)"
        sleep 3
        rts_close_own_terminal && return "$code"
        printf '（这个终端窗口可以关掉了：⌘W）\n'
    else
        printf '按回车关闭'
        IFS= read -r _ || true
    fi
    return "$code"
}

say() { printf '%s\n' "$*"; }

# 关掉**我们启动的这个** Terminal 窗口。
# 判据是 tty 对得上：Terminal 报的 front window 的 tty 等于我们的 $TTY 时才关。
# 对不上（比如用户切到了别的窗口）就放弃——关错窗口比留着窗口糟得多。
# 非 Terminal 的终端（iTerm/VS Code 内置终端）走不到这条，窗口留着让人自己关。
rts_close_own_terminal() {
    [ -n "${TTY:-}" ] || return 1
    command -v osascript >/dev/null 2>&1 || return 1
    local win_tty
    win_tty="$(osascript -e 'tell application "Terminal" to get tty of selected tab of front window' 2>/dev/null)" || return 1
    win_tty="$(printf '%s' "$win_tty" | tr -d '[:space:]')"
    [ -n "$win_tty" ] && [ "$win_tty" = "$TTY" ] || return 1
    osascript -e 'tell application "Terminal" to close (front window) saving no' >/dev/null 2>&1 || return 1
    return 0
}

