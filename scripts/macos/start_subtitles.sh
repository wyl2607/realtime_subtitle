#!/usr/bin/env bash
# 启动实时双语字幕（系统声音 → Whisper 识别 → Ollama 翻译 → 悬浮窗）
#
# 用法：bash scripts/macos/start_subtitles.sh
#
# 对应 scripts/windows/start_subtitles.ps1，逐条照抄它的语义。
# ☠️ 两个平台有一处**根本性**差别，别的都对称：
#   Windows 的 subtitle.pid 记的是 venv 启动器存根，真正的程序是它的子进程；
#   macOS 的 venv/bin/python 就是真的解释器（没有存根），起进程拿到的 PID
#   直接就是程序本身。所以这里不需要"停存根顺带停子进程"那套推理，
#   但**别**因此放松身份校验：PID 照样会被系统回收复用。
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"
# shellcheck source=scripts/macos/_identity.sh
. "$RTS_SCRIPT_DIR/_identity.sh"

VENV_PY="$(venv_python)"
MAIN_PY="$RTS_REPO_ROOT/main.py"

if [ -f "$PID_FILE" ]; then
    old_pid="$(read_identity_pid "$PID_FILE" || true)"
    old_start="$(read_identity_field "$PID_FILE" start_time || true)"
    # PID 会被系统回收复用：光"这个 PID 有进程"不算数，创建时间也得对上
    if [ -n "$old_pid" ] && identity_is_ours "$old_pid" "$old_start"; then
        say "已经在运行中（PID ${old_pid}），不用重复启动。要重启请先运行 stop_subtitles.sh"
        exit 0
    fi
    rm -f "$PID_FILE"   # 残留的过期 pid 文件
fi
# ☠️ pid 文件没了不等于没在运行（跑测试误删过、用户手删过）。以前这里直接往下走：
# 先把正在写的 subtitle.log 截断，再起一个被单实例挡下秒退的新进程，最后照样
# 打印"已启动"。按进程身份再查一遍，查到就顺手把 pid 文件补上，这样停止/更新
# 脚本也重新认得它。
running="$(get_running_realtime_pid)"
if [ -n "$running" ]; then
    write_identity "$running"
    say "已经在运行中（PID ${running}，已补回 subtitle.pid），不用重复启动。要重启请先运行 stop_subtitles.sh"
    exit 0
fi

# venv 没建 = 还没跑安装（zip 拷贝/只 clone 就双击）。给人话别给 Python 栈
if [ ! -x "$VENV_PY" ]; then
    say "❌ 还没有安装运行环境（venv 不存在）。"
    say "   请先运行 bash scripts/macos/install.sh（或双击桌面「德语实时字幕」文件夹里的安装说明）。"
    exit 1
fi

# 清掉可能残留的暂停/停止标记，保证每次启动都是正常运行状态
rm -f "$PAUSE_FLAG" "$STOP_FLAG"

if ! ollama_exe >/dev/null 2>&1; then
    say "❌ 找不到 Ollama。请先安装: brew install ollama，或运行 bash scripts/macos/install.sh"
    exit 1
fi

say "检查 Ollama 服务..."
# ☠️ 轮询等就绪而不是固定睡几秒：Ollama 刚更新完/冷启动时可能十几秒才监听端口
if ! ollama_ensure_serving; then
    say "❌ 等了一分多钟 Ollama 服务还没就绪（等 launchd 服务 20 秒 + 自己兜底 serve 60 秒）。"
    say "   可能它正在更新或上次更新没装好——等更新完成后再运行一次即可。"
    say "   还不行的话，手动开个终端运行 ollama serve 看报什么错。"
    exit 1
fi

# 模型名从 config 读（config_local.py 里可能配了小模型），不要硬编码。
# ☠️ 只认带 RSCFG: 前缀的行：config_local.py 是 exec 进来的，里面随手一个
# print 就会混进 stdout，直接取第 1/2 行会把它当模型名拿去 `ollama pull`，
# 报出"网络/模型名过期"，真正的原因反而被盖住。
# 一次 python 调用读两个值（冷启动约 0.5 秒，起两次纯浪费），按行号取。
cfg="$(rts_read_config OLLAMA_MODEL WHISPER_MODEL)"
tx_model="$(printf '%s\n' "$cfg" | sed -n '1p')"
whisper_model="$(printf '%s\n' "$cfg" | sed -n '2p')"
if [ -z "$tx_model" ]; then
    say "❌ 读取配置失败（上面几行是 Python 的原始报错）。"
    say "   最常见原因：config_local.py 写坏了（语法错误/拼写错误）。"
    say "   修好它、或暂时改名成 config_local.py.bak 之后再启动。"
    exit 1
fi

# 首次启动检测：Whisper 模型还没下载过（HF 缓存里没有）就明确告知要等几分钟。
# 后台启动 + 下载无进度条，不提示的话新用户会以为"双击没反应"
first_run=0
if ! whisper_model_cached "$whisper_model"; then
    first_run=1
fi

if ! ollama_model_present "$tx_model"; then
    if ! ollama_pull "$tx_model"; then
        say "❌ 拉取翻译模型 $tx_model 失败（网络/磁盘/模型名过期）。"
        say "   模型名会随 qwen 迭代变化：到 https://ollama.com/library 查当前名字，"
        say "   写进 config_local.py 的 OLLAMA_MODEL 后重试。国内网络不稳就多试一次。"
        exit 1
    fi
fi

say "启动实时字幕..."

# ☠️ 日志不能一启动就截断。日志里有"概况"诊断行（识别/翻译分位数、缓冲分桶…），
# 要靠跨天累积的数据来判断该不该调参数——每次启动清空的话，重启一次或关一次机
# 就前功尽弃。所以启动前先把上一份挪进 logs/ 存档，只保留最近 30 份。
log_dir="$RTS_REPO_ROOT/logs"
mkdir -p "$log_dir"
for name in subtitle.log subtitle.err.log; do
    cur="$RTS_REPO_ROOT/$name"
    if [ -s "$cur" ]; then
        base="${name%.log}"
        mv -f "$cur" "$log_dir/$base-$(now_stamp).log" 2>/dev/null || true
    fi
done
# ☠️ 两个前缀要分别裁剪，不能只写 "subtitle-*.log"。归档名来自上面的 $base，
# stderr 那份是 "subtitle.err-<时间戳>.log"——它**不匹配** `subtitle-*`（中间是
# `.err`），于是 Windows 上 err 归档从 2026-08-04 起一份都没被删过。这里照抄那个
# 按前缀分开裁的写法，别"顺手简化"。
# 各留 30 份而不是合起来 30 份：一次运行产出一对，合并计数会让 err 少的那边
# 把 stdout 的历史挤掉。
for prefix in subtitle subtitle.err; do
    find "$log_dir" -maxdepth 1 -type f -name "$prefix-*.log" -print 2>/dev/null \
        | while IFS= read -r f; do
            case "$(basename "$f")" in
                "$prefix"-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].log) ;;
                *) continue ;;
            esac
            printf '%s\t%s\n' "$(stat -f '%m' "$f")" "$f"
        done | sort -rn | tail -n +31 | cut -f2- | while IFS= read -r old; do
            rm -f "$old"
        done
done

# 后台启动 + 输出写进日志文件。
# ☠️ 三件事缺一不可，缺了症状都很难认：
#   nohup          终端窗口关闭时（.command 就是这么用的）收到 SIGHUP 会连带死掉
#   </dev/null     否则它会一直等 stdin，Finder 双击时 stdin 是关着的
#   追加而不是覆盖 见上面那段归档（vital：概况行要跨次启动累积）
cd "$RTS_REPO_ROOT" || exit 1
nohup "$VENV_PY" -u "$MAIN_PY" >> "$LOG_FILE" 2>> "$ERR_LOG_FILE" </dev/null &
proc=$!
# ☠️ nohup 是 exec 出去的，$! 就是 python 自己的 PID（不是存根），所以这里
# 记下来的 PID 可以直接拿去 kill —— 但仍然要过一遍身份校验再动手。
write_identity "$proc" || {
    rm -f "$PID_FILE"
    say "❌ 没能记录 subtitle.pid（写文件失败）。请检查仓库目录权限。"
    kill -9 "$proc" 2>/dev/null
    exit 1
}

# ☠️ 起来了不等于跑起来了：import 期的致命错误、以及单实例 mutex（另一份装在
# 别处的副本在跑——mutex 是全机的，上面的进程身份检查只认本仓库）都会让它
# 一两秒内就退出。以前不看，照样打印"已启动"，用户只看到窗口自动关掉、字幕没出现。
# 正常启动时这里只多等 2 秒（模型在后台加载，远没到会退出的时候）。
for _ in 1 2 3 4 5 6 7 8; do
    proc_alive "$proc" || break
    sleep 0.25
done
if ! proc_alive "$proc"; then
    rm -f "$PID_FILE"
    if grep -q "已经在运行" "$LOG_FILE" 2>/dev/null; then
        say "实时字幕已经在运行了（可能是装在别的目录的另一份），没有启动第二个。"
        exit 0
    fi
    say "❌ 字幕程序启动后立刻退出了。最后几行输出："
    cat "$LOG_FILE" "$ERR_LOG_FILE" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n 12 | while IFS= read -r line; do
        say "   $line"
    done
    say "   完整日志：subtitle.log / subtitle.err.log（可以直接发给 AI 排查）"
    exit 1
fi

ok "已启动 (PID $proc)，运行日志: subtitle.log"
if [ "$first_run" -eq 1 ]; then
    say ""
    say "⏬ 首次启动：字幕悬浮窗几秒内会先出现（带加载提示），同时在后台"
    say "   下载语音识别模型（1-3GB，视网速需要几分钟），下载完自动就绪。"
    say "   想看进度：另开一个终端 tail -f \"$LOG_FILE\"。"
    say "   （中国大陆网络若长时间无进展，参见 CLAUDE.md 的 HF_ENDPOINT 镜像设置）"
    sleep 10   # 首次启动多留几秒让人读完上面这段
else
    say "字幕悬浮窗几秒内出现，模型在后台继续加载（悬浮窗上有进度提示）。这个窗口马上自动关闭。"
fi
exit 0

