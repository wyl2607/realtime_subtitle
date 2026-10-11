#!/usr/bin/env bash
# ============================================================
# 一键更新：从 GitHub 拉最新版 + 按需同步依赖（macOS）
#
# 不会动的东西（都不在 git 里）：
#   config_local.py / window_state.json / transcripts/ / downloads/
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

USE_MIRROR=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mirror|-Mirror)
            USE_MIRROR=1
            ;;
        -h|--help)
            sed -n '1,18p' "$0"
            exit 0
            ;;
        *)
            echo "❌ 未知参数：$1"
            exit 1
            ;;
    esac
    shift
done

read_local_version() {
    local f="${REPO_ROOT}/realtime_subtitle/version.py"
    if [[ ! -f "${f}" ]]; then
        printf '?'
        return
    fi
    sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${f}" | head -n 1
}

deps_fingerprint_path() {
    printf '%s\n' "${REPO_ROOT}/venv/.deps_fingerprint"
}

current_deps_fingerprint() {
    local req="${REPO_ROOT}/requirements.txt"
    if [[ ! -f "${req}" ]]; then
        printf ''
        return
    fi
    if command -v shasum >/dev/null 2>&1; then
        { printf 'macos\n'; cat "${req}"; } | shasum -a 256 | awk '{print $1}'
    else
        { printf 'macos\n'; cat "${req}"; } | openssl dgst -sha256 | awk '{print $NF}'
    fi
}

stored_deps_fingerprint() {
    local fp
    fp="$(deps_fingerprint_path)"
    if [[ -f "${fp}" ]]; then
        tr -d '\r\n' < "${fp}"
    fi
}

write_deps_fingerprint() {
    local fp="$1"
    mkdir -p "${REPO_ROOT}/venv"
    printf '%s\n' "${fp}" > "$(deps_fingerprint_path)"
}

sync_dependencies() {
    # Keep this in sync with scripts/macos/install.sh step [3/5]:
    #   uv venv venv -p 3.12（venv 不存在时）
    #   env VIRTUAL_ENV="${REPO_ROOT}/venv" uv pip install -r requirements.txt [--index-url ...]
    if ! command -v uv >/dev/null 2>&1; then
        echo "❌ 没有找到 uv。请先运行：brew install uv"
        return 1
    fi
    # ☠️ uv 遇到已存在的 venv 直接报错退出（不会替换），不加判断的话每次依赖变化都会
    # 被当成「同步失败」回滚代码；重建也不行——会把几个 GB 的 MLX/torch 全部重下
    if [[ ! -x "${REPO_ROOT}/venv/bin/python" ]]; then
        uv venv venv -p 3.12 || return 1
    fi
    if [[ "${USE_MIRROR}" -eq 1 ]]; then
        env "VIRTUAL_ENV=${REPO_ROOT}/venv" uv pip install -r requirements.txt \
            --index-url https://pypi.tuna.tsinghua.edu.cn/simple
    else
        env "VIRTUAL_ENV=${REPO_ROOT}/venv" uv pip install -r requirements.txt
    fi
}

rollback_code_update() {
    local why="$1"
    if [[ "${CODE_CHANGED}" -ne 1 ]]; then
        echo "❌ ${why}"
        echo "   下次启动会继续重试依赖同步。"
        return 1
    fi
    if git reset --hard --quiet "${OLD_COMMIT}"; then
        echo "❌ ${why}"
        echo "   代码已退回 v${OLD_VERSION} (${OLD_COMMIT_SHORT})，避免新代码配旧依赖。"
        echo "   下次启动会自动重试这次更新。"
    else
        echo "❌ ${why}"
        echo "   ⚠️ 代码没能退回旧版本，新代码配旧依赖可能起不来。"
        echo "   处理办法：把这几行发给 AI 助手，或手动运行 git reset --hard ${OLD_COMMIT}"
    fi
    return 1
}

if ! command -v git >/dev/null 2>&1; then
    echo "❌ 没有安装 git，无法自动更新。"
    echo "   安装：brew install git（或 https://git-scm.com/download/mac）"
    exit 1
fi
if [[ ! -d "${REPO_ROOT}/.git" ]]; then
    echo "❌ 本目录不是 git 克隆（可能当初是解压 zip 装的），无法增量更新。"
    echo "   建议重装：git clone https://github.com/wyl2607/realtime_subtitle.git"
    echo "   然后运行 bash scripts/macos/install.sh（个人配置 config_local.py 可以拷过去）"
    exit 1
fi

if ! git diff-index --quiet HEAD --; then
    echo "❌ 工作区里有本地改过的仓库文件，这次先不自动更新。"
    echo "   处理办法：git stash 暂存后重试；个人调参请挪到 config_local.py。"
    exit 1
fi

OLD_VERSION="$(read_local_version)"
if [[ -z "${OLD_VERSION}" ]]; then
    OLD_VERSION="?"
fi
OLD_COMMIT="$(git rev-parse HEAD)"
OLD_COMMIT_SHORT="$(printf '%s' "${OLD_COMMIT}" | cut -c1-7)"
echo "当前版本：v${OLD_VERSION} (${OLD_COMMIT_SHORT})"
echo "正在检查更新..."

if [[ -z "${GIT_HTTP_LOW_SPEED_LIMIT:-}" ]]; then
    export GIT_HTTP_LOW_SPEED_LIMIT=1000
fi
if [[ -z "${GIT_HTTP_LOW_SPEED_TIME:-}" ]]; then
    export GIT_HTTP_LOW_SPEED_TIME=20
fi
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never

if ! git fetch --quiet; then
    echo ""
    echo "❌ 检查更新失败：连不上 GitHub（断网、被墙或网络很慢）。这次先不更新。"
    exit 1
fi

UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [[ -z "${UPSTREAM}" ]]; then
    echo ""
    echo "❌ 当前分支没有跟踪远端分支，不知道该更新到哪里。"
    echo "   处理办法：切回有 upstream 的分支后重试，或把这几行发给 AI 助手。"
    exit 1
fi

if ! git merge --ff-only --quiet '@{u}'; then
    echo ""
    echo "❌ 更新失败。最常见原因：本地直接改过仓库文件，与新版本冲突。"
    echo "   个人调参请写在 config_local.py（永远不会冲突），不要直接改 config.py。"
    echo "   处理办法：git stash 暂存本地改动后重试，或把改动挪到 config_local.py。"
    exit 1
fi

NEW_COMMIT="$(git rev-parse HEAD)"
NEW_COMMIT_SHORT="$(printf '%s' "${NEW_COMMIT}" | cut -c1-7)"
CODE_CHANGED=0
if [[ "${OLD_COMMIT}" != "${NEW_COMMIT}" ]]; then
    CODE_CHANGED=1
fi

CURRENT_FP="$(current_deps_fingerprint)"
STORED_FP="$(stored_deps_fingerprint)"
NEED_DEPS=0
if [[ -z "${CURRENT_FP}" || "${STORED_FP}" != "${CURRENT_FP}" ]]; then
    NEED_DEPS=1
fi

if [[ "${CODE_CHANGED}" -eq 0 && "${NEED_DEPS}" -eq 0 ]]; then
    echo "✅ 已经是最新版本（代码和依赖都无需更新）。"
    exit 0
fi

NEW_VERSION="$(read_local_version)"
if [[ -z "${NEW_VERSION}" ]]; then
    NEW_VERSION="?"
fi
echo ""
if [[ "${CODE_CHANGED}" -eq 1 ]]; then
    if [[ "${NEW_VERSION}" != "${OLD_VERSION}" ]]; then
        echo "版本：v${OLD_VERSION} → v${NEW_VERSION}"
    else
        echo "版本：v${NEW_VERSION}（版本号未变，是修补更新）"
    fi
    echo "本次更新内容："
    git log --oneline --no-decorate "${OLD_COMMIT}..${NEW_COMMIT}"
    echo ""
else
    echo "代码已经最新，但依赖上次安装失败或不完整，正在补装..."
fi

if [[ "${NEED_DEPS}" -eq 1 ]]; then
    echo "正在同步依赖（使用 scripts/macos/install.sh 的 uv 安装路径）..."
    if ! sync_dependencies; then
        rollback_code_update "依赖同步失败，请检查网络后重试（大陆网络可加 --mirror）。"
        exit 1
    fi
    write_deps_fingerprint "${CURRENT_FP}"
fi

if [[ "${CODE_CHANGED}" -eq 1 ]] \
    && git diff --name-only "${OLD_COMMIT}" "${NEW_COMMIT}" | grep -q '^macos-native/' \
    && command -v swift >/dev/null 2>&1 \
    && [[ -d "${REPO_ROOT}/macos-native" ]]; then
    echo "macos-native/ 有更新，正在重建 rstranslate..."
    if (cd "${REPO_ROOT}/macos-native" && swift build -c release --product rstranslate); then
        echo "✅ rstranslate 已构建"
    else
        echo "⚠️  rstranslate 构建失败，实时句子翻译会回退 Ollama"
    fi
fi

echo ""
echo "🎉 更新完成。"
