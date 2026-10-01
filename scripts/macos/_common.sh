#!/usr/bin/env bash
# macOS 脚本共用的一层：仓库根、venv 解释器、日志、Ollama 就绪判断、读配置。
#
# 只被同目录的脚本 source，不直接执行。
#
# ☠️ 这里的东西和 Windows 那套（scripts/windows/_identity.ps1 等）是**一一对应**
# 的：跨进程文件约定（subtitle.pid / .stop / .paused / subtitle.log）由本目录的
# 脚本和 Python 双方各写一半，路径必须字字相同——Python 侧一律按
# realtime_subtitle/paths.py 的 REPO_ROOT 落点。CLAUDE.md 第 4 节第 28 条记的
# 就是上一次重构两边各算各的、暂停脚本失效一整天那次。
#
# 其它跨进程约定也照抄 Windows 那边的写法，别在 macOS 这边"顺手优化"：
#   - 读配置只认 stdout 里 `RSCFG:` 前缀的行（config_local.py 是 exec 进来的，
#     里面随手一个 print 就会混进 stdout，见 tests/test_config_stdout_contract.py）
#   - Ollama 一律走 http://127.0.0.1:11434，写主机名会付 IPv6 回环那两秒
#   - 卸载模型用 HTTP keep_alive=0，**不要**改用 `ollama stop` CLI

# ☠️ 整个文件关掉 SC2034：本文件是被 source 的，"某个变量在本文件里没被用到"
# 恰恰说明它是被别处用着的（下面那一批路径常量都是给其它脚本消费的）。
# 放在第一条命令之前 = 对整个文件生效。
# shellcheck disable=SC2034

# 入口脚本先算好这两条（见各入口开头的 RTS_SCRIPT_DIR / RTS_REPO_ROOT），
# source 本文件时复用，不要在这里重新推一遍。
: "${RTS_REPO_ROOT:?入口脚本必须先算出 RTS_REPO_ROOT 再 source 本文件}"

# ---------------------------------------------------------------- 输出

# 彩色只给"给人看的那几行"，判据一律靠退出码——退出码才是脚本之间的契约
# （start_and_update 要靠它判断"更新失败也要照常启动"），颜色会被重定向丢掉。
if [ -t 1 ]; then
    _RTS_DIM=$'\033[2m'; _RTS_RED=$'\033[31m'; _RTS_GRN=$'\033[32m'
    _RTS_YEL=$'\033[33m'; _RTS_OFF=$'\033[0m'
else
    _RTS_DIM=''; _RTS_RED=''; _RTS_GRN=''; _RTS_YEL=''; _RTS_OFF=''
fi

say()  { printf '%s\n' "$*"; }
info() { printf '%s\n' "${_RTS_DIM}$*${_RTS_OFF}"; }
ok()   { printf '%s\n' "${_RTS_GRN}$*${_RTS_OFF}"; }
warn() { printf '%s\n' "${_RTS_YEL}$*${_RTS_OFF}" >&2; }
die()  { printf '%s\n' "${_RTS_RED}$*${_RTS_OFF}" >&2; exit 1; }

# ---------------------------------------------------------------- 路径

# 仓库根下的文件（跨进程约定的全部落点都走这里，别再自己拼字符串）
repo_file() { printf '%s/%s\n' "$RTS_REPO_ROOT" "$1"; }

# 下面这几个只被 source 本文件的**其它脚本**用
PID_FILE="$(repo_file subtitle.pid)"
STOP_FLAG="$(repo_file .stop)"
PAUSE_FLAG="$(repo_file .paused)"
LOG_FILE="$(repo_file subtitle.log)"
ERR_LOG_FILE="$(repo_file subtitle.err.log)"
OFFLINE_ENTRY="$(repo_file download_subtitle.py)"

# venv 解释器。☠️ RTS_PYTHON 是给本仓库 tests/test_macos_scripts.py 的桩解释器
# 留的口子（真实场景永远用不到），别拿它在运行时改解释器。
venv_python() { printf '%s\n' "${RTS_PYTHON:-$RTS_REPO_ROOT/venv/bin/python}"; }

venv_python_exists() { [ -x "$(venv_python)" ]; }

# Ollama 地址。☠️ 同 _common.sh 开头：写主机名会付 IPv6 回环那两秒（CLAUDE.md
# 第 4 节第 22 条实测 2.04 秒/句）。RTS_OLLAMA_URL 同样只给测试用。
ollama_url() { printf '%s\n' "${RTS_OLLAMA_URL:-http://127.0.0.1:11434}"; }

now_stamp() { date +%Y%m%d-%H%M%S; }

# ---------------------------------------------------------------- HuggingFace 缓存

# 优先级必须和 huggingface_hub 自己的一致：
#   HF_HUB_CACHE > HUGGINGFACE_HUB_CACHE(旧名) > HF_HOME/hub > ~/.cache/huggingface/hub
# ☠️ 只认 HF_HOME 是踩过的坑：设了 HF_HUB_CACHE 的用户每次启动都会看到
# "首次启动，要下 1-3GB"的假提示并被多留 10 秒——而模型明明早就在本地。
# uninstall.sh 里也有一份同样的实现，那边漏掉的后果更重：模型扫不到、删不掉。
hf_hub_dir() {
    if [ -n "${HF_HUB_CACHE:-}" ]; then printf '%s\n' "$HF_HUB_CACHE"; return; fi
    if [ -n "${HUGGINGFACE_HUB_CACHE:-}" ]; then printf '%s\n' "$HUGGINGFACE_HUB_CACHE"; return; fi
    if [ -n "${HF_HOME:-}" ]; then printf '%s/hub\n' "$HF_HOME"; return; fi
    printf '%s/.cache/huggingface/hub\n' "$HOME"
}

# Whisper 模型在 HF 缓存里了吗？（首次启动要提示"要下 1-3GB"，别让人以为双击没反应）
whisper_model_cached() {
    local want="$1" dir name
    [ -n "$want" ] || return 1
    dir="$(hf_hub_dir)"
    [ -d "$dir" ] || return 1
    # 缓存里的目录名形如 models--Systran--faster-whisper-large-v3-turbo
    for name in "$dir"/models--*whisper*; do
        [ -d "$name" ] || continue
        case "$(basename "$name")" in
            *"$want"*) return 0 ;;
        esac
    done
    return 1
}

# ---------------------------------------------------------------- 读配置

# rts_read_config OLLAMA_MODEL GAME_MODE_OLLAMA_MODEL ...
# 一次 python 调用读多个值（venv python 冷启动约 0.5 秒，起两次纯浪费），
# 只认 `RSCFG:` 前缀的行。某个键取不到就输出空行，绝不猜。
rts_read_config() {
    local py
    py="$(venv_python)"
    "$py" -c '
import sys
from realtime_subtitle import config
for name in sys.argv[1:]:
    value = config
    for part in name.split("."):
        value = getattr(value, part, None)
    print("RSCFG:" + ("" if value is None else str(value)))
' "$@" | sed -n 's/^RSCFG://p'
}

# ---------------------------------------------------------------- Ollama

ollama_ready() {
    curl -fsS --max-time 3 "$(ollama_url)/api/tags" >/dev/null 2>&1
}

ollama_exe() {
    local exe
    exe="$(command -v ollama 2>/dev/null || true)"
    [ -n "$exe" ] && printf '%s\n' "$exe" && return 0
    # ☠️ 测试专用：不许兜底到系统路径。2026-10-01 测试「没装 ollama」场景时
    # PATH 里没有假 ollama，就落到下面的 /opt/homebrew/bin/ollama——恰好那时
    # 本机刚装上真 Ollama，测试真的 pull 了 6.6GB 的 qwen3.5:9b。
    [ -n "${RTS_NO_SYSTEM_OLLAMA:-}" ] && return 1
    # Homebrew 的 app 装了也可能只在 /opt/homebrew/bin 里（Apple Silicon 正常如此）
    for exe in /opt/homebrew/bin/ollama /usr/local/bin/ollama \
               "$HOME/.ollama/bin/ollama" /Applications/Ollama.app/Contents/Resources/ollama; do
        if [ -x "$exe" ]; then printf '%s\n' "$exe"; return 0; fi
    done
    return 1
}

# 服务没起来就拉它。macOS 上 Ollama 通常是 launchd 服务（brew services），
# 但也可能只是没启动过——所以两条路都试，最后兜底自己 serve。
# ☠️ 轮询等就绪而不是固定睡几秒：Ollama 刚装完/刚更新完可能十几秒才监听端口。
ollama_ensure_serving() {
    local exe deadline
    if ollama_ready; then return 0; fi
    exe="$(ollama_exe)" || {
        warn "❌ 找不到 Ollama。请先安装: brew install ollama，或运行 scripts/macos/install.sh"
        return 1
    }
    say "正在启动 Ollama..."
    if command -v brew >/dev/null 2>&1 && brew list --formula 2>/dev/null | grep -qx ollama; then
        brew services start ollama >/dev/null 2>&1 || true
    fi
    deadline=$((SECONDS + 20))
    while [ "$SECONDS" -lt "$deadline" ]; do
        ollama_ready && return 0
        sleep 1
    done
    # brew 服务没起来（用户是手动装/自己 serve 的）就自己兜底
    nohup "$exe" serve >/dev/null 2>&1 &
    deadline=$((SECONDS + 60))
    while [ "$SECONDS" -lt "$deadline" ]; do
        ollama_ready && return 0
        sleep 1
    done
    return 1
}

# 名字精确匹配 + 前缀匹配：config 写 "qwen3.5:4b" 时 ollama list 可能报
# "qwen3.5:4b"，写 "qwen3.5" 时可能报 "qwen3.5:latest"，只做相等会漏。
ollama_model_present() {
    local want="$1"
    [ -n "$want" ] || return 1
    command -v ollama >/dev/null 2>&1 || return 1
    ollama list 2>/dev/null | awk -v want="$want" '
        NR > 1 {
            n = $1
            if (n == want || index(n, want ":") == 1) { found = 1; exit }
        }
        END { exit(found ? 0 : 1) }
    '
}

ollama_pull() {
    local model="$1" exe
    exe="$(ollama_exe)" || return 1
    say "正在下载翻译模型 ${model}（首次需要几分钟）..."
    "$exe" pull "$model"
}

# 卸掉"确实加载着"的本项目模型。只卸 /api/ps 里有的——对没加载的模型发
# keep_alive=0 会先触发一次完整加载，纯浪费。
# ☠️ 名字里的 JSON 是手拼的：模型名只可能含字母数字/短横/点/冒号，
# 拼不出需要转义的字符（真出现了也只是卸载失败，不会变成注入）。
ollama_unload() {
    local name="$1" payload
    payload="$(printf '{"model":"%s","prompt":"","keep_alive":0}' "$name")"
    curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' \
        -d "$payload" "$(ollama_url)/api/generate" >/dev/null 2>&1
}

# /api/ps 里现在加载着的模型名（一行一个）
ollama_loaded_models() {
    local py
    py="$(venv_python)"
    [ -x "$py" ] || return 0
    curl -fsS --max-time 3 "$(ollama_url)/api/ps" 2>/dev/null | "$py" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except ValueError:
    raise SystemExit(0)
for item in payload.get("models", []):
    name = item.get("name")
    if name:
        print(name)
' 2>/dev/null || true
}

# ---------------------------------------------------------------- 体积（uninstall 用）

# 目录体积（KB）。du 在路径不存在时非零退出，所以先判存在。
dir_size_kb() {
    local path="$1"
    [ -e "$path" ] || { printf '0\n'; return 0; }
    du -sk "$path" 2>/dev/null | awk '{print $1}' || printf '0\n'
}

# 按量级选单位：残文件经常只有几百 KB，一律按 GB 显示会变成一串"0 GB"，
# 看着像脚本坏了
format_size_kb() {
    awk -v kb="${1:-0}" 'BEGIN {
        if (kb >= 1048576) printf "%.2f GB", kb / 1048576;
        else if (kb >= 1024) printf "%.1f MB", kb / 1024;
        else printf "%d KB", kb;
    }'
}

