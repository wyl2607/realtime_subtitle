#!/usr/bin/env bash
# ============================================================
# 一键更新：从 GitHub 拉最新版 + 按需同步依赖
#
# 用法：bash scripts/macos/update_subtitles.sh [--mirror] [--prune]
#
# 不会动的东西（都不在 git 里）：
#   config_local.py（个人配置）/ window_state.json（窗口位置）/ transcripts/（字幕记录）
# 更新后需要重启字幕（stop_subtitles.sh → start_subtitles.sh）才生效。
#
# 对应 scripts/windows/update_subtitles.ps1，那边每一条 ☠️ 注释的由来这里都成立
# （网络半通时 git 无限挂、依赖装不成不许把代码留在新版本上…），别"简化"掉。
# ============================================================
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"
# shellcheck source=scripts/macos/_identity.sh
. "$RTS_SCRIPT_DIR/_identity.sh"
# shellcheck source=scripts/macos/_deps.sh
. "$RTS_SCRIPT_DIR/_deps.sh"

mirror=""
prune=0
for arg in "$@"; do
    case "$arg" in
        --mirror) mirror=--mirror ;;
        --prune)  prune=1 ;;
        -h|--help)
            say "用法：bash scripts/macos/update_subtitles.sh [--mirror] [--prune]"
            say "  --mirror  依赖同步走清华 PyPI 镜像（与 install.sh 同参数）"
            say "  --prune   顺手卸载掉已经不在依赖清单里的包（见 scripts/prune_venv.py）"
            exit 0 ;;
        *) die "不认识的参数：${arg}（--help 看用法）" ;;
    esac
done

if ! command -v git >/dev/null 2>&1; then
    say "❌ 没有安装 git，无法自动更新。"
    say "   安装：brew install git   （或 https://git-scm.com/download/mac）"
    exit 1
fi
# ☠️ .git 可能是**文件**不是目录：worktree / 子模块里就是。别用 -d 判断。
if [ ! -e "$RTS_REPO_ROOT/.git" ]; then
    say "❌ 本目录不是 git 克隆（可能当初是解压 zip 装的），无法增量更新。"
    say "   建议重装：git clone https://github.com/wyl2607/realtime_subtitle.git"
    say "   然后运行 install.sh（个人配置 config_local.py 可以直接拷过去）"
    exit 1
fi

cd "$RTS_REPO_ROOT" || exit 1

# 版本号从 version.py 里正则抠出来。刻意不调 venv 的 python：更新脚本要能在
# venv 坏掉/还没建的时候照跑，多拉一个依赖不划算（version.py 保证是纯常量）
read_local_version() {
    sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' \
        "$RTS_REPO_ROOT/realtime_subtitle/version.py" 2>/dev/null | head -n 1
}

old_ver="$(read_local_version)"
old_ver="${old_ver:-?}"
old="$(git rev-parse HEAD 2>/dev/null | tr -d ' \n')"
say "当前版本：v$old_ver (${old:0:7})"
say "正在检查更新..."

# ☠️ git 默认**没有任何超时**：GitHub 连不上/网络半通时 HTTPS 连上了却一直不给
# 数据，git 就无限等下去。而"启动字幕"入口 = 更新 + 启动，每次双击都先走这里
# ——用户看到的是"双击没反应"，字幕永远起不来（更新失败本来是能跳过的）。
# 低速阈值：连续 N 秒低于 1KB/s 就放弃。git 要 LIMIT 和 TIME 都设了才启用，
# 用户自己设过的值不覆盖。
export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1000}"
export GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-20}"
# 公开仓库不需要凭据；真弹出用户名/密码提示（仓库地址被改、代理劫持）时，
# 从 .command 启动的终端里没人能输入，只会永远挂着——直接失败
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never

# fetch 和 merge 分开做：两种失败原因完全不同，用户该做的事也不同——连不上是
# 网络问题，合不上是本地改过文件。混成一句提示只会误导。
if ! git fetch --quiet; then
    say ""
    say "❌ 检查更新失败：连不上 GitHub（断网或网络很慢）。这次先不更新。"
    say "   中国大陆网络可以给 git 配代理，或者过一会儿再试。"
    exit 1
fi
if ! upstream="$(git rev-parse --verify --quiet '@{u}')" || [ -z "$upstream" ]; then
    say ""
    say "❌ 当前分支没有跟踪远端分支，不知道该更新到哪里。"
    say "   处理办法：git checkout master 回到主分支后重试（或把这行发给 AI 助手）。"
    exit 1
fi
if ! git merge --ff-only --quiet '@{u}'; then
    say ""
    say "❌ 更新失败。最常见原因：本地直接改过仓库文件，与新版本冲突。"
    say "   个人调参请写在 config_local.py（永远不会冲突），不要直接改 config.py。"
    say "   处理办法：把上面的报错原样发给你的 AI 助手；"
    say "   或手动运行 git stash 暂存本地改动后重试。"
    exit 1
fi
new="$(git rev-parse HEAD | tr -d ' \n')"
need_deps=0
if deps_need_install; then need_deps=1; fi
code_changed=0
[ "$old" != "$new" ] && code_changed=1

if [ "$code_changed" -eq 0 ] && [ "$need_deps" -eq 0 ]; then
    ok "✅ 已经是最新版本（代码和依赖都无需更新）。"
    exit 0
fi

# ☠️ 依赖没装成就不许把代码留在新版本上。以前 pip 失败（断网、被离线任务挡住）
# 时时代码已经 pull 下来了，启动脚本接着就用「新代码 + 旧依赖」把字幕拉起来：
# 新代码 import 一个还没装的包，或者撞上改了 API 的旧版本包，字幕直接起不来，
# 而且"更新失败就用现有版本照常启动"这条退路恰恰被它自己堵死了。
# 退回的是旧提交（--keep，不碰工作区里的本地改动；merge --ff-only 能成功就说明
# 本地改动和这次更新不重叠，退回去也一定不重叠）。指纹没写，下次启动会重来一遍。
undo_code_update() {
    local why="$1"
    if [ "$code_changed" -eq 0 ]; then
        say "❌ $why"
        say "   下次即使提交不变也会重试依赖。"
        return
    fi
    if git reset --keep --quiet "$old"; then
        say "❌ $why"
        say "   代码已退回 v$old_ver (${old:0:7})，和已装好的依赖保持一致，"
        say "   字幕照常能用。下次运行会自动重试这次更新。"
    else
        say "❌ $why"
        say "   ⚠️ 代码没能退回旧版本，新代码配旧依赖可能起不来。"
        say "   处理办法：把这几行发给 AI 助手，或手动运行 git reset --keep $old"
    fi
}

new_ver="$(read_local_version)"
new_ver="${new_ver:-?}"
say ""
if [ "$code_changed" -eq 1 ]; then
    if [ "$new_ver" != "$old_ver" ]; then
        say "版本：v$old_ver → v$new_ver"
    else
        say "版本：v${new_ver}（版本号未变，是修补更新）"
    fi
    say "本次更新内容："
    git log --oneline --no-decorate "$old..$new"
    say ""
else
    say "代码已经最新，但依赖上次安装失败或不完整，正在补装..."
fi

stopped_for_deps=0
if [ "$need_deps" -eq 1 ]; then
    # ☠️ venv 不一定存在：还没跑过 install.sh，或者被清理工具删过文件。
    # 以前这里直接执行一个不存在的解释器，用户只看到一屏报错，以为整个更新
    # 失败了——代码其实已经拉下来了。
    if ! venv_python_exists; then
        say "⚠️ 代码已经更新好了，但没找到 venv："
        say "   $(venv_python)"
        say "   依赖必须同步才能跑起来。跑一次安装脚本即可（幂等，会自动把缺的补上）："
        say "   bash scripts/macos/install.sh $mirror"
        exit 1
    fi
    # 先把 venv 腾出来再动依赖（为什么见 _deps.sh）
    if ! deps_release_for_pip; then
        undo_code_update "这个 venv 还被别的 python 进程占着，多半是还在跑的离线任务（YouTube下载加字幕），强行装依赖会半路失败，这次先不更新。"
        exit 1
    fi
    [ "${RTS_STOPPED_REALTIME:-0}" = "1" ] && stopped_for_deps=1
    say "正在同步依赖（可能需要几分钟）..."
    if ! deps_install "$mirror"; then
        # 最常见的失败（断网/镜像不可用）发生在下载阶段，那时还一个包都没换，
        # 退回代码就是完全一致的旧状态。安装阶段才失败（磁盘满等）时可能已换掉
        # 一部分包，退回代码只能尽力而为——但仍比新代码配旧依赖更接近能跑。
        undo_code_update "依赖安装失败，请检查网络后重试（国内网络加 --mirror 参数）。"
        exit 1
    fi
    deps_fingerprint_write
fi

# ☠️ 指纹只认依赖清单的**文本**，而安装命令从不卸载东西：从清单里删掉一行之后，
# 那个包和它拖来的传递依赖会在已装好的机器上永远留着。它们不会被 import，但会让
# 依赖列表和排障时的判断持续跑偏。默认只提示不动手——卸载是不可逆的，不该在
# "点一下更新"的流程里悄悄发生；真要清就显式加 --prune。
if [ "$need_deps" -eq 1 ] || [ "$prune" -eq 1 ]; then
    pruner="$RTS_REPO_ROOT/scripts/prune_venv.py"
    if venv_python_exists && [ -f "$pruner" ]; then
        if [ "$prune" -eq 1 ]; then
            "$(venv_python)" "$pruner" --yes
        else
            "$(venv_python)" "$pruner" --check
        fi
    fi
fi

if [ "$code_changed" -eq 1 ]; then
    changed="$(git diff --name-only "$old" "$new")"
    # ☠️ 必须按后缀匹配，不能写成 `contains install.sh`：git diff --name-only
    # 返回的是带目录的路径，逐字比较只会命中根目录那个转发壳。
    # install.sh 本身变了意味着"本机配置检测/启动器"可能要重新生成。
    if printf '%s\n' "$changed" | grep -qE '(^|/)install\.sh$'; then
        say "ℹ️ 安装脚本本身有更新，建议重跑一次 install.sh（会刷新桌面启动器/本机配置检测）"
    fi
fi

# 字幕正在运行的话提醒重启
if [ "$stopped_for_deps" -eq 1 ]; then
    # start_and_update_subtitles.sh 接着会把它拉起来；单独跑本脚本时要告诉用户
    say "ℹ️ 为了更新依赖已停掉字幕，再跑一次启动入口即可用上新版本。"
elif [ -f "$PID_FILE" ]; then
    run_pid="$(get_running_realtime_pid)"
    if [ -n "$run_pid" ]; then
        say "⚠️ 字幕正在运行——先 stop_subtitles.sh 再 start_subtitles.sh，新版本才生效。"
    fi
fi

say ""
say "🎉 更新完成。"

