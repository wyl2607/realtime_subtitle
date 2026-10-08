#!/usr/bin/env bash
# ============================================================
# 实时字幕翻译系统 一键安装脚本（macOS / Apple Silicon）
#
# 用法：
#   bash scripts/macos/install.sh
# 可选：
#   --mirror        使用清华 PyPI 镜像
#   --lean          硬盘/内存紧张：Whisper 改用 turbo-q4
#   --skip-models   跳过 ollama pull
#   --force-config  覆盖已有 config_local.py
#   --dry-run       只打印将执行的动作和将生成的配置，不写文件
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

USE_MIRROR=0
LEAN=0
SKIP_MODELS=0
FORCE_CONFIG=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mirror|-Mirror)
            USE_MIRROR=1
            ;;
        --lean)
            LEAN=1
            ;;
        --skip-models)
            SKIP_MODELS=1
            ;;
        --force-config)
            FORCE_CONFIG=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            sed -n '1,22p' "$0"
            exit 0
            ;;
        *)
            echo "❌ 未知参数：$1"
            exit 1
            ;;
    esac
    shift
done

run_cmd() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        shift
        printf '  [dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        shift
        "$@"
    fi
}

model_rank() {
    case "$1" in
        qwen3.5:2b) echo 2 ;;
        qwen3.5:4b) echo 4 ;;
        qwen3.5:9b) echo 9 ;;
        *) echo 999 ;;
    esac
}

echo ""
echo "=========================================="
echo "   实时字幕翻译系统 - macOS 安装程序"
echo "=========================================="
echo ""

echo "[1/5] 检查系统环境..."
ARCH="$(uname -m)"
if [[ "${ARCH}" != "arm64" ]]; then
    echo "  ❌ 当前是 ${ARCH}，macOS 版只支持 Apple Silicon (arm64)。"
    exit 1
fi
echo "  ✅ Apple Silicon (${ARCH})"

if ! command -v brew >/dev/null 2>&1; then
    echo "  ❌ 没有找到 Homebrew。请先安装 Homebrew 后重跑。"
    exit 1
fi
echo "  ✅ Homebrew"

if ! command -v uv >/dev/null 2>&1; then
    echo "  ❌ 没有找到 uv。请先运行：brew install uv"
    exit 1
fi
echo "  ✅ uv"

if ! command -v ollama >/dev/null 2>&1; then
    echo "  ❌ 没有找到 Ollama。请先运行：brew install ollama"
    exit 1
fi
echo "  ✅ Ollama"

echo "[2/5] 按内存选择模型档位..."
MEM_BYTES="$(sysctl -n hw.memsize)"
MEM_GB=$(( (MEM_BYTES + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024) ))
if (( MEM_BYTES < 12 * 1024 * 1024 * 1024 )); then
    WHISPER_MLX_REPO="mlx-community/whisper-large-v3-turbo-q4"
    OLLAMA_MODEL="qwen3.5:2b"
    TIER="<12GB 轻量档"
elif (( MEM_BYTES < 32 * 1024 * 1024 * 1024 )); then
    WHISPER_MLX_REPO="mlx-community/whisper-large-v3-turbo"
    OLLAMA_MODEL="qwen3.5:4b"
    TIER="12-31GB 均衡档"
else
    WHISPER_MLX_REPO="mlx-community/whisper-large-v3-turbo"
    OLLAMA_MODEL="qwen3.5:9b"
    TIER="≥32GB 高配档"
fi
if [[ "${LEAN}" -eq 1 ]]; then
    WHISPER_MLX_REPO="mlx-community/whisper-large-v3-turbo-q4"
    TIER="${TIER} + --lean"
fi
echo "  ✅ 内存约 ${MEM_GB}GB：${TIER}"
echo "     Whisper: ${WHISPER_MLX_REPO}"
echo "     翻译: ${OLLAMA_MODEL}"

if OLLAMA_LIST="$(ollama list 2>/dev/null || true)"; then
    SELECTED_RANK="$(model_rank "${OLLAMA_MODEL}")"
    REUSABLE=()
    for m in qwen3.5:2b qwen3.5:4b qwen3.5:9b; do
        if (( "$(model_rank "${m}")" <= SELECTED_RANK )) && grep -Eq "(^|[[:space:]])${m}([[:space:]]|$)" <<<"${OLLAMA_LIST}"; then
            REUSABLE+=("${m}")
        fi
    done
    if [[ "${#REUSABLE[@]}" -gt 0 ]]; then
        echo "  ℹ️ 已有同系列小/同档模型：${REUSABLE[*]}（可在 config_local.py 里手动复用）"
    fi
fi

echo "[3/5] 准备 Python 运行环境..."
UV_PIP_ARGS=(env "VIRTUAL_ENV=${REPO_ROOT}/venv" uv pip install -r requirements.txt)
if [[ "${USE_MIRROR}" -eq 1 ]]; then
    UV_PIP_ARGS+=(--index-url https://pypi.tuna.tsinghua.edu.cn/simple)
fi
run_cmd "uv venv" uv venv venv -p 3.12
run_cmd "uv pip install" "${UV_PIP_ARGS[@]}"

CONFIG_CONTENT="$(cat <<EOF
# macOS Apple Silicon 本机配置（install.sh 自动生成）
# 档位：${TIER}（检测到内存约 ${MEM_GB}GB）
# P0 实测依据：
# - whisper-large-v3-turbo fp16：WER 4.05%，约 2.1GB
# - whisper-large-v3-turbo-q4：WER 4.84%，约 1.1GB
# - qwen3.5:2b/4b/9b 约 2.4GB / 3.2GB / 6.2GB；16GB 机器上 9b 单独跑已有内存压力
# --lean 会把 Whisper 固定到 q4：省约 1.1GB 硬盘与 1GB 内存，代价是 WER 4.05% → 4.84%
WHISPER_BACKEND = "mlx"
WHISPER_MLX_REPO = "${WHISPER_MLX_REPO}"
OLLAMA_MODEL = "${OLLAMA_MODEL}"
EOF
)"

echo "[4/5] 生成本机配置 config_local.py..."
if [[ -f "${REPO_ROOT}/config_local.py" && "${FORCE_CONFIG}" -ne 1 ]]; then
    echo "  ℹ️ 已有 config_local.py，保留现有本机配置（要覆盖请加 --force-config）"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] 若加 --force-config，将写入："
        printf '%s\n' "${CONFIG_CONTENT}" | sed 's/^/    /'
    fi
else
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] 将写入 config_local.py："
        printf '%s\n' "${CONFIG_CONTENT}" | sed 's/^/    /'
    else
        printf '%s\n' "${CONFIG_CONTENT}" > "${REPO_ROOT}/config_local.py"
        echo "  ✅ 已生成 config_local.py"
    fi
fi

echo "[5/6] 构建 Apple Translation helper..."
if command -v swift >/dev/null 2>&1 && [[ -d "${REPO_ROOT}/macos-native" ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] (cd macos-native && swift build -c release --product rstranslate)"
    elif (cd "${REPO_ROOT}/macos-native" && swift build -c release --product rstranslate); then
        echo "  ✅ rstranslate 已构建"
    else
        echo "  ⚠️  rstranslate 构建失败，实时句子翻译会回退 Ollama"
    fi
else
    echo "  ℹ️ 未找到 swift 或 macos-native/，跳过 rstranslate 构建（Ollama 路径仍可用）"
fi

echo "[6/6] 准备翻译模型..."
if [[ "${SKIP_MODELS}" -eq 1 ]]; then
    echo "  ℹ️ 已跳过 ollama pull（--skip-models）"
else
    run_cmd "ollama pull" ollama pull "${OLLAMA_MODEL}"
fi

echo ""
echo "✅ macOS 安装步骤已完成。"
echo ""
echo "BlackHole 提示：macOS 抓系统声音需要："
echo "  brew install blackhole-2ch"
echo "  然后在「音频 MIDI 设置」里创建「多输出设备」，同时勾选扬声器和 BlackHole 2ch。"
echo ""
echo "磁盘占用预估："
echo "  Python 依赖：约 1-2GB"
echo "  Whisper：turbo 约 2.1GB；turbo-q4 约 1.1GB（首次启动自动下载）"
echo "  Ollama：${OLLAMA_MODEL} 约 $([[ "${OLLAMA_MODEL}" == "qwen3.5:2b" ]] && echo "2.4GB" || ([[ "${OLLAMA_MODEL}" == "qwen3.5:4b" ]] && echo "3.2GB" || echo "6.2GB"))"
