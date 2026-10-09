#!/usr/bin/env bash
# ============================================================
# 节点一键安装（TK-003，RFC 架构选择第 6 条 / S5 / S6 / P6）
#
# 用法：
#   bash scripts/node/install_node.sh [--dry-run] --local
#   bash scripts/node/install_node.sh [--dry-run] <ssh-host>
#
#   <ssh-host>  形如 mini2 / user@mini2；只允许 [A-Za-z0-9._@-]
#   --local     装到本机（本机节点靠 UDS 被发现，不写 nodes.json）
#   --dry-run   只打印将执行的动作（token 显示为 <redacted>），不联网、不写文件
#
# 在「控制端」（运行本脚本的机器）上跑；目标机通过 ssh 驱动。目标机必须是
# Apple Silicon Mac，且已装 brew / ollama / uv / swift / Tailscale
# （scripts/macos/install.sh 本身要求前四项）。
#
# 幂等：node_id、token 已有就沿用；重跑会重新部署并保留上一版为 ~/rs-node.prev。
# ============================================================
# SC2016：交给目标机执行的命令字符串必须用单引号，$HOME 要在目标机展开而不是控制端
# shellcheck disable=SC2016
set -euo pipefail
# 整个脚本会经手 token。xtrace 会把 `printf '%s' "$TOKEN"` 原样打到 stderr，
# 所以一进来就关掉（S5：token 不出现在 set -x 输出里）。
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# 与 realtime_subtitle/node/gateway.py 的 PORT 保持一致
NODE_PORT=8791
HAD_PREV=0
LABEL="com.realtimesubtitle.node"
# venv(~3GB) + 依赖缓存 + Whisper 模型(~2.1GB)，且 .new 与 .prev 会同时存在
MIN_FREE_GB=8

DRY_RUN=0
LOCAL=0
HOST=""
HOST_SET=0
NODE_ID=""
NODE_TOKEN=""

info() { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die() {
    printf '❌ %s\n' "$*" >&2
    exit 1
}
plan() { printf '  [dry-run] %s\n' "$*"; }

# ------------------------------------------------------------
# 目标机上执行的脚本（经 stdin 交给 `bash -s`）。
# 用单引号 heredoc：$HOME 等必须由「目标机」展开，不是控制端。
# 目标机的登录 shell 是 zsh，所以统一显式起 bash，不依赖对端 shell 的语法。
# ------------------------------------------------------------

# ssh 非交互会话的 PATH 不含 /opt/homebrew/bin，也不含 Tailscale.app 里的命令行。
# 前缀抽成 RS_TOOL_PATH_PREFIX：测试可把它指向桩目录把真 tailscale/uv/ollama 挡在 PATH 外面；
# 默认值逐字不变，远端脚本里用 ${RS_TOOL_PATH_PREFIX:-…} 兜底、同名生效。
# 单引号：前缀只在目标端展开，控制端的值不会被拼进发给 ssh 的命令串（CR-010：防注入）。
TGT_PATH_EXPORT='export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH'

IFS= read -r -d '' TS_IP_LIB_SH <<'EOF' || true
is_ts_ipv4() {
    local re='^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(0|[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.(0|[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-5])$'
    [[ $1 =~ $re ]]
}
EOF

IFS= read -r -d '' PREFLIGHT_SH <<'EOF' || true
export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH
min_gb=$1
[ "$(uname -s)" = Darwin ] || { echo "目标机不是 macOS"; exit 1; }
free_kb=$(df -k "$HOME" | awk 'NR==2{print $4}')
if [ "${free_kb:-0}" -lt $((min_gb * 1024 * 1024)) ]; then
    echo "目标机家目录所在磁盘剩余不足 ${min_gb}GB（当前约 $(( ${free_kb:-0} / 1024 / 1024 ))GB）"
    exit 1
fi
for c in brew ollama uv swift; do
    command -v "$c" >/dev/null 2>&1 || { echo "目标机没有找到 ${c}（scripts/macos/install.sh 需要它）。请先在目标机安装后重跑"; exit 1; }
done
# TAILSCALE_BE_CLI=1：App Store 版二进制在环境里没有 SHLVL 时会按 GUI 启动，往 stdout 打报错且退出码 0
ts_ip=$(TAILSCALE_BE_CLI=1 tailscale ip -4 2>/dev/null </dev/null | head -n 1 || true)
[ -n "$ts_ip" ] || { echo "目标机没有 Tailscale IPv4（tailscale ip -4 无输出）。请先登录 Tailscale"; exit 1; }
if ! is_ts_ipv4 "$ts_ip"; then
    echo "目标机 tailscale ip -4 的首行不是 Tailscale IPv4（100.64.0.0/10）。请检查 Tailscale 是否已登录/运行"
    exit 1
fi
echo preflight-ok
EOF
PREFLIGHT_SH="${TS_IP_LIB_SH}${PREFLIGHT_SH}"

# 目标机上生成/读取 node_id（info.py 的 STATE_DIR / NODE_ID_FILENAME），stdout 只输出 node_id
IFS= read -r -d '' NODE_ID_SH <<'EOF' || true
set -e
umask 077
d="$HOME/Library/Application Support/rs-node"
mkdir -p "$d"
chmod 700 "$d"
f="$d/node_id"
if [ ! -s "$f" ]; then
    id=$(openssl rand -hex 16)
    printf '%s\n' "$id" > "$f.tmp.$$"
    chmod 600 "$f.tmp.$$"
    mv "$f.tmp.$$" "$f"
fi
cat "$f"
EOF

# v1 记录/重拉共用的校验库（CR-005 第 2 轮：R2-S1/R2-D1/R2-D2/R2-S2）。
# 原则：绝不重放 ps 里的命令行，也不经 sh -c——只从命令行里按白名单抠出
# --host / --port / --token-file 三个值（server.py 的 argparse 里一共就这三个参数），
# 记录里只存校验过的字段，重拉时固定用 <cwd>/venv/bin/python + 数组 exec。
# 同一份库会拼进 STOP_V1_SH（记录）和 SWAP_SH（重拉），读记录时再校验一遍。
IFS= read -r -d '' V1_LIB_SH <<'EOF' || true
uid_n=$(id -u)
# python 与 -m 之间只容许「无取值的单字母短选项」(如真实 v1 的 -u)；-X dev / -c 之类带值的不匹配
pat='[Pp]ython[0-9.]*( -[uBEsIOq]+)* -m [r]ealtime_subtitle\.remote\.server'
ere='(^|/)[Pp]ython[0-9.]*( -[uBEsIOq]+)* -m realtime_subtitle\.remote\.server( |$)'

is_v1() {
    c=$(ps -o command= -p "$1" 2>/dev/null </dev/null) || return 1
    printf '%s\n' "$c" | grep -Eq "$ere"
}
find_v1() {
    for p in $(pgrep -u "$uid_n" -f "$pat" 2>/dev/null </dev/null || true); do
        if is_v1 "$p"; then printf '%s\n' "$p"; fi
    done
}

v1_valid_host() {
    local h=$1 o
    local re4='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$'
    local re6='^[0-9A-Fa-f:]{2,39}$'
    if [[ $h =~ $re4 ]]; then
        for o in "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"; do
            [ $((10#$o)) -le 255 ] || return 1
        done
        return 0
    fi
    [[ $h =~ $re6 ]] && [[ $h == *:*:* ]]
}
v1_valid_port() {
    local re='^[0-9]{1,5}$'
    [[ $1 =~ $re ]] && [ $((10#$1)) -ge 1 ] && [ $((10#$1)) -le 65535 ]
}
v1_valid_tokfile() {
    local re='^[A-Za-z0-9._/~][A-Za-z0-9._/~-]*$'
    [ ${#1} -le 4096 ] && [[ $1 =~ $re ]]
}
# cwd：绝对路径、无控制字符、是普通目录且本身不是符号链接（路径里可以有空格，只会被加引号使用）
v1_valid_cwd() {
    local re='[[:cntrl:]]'
    case "$1" in /?*) ;; *) return 1 ;; esac
    if [[ $1 =~ $re ]]; then return 1; fi
    [ -d "$1" ] && [ ! -L "$1" ]
}
# 从 `-m realtime_subtitle.remote.server` 之后的剩余命令行里按白名单解析参数。
# 结果在 V_HOST / V_PORT / V_TOK；拒绝时 V_WHY 给原因（不回显 token 内容）。
v1_parse_args() {
    V_HOST='' V_PORT='' V_TOK='' V_WHY=''
    local opt val
    set -f
    # shellcheck disable=SC2086
    set -- $1
    set +f
    while [ $# -gt 0 ]; do
        opt=$1
        shift
        case "$opt" in
            --host=* | --port=* | --token-file=*)
                val=${opt#*=}
                opt=${opt%%=*}
                ;;
            --host | --port | --token-file)
                if [ $# -eq 0 ]; then V_WHY="$opt 缺少取值"; return 1; fi
                val=$1
                shift
                ;;
            *)
                V_WHY="含有不在白名单内的参数"
                return 1
                ;;
        esac
        case "$opt" in
            --host)
                [ -z "$V_HOST" ] || { V_WHY="--host 重复"; return 1; }
                v1_valid_host "$val" || { V_WHY="--host 不是 IP 字面量"; return 1; }
                V_HOST=$val
                ;;
            --port)
                [ -z "$V_PORT" ] || { V_WHY="--port 重复"; return 1; }
                v1_valid_port "$val" || { V_WHY="--port 不是 1-65535 的数字"; return 1; }
                V_PORT=$val
                ;;
            --token-file)
                [ -z "$V_TOK" ] || { V_WHY="--token-file 重复"; return 1; }
                v1_valid_tokfile "$val" || { V_WHY="--token-file 含空白或特殊字符"; return 1; }
                V_TOK=$val
                ;;
        esac
    done
    [ -n "$V_HOST" ] || { V_WHY="缺少 --host"; return 1; }
    [ -n "$V_TOK" ] || { V_WHY="缺少 --token-file"; return 1; }
    return 0
}
# 可复制的手动启动命令（只含 token 文件路径，不含 token 内容）；$1=cwd，其余读 V_*
v1_manual_cmd() {
    local out
    out="cd $(printf '%q' "$1") && ./venv/bin/python -m realtime_subtitle.remote.server --host $V_HOST"
    if [ -n "$V_PORT" ]; then out="$out --port $V_PORT"; fi
    printf '%s' "$out --token-file $V_TOK"
}
EOF

# 停掉 v1 远程服务：只杀进程，不动 ~/rs-remote 目录。
# v1 在 mini2 上以 --port 8791 运行，和 v2 抢同一端口，所以必须在 bootstrap 之前停。
# 为了让「v2 装失败」不等于「什么都没在跑」，停之前先把 v1 的 cwd 与白名单参数
# （--host/--port/--token-file，值已校验）记到状态目录（0600；不存原始命令行，
# token 只有文件路径），SWAP_SH 回滚时据此重新拉起。
# 命令行里出现任何白名单外的东西就拒绝记录（回滚时不重拉，只提示手动启动）。
# 状态目录或记录文件是符号链接时一律拒绝读写（R2-S2）。
# 匹配刻意收紧（CR-005 S-F1）：只认「本人」的 `python* -m realtime_subtitle.remote.server`，
# 点号转义（否则 `tail -f .../realtime_subtitle/remote/server.py` 也会命中），
# 且每次动手（TERM / KILL）前都用 ps 的完整命令行再确认一遍。
# Python 首字母大小写都认：macOS 框架版 Python 的进程名是 `Python`。
IFS= read -r -d '' STOP_V1_SH <<'EOF' || true
export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH
umask 077
state="$HOME/Library/Application Support/rs-node"
rec="$state/v1.restart"

# 写记录：$1 为完整内容。状态目录/记录文件是符号链接或类型不对就拒绝
write_rec() {
    if [ -L "$state" ] || { [ -e "$state" ] && [ ! -d "$state" ]; }; then
        echo "⚠️ 状态目录是符号链接或不是目录，拒绝写入 v1 记录"
        return 1
    fi
    if [ -L "$rec" ] || { [ -e "$rec" ] && [ ! -f "$rec" ]; }; then
        echo "⚠️ v1 记录文件是符号链接或不是普通文件，拒绝写入"
        return 1
    fi
    mkdir -p "$state" && chmod 700 "$state" || return 1
    tmp="$rec.tmp.$$"
    # noclobber：临时文件名被人预置成符号链接/已存在时直接失败，不跟随
    ( set -C; printf '%s\n' "$1" > "$tmp" ) || return 1
    chmod 600 "$tmp" && mv "$tmp" "$rec"
}
drop_rec() {
    if [ ! -L "$state" ] && [ ! -L "$rec" ] && [ -f "$rec" ]; then rm -f "$rec"; fi
}

pids=$(find_v1)
if [ -z "$pids" ]; then
    drop_rec
    echo "v1 远程服务：未在运行"
    exit 0
fi

first=$(printf '%s\n' "$pids" | head -n 1)
cmd=$(ps -o command= -p "$first" 2>/dev/null </dev/null | sed 's/^ *//')
cwd=$(lsof -a -p "$first" -d cwd -Fn 2>/dev/null </dev/null | sed -n 's/^n//p' | head -n 1)
mod='-m realtime_subtitle.remote.server'
why=''
if [ -z "$cmd" ] || [ -z "$cwd" ]; then
    why="没能读到命令行或工作目录"
elif ! v1_valid_cwd "$cwd"; then
    why="工作目录不是普通目录（或是符号链接）"
elif [ "${cmd#*"$mod"}" = "$cmd" ]; then
    why="命令行形状无法识别"
elif ! v1_parse_args "${cmd#*"$mod"}"; then
    why=$V_WHY
fi
if [ -z "$why" ]; then
    rec_body="cwd=$cwd
host=$V_HOST
port=$V_PORT
token_file=$V_TOK"
    if write_rec "$rec_body"; then
        echo "已记录 v1 的启动参数与工作目录（v2 安装失败时用来恢复 v1）"
    else
        echo "⚠️ 没能写入 v1 记录：如果 v2 安装失败，v1 需要手动重新启动"
    fi
else
    echo "⚠️ 不记录 v1 的启动命令（${why}）：如果 v2 安装失败，v1 需要手动重新启动"
    drop_rec
    write_rec "unrecorded=1" || true
fi

for p in $pids; do
    if is_v1 "$p"; then kill -TERM "$p" 2>/dev/null || true; fi
done
for _ in 1 2 3 4 5; do
    [ -z "$(find_v1)" ] && { echo "v1 远程服务：已停止（~/rs-remote 目录保留）"; exit 0; }
    sleep 1
done
for p in $(find_v1); do
    kill -KILL "$p" 2>/dev/null || true
done
echo "v1 远程服务：已强制停止（~/rs-remote 目录保留）"
EOF
STOP_V1_SH="${V1_LIB_SH}${STOP_V1_SH}"

# 在 ~/rs-node.new 里建环境。install.sh 负责 venv / 依赖 / rstranslate；
# 节点只多一个 websockets，单独装。--skip-models：节点默认用系统翻译，不拉 Ollama 模型。
IFS= read -r -d '' BUILD_SH <<'EOF' || true
set -e
export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH
cd "$HOME/rs-node.new"
bash scripts/macos/install.sh --skip-models </dev/null
VIRTUAL_ENV="$PWD/venv" uv pip install -r realtime_subtitle/node/requirements.txt </dev/null
EOF

# 换名之前在 .new 里自检：翻译语言包状态 + 引擎能加载 + 测一次 rtf。
# 为什么不等换名后再测：失败时 ~/rs-node 还是完好的旧版，回滚成本为零。
# 音频只放 mktemp 目录，退出即删，不落盘保存。
IFS= read -r -d '' SELFCHECK_SH <<'EOF' || true
export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH
cd "$HOME/rs-node.new" || exit 1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
wav=""
voice=""
if command -v say >/dev/null 2>&1 && command -v afconvert >/dev/null 2>&1; then
    voices=$(say -v '?' 2>/dev/null || true)
    if printf '%s\n' "$voices" | grep -q '^Anna '; then
        voice=Anna
    else
        voice=$(printf '%s\n' "$voices" | sed -n 's/^\(.*[^ ]\)  *de_DE .*/\1/p' | head -n 1)
    fi
fi
if [ -n "$voice" ] \
   && say -v "$voice" -o "$tmp/a.aiff" "Guten Tag, heute sprechen wir über die Entwicklung der Wirtschaft in Deutschland." </dev/null \
   && afconvert -f WAVE -d LEI16@16000 -c 1 "$tmp/a.aiff" "$tmp/a.wav"; then
    wav="$tmp/a.wav"
else
    echo "RTF_NOTE=没有可用的德语声音，跳过 rtf 实测（之后每次会话结束会自动更新）"
fi
mkdir -p "$HOME/Library/Application Support/rs-node"
chmod 700 "$HOME/Library/Application Support/rs-node"
venv/bin/python - "$wav" <<'PYEOF'
import json
import math
import os
import sys
import time
import wave

import numpy as np

import realtime_subtitle.config as config
from realtime_subtitle.node.engines import WhisperEngine
from realtime_subtitle.node.info import ASR_STATE_FILENAME, STATE_DIR
from realtime_subtitle.translate.apple_translate import AppleTranslator

wav_path = sys.argv[1]

helper = AppleTranslator()
try:
    status = helper.status("de", "zh")
finally:
    helper.close()
print("TRANSLATION_STATUS=" + str(status))
translator = "apple" if status == "installed" else "ollama:" + str(config.OLLAMA_MODEL)

engine = WhisperEngine()
try:
    engine.load()
except Exception as e:  # 只报类名：异常文本不进日志（S7）
    print("ENGINE_LOAD_FAILED=" + type(e).__name__)
    sys.exit(3)

rtf = None
if wav_path:
    with wave.open(wav_path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio = pcm.astype(np.float32) / 32768.0
    dur = len(audio) / 16000
    try:
        engine.transcribe(audio, "de")  # 预热：首次调用含惰性加载，不计入
        spent = 0.0
        total = 0.0
        for _ in range(3):
            t0 = time.perf_counter()
            engine.transcribe(audio, "de")
            spent += time.perf_counter() - t0
            total += dur
        if total > 0:
            rtf = round(spent / total, 4)
    except Exception as e:
        print("RTF_NOTE=识别失败（" + type(e).__name__ + "），跳过 rtf")

path = STATE_DIR / ASR_STATE_FILENAME
if rtf is None:
    try:
        old = json.loads(path.read_text(encoding="utf-8")).get("rtf")
        if isinstance(old, int | float) and not isinstance(old, bool) and math.isfinite(old) and old >= 0:
            rtf = old
    except (OSError, ValueError, AttributeError):
        pass
info = engine.info()
state = {"model": info["model"], "backend": info["backend"], "rtf": rtf, "translator": translator}
tmp = STATE_DIR / ("." + ASR_STATE_FILENAME + "." + str(os.getpid()) + ".tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(state, f, allow_nan=False)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("RTF=" + str(rtf))
PYEOF
EOF

# 换名 + 启动 LaunchAgent + 经 UDS 验 /v1/info；任何一步失败都回滚。
# 回滚的目标是「不留下半个 v2」：
#   有 .prev → 恢复 .prev 并重新 bootstrap；
#   无 .prev → bootout 并删掉 plist（RunAtLoad+KeepAlive 的 plist 指向不存在的 venv 会在每次登录反复拉起失败），
#              失败版改名 ~/rs-node.failed 留着排查。
# 两种情况都按 STOP_V1_SH 记下的白名单参数把 v1 重新拉起（见 restore_v1）。
IFS= read -r -d '' SWAP_SH <<'EOF' || true
export PATH=${RS_TOOL_PATH_PREFIX:-/opt/homebrew/bin:/usr/local/bin:/Applications/Tailscale.app/Contents/MacOS}:$PATH
uid_n=$(id -u)
label=$1
plist="$HOME/Library/LaunchAgents/$label.plist"
state="$HOME/Library/Application Support/rs-node"
sock="$state/gw.sock"
rec="$state/v1.restart"
boot_tries=${RS_BOOT_TRIES:-5}
info_wait=${RS_INFO_WAIT:-40}
tcp_wait=${RS_TCP_WAIT:-30}
node_port=${2:-8791}

# 按 STOP_V1_SH 的记录重拉 v1：固定入口 <cwd>/venv/bin/python -m realtime_subtitle.remote.server
# + 白名单参数，bash 数组直接 exec，不经 sh -c；读记录时每个值再校验一遍。
# 拉起后有界轮询（RS_V1_WAIT 秒，默认 5）用 find_v1 复核，没起来就给可复制的手动命令。
restore_v1() {
    if [ -L "$state" ]; then
        echo "⚠️ 状态目录是符号链接，拒绝读取 v1 记录；v1 如已停止请手动重新启动"
        return 0
    fi
    if [ -L "$rec" ]; then
        echo "⚠️ v1 记录文件是符号链接，拒绝读取；v1 如已停止请手动重新启动"
        return 0
    fi
    [ -e "$rec" ] || return 0
    if [ ! -f "$rec" ]; then
        echo "⚠️ v1 记录不是普通文件，拒绝读取；v1 如已停止请手动重新启动"
        return 0
    fi
    r_cwd='' r_host='' r_port='' r_tok='' r_unrec='' r_bad=''
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            cwd=*) r_cwd=${line#cwd=} ;;
            host=*) r_host=${line#host=} ;;
            port=*) r_port=${line#port=} ;;
            token_file=*) r_tok=${line#token_file=} ;;
            unrecorded=1) r_unrec=1 ;;
            *) r_bad=1 ;;
        esac
    done < "$rec"
    rm -f "$rec"
    if [ -n "$r_unrec" ]; then
        echo "⚠️ v2 安装失败；停 v1 时它的命令行不在白名单内、没有记录，没能恢复 v1，请手动重新启动"
        return 0
    fi
    if [ -n "$r_bad" ] || ! v1_valid_cwd "$r_cwd" || ! v1_valid_host "$r_host" \
        || ! v1_valid_tokfile "$r_tok" || { [ -n "$r_port" ] && ! v1_valid_port "$r_port"; }; then
        echo "⚠️ v2 安装失败，但 v1 的记录无效或不完整，没能恢复 v1，请手动重新启动"
        return 0
    fi
    V_HOST=$r_host V_PORT=$r_port V_TOK=$r_tok
    manual=$(v1_manual_cmd "$r_cwd")
    if [ ! -x "$r_cwd/venv/bin/python" ]; then
        echo "⚠️ v2 安装失败；$r_cwd/venv/bin/python 不存在，v1 未能自动恢复，请手动启动：$manual"
        return 0
    fi
    v1_args=("$r_cwd/venv/bin/python" -m realtime_subtitle.remote.server --host "$r_host")
    if [ -n "$r_port" ]; then v1_args+=(--port "$r_port"); fi
    v1_args+=(--token-file "$r_tok")
    ( cd "$r_cwd" && nohup "${v1_args[@]}" >/dev/null 2>&1 </dev/null & )
    v1_end=$((SECONDS + ${RS_V1_WAIT:-5}))
    while :; do
        if [ -n "$(find_v1)" ]; then
            echo "⚠️ v2 安装失败，已恢复 v1：在 $r_cwd 重新拉起 $r_cwd/venv/bin/python -m realtime_subtitle.remote.server"
            return 0
        fi
        [ "$SECONDS" -lt "$v1_end" ] || break
        sleep 1
    done
    echo "⚠️ v2 安装失败，v1 未能自动恢复，请手动启动：$manual"
}

restart_old_agent() {
    if [ -f "$plist" ] && launchctl bootstrap "gui/$uid_n" "$plist" 2>/dev/null; then
        launchctl kickstart -k "gui/$uid_n/$label" 2>/dev/null || true
        return 0
    fi
    return 1
}

rollback() {
    echo "ROLLBACK：$1"
    launchctl bootout "gui/$uid_n/$label" 2>/dev/null || true
    if [ -d "$HOME/rs-node.prev" ]; then
        rm -rf "$HOME/rs-node"
        mv "$HOME/rs-node.prev" "$HOME/rs-node"
        if restart_old_agent; then
            echo "已回滚到上一版并重新启动"
        else
            echo "已回滚到上一版目录，但 LaunchAgent 没能重新启动，请手动检查"
        fi
    else
        rm -f "$plist"
        if [ -d "$HOME/rs-node" ]; then
            rm -rf "$HOME/rs-node.failed"
            mv "$HOME/rs-node" "$HOME/rs-node.failed"
            echo "没有上一版可回滚：节点未安装；失败版保留在 ~/rs-node.failed 供排查，LaunchAgent plist 已删除"
        else
            echo "没有上一版可回滚：节点未安装；LaunchAgent plist 已删除"
        fi
    fi
    restore_v1
}

launchctl bootout "gui/$uid_n/$label" 2>/dev/null || true
rm -rf "$HOME/rs-node.prev"
if [ -d "$HOME/rs-node" ]; then
    mv "$HOME/rs-node" "$HOME/rs-node.prev" || {
        echo "无法把旧版换名为 .prev"
        if restart_old_agent; then
            echo "已重新启动旧版 LaunchAgent"
        else
            echo "旧版 LaunchAgent 没能重新启动，请手动检查"
        fi
        restore_v1
        exit 1
    }
fi
mv "$HOME/rs-node.new" "$HOME/rs-node" || { rollback "换名失败"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/rs-node"
chmod 700 "$HOME/Library/Logs/rs-node"
content=$(cat "$HOME/rs-node/scripts/node/com.realtimesubtitle.node.plist.template") \
    || { rollback "读不到 plist 模板"; exit 1; }
content=${content//@HOME@/$HOME}
printf '%s\n' "$content" > "$plist.tmp.$$" || { rollback "写 plist 失败"; exit 1; }
plutil -lint "$plist.tmp.$$" >/dev/null || { rm -f "$plist.tmp.$$"; rollback "plist 校验失败"; exit 1; }
mv "$plist.tmp.$$" "$plist" || { rollback "plist 落盘失败"; exit 1; }

# bootout 是异步的，紧接着 bootstrap 偶发 I/O error，重试几次
booted=0
for _ in $(seq 1 "$boot_tries"); do
    if launchctl bootstrap "gui/$uid_n" "$plist" 2>/dev/null; then booted=1; break; fi
    sleep 1
done
[ "$booted" = 1 ] || { rollback "launchctl bootstrap 失败（目标机用户是否已登录桌面？LaunchAgent 需要登录会话）"; exit 1; }
launchctl kickstart -k "gui/$uid_n/$label" || { rollback "launchctl kickstart 失败"; exit 1; }

node_id=$(cat "$state/node_id" 2>/dev/null || true)
ok=0
for _ in $(seq 1 "$info_wait"); do
    body=$(curl -fsS --max-time 3 --unix-socket "$sock" http://localhost/v1/info 2>/dev/null </dev/null || true)
    if [ -n "$body" ] && printf '%s' "$body" | "$HOME/rs-node/venv/bin/python" -c \
        'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("v")==2 and d.get("node_id")==sys.argv[1] and d["node_id"] else 1)' "$node_id"; then
        ok=1
        break
    fi
    sleep 1
done
[ "$ok" = 1 ] || { rollback "gateway ${info_wait} 秒内没有通过 /v1/info 自检（v==2 且 node_id 匹配）；日志见 ~/Library/Logs/rs-node/"; exit 1; }

# UDS 通了不代表 TCP 绑上了：launchd 下取不到 Tailscale IP 时 gateway 只退避重试、UDS 照常服务。
# 有界等待（RS_TCP_WAIT 秒）确认 gateway 进程在 <Tailscale IP>:端口 LISTEN，且端口上没有任何通配监听。
case "$node_port" in
    ''|*[!0-9]*) rollback "内部错误：端口参数无效"; exit 1 ;;
esac
ts_ip=$(TAILSCALE_BE_CLI=1 tailscale ip -4 2>/dev/null </dev/null | head -n 1 || true)
if ! is_ts_ipv4 "$ts_ip"; then
    rollback "取不到 Tailscale IPv4，无法确认 gateway 的 TCP 监听"; exit 1
fi
tcp_end=$((SECONDS + tcp_wait))
tcp_ok=0 tcp_wild=0
while :; do
    gw_pid=$(launchctl print "gui/$uid_n/$label" 2>/dev/null </dev/null | awk '$1=="pid" && $2=="="{print $3; exit}' || true)
    tcp_ok=0 tcp_wild=0 cur=''
    case "$gw_pid" in ''|*[!0-9]*) gw_pid='' ;; esac
    while IFS= read -r l; do
        case "$l" in
            p*) cur=${l#p} ;;
            n\*:"$node_port"|n0.0.0.0:"$node_port"|n\[::\]:"$node_port") tcp_wild=1 ;;
            n"$ts_ip":"$node_port") [ -n "$gw_pid" ] && [ "$cur" = "$gw_pid" ] && tcp_ok=1 ;;
        esac
    done <<LSOF_OUT
$(lsof -nP -iTCP:"$node_port" -sTCP:LISTEN -Fpn 2>/dev/null </dev/null || true)
LSOF_OUT
    if [ "$tcp_wild" = 1 ]; then break; fi
    if [ "$tcp_ok" = 1 ]; then break; fi
    [ "$SECONDS" -lt "$tcp_end" ] || break
    sleep 1
done
if [ "$tcp_wild" = 1 ]; then
    rollback "端口 ${node_port} 上存在通配（*/0.0.0.0）监听，违反只绑 Tailscale IP 的约束"; exit 1
fi
[ "$tcp_ok" = 1 ] || { rollback "gateway ${tcp_wait} 秒内没有在 ${ts_ip}:${node_port} 上监听 TCP（UDS 自检虽通过；日志 ~/Library/Logs/rs-node/ 里看 tcp_bind_failed reason=）"; exit 1; }
rm -f "$rec"
echo "gateway-ok：/v1/info 返回 200，v=2；TCP 已在 ${ts_ip}:${node_port} 监听"
if [ -d "$HOME/rs-node.prev" ]; then echo "prev-kept"; fi
EOF
SWAP_SH="${TS_IP_LIB_SH}${V1_LIB_SH}${SWAP_SH}"

# ------------------------------------------------------------
# 控制端函数
# ------------------------------------------------------------

# S6：白名单 + 不许以 - 开头（有 `--` 兜底，这里是第二道）
valid_host() {
    [[ -n "$1" && "$1" =~ ^[A-Za-z0-9._@-]+$ && "$1" != -* ]]
}

# 在目标机执行一条命令，stdin 原样透传（token、tar 流、脚本都走这里）。
# ssh 在主机名前加 `--`，host 即使是 -oXxx 也只会被当主机名。
tgt_run() {
    if [[ "${LOCAL}" -eq 1 ]]; then
        bash -c "$1"
    else
        ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" "$1"
    fi
}

# 把 heredoc 脚本交给目标机的 bash；"$@" 是脚本参数（只放非敏感值）
tgt_script() {
    local script="$1"
    shift
    printf '%s\n' "${script}" | tgt_run "bash -s -- $*"
}

# P6：把一个节点并入 nodes.json。按 id 去重（同 id 原位更新，其它条目原样保留），
# 临时文件 + rename 原子写，0600。参数都不是秘密（token 只传路径）。
# 现有文件不是 JSON 数组时拒绝覆盖：那更可能是用户手改坏了，静默重写会丢数据。
merge_nodes_json() {
    local file="$1" id="$2" node_id="$3" url="$4" token_file="$5"
    python3 -I -c '
import json, os, sys
path, nid, node_id, url, token_file = sys.argv[1:6]
entries = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        sys.exit("nodes.json 不是数组，拒绝覆盖：" + path)
new = {"id": nid, "node_id": node_id, "url": url, "token_file": token_file}
for i, e in enumerate(entries):
    if isinstance(e, dict) and e.get("id") == nid:
        entries[i] = new
        break
else:
    entries.append(new)
d = os.path.dirname(path)
os.makedirs(d, mode=0o700, exist_ok=True)
tmp = os.path.join(d, ".nodes.json." + str(os.getpid()) + ".tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$file" "$id" "$node_id" "$url" "$token_file"
}

# 控制端本地写 token 文件：只用 shell 内建 printf，token 不进任何 argv
write_secret_file() {
    local path="$1" content="$2" dir tmp
    dir="$(dirname "${path}")"
    (
        umask 077
        mkdir -p "${dir}"
        chmod 700 "${dir}"
        tmp="${path}.tmp.$$"
        printf '%s' "${content}" > "${tmp}"
        chmod 600 "${tmp}"
        mv "${tmp}" "${path}"
    )
}

# MagicDNS 名：tailscale status --json 的 Self.DNSName（去掉末尾点）
resolve_magicdns() {
    local js
    js="$(tgt_run "${TGT_PATH_EXPORT}; tailscale status --json")" || die "取不到目标机的 tailscale status"
    printf '%s' "${js}" | python3 -I -c '
import json, sys
print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))
'
}

usage() {
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

parse_args() {
    local only_pos=0
    while [[ $# -gt 0 ]]; do
        if [[ "${only_pos}" -eq 0 ]]; then
            case "$1" in
                --dry-run) DRY_RUN=1; shift; continue ;;
                --local) LOCAL=1; shift; continue ;;
                -h | --help) usage; exit 0 ;;
                --) only_pos=1; shift; continue ;;
                -*) die "未知参数：$1" ;;
            esac
        fi
        [[ "${HOST_SET}" -eq 0 ]] || die "只能指定一个目标主机"
        HOST="$1"
        HOST_SET=1
        shift
    done
    if [[ "${LOCAL}" -eq 1 ]]; then
        [[ "${HOST_SET}" -eq 0 ]] || die "--local 与 <ssh-host> 不能同时指定"
    else
        [[ "${HOST_SET}" -eq 1 ]] || { usage >&2; die "需要 --local 或 <ssh-host>"; }
        valid_host "${HOST}" || die "主机名不合法（只允许 [A-Za-z0-9._@-]，且不能以 - 开头）"
    fi
}

print_dry_run() {
    local where="${HOST}"
    [[ "${LOCAL}" -eq 1 ]] && where="本机"
    info "[dry-run] 目标：${where}（不联网、不写文件）"
    plan "1. 预检：磁盘 ≥${MIN_FREE_GB}GB、brew/ollama/uv/swift、Tailscale(tailscale ip -4)"
    plan "2. 确保 node_id：~/Library/Application Support/rs-node/node_id（已有沿用，0600 原子写）"
    plan "3. 确保 token：目标机 ~/.config/rs-node/token (0600)，内容 <redacted>，仅经 stdin 传递"
    if [[ "${LOCAL}" -eq 0 ]]; then
        plan "   客户端副本 ~/.config/rslite/tokens/${HOST##*@}.token (0600)，内容 <redacted>"
    fi
    plan "4. git archive HEAD（不含 .git / 用户数据）→ ${where}:~/rs-node.new"
    plan "5. 在 ~/rs-node.new 运行 scripts/macos/install.sh --skip-models，并安装 realtime_subtitle/node/requirements.txt"
    plan "6. 自检：翻译语言包 de→zh 状态；say -v Anna 现场合成德语 → 测 rtf → 写 asr_state.json；音频只在临时目录，测完删除"
    plan "7. 停止 v1 realtime_subtitle.remote.server 进程（精确匹配本人进程；先记录其命令行与 cwd；保留 ~/rs-remote 目录）"
    plan "8. 原子换名：~/rs-node → ~/rs-node.prev，~/rs-node.new → ~/rs-node"
    plan "9. 渲染 plist（不含 IP、不含 token）→ ~/Library/LaunchAgents/${LABEL}.plist；launchctl bootstrap gui/\$UID + kickstart -k"
    plan "10. 经 UDS 请求 /v1/info，要求 200 且 v=2、node_id 匹配；再有界等待（RS_TCP_WAIT，默认 30 秒）确认 gateway 进程在 <Tailscale IP>:${NODE_PORT} LISTEN 且该端口无 */0.0.0.0 监听；任一失败 → 回滚（有 .prev 则恢复；无则删 plist、失败版留作 ~/rs-node.failed），并按记录重新拉起 v1、告警"
    if [[ "${LOCAL}" -eq 0 ]]; then
        plan "11. 写 ~/.config/rslite/nodes.json（0600、按 id=${HOST##*@} 去重、原子写）：url=ws://<MagicDNS 名>:${NODE_PORT}"
    else
        plan "11. --local：不写 nodes.json（本机节点靠 UDS 发现）"
    fi
}

step_preflight() {
    info "[1/9] 预检目标机..."
    local out
    command -v git >/dev/null 2>&1 || die "控制端没有 git"
    command -v openssl >/dev/null 2>&1 || die "控制端没有 openssl（生成 token 需要）"
    if [[ "${LOCAL}" -eq 0 ]]; then
        command -v python3 >/dev/null 2>&1 || die "控制端没有 python3（写 nodes.json 需要）"
        command -v ssh >/dev/null 2>&1 || die "控制端没有 ssh"
    fi
    out="$(tgt_script "${PREFLIGHT_SH}" "${MIN_FREE_GB}")" || {
        printf '%s\n' "${out:-}" >&2
        die "预检未通过（连不上目标机，或上面列出的条件不满足）"
    }
    info "  ✅ 预检通过"
}

step_node_id() {
    info "[2/9] 确保 node_id..."
    NODE_ID="$(tgt_script "${NODE_ID_SH}")" || die "无法在目标机生成 node_id"
    [[ "${NODE_ID}" =~ ^[A-Za-z0-9_-]{8,64}$ ]] || die "目标机上的 node_id 格式异常"
    info "  ✅ node_id 就绪"
}

# 来源优先级：目标机已有 > 客户端已有 > 新生成。目标机的那份才是这个节点真正在用的，
# 以它为准才不会因为客户端副本过期而把别的客户端挤掉。
step_token() {
    info "[3/9] 确保 token（只经 stdin 传递）..."
    local client_file="" existing=""
    if [[ "${LOCAL}" -eq 0 ]]; then
        client_file="${HOME}/.config/rslite/tokens/${HOST##*@}.token"
    fi
    existing="$(tgt_run 'cat "$HOME/.config/rs-node/token" 2>/dev/null || true')"
    # 必须是 openssl rand -hex 32 的形状：只验长度的话，远端 shell 启动输出或多行文件会被当成 token
    if [[ "${existing}" =~ ^[0-9a-f]{64}$ ]]; then
        NODE_TOKEN="${existing}"
    elif [[ -n "${client_file}" && -f "${client_file}" && "$(cat "${client_file}")" =~ ^[0-9a-f]{64}$ ]]; then
        NODE_TOKEN="$(cat "${client_file}")"
    else
        NODE_TOKEN="$(openssl rand -hex 32)"
    fi
    [[ "${NODE_TOKEN}" =~ ^[0-9a-f]{64}$ ]] || die "token 生成失败"

    if [[ "${existing}" != "${NODE_TOKEN}" ]]; then
        printf '%s' "${NODE_TOKEN}" | tgt_run 'umask 077; d="$HOME/.config/rs-node"; mkdir -p "$d"; chmod 700 "$d"; cat > "$d/token.tmp.$$"; chmod 600 "$d/token.tmp.$$"; mv "$d/token.tmp.$$" "$d/token"' \
            || die "token 写入目标机失败"
    else
        # 沿用时也收紧权限：过宽的话 gateway 的 load_token 会拒绝，40 秒后才以笼统的报错回滚
        tgt_run 'chmod 700 "$HOME/.config/rs-node" && chmod 600 "$HOME/.config/rs-node/token"' \
            || die "无法收紧目标机 token 的权限"
    fi
    if [[ -n "${client_file}" ]]; then
        write_secret_file "${client_file}" "${NODE_TOKEN}"
    fi
    info "  ✅ token 就绪（目标机 ~/.config/rs-node/token）"
}

step_deploy() {
    info "[4/9] 部署 HEAD 到 ~/rs-node.new ..."
    if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
        warn "工作区有未提交改动；只会部署已提交的 HEAD"
    fi
    # 残留的 .new 来自上次中断，直接清掉重来
    git -C "${REPO_ROOT}" archive --format=tar HEAD \
        | tgt_run 'rm -rf "$HOME/rs-node.new"; mkdir -p "$HOME/rs-node.new"; tar -x -C "$HOME/rs-node.new"' \
        || die "部署失败"
    info "[5/9] 安装 Python 环境（可能要几分钟）..."
    tgt_script "${BUILD_SH}" || { cleanup_new; die "目标机安装失败（旧版未受影响）"; }
}

cleanup_new() {
    tgt_run 'rm -rf "$HOME/rs-node.new"' || true
}

step_selfcheck() {
    info "[6/9] 自检：翻译语言包 + 识别引擎 + rtf（首次会下载 Whisper 模型）..."
    local out
    out="$(tgt_script "${SELFCHECK_SH}")" || {
        printf '%s\n' "${out:-}" >&2
        cleanup_new
        die "目标机自检失败（旧版未受影响）"
    }
    printf '%s\n' "${out}" | sed 's/^/  /'
    if grep -q '^TRANSLATION_STATUS=None$' <<<"${out}"; then
        warn "rstranslate 不可用（构建失败？helper 没有响应），节点会退回 Ollama。"
        warn "  请查看上面 [5/9] 的构建输出，修好后重跑 install_node.sh（重跑会重写 asr_state.json）"
        warn "  （退回 Ollama 时还需要目标机已 ollama pull 对应翻译模型）"
    elif ! grep -q '^TRANSLATION_STATUS=installed$' <<<"${out}"; then
        warn "系统翻译的 德语→中文 语言包还没装好，节点会退回 Ollama。请在目标机手动下载："
        warn "  系统设置 → 通用 → 语言与地区 → 翻译语言 → 下载「德语」和「中文（简体）」"
        warn "  装好后，下一次足够长的会话结束时 worker 会自动改报系统翻译；想立刻生效就重跑 install_node.sh"
        warn "  （退回 Ollama 时还需要目标机已 ollama pull 对应翻译模型）"
    fi
}

step_swap() {
    info "[7/9] 停止 v1 远程服务..."
    tgt_script "${STOP_V1_SH}" || warn "停止 v1 进程时出现问题，继续安装"
    info "[8/9] 换名并启动 LaunchAgent..."
    local out
    out="$(tgt_script "${SWAP_SH}" "${LABEL}" "${NODE_PORT}")" || {
        printf '%s\n' "${out:-}" >&2
        die "节点启动或自检失败，已按上面的说明回滚"
    }
    printf '%s\n' "${out}" | sed 's/^/  /'
    if grep -qx 'prev-kept' <<<"${out}"; then HAD_PREV=1; fi
}

step_nodes_json() {
    if [[ "${LOCAL}" -eq 1 ]]; then
        info "[9/9] --local：不写 nodes.json（本机节点靠 UDS 发现）"
        return 0
    fi
    info "[9/9] 写客户端 nodes.json..."
    local dns id
    dns="$(resolve_magicdns)"
    [[ "${dns}" =~ ^[A-Za-z0-9.-]+$ ]] || die "MagicDNS 名异常，没有写 nodes.json"
    id="${HOST##*@}"
    merge_nodes_json "${HOME}/.config/rslite/nodes.json" "${id}" "${NODE_ID}" \
        "ws://${dns}:${NODE_PORT}" "${HOME}/.config/rslite/tokens/${id}.token"
    info "  ✅ ${id} → ws://${dns}:${NODE_PORT}"
}

main() {
    parse_args "$@"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        print_dry_run
        return 0
    fi
    step_preflight
    step_node_id
    step_token
    step_deploy
    step_selfcheck
    step_swap
    step_nodes_json
    info ""
    local prev_note=""
    if [[ "${HAD_PREV}" == "1" ]]; then prev_note="；上一版保留在 ~/rs-node.prev"; fi
    info "✅ 节点安装完成。日志：目标机 ~/Library/Logs/rs-node/${prev_note}"
}

# 被 source 时只提供函数（测试直接调 merge_nodes_json 等），不执行 main
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
