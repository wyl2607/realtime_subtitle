#!/usr/bin/env bash
# 停止实时字幕（macOS）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

touch "${REPO_ROOT}/.stop"
echo "已写入停止标记 .stop，等程序自行优雅退出..."

# ☠️ 必须先等进程退出再卸模型（避坑第 13 条）：翻译请求带 keep_alive=2h，
# 卸载之后才落地的请求会把模型重新拉回内存留驻两小时。程序自己的优雅退出
# 本来就会等在飞请求、再卸载；这里的卸载只是它没退干净时的兜底
PID="$(cat "${REPO_ROOT}/subtitle.pid" 2>/dev/null || true)"
if [[ -n "${PID}" ]]; then
    for _ in $(seq 1 20); do
        kill -0 "${PID}" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "${PID}" 2>/dev/null; then
        echo "20 秒内没退出，强制结束 (PID ${PID})"
        kill "${PID}" 2>/dev/null || true
    fi
fi

if ! curl -fsS --max-time 2 "http://127.0.0.1:11434/api/version" >/dev/null; then
    echo "Ollama 服务未运行，跳过模型卸载。"
    exit 0
fi
# 只卸当前确实驻留的模型里属于本程序的那几个（/api/ps 列出来的），
# 用 HTTP keep_alive=0，不用 ollama 的 stop 子命令（见避坑第 7 条）
"${REPO_ROOT}/venv/bin/python" - <<'PY' || true
import requests
from realtime_subtitle import config
ours = {getattr(config, "OLLAMA_MODEL", ""), getattr(config, "GAME_MODE_OLLAMA_MODEL", "")}
ours |= set(getattr(config, "TRANSLATION_TIERS", []) or [])
base = "http://127.0.0.1:11434"
for m in requests.get(f"{base}/api/ps", timeout=3).json().get("models", []):
    if m.get("name") in ours:
        requests.post(f"{base}/api/generate",
                      json={"model": m["name"], "prompt": "", "keep_alive": 0}, timeout=20)
        print(f"已卸载 Ollama 模型 {m['name']}")
PY
