#!/bin/bash
# 暂停 / 继续字幕 —— 切换暂停（对应桌面的「暂停继续字幕.bat」）
#
# ☠️ 这个文件刻意只有十几行：找仓库 + 收尾的逻辑都在 _launcher.sh 里。
# 双击运行时 $0 就是这个文件的位置，靠它（以及桌面上的 .repo-root）定位仓库。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# 被安装脚本拷到桌面后，按自己的位置往上找就找不到了，旁边那个 .repo-root
# 才是那时候的真相来源（安装脚本写的绝对路径）
RTS_REPO_ROOT="$(head -n 1 "$HERE/.repo-root" 2>/dev/null || true)"
if [ -z "$RTS_REPO_ROOT" ] || [ ! -d "$RTS_REPO_ROOT" ]; then
    RTS_REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
fi
if [ ! -f "$RTS_REPO_ROOT/scripts/macos/_launcher.sh" ]; then
    printf '❌ 找不到字幕程序所在目录：%s\n' "$RTS_REPO_ROOT"
    printf '   重新运行 bash scripts/macos/install.sh 重新生成桌面启动器。\n'
    printf '按回车关闭'
    IFS= read -r _ || true
    exit 1
fi
. "$RTS_REPO_ROOT/scripts/macos/_launcher.sh"
rts_launcher pause_subtitles.sh auto

