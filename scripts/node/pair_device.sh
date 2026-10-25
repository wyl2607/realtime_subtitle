#!/usr/bin/env bash
# 手机配对出码（控制端）。节点可以是 Mac 或 Windows。
#
# 用法：
#   bash scripts/node/pair_device.sh [--dry-run] [--rotate] <ssh-host> <device-name>
#   bash scripts/node/pair_device.sh [--dry-run] --list <ssh-host>
#   bash scripts/node/pair_device.sh [--dry-run] --revoke <device-name> <ssh-host>
#
# <ssh-host>  形如 mini2 / user@mini2；只允许 [A-Za-z0-9._@-]，且不能以 - 开头。
#             nodes.json 用 @ 后面的那段当 id。
# <device-name>  与 gateway 的 _DEVICE_RE 一致：^[A-Za-z0-9_.-]{1,64}$
# --dry-run   只打印将执行的动作（token 显示为 <redacted>），不 ssh、不写文件
# --rotate    同名设备已存在时换一把新 token；默认拒绝覆盖
# --list      只列设备名，不读 token 内容
# --revoke    删除该设备 token，再用旧 token 请求 /v1/info，必须得到 401
#
# token 只经 stdin / 临时文件。不进 ssh / curl / qrencode 的 argv。
# Mac 节点：~/.config/rs-node/tokens/<device>.token（目录 0700，文件 0600）
# Windows 节点：%LOCALAPPDATA%\rs-node\tokens\<device>.token（icacls 仅当前用户与 SYSTEM）
# 配对链接的 host/port/id 取自 ~/.config/rslite/nodes.json 的 url 与 node_id。
# /v1/info 钉在节点自己的 Tailscale IPv4 上，不连 url 里的主机名。

# xtrace 会把 token 打到 stderr。第一件事就是关掉。
set +x
set -euo pipefail
umask 077

NODE_PORT_DEFAULT=8791
LABEL="com.realtimesubtitle.node"
INFO_WAIT="${RS_INFO_WAIT:-15}"
INFO_SLEEP="${RS_INFO_SLEEP:-1}"
POWERSHELL='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

DRY_RUN=0
ROTATE=0
ACTION="pair"
HOST=""
DEVICE=""
OS=""
NODE_ID=""
NODE_HOST=""
NODE_PORT=""
PINNED_IP=""

TOKFILE=""
CFG=""
BODY=""

cleanup() {
    [ -z "${TOKFILE}" ] || rm -f "${TOKFILE}"
    [ -z "${CFG}" ] || rm -f "${CFG}"
    [ -z "${BODY}" ] || rm -f "${BODY}"
}
trap cleanup EXIT

log() { printf '%s\n' "$*" >&2; }
die() { printf '❌ %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：
  bash scripts/node/pair_device.sh [--dry-run] [--rotate] <ssh-host> <device-name>
  bash scripts/node/pair_device.sh [--dry-run] --list <ssh-host>
  bash scripts/node/pair_device.sh [--dry-run] --revoke <device-name> <ssh-host>

<ssh-host>     形如 mini2 / user@mini2。只允许 [A-Za-z0-9._@-]，且不能以 - 开头。
<device-name>  字母、数字、点、下划线、连字符，1–64 位。
--dry-run      只打印计划（token 显示为 <redacted>），不 ssh、不写文件。
--rotate       同名设备已存在时更换 token。
--list         列出已配对设备名（不读 token）。
--revoke       删除该设备 token，并确认旧 token 请求 /v1/info 得到 401。
EOF
}

# 与 scripts/node/install_node.sh 的 valid_host 一致。
valid_host() {
    [[ -n "$1" && "$1" =~ ^[A-Za-z0-9._@-]+$ && "$1" != -* ]]
}

# 与 realtime_subtitle/node/gateway.py 的 _DEVICE_RE 一致。
valid_device() {
    [[ "$1" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]
}

# 与 install_node.sh 的 is_ts_ipv4 一致（100.64.0.0/10）。
is_ts_ipv4() {
    local re='^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(0|[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.(0|[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-5])$'
    [[ $1 =~ $re ]]
}

need_int_env() {
    [[ "$2" =~ ^[0-9]+$ ]] || die "$1 不合法"
}

node_key() {
    printf '%s' "${HOST##*@}"
}

ssh_run() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" "$1"
}

# 远端登录 shell 可能是 zsh。脚本交给 bash -c，避免 zsh 的 nomatch。
ssh_bash() {
    local quoted
    quoted="$(printf '%q' "$1")"
    ssh_run "bash -c ${quoted}"
}

mktemp_secret() {
    local f
    f="$(mktemp)"
    chmod 600 "${f}"
    printf '%s\n' "${f}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=1; shift ;;
            --rotate) ROTATE=1; shift ;;
            --list) ACTION="list"; shift ;;
            --revoke)
                [[ $# -ge 2 ]] || die "--revoke 需要设备名"
                case "$2" in
                    -*) die "--revoke 需要设备名" ;;
                esac
                ACTION="revoke"
                DEVICE="$2"
                shift 2
                ;;
            -h|--help) usage; exit 0 ;;
            --) shift; break ;;
            -*) die "未知参数：$1" ;;
            *)
                if [[ -z "${HOST}" ]]; then
                    HOST="$1"
                elif [[ -z "${DEVICE}" ]]; then
                    DEVICE="$1"
                else
                    die "多余参数：$1"
                fi
                shift
                ;;
        esac
    done
    while [[ $# -gt 0 ]]; do
        if [[ -z "${HOST}" ]]; then
            HOST="$1"
        elif [[ -z "${DEVICE}" ]]; then
            DEVICE="$1"
        else
            die "多余参数：$1"
        fi
        shift
    done

    [[ -n "${HOST}" ]] || { usage >&2; die "需要 <ssh-host>"; }
    valid_host "${HOST}" || die "主机名不合法（只允许 [A-Za-z0-9._@-]，且不能以 - 开头）"

    case "${ACTION}" in
        pair)
            [[ -n "${DEVICE}" ]] || { usage >&2; die "需要 <device-name>"; }
            ;;
        list)
            [[ "${ROTATE}" -eq 0 ]] || die "--rotate 不能和 --list 一起用"
            [[ -z "${DEVICE}" ]] || die "--list 不接收设备名"
            ;;
        revoke)
            [[ "${ROTATE}" -eq 0 ]] || die "--rotate 不能和 --revoke 一起用"
            [[ -n "${DEVICE}" ]] || die "--revoke 需要设备名"
            ;;
        *) die "未知动作" ;;
    esac
    if [[ "${ACTION}" != "list" ]]; then
        valid_device "${DEVICE}" || die "设备名不合法（只允许 [A-Za-z0-9_.-]{1,64}）"
    fi
    if [[ "${ACTION}" != "pair" && "${ROTATE}" -eq 1 ]]; then
        die "--rotate 只用于配对"
    fi
}

print_dry_run() {
    local key
    key="$(node_key)"
    printf '%s\n' "[dry-run] 目标 ${HOST}（nodes.json id=${key}）。不 ssh、不写文件。token 显示为 <redacted>。"
    case "${ACTION}" in
        list)
            printf '%s\n' "[dry-run] 列出 tokens/*.token 的文件名。不读 token 内容。"
            printf '%s\n' "[dry-run] Mac：~/.config/rs-node/tokens    Windows：%LOCALAPPDATA%\\rs-node\\tokens"
            ;;
        revoke)
            printf '%s\n' "[dry-run] 读取并删除 tokens/${DEVICE}.token（内容 <redacted>），经 Tailscale 钉 IP 请求 /v1/info，要求 401。"
            printf '%s\n' "[dry-run] Mac：~/.config/rs-node/tokens/${DEVICE}.token (0600) 并 SIGHUP ${LABEL}"
            printf '%s\n' "[dry-run] Windows：%LOCALAPPDATA%\\rs-node\\tokens\\${DEVICE}.token （icacls 仅当前用户与 SYSTEM）"
            ;;
        pair)
            if [[ "${ROTATE}" -eq 1 ]]; then
                printf '%s\n' "[dry-run] --rotate：同名设备已存在也会更换 token（内容 <redacted>）。"
            else
                printf '%s\n' "[dry-run] 同名设备已存在则拒绝。要更换请加 --rotate。"
            fi
            printf '%s\n' "[dry-run] 控制端 openssl rand -hex 32 生成 token，只经 ssh stdin 写入。内容 <redacted>。"
            printf '%s\n' "[dry-run] Mac：~/.config/rs-node/tokens/${DEVICE}.token (0600)，目录 0700，然后 launchctl kill HUP ${LABEL}"
            printf '%s\n' "[dry-run] Windows：%LOCALAPPDATA%\\rs-node\\tokens\\${DEVICE}.token ，icacls 去掉继承，只留当前用户与 SYSTEM。"
            printf '%s\n' "[dry-run] 用该 token 请求 http://<Tailscale IP>:<port>/v1/info（token 从文件读，不进 argv）。核对 v=2 与 node_id。失败则删掉刚写的文件。"
            printf '%s\n' "[dry-run] 成功后打印 rsnode://pair?... 并用 qrencode -t ANSIUTF8 出码。没有 qrencode 则只打印链接，并提示 brew install qrencode。"
            ;;
    esac
}

require_tools() {
    command -v ssh >/dev/null 2>&1 || die "控制端没有 ssh"
    command -v python3 >/dev/null 2>&1 || die "控制端没有 python3"
    if [[ "${ACTION}" == "pair" ]]; then
        command -v openssl >/dev/null 2>&1 || die "控制端没有 openssl（生成 token 需要）"
    fi
    if [[ "${ACTION}" != "list" ]]; then
        command -v curl >/dev/null 2>&1 || die "控制端没有 curl"
    fi
}

load_node() {
    local file key fields rest
    file="${HOME}/.config/rslite/nodes.json"
    key="$(node_key)"
    [[ -f "${file}" ]] || die "没有 ${file}。请先安装节点。"
    fields="$(python3 -I - "${file}" "${key}" "${NODE_PORT_DEFAULT}" <<'PY'
import json, sys
from urllib.parse import urlsplit
path, key, default_port = sys.argv[1:4]
default_port = int(default_port)
try:
    entries = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    sys.exit("nodes.json 读不了")
if not isinstance(entries, list):
    sys.exit("nodes.json 不是数组")
for entry in entries:
    if isinstance(entry, dict) and entry.get("id") == key:
        url = entry.get("url")
        node_id = entry.get("node_id")
        if not isinstance(url, str) or not isinstance(node_id, str):
            sys.exit("节点条目缺少 url 或 node_id")
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or default_port
        sys.stdout.write(node_id + "\n" + host + "\n" + str(port))
        break
else:
    sys.exit("nodes.json 里没有 id=" + key)
PY
)" || die "读取 nodes.json 失败"
    NODE_ID="${fields%%$'\n'*}"
    rest="${fields#*$'\n'}"
    NODE_HOST="${rest%%$'\n'*}"
    NODE_PORT="${rest#*$'\n'}"
    [[ "${NODE_ID}" =~ ^[A-Za-z0-9_-]{8,64}$ ]] || die "node_id 格式异常"
    [[ "${NODE_HOST}" =~ ^[A-Za-z0-9.-]+$ ]] || die "节点主机名异常"
    [[ "${NODE_PORT}" =~ ^[0-9]+$ ]] || die "端口不合法"
    if [[ "${NODE_PORT}" -lt 1 || "${NODE_PORT}" -gt 65535 ]]; then
        die "端口不合法"
    fi
    if [[ "${NODE_HOST}" =~ ^[0-9.]+$ ]]; then
        is_ts_ipv4 "${NODE_HOST}" || die "节点 url 的 IP 不在 Tailscale 网段（100.64.0.0/10）"
    fi
}

detect_os() {
    local out
    out="$(ssh_run "uname -s" 2>/dev/null || true)"
    out="${out//$'\r'/}"
    out="${out%%$'\n'*}"
    if [[ "${out}" == "Darwin" ]]; then
        OS="mac"
    else
        OS="windows"
    fi
    log "目标机系统：${OS}"
}

fetch_pinned_ip() {
    local out cmd
    if [[ "${OS}" == "mac" ]]; then
        cmd="TAILSCALE_BE_CLI=1 tailscale ip -4"
    else
        cmd="tailscale ip -4"
    fi
    out="$(ssh_run "${cmd}")" || die "取不到目标机的 Tailscale IPv4"
    out="${out//$'\r'/}"
    PINNED_IP="${out%%$'\n'*}"
    is_ts_ipv4 "${PINNED_IP}" || die "目标机 Tailscale IP 不在 100.64.0.0/10：${PINNED_IP}"
    log "钉 IP ${PINNED_IP}（token <redacted>）"
}

mac_exists_cmd() {
    cat <<EOF
set +x
set -eu
f="\$HOME/.config/rs-node/tokens/${DEVICE}.token"
[ -f "\$f" ]
EOF
}

mac_write_cmd() {
    cat <<EOF
set +x
set -eu
umask 077
d="\$HOME/.config/rs-node"
mkdir -p "\$d/tokens"
chmod 700 "\$d" "\$d/tokens"
f="\$d/tokens/${DEVICE}.token"
if [ -e "\$f" ] && [ "${ROTATE}" != 1 ]; then
  echo exists >&2
  exit 2
fi
tmp="\$d/tokens/.${DEVICE}.\$\$.tmp"
cat > "\$tmp"
chmod 600 "\$tmp"
grep -qxE '^[0-9a-f]{64}\$' "\$tmp" || { rm -f "\$tmp"; echo "token shape" >&2; exit 1; }
mv -f "\$tmp" "\$f"
if command -v launchctl >/dev/null 2>&1; then
  launchctl kill HUP "gui/\$(id -u)/${LABEL}" || true
fi
EOF
}

mac_read_cmd() {
    cat <<EOF
set +x
set -eu
f="\$HOME/.config/rs-node/tokens/${DEVICE}.token"
[ -f "\$f" ] || { echo missing >&2; exit 1; }
cat "\$f"
EOF
}

mac_delete_cmd() {
    cat <<EOF
set +x
set -eu
f="\$HOME/.config/rs-node/tokens/${DEVICE}.token"
rm -f "\$f"
if command -v launchctl >/dev/null 2>&1; then
  launchctl kill HUP "gui/\$(id -u)/${LABEL}" || true
fi
EOF
}

mac_list_cmd() {
    cat <<'EOF'
set +x
set -eu
d="$HOME/.config/rs-node/tokens"
[ -d "$d" ] || exit 0
find "$d" -maxdepth 1 -type f -name '*.token' -print | LC_ALL=C sort | while IFS= read -r f; do
  name=${f##*/}
  name=${name%.token}
  printf '%s\n' "$name"
done
EOF
}

win_ps() {
    python3 -I - "$1" "$2" "$3" <<'PY'
import re, sys
op, device, rotate = sys.argv[1:4]
if op not in {"write", "list", "delete", "read", "exists"}:
    sys.exit("bad op")
if rotate not in {"0", "1"}:
    sys.exit("bad rotate")
if device and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device):
    sys.exit("bad device")
if op == "list" and device:
    sys.exit("bad device")
if op != "list" and not device:
    sys.exit("bad device")
header = (
    "$ErrorActionPreference = 'Stop'\n"
    "$RsOp = '%s'\n$RsDevice = '%s'\n$RsRotate = '%s'\n" % (op, device, rotate)
)
lock = r"""
function Lock-Down([string]$Path) {
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r 'SYSTEM:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'icacls failed' }
}
"""
path_setup = r"""
$dir = Join-Path $env:LOCALAPPDATA 'rs-node\tokens'
$path = Join-Path $dir ($RsDevice + '.token')
"""
bodies = {
    "exists": path_setup + r"""
if (Test-Path -LiteralPath $path) { exit 0 } else { exit 1 }
""",
    "list": r"""
$dir = Join-Path $env:LOCALAPPDATA 'rs-node\tokens'
if (-not (Test-Path -LiteralPath $dir)) { exit 0 }
Get-ChildItem -LiteralPath $dir -Filter '*.token' -File | ForEach-Object { $_.BaseName } | Sort-Object
""",
    "read": path_setup + r"""
if (-not (Test-Path -LiteralPath $path)) { Write-Error 'missing'; exit 1 }
$bytes = [IO.File]::ReadAllBytes($path)
[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)
""",
    "delete": path_setup + r"""
if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
""",
    "write": lock + path_setup + r"""
$tok = [Console]::In.ReadLine()
if ($tok -notmatch '^[0-9a-f]{64}$') { throw 'token shape' }
if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
Lock-Down $dir
if ($RsDevice -notmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'bad device' }
if ((Test-Path -LiteralPath $path) -and $RsRotate -ne '1') { Write-Error 'exists'; exit 2 }
$tmp = $path + '.tmp'
[IO.File]::WriteAllBytes($tmp, [Text.Encoding]::ASCII.GetBytes($tok))
Lock-Down $tmp
Move-Item -Force $tmp $path
Lock-Down $path
""",
}
sys.stdout.write(header + bodies[op])
PY
}

ps_encode() {
    python3 -I -c 'import sys,base64; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read().decode("utf-8").encode("utf-16le")).decode("ascii"))'
}

win_ssh() {
    local ps b64 remote
    ps="$(win_ps "$1" "$2" "${3:-0}")"
    b64="$(printf '%s' "${ps}" | ps_encode)"
    remote="${POWERSHELL} -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ${b64}"
    ssh_run "${remote}"
}

token_exists() {
    if [[ "${OS}" == "mac" ]]; then
        ssh_bash "$(mac_exists_cmd)"
    else
        win_ssh exists "${DEVICE}" 0 >/dev/null
    fi
}

generate_token() {
    TOKFILE="$(mktemp_secret)"
    openssl rand -hex 32 | tr -d '[:space:]' > "${TOKFILE}"
    chmod 600 "${TOKFILE}"
    grep -qxE '^[0-9a-f]{64}$' "${TOKFILE}" || die "token 生成失败"
    log "已生成 token（内容 <redacted>）"
}

write_token() {
    local rc=0
    if [[ "${OS}" == "mac" ]]; then
        ssh_bash "$(mac_write_cmd)" < "${TOKFILE}" >/dev/null && rc=0 || rc=$?
    else
        { cat "${TOKFILE}"; printf '\n'; } | win_ssh write "${DEVICE}" "${ROTATE}" >/dev/null && rc=0 || rc=$?
    fi
    if [[ "${rc}" -eq 2 ]]; then
        die "设备 ${DEVICE} 已存在，拒绝覆盖。要更换请加 --rotate"
    fi
    [[ "${rc}" -eq 0 ]] || die "token 写入目标机失败"
    log "已写入 tokens/${DEVICE}.token（内容 <redacted>）"
}

delete_token() {
    if [[ "${OS}" == "mac" ]]; then
        ssh_bash "$(mac_delete_cmd)" >/dev/null
    else
        win_ssh delete "${DEVICE}" 0 >/dev/null
    fi
}

read_token_to_file() {
    local rc=0
    TOKFILE="$(mktemp_secret)"
    if [[ "${OS}" == "mac" ]]; then
        ssh_bash "$(mac_read_cmd)" > "${TOKFILE}" && rc=0 || rc=$?
    else
        win_ssh read "${DEVICE}" 0 > "${TOKFILE}" && rc=0 || rc=$?
    fi
    chmod 600 "${TOKFILE}"
    if [[ "${rc}" -ne 0 ]]; then
        die "节点上没有设备 ${DEVICE} 的 token"
    fi
    # 旧文件可能带换行。比对和请求都用去掉空白后的 64 位十六进制。
    if ! python3 -I - "${TOKFILE}" <<'PY'
import sys
path = sys.argv[1]
raw = open(path, encoding="utf-8").read().strip()
if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
    sys.exit(1)
with open(path, "w", encoding="ascii") as f:
    f.write(raw)
PY
    then
        die "读到的 token 形状不对"
    fi
    chmod 600 "${TOKFILE}"
}

prepare_curl_config() {
    CFG="$(mktemp_secret)"
    BODY="$(mktemp_secret)"
    python3 -I - "${PINNED_IP}" "${NODE_PORT}" "${TOKFILE}" "${CFG}" "${BODY}" <<'PY'
import sys
ip, port, tok_file, cfg, body = sys.argv[1:6]
try:
    tok = open(tok_file, encoding="utf-8").read().strip()
except OSError:
    sys.exit("cannot read token file")
if len(tok) != 64 or any(c not in "0123456789abcdef" for c in tok):
    sys.exit("token file unusable")
text = "\n".join([
    "silent",
    "show-error",
    'noproxy = "*"',
    "max-time = 5",
    'url = "http://%s:%s/v1/info"' % (ip, port),
    'header = "Authorization: Bearer %s"' % tok,
    'output = "%s"' % body,
    'write-out = "%{http_code}"',
    "",
])
with open(cfg, "w", encoding="utf-8") as f:
    f.write(text)
PY
    chmod 600 "${CFG}" "${BODY}"
}

http_code() {
    local code
    code="$(curl --config "${CFG}" || true)"
    code="${code//$'\r'/}"
    code="${code//$'\n'/}"
    printf '%s' "${code}"
}

classify_body() {
    python3 -I - "${BODY}" "${NODE_ID}" <<'PY'
import json, sys
path, expect = sys.argv[1:3]
try:
    data = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    sys.stdout.write("retry")
    raise SystemExit(0)
got = data.get("node_id")
if data.get("v") == 2 and got == expect and expect:
    sys.stdout.write("match")
elif isinstance(got, str) and got != expect:
    sys.stdout.write("mismatch")
else:
    sys.stdout.write("retry")
PY
}

poll_info() {
    # $1 = match | 401
    local deadline code kind
    deadline=$((SECONDS + INFO_WAIT))
    while :; do
        code="$(http_code)"
        if [[ "$1" == "401" ]]; then
            [[ "${code}" == "401" ]] && return 0
        elif [[ "${code}" == "200" ]]; then
            kind="$(classify_body)"
            [[ "${kind}" == "match" ]] && return 0
            [[ "${kind}" == "mismatch" ]] && return 3
        fi
        [[ "${SECONDS}" -ge "${deadline}" ]] && break
        [[ "${INFO_SLEEP}" == "0" ]] || sleep "${INFO_SLEEP}"
    done
    return 1
}

rollback_written() {
    log "删除刚写入的 tokens/${DEVICE}.token"
    delete_token || log "删除失败。请在节点上手动删掉 tokens/${DEVICE}.token"
}

print_pair_link() {
    local link
    link="$(python3 -I - "${NODE_ID}" "${NODE_HOST}" "${NODE_PORT}" "${DEVICE}" "${TOKFILE}" <<'PY'
import sys
from urllib.parse import quote
node_id, host, port, name, tok_file = sys.argv[1:6]
tok = open(tok_file, encoding="utf-8").read().strip()
def q(value):
    return quote(value, safe="")
sys.stdout.write(
    "rsnode://pair?v=1&id=%s&host=%s&port=%s&tok=%s&name=%s"
    % (q(node_id), q(host), q(str(port)), q(tok), q(name))
)
PY
)"
    printf '%s\n' "${link}"
    if command -v qrencode >/dev/null 2>&1; then
        printf '%s' "${link}" | qrencode -t ANSIUTF8
    else
        printf '%s\n' "brew install qrencode"
    fi
}

do_list() {
    detect_os
    log "已配对设备（只列名字）："
    if [[ "${OS}" == "mac" ]]; then
        ssh_bash "$(mac_list_cmd)" | tr -d '\r'
    else
        win_ssh list "" 0 | tr -d '\r'
    fi
}

do_revoke() {
    local code rc=0
    load_node
    detect_os
    fetch_pinned_ip
    read_token_to_file
    delete_token
    prepare_curl_config
    log "已删除 tokens/${DEVICE}.token，正在用旧 token 请求 /v1/info（内容 <redacted>）"
    if poll_info 401; then
        log "吊销已确认：/v1/info 返回 401"
        return 0
    fi
    code="$(http_code)"
    log "吊销未确认（/v1/info 返回 ${code:-无响应}，不是 401）。正在写回 token（内容 <redacted>）"
    ROTATE=1
    write_token || die "吊销没有得到 401，而且 token 写回失败"
    die "吊销没有得到 401，已把 token 写回"
}

do_pair() {
    local rc=0
    load_node
    detect_os
    fetch_pinned_ip
    if [[ "${ROTATE}" != 1 ]] && token_exists; then
        die "设备 ${DEVICE} 已存在，拒绝覆盖。要更换请加 --rotate"
    fi
    generate_token
    write_token
    prepare_curl_config
    log "正在请求 /v1/info 核对 node_id（token <redacted>）"
    poll_info match && rc=0 || rc=$?
    if [[ "${rc}" -eq 3 ]]; then
        rollback_written
        die "/v1/info 的 node_id 与 nodes.json 不一致，已删除刚写入的 token"
    fi
    if [[ "${rc}" -ne 0 ]]; then
        rollback_written
        die "/v1/info 核对失败，已删除刚写入的 token"
    fi
    log "核对通过（token <redacted>）"
    print_pair_link
}

main() {
    parse_args "$@"
    need_int_env RS_INFO_WAIT "${INFO_WAIT}"
    need_int_env RS_INFO_SLEEP "${INFO_SLEEP}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        print_dry_run
        return 0
    fi
    require_tools
    case "${ACTION}" in
        list) do_list ;;
        revoke) do_revoke ;;
        pair) do_pair ;;
    esac
}

main "$@"
