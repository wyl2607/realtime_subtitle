#!/usr/bin/env bash
# 暂停/继续 实时字幕的识别与翻译（不重启进程，识别模型继续留在内存里，切回来不用重新加载）
#
# 用法：bash scripts/macos/pause_subtitles.sh
#
# 对应 scripts/windows/pause_subtitles.ps1：靠 .paused 这个文件本身当状态，
# 所以"双击两次"就是切换，不用脚本自己记状态——脚本进程活不过这次双击。
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"

if [ -f "$PAUSE_FLAG" ]; then
    rm -f "$PAUSE_FLAG"
    say "已继续识别与翻译"
else
    if [ ! -f "$PID_FILE" ]; then
        say "实时字幕程序当前没有在运行，暂停没有意义"
        exit 0
    fi
    : > "$PAUSE_FLAG"
    say "已暂停识别与翻译（悬浮窗还在，不会做识别）"
fi

