#!/usr/bin/env bash
# 启动实时字幕（macOS）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

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
    for _ in $(seq 1 30); do
        if ollama_ready; then
            break
        fi
        sleep 2
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
