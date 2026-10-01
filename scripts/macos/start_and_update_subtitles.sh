#!/usr/bin/env bash
# ============================================================
# 启动字幕 —— 一次双击 = 拉最新版 + 起字幕
#
# 用法：bash scripts/macos/start_and_update_subtitles.sh [--mirror]
#
# ☠️ 文件名里还留着 and_update，但它现在就是**唯一的**启动入口：桌面「德语实时字幕」
# 文件夹里的「启动字幕.command」指向的就是这里（对应 Windows 侧 2026-09-20 把
# 「启动」「更新」「启动并更新」三个桌面入口合并成那一个 .bat）。
# 文件没跟着改名，是因为名字描述的是它**做什么**（先更新再启动），而
# start_subtitles.sh 这个名字已经被它调用的那个脚本占着了。
#
# 本脚本**只做流程编排**，三段实际工作全部交给现成脚本：
#   update_subtitles.sh →（必要时）stop_subtitles.sh → start_subtitles.sh
# 一行业务逻辑都不复制过来。那三个脚本里每一条 ☠️ 注释都是真踩出来的，
# 抄一份到这里只会漂移成两套行为。
#
# ☠️ 必须用子进程跑（bash "$script"），不能 source，两个理由都是硬的：
#   1. 那三个脚本里到处是 `exit 1`。source 的话它们的 exit 会把**本脚本**一起
#      退掉，后面的步骤根本跑不到——而"更新失败也要照常启动"正是本脚本存在的
#      全部理由。
#   2. git pull 可能刚好换掉 start_subtitles.sh 自己。子进程读的是**刚拉下来的
#      新版**；source 则本进程早已解析完毕，等于拿旧逻辑去启动新代码。
#      （本文件被 pull 换掉不受影响：bash 启动时整份已解析进内存，这一趟跑的
#        是旧的，下一趟才是新的。）
#
# 行为约定：
#   - 更新失败（断网 / git 冲突 / 压根不是 git 克隆装的）**不中断**，
#     打印原因后用当前已安装的版本照常启动。
#   - 只有"真拉到了新代码"且"字幕正在跑"时才停掉重启。没拉到新代码就
#     一根手指都不碰它。
# ============================================================
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"
# shellcheck source=scripts/macos/_identity.sh
. "$RTS_SCRIPT_DIR/_identity.sh"

# git 不可用、或这份不是 git 克隆装的（解压 zip）时输出空。调用方据此放弃
# "代码变没变"的判断，而不是把两个空值判成"没变"。
get_head_commit() {
    command -v git >/dev/null 2>&1 || return 0
    [ -e "$RTS_REPO_ROOT/.git" ] || return 0
    git -C "$RTS_REPO_ROOT" rev-parse HEAD 2>/dev/null | tr -d ' \n'
}

# ---------- 1. 更新 ----------
before="$(get_head_commit)"

update_code=0
bash "$RTS_SCRIPT_DIR/update_subtitles.sh" "$@" || update_code=$?

if [ "$update_code" -ne 0 ]; then
    say ""
    warn "⚠️ 更新没能完成（原因见上面几行），改用当前已装好的版本继续启动。"
    warn "   常见原因：断网、直接改过仓库文件导致冲突、这份不是 git 克隆装的。"
    sleep 3
fi

after="$(get_head_commit)"
# 两头都拿到 commit 才敢说"代码变了"。任何一头拿不到就一律当作没变：
# 宁可少重启一次（用户手动停一下就是了），也不能因为判断不了就去掐断
# 人家正在看的字幕。
code_changed=0
if [ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ]; then
    code_changed=1
fi

# ---------- 2. 需要的话先停掉旧进程 ----------
# pid 文件丢了也要认得出来：否则拉到新代码时旧实例不会被停，而启动脚本又会
# 因为"已经在运行"拒绝启动——新代码永远上不去。
running="$(get_running_realtime_pid)"

if [ "$code_changed" -eq 1 ] && [ -n "$running" ]; then
    say ""
    say "拉到了新版本，正在停掉当前字幕好让新版生效..."
    say "（识别模型和翻译模型都要重新加载，字幕会中断半分钟左右）"
    bash "$RTS_SCRIPT_DIR/stop_subtitles.sh" >/dev/null 2>&1 || true
elif [ -n "$running" ]; then
    say ""
    say "字幕已经在运行，而且没有拉到新代码——不打断它。"
fi

# ---------- 3. 启动 ----------
say ""
start_code=0
bash "$RTS_SCRIPT_DIR/start_subtitles.sh" || start_code=$?

if [ "$start_code" -eq 0 ] && [ "$code_changed" -eq 1 ]; then
    # 有更新时多留几秒，让人读得完上面 update 打的那段 git log。
    sleep 5
fi

exit "$start_code"

