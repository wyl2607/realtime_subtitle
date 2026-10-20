#!/usr/bin/env bash
# 启动实时字幕（macOS）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

NO_UPDATE=0
UPDATE_MIRROR=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-update)
            NO_UPDATE=1
            ;;
        --mirror|-Mirror)
            UPDATE_MIRROR=1
            ;;
        -h|--help)
            echo "用法：bash scripts/macos/start.sh [--no-update] [--mirror]"
            exit 0
            ;;
        *)
            echo "❌ 未知参数：$1"
            exit 1
            ;;
    esac
    shift
done

get_head_commit() {
    if ! command -v git >/dev/null 2>&1 || [[ ! -d "${REPO_ROOT}/.git" ]]; then
        return 0
    fi
    git rev-parse HEAD 2>/dev/null || true
}

subtitle_running() {
    local pid
    pid="$(cat "${REPO_ROOT}/subtitle.pid" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    return 1
}

BEFORE_COMMIT="$(get_head_commit)"
if [[ "${RS_NO_UPDATE:-0}" == "1" || "${NO_UPDATE}" -eq 1 ]]; then
    echo "已跳过启动前更新（--no-update / RS_NO_UPDATE=1）。"
else
    if [[ "${UPDATE_MIRROR}" -eq 1 ]]; then
        if ! /bin/bash "${SCRIPT_DIR}/update.sh" --mirror; then
            echo ""
            echo "⚠️ 更新没能完成（原因见上面几行），改用当前已装好的版本继续启动。"
        fi
    else
        if ! /bin/bash "${SCRIPT_DIR}/update.sh"; then
            echo ""
            echo "⚠️ 更新没能完成（原因见上面几行），改用当前已装好的版本继续启动。"
        fi
    fi
fi
AFTER_COMMIT="$(get_head_commit)"
CODE_CHANGED=0
if [[ -n "${BEFORE_COMMIT}" && -n "${AFTER_COMMIT}" && "${BEFORE_COMMIT}" != "${AFTER_COMMIT}" ]]; then
    CODE_CHANGED=1
fi

if [[ "${CODE_CHANGED}" -eq 1 ]] && subtitle_running; then
    echo ""
    echo "拉到了新版本，正在停掉当前字幕好让新版生效..."
    echo "（沿用 Windows 启动入口的约定：代码真变了才重启，更新失败不影响启动。）"
    /bin/bash "${SCRIPT_DIR}/stop.sh" || true
elif subtitle_running; then
    echo "字幕已经在运行，而且没有拉到新代码——不打断它。"
    exit 0
fi

if [[ ! -x "${REPO_ROOT}/venv/bin/python" ]]; then
    echo "❌ 还没有安装运行环境（venv 不存在）。请先运行 bash scripts/macos/install.sh"
    exit 1
fi
if ! command -v ollama >/dev/null 2>&1; then
    echo "❌ 找不到 Ollama。请先运行：brew install ollama"
    exit 1
fi

rm -f "${REPO_ROOT}/.stop" "${REPO_ROOT}/.paused"

ollama_ready() {
    curl -fsS --max-time 3 "http://127.0.0.1:11434/api/version" >/dev/null
}

echo "检查 Ollama 服务..."
if ! ollama_ready; then
    echo "正在启动 Ollama..."
    (ollama serve >> "${REPO_ROOT}/ollama.log" 2>> "${REPO_ROOT}/ollama.err.log" &)
    # 200ms 轮询：Ollama 冷启动本机实测 ~8.7 秒，固定 sleep 2 平均多等 1 秒
    # （最坏 2 秒）。总预算仍是 60 秒，只是把"检查→睡 2 秒"换成细粒度重试。
    for _ in $(seq 1 300); do
        if ollama_ready; then
            break
        fi
        sleep 0.2
    done
    if ! ollama_ready; then
        echo "❌ 等了 60 秒 Ollama 服务还没就绪。可手动运行 ollama serve 查看报错。"
        exit 1
    fi
fi

echo "启动实时字幕..."
nohup "${REPO_ROOT}/venv/bin/python" -u main.py \
    > "${REPO_ROOT}/subtitle.log" \
    2> "${REPO_ROOT}/subtitle.err.log" &
echo "$!" > "${REPO_ROOT}/subtitle.pid"
echo "已启动 (PID $!)，运行日志: subtitle.log / subtitle.err.log"
