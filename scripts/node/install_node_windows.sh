#!/usr/bin/env bash
# 把 Realtime Subtitle 节点（v2 gateway + worker）装到一台 Windows 机器上（在 Mac 控制端运行）。
#
# 用法：bash scripts/node/install_node_windows.sh <ssh 别名> [--dry-run] [--client <Windows客户端ssh别名>]
#
# 与 install_node.sh（装到 Mac）同一套契约：token 只经 stdin/文件、node_id 沿用、
# 旧版保留为 %USERPROFILE%\rs-node.prev、自检失败回滚、写本机 ~/.config/rslite/nodes.json。
# Windows 端差异：计划任务（登录时、当前用户会话，CUDA 需要）代替 LaunchAgent；
# icacls 代替 chmod 600；防火墙只放行 Tailscale 网段；翻译走本机 Ollama（默认 qwen3.5:4b）。
# 目标机要求：OpenSSH Server、Python 3.11+、git、Tailscale、Ollama 已装且已拉好翻译模型。
set -euo pipefail
set +x  # 脚本经手 token，xtrace 会把它打到 stderr

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# 复用 nodes.json 合并、本地写 secret、主机名校验（被 source 时 install_node.sh 不执行 main）
# shellcheck source=install_node.sh
source "${SCRIPT_DIR}/install_node.sh"

NODE_PORT="${RS_NODE_PORT:-8791}"
valid_tcp_port "${NODE_PORT}" || die "端口不合法"
OLLAMA_MODEL="${RS_OLLAMA_MODEL:-qwen3.5:4b}"
INFO_WAIT="${RS_INFO_WAIT:-90}"
SELFCHECK_WAV_SRC="${RS_SELFCHECK_WAV:-${HOME}/projects/rs-mac-native-data/concat5.wav}"
PS_REMOTE='%LOCALAPPDATA%\rs-node\node_setup.ps1'
POWERSHELL='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

HOST=""
DRY_RUN=0
CLIENT=""

win_run() {  # 目标机 cmd.exe 执行；stdin 原样透传
    ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" "$1" 2> >(grep -v -E 'post-quantum|store now, decrypt later|openssh.com/pq.html' >&2)
}

ps_action() {  # $1=Action，其余为额外参数（非敏感）
    local action="$1"
    shift
    win_run "${POWERSHELL} -NoProfile -ExecutionPolicy Bypass -File \"${PS_REMOTE}\" -Action ${action} -Port ${NODE_PORT} -OllamaModel ${OLLAMA_MODEL} $*" | tr -d '\r'
}

kv() { grep -m1 "^$1=" <<<"$2" | cut -d= -f2-; }

usage_win() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

parse_args_win() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --client)
                # 这里只 shift 一次：循环末尾还有一次 shift，两次合起来吃掉别名。
                [[ $# -ge 2 ]] || die "--client 需要一个 ssh 别名"
                CLIENT="$2"
                valid_host "${CLIENT}" || die "客户端别名不合法：${CLIENT}"
                shift
                ;;
            -h | --help) usage_win; exit 0 ;;
            -*) die "未知参数：$1" ;;
            *) [[ -z "${HOST}" ]] || die "只能指定一个目标主机"; HOST="$1" ;;
        esac
        shift
    done
    [[ -n "${HOST}" ]] || { usage_win; exit 2; }
    valid_host "${HOST}" || die "主机名含非法字符：${HOST}"
}

print_plan() {
    plan "1. 上传 node_setup.ps1 → %LOCALAPPDATA%\\rs-node\\；preflight：python/git/uv/tailscale/nvidia-smi/ollama 模型 ${OLLAMA_MODEL}/磁盘"
    plan "2. node_id：%LOCALAPPDATA%\\rs-node\\node_id（已有沿用，icacls 仅当前用户）"
    plan "3. token：目标机 %LOCALAPPDATA%\\rs-node\\token（已有沿用，否则沿用客户端副本或新生成；仅经 stdin），内容 <redacted>"
    plan "   客户端副本 ~/.config/rslite/tokens/${HOST##*@}.token (0600)"
    plan "4. git archive HEAD → %USERPROFILE%\\rs-node.new（tar 经 ssh stdin）"
    plan "5. 建 .venv、装 requirements.txt + node/requirements.txt，写 config_local.py（OLLAMA_MODEL=${OLLAMA_MODEL}）"
    plan "6. 自检：加载 faster-whisper、用 concat5 前 14 秒实测 rtf → asr_state.json"
    plan "7. 停旧 gateway（按命令行精确匹配）→ rs-node→rs-node.prev、rs-node.new→rs-node → 计划任务 rs-node-gateway（登录时）→ 防火墙 TCP ${NODE_PORT} 仅 100.64.0.0/10 → 启动"
    plan "8. 控制端经 Tailscale 带 token 请求 /v1/info（${INFO_WAIT}s 内 v=2 且 node_id 匹配），失败 → 回滚"
    plan "9. 写 ~/.config/rslite/nodes.json（按 id=${HOST##*@} 去重、原子写）：url=ws://<MagicDNS>:${NODE_PORT}"
    if [[ -n "${CLIENT}" ]]; then
        plan "另：下发到 Windows 客户端 ${CLIENT}：scp push_client.ps1，再 powershell -File PushClient；按 id 合并 %APPDATA%\\realtime_subtitle\\nodes.json；token 经 stdin 写入 tokens\\<id>.token 并 icacls；内容 <redacted>"
    fi
}

info_ok() {  # $1=url $2=token 文件 $3=node_id；token 只从文件读，不进 argv
    python3 -I - "$1" "$2" "$3" <<'PY'
import json, sys, urllib.request
url, tok_file, node_id = sys.argv[1:4]
tok = open(tok_file, encoding="utf-8").read().strip()
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.load(r)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("v") == 2 and d.get("node_id") == node_id and node_id else 1)
PY
}

main_win() {
    parse_args_win "$@"
    if [[ "${DRY_RUN}" -eq 1 ]]; then print_plan; return 0; fi
    command -v openssl >/dev/null || die "控制端没有 openssl（生成 token 需要）"
    if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
        warn "工作区有未提交改动；只会部署已提交的 HEAD"
    fi

    info "[1/9] 上传安装脚本 + preflight..."
    win_run 'if not exist "%LOCALAPPDATA%\rs-node" mkdir "%LOCALAPPDATA%\rs-node"' >/dev/null
    scp -q -o BatchMode=yes -- "${SCRIPT_DIR}/windows/node_setup.ps1" "${HOST}:AppData/Local/rs-node/node_setup.ps1" \
        || die "上传 node_setup.ps1 失败"
    local pre ts_ip ts_dns
    pre="$(ps_action Preflight)" || die "preflight 失败：${pre:-无输出}"
    printf '%s\n' "${pre}" | sed 's/^/  /'
    ts_ip="$(kv TS_IP "${pre}")"
    ts_dns="$(kv TS_DNS "${pre}")"
    [[ "${ts_ip}" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+$ ]] || die "目标机 Tailscale IP 异常：${ts_ip}"
    [[ "${ts_dns}" =~ ^[A-Za-z0-9.-]+$ ]] || die "目标机 MagicDNS 名异常"
    [[ "$(kv OLLAMA_MODEL "${pre}")" == present ]] || die "目标机没有 Ollama 模型 ${OLLAMA_MODEL}：先在目标机 ollama pull ${OLLAMA_MODEL}"

    info "[2/9] 确保 node_id..."
    NODE_ID="$(kv NODE_ID "$(ps_action NodeId)")"
    [[ "${NODE_ID}" =~ ^[A-Za-z0-9_-]{8,64}$ ]] || die "目标机上的 node_id 格式异常"

    info "[3/9] 确保 token（只经 stdin 传递）..."
    local client_file existing
    client_file="${HOME}/.config/rslite/tokens/${HOST##*@}.token"
    existing="$(ps_action ReadToken || true)"
    if [[ "${existing}" =~ ^[0-9a-f]{64}$ ]]; then
        NODE_TOKEN="${existing}"
    elif [[ -f "${client_file}" && "$(cat "${client_file}")" =~ ^[0-9a-f]{64}$ ]]; then
        NODE_TOKEN="$(cat "${client_file}")"
    else
        NODE_TOKEN="$(openssl rand -hex 32)"
    fi
    [[ "${NODE_TOKEN}" =~ ^[0-9a-f]{64}$ ]] || die "token 生成失败"
    # 沿用时也重写一遍：顺带把 ACL 收紧到位
    printf '%s\n' "${NODE_TOKEN}" | ps_action WriteToken >/dev/null || die "token 写入目标机失败"
    write_secret_file "${client_file}" "${NODE_TOKEN}"

    info "[4/9] 部署 HEAD 到 %USERPROFILE%\\rs-node.new ..."
    git -C "${REPO_ROOT}" archive --format=tar HEAD \
        | win_run '(if exist "%USERPROFILE%\rs-node.new" rmdir /s /q "%USERPROFILE%\rs-node.new") & mkdir "%USERPROFILE%\rs-node.new" && tar -xf - -C "%USERPROFILE%\rs-node.new"' \
        || die "部署失败"
    # 写入代码版本（短哈希）到 .rs_commit，供 cluster.py doctor 读取
    local short_commit
    short_commit=$(git -C "${REPO_ROOT}" rev-parse --short HEAD)
    win_run "cmd /c echo ${short_commit} > \"%USERPROFILE%\\rs-node.new\\.rs_commit\"" \
        || die "写入 .rs_commit 失败"
    if [[ -f "${SELFCHECK_WAV_SRC}" ]]; then
        local wav_tmp
        wav_tmp="$(mktemp -t rs-selfcheck).wav"
        python3 -I - "${SELFCHECK_WAV_SRC}" "${wav_tmp}" <<'PY'
import sys, wave
with wave.open(sys.argv[1]) as w:
    frames = w.readframes(min(w.getnframes(), 14 * w.getframerate()))
    params = w.getparams()
with wave.open(sys.argv[2], "wb") as o:
    o.setparams(params)
    o.writeframes(frames)
PY
        scp -q -o BatchMode=yes -- "${wav_tmp}" "${HOST}:rs-node.new/selfcheck.wav" || warn "自检音频上传失败，跳过 rtf 实测"
        rm -f "${wav_tmp}"
    fi

    info "[5/9] 安装 Python 环境（首次要下载几个 GB）..."
    ps_action Build | sed 's/^/  /' || { win_run 'rmdir /s /q "%USERPROFILE%\rs-node.new"' || true; die "目标机安装失败（旧版未受影响）"; }

    info "[6/9] 自检：识别引擎 + rtf（首次会下载 Whisper 模型）..."
    local sc
    sc="$(ps_action SelfCheck)" || { printf '%s\n' "${sc:-}" >&2; win_run 'rmdir /s /q "%USERPROFILE%\rs-node.new"' || true; die "目标机自检失败（旧版未受影响）"; }
    printf '%s\n' "${sc}" | sed 's/^/  /'

    info "[7/9] 换名、注册计划任务、防火墙、启动..."
    ps_action Swap | sed 's/^/  /' || { ps_action Rollback | sed 's/^/  /' || true; die "换名/启动失败，已回滚"; }

    info "[8/9] 自检 /v1/info（经 Tailscale，最多 ${INFO_WAIT} 秒）..."
    local url="http://${ts_ip}:${NODE_PORT}/v1/info" ok=0 i
    for ((i = 0; i < INFO_WAIT; i += 3)); do
        if info_ok "${url}" "${client_file}" "${NODE_ID}"; then ok=1; break; fi
        sleep 3
    done
    if [[ "${ok}" != 1 ]]; then
        ps_action Rollback | sed 's/^/  /' || true
        die "gateway ${INFO_WAIT} 秒内没有通过 /v1/info 自检（v==2 且 node_id 匹配），已回滚；日志见目标机 %LOCALAPPDATA%\\rs-node\\logs\\gateway.log"
    fi
    info "  ✅ /v1/info 通过"

    info "[9/9] 写客户端 nodes.json..."
    merge_nodes_json "${HOME}/.config/rslite/nodes.json" "${HOST##*@}" "${NODE_ID}" \
        "ws://${ts_dns}:${NODE_PORT}" "${client_file}"
    info "  ✅ ${HOST##*@} → ws://${ts_dns}:${NODE_PORT}"
    NODE_DNS="${ts_dns}"
    step_push_client
    info ""
    info "✅ Windows 节点安装完成。日志：目标机 %LOCALAPPDATA%\\rs-node\\logs\\gateway.log"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main_win "$@"
fi
