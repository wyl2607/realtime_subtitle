#!/usr/bin/env bash
# ============================================================
# 实时字幕翻译系统 一键安装脚本（macOS）
#
# 用法：
#   bash scripts/macos/install.sh
#   bash scripts/macos/install.sh --mirror     # 国内网络走清华 PyPI 镜像
#
# 做的事（和 scripts/windows/install.ps1 一一对应）：
#   0. 拦住太老的 macOS；非 Apple Silicon 只告警不拦
#   1. Homebrew 依赖（ffmpeg / ollama / uv）
#   2. 用 uv 建 venv（Python 3.12）+ 装 requirements-macos.txt
#   3. 编 Swift 音频采集助手（tools/macos/sc_audio_tap，没有就跳过并告警）
#   4. config_local.py：已存在一律保留
#   5. 拉起 Ollama + 拉翻译模型（模型名从最终生效的配置里读）
#   6. 在桌面生成「德语实时字幕」启动器文件夹
#   7. 装完自检 + 讲清楚"屏幕与系统音频录制"权限怎么给
# 全程可重复运行（幂等），中断后重跑即可。
#
# ☠️ 和 Windows 那版最不一样的一处：**权限**。Windows 上 WASAPI Loopback 不需要
# 用户授权；macOS 上采集系统音频必须先在「系统设置 → 隐私与安全性 → 屏幕与系统
# 音频录制」里授权，脚本自己给不了（那是 TCC 的事）。所以第 7 步必须把这件事
# 讲清楚——装完第一次启动却抓不到声音，99% 是这里。
# ============================================================
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"
# shellcheck source=scripts/macos/_deps.sh
. "$RTS_SCRIPT_DIR/_deps.sh"

MIRROR=""
for arg in "$@"; do
    case "$arg" in
        --mirror) MIRROR=--mirror ;;
        -h|--help)
            say "用法：bash scripts/macos/install.sh [--mirror]"
            exit 0 ;;
        *) die "不认识的参数：${arg}（--help 看用法）" ;;
    esac
done

say ""
say "=========================================="
say "   实时字幕翻译系统 - 安装程序（macOS）"
say "=========================================="
say ""

# ---------- 0. 系统检查 ----------
say "[1/7] 检查系统..."
if [ "$(uname -s)" != "Darwin" ]; then
    die "❌ 这个脚本只能在 macOS 上跑（Windows 请用 scripts\\windows\\install.ps1）。"
fi
MACOS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
MACOS_NAME="$(sw_vers -productName 2>/dev/null || echo macOS)"
MACOS_BUILD="$(sw_vers -buildVersion 2>/dev/null || echo '?')"
case "$MACOS_MAJOR" in
    ''|*[!0-9]*) MACOS_MAJOR=0 ;;
esac
if [ "$MACOS_MAJOR" -lt 14 ]; then
    say "  ❌ 需要 macOS 14 或更新的系统，当前是 $MACOS_NAME ${MACOS_MAJOR}。"
    say "     原因：音频采集助手走 ScreenCaptureKit（系统音频的官方接口），"
    say "     低版本上拿不到音频流。系统设置 → 通用 → 软件更新，升完再重跑本脚本。"
    exit 1
fi
say "  ✅ $MACOS_NAME ${MACOS_MAJOR}（${MACOS_BUILD}）"
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
    say "  ✅ Apple Silicon（${ARCH}）"
else
    # 不拦：ctranslate2 在 x86_64 上有 cpu 轮子，只是慢一大截（转录延迟会明显
    # 高于实时）。拦下来只会让人以为"装不了"，不如装上再按慢机器的档位调。
    warn "  ⚠️  当前不是 Apple Silicon（${ARCH}）。能装能跑，但识别会明显慢于 M 系；"
    warn "     若只是想要能跑：install.sh 会把识别配置压到 CPU + int8。"
fi

command -v brew >/dev/null 2>&1 || die \
"❌ 没有 Homebrew（ffmpeg / Ollama / uv 都靠它装）。
   先装 Homebrew（官方脚本，会要你的登录密码）：
     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
   装完按它提示把 brew 加进 PATH，重新开个终端再跑本脚本。"

# ---------- 1. Homebrew 依赖 ----------
say "[2/7] 检查 Homebrew 依赖（ffmpeg / ollama / uv）..."
for formula in ffmpeg ollama uv; do
    if brew list --formula 2>/dev/null | grep -qx "$formula"; then
        say "  ✅ $formula 已安装"
    else
        say "  正在安装 $formula （第一次会下载编译/拉镜像，请耐心）..."
        if ! brew install "$formula"; then
            die "  ❌ brew install $formula 失败。常见原因是网络或 Xcode 命令行工具缺失；
     先跑 xcode-select --install，装完再重跑本脚本。"
        fi
    fi
done
# 装完当前进程的 PATH 不会刷新（brew 的 bin 目录可能刚加进 shell 配置），
# 不能只靠 command -v
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
have_ffmpeg="$(command -v ffmpeg || true)"
[ -n "$have_ffmpeg" ] || die "❌ ffmpeg 装完仍然找不到（brew 的 PATH 没生效）。请重开一个终端再跑本脚本。"
have_ollama="$(ollama_exe || true)"
[ -n "$have_ollama" ] || die "❌ Ollama 装完仍然找不到。装完 Ollama.app 的话重开终端；或 brew reinstall ollama"
have_uv="$(command -v uv || true)"
[ -n "$have_uv" ] || die "❌ uv 装完仍然找不到。brew install uv 之后重开终端再试。"

# ---------- 2. venv + 依赖 ----------
say "[3/7] 建 venv 并装依赖（首次需要几分钟）..."
# ☠️ 用 uv 自己托管的 Python 3.12，不要用系统 python3：
#   macOS 自带的 /usr/bin/python3 跟着系统走（小版本随时会变），而 PyQt6 /
#   ctranslate2 这些包对具体小版本敏感。uv 装的是独立的一份，版本固定。
"$have_uv" python install 3.12 >/dev/null 2>&1 || true
if ! "$have_uv" venv --python 3.12 --seed "$RTS_REPO_ROOT/venv"; then
    die "❌ 建 venv 失败。若提示下载 Python 失败，多半是网络问题（国内可先
     export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple）。"
fi
VENV_PY="$RTS_REPO_ROOT/venv/bin/python"
[ -x "$VENV_PY" ] || die "❌ venv 建好了但找不到解释器：$VENV_PY"

# ☠️ 不能退回 requirements.txt：那份是 Windows 的（CUDA 运行库、torch、
# pyaudiowpatch），在 macOS 上要么装不上、要么白占几个 GB。
REQ_FILE="$RTS_REPO_ROOT/requirements-macos.txt"
[ -f "$REQ_FILE" ] || die "❌ 仓库里没有 requirements-macos.txt，代码不完整，先 git pull 再装。"
say "  依赖清单：$(basename "$REQ_FILE")"
extra=()
if [ -n "$MIRROR" ]; then
    extra+=(--index-url https://pypi.tuna.tsinghua.edu.cn/simple)
fi
if ! "$have_uv" pip install --python "$VENV_PY" "${extra[@]+"${extra[@]}"}" -r "$REQ_FILE"; then
    die "❌ 依赖安装失败，请检查网络后重跑（国内网络加 --mirror 参数）"
fi
ok "  ✅ 依赖安装完成"
# 指纹要写成功：不然下次启动会以为"依赖还没装"又重装一遍（Windows 侧同样）
deps_fingerprint_write

# ---------- 3. Swift 音频采集助手 ----------
say "[4/7] 编译音频采集助手（Swift）..."
TAP_DIR="$RTS_REPO_ROOT/tools/macos/sc_audio_tap"
if [ ! -f "$TAP_DIR/Package.swift" ]; then
    warn "  ⚠️  仓库里没有 tools/macos/sc_audio_tap（音频采集助手还没就位？），跳过编译。"
    warn "     没有它的话程序抓不到系统声音——装好之后重跑本脚本这一步即可。"
elif ! command -v swift >/dev/null 2>&1; then
    warn "  ⚠️  没有 swift 命令，编译不了音频采集助手。"
    warn "     装 Xcode 命令行工具：xcode-select --install（装完重开终端再重跑本脚本）"
else
    if (cd "$TAP_DIR" && swift build -c release); then
        ok "  ✅ 音频采集助手已编译：tools/macos/sc_audio_tap/.build/release"
    else
        warn "  ⚠️  编译音频采集助手失败。程序会起来但抓不到声音，"
        warn "     报错在 subtitle.err.log 里；修好之后重跑本脚本即可。"
    fi
fi

# ---------- 4. 本机配置 ----------
# config_local.py 会覆盖 config.py 的同名配置（config.py 末尾 import 它）。
# ⚠️ 已存在的 config_local.py 一律保留（可能是人工调过的）——想按硬件重新生成
# 就先删掉它。Windows 那版在这里按显存写一整套分档，macOS 这版**刻意不写**：
# ctranslate2 没有 Metal/CUDA 后端，macOS 只有 cpu 一档，档位差异只剩"识别模型
# 多大"，而那个属于 config.py 的默认值该管的事（见 REQUESTS）。这里只在
# **配置真的跑不起来**的时候补最小的一行，别让两个真相源打架。
say "[5/7] 检查本机配置（config_local.py）..."
cfg_device="$(rts_read_config WHISPER_DEVICE | sed -n '1p')"
cfg_compute="$(rts_read_config WHISPER_COMPUTE_TYPE | sed -n '1p')"
LOCAL_CFG="$RTS_REPO_ROOT/config_local.py"
if [ -f "$LOCAL_CFG" ]; then
    say "  ℹ️ 已有 config_local.py，保留现有本机配置（想重新生成：删掉它再重跑）"
elif [ "$cfg_device" != "cpu" ] || [ "$cfg_compute" = "float16" ]; then
    cat > "$LOCAL_CFG" <<'EOF_CFG'
# 本机降级配置（install.sh 自动生成）
#
# ctranslate2 在 macOS 上没有 CUDA/Metal 后端，只有 cpu 一档，所以这两行是
# "能不能跑起来"的问题，不是"跑多快"的问题——float16 + cuda 在 macOS 上会
# 直接加载失败。
#
# 识别模型大小 / 翻译模型大小请改 config.py 的默认值或直接在这里加：
#   16GB 统一内存：WHISPER_MODEL="medium"、OLLAMA_MODEL="qwen3.5:4b" 比较稳
#   8GB  或更小： WHISPER_MODEL="small"、OLLAMA_MODEL="qwen3.5:2b"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
EOF_CFG
    ok "  ✅ 已生成 config_local.py（cpu + int8，macOS 上唯一可用的一档）"
else
    say "  ✅ config.py 的默认值在 macOS 上可用（$cfg_device / ${cfg_compute}），不用写 config_local.py"
fi
RAM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
case "$RAM_BYTES" in ''|*[!0-9]*) RAM_BYTES=0 ;; esac
say "  ℹ️  本机统一内存 $(format_size_kb $((RAM_BYTES / 1024)))。嫌慢/嫌占地方就改 config_local.py 里的模型大小（见上面注释）"

# ---------- 5. Ollama + 翻译模型 ----------
say "[6/7] 检查 Ollama（本地翻译模型运行时）..."
if ! ollama_ensure_serving; then
    die "❌ 等了 60 秒 Ollama 服务还没就绪。
   手动开个终端运行 ollama serve 看报什么错，或 brew services restart ollama。"
fi
ok "  ✅ Ollama 已在运行（${have_ollama}）"
# 模型名从「最终生效的配置」里读（config.py + 刚生成的 config_local.py），
# 保证脚本和程序运行时用的一定是同一个模型，不会各说各话
cfg_models="$(rts_read_config OLLAMA_MODEL GAME_MODE_OLLAMA_MODEL)"
# 去重 + 跳过空值（bash 3.2 没有数组可去重用，靠 sort -u）
model_list="$(mktemp -t rts_models)"
# 用 trap 收：die 也会走到退出，顺手就把临时文件带走了
trap 'rm -f "$model_list"' EXIT
printf '%s\n' "$cfg_models" | grep -v '^[[:space:]]*$' | sort -u > "$model_list"
while IFS= read -r model; do
    [ -n "$model" ] || continue
    if ollama_model_present "$model"; then
        ok "  ✅ 翻译模型 $model 已就绪"
    else
        if ! ollama_pull "$model"; then
            die "❌ 拉取 $model 失败。若报模型不存在：qwen 系列命名可能已迭代，
     到 https://ollama.com/library 查最新名字，写进 config_local.py 的
     OLLAMA_MODEL 后重跑本脚本。"
        fi
    fi
done < "$model_list"

# ---------- 6. 桌面启动器 ----------
say "[7/7] 生成桌面启动器..."
SHORTCUT_DIR="$HOME/Desktop/德语实时字幕"
mkdir -p "$SHORTCUT_DIR"
# ☠️ 这张表是桌面入口的**单一真相源**，tests/test_macos_scripts.py 盯着它：
# 每个 .command 必须真的存在（脚本里调用的那个 .sh 要在），且每个启动器都得在
# docs/macos.md 里被解释过。以前 Windows 那侧这三处（install.ps1 的表 /
# 操作说明模板 / README）各写各的，改一处漏两处。
for launcher in "$RTS_SCRIPT_DIR"/launchers/*.command; do
    [ -f "$launcher" ] || continue
    name="$(basename "$launcher")"
    cp -f "$launcher" "$SHORTCUT_DIR/$name"
    chmod +x "$SHORTCUT_DIR/$name"
done
# 启动器要知道自己指向哪个仓库：仓库里的启动器靠自己的位置往上找，被拷到桌面
# 之后那个位置就失效了，所以旁边放一个 .repo-root 记着绝对路径。
printf '%s\n' "$RTS_REPO_ROOT" > "$SHORTCUT_DIR/.repo-root"
# 操作说明从 docs/macos.md 生成（单一真相源，和 Windows 侧从模板生成同一思路）
if [ -f "$RTS_REPO_ROOT/docs/macos.md" ]; then
    cp -f "$RTS_REPO_ROOT/docs/macos.md" "$SHORTCUT_DIR/操作说明.md"
fi
# ☠️ 一定要清 quarantine 属性：用户如果是从浏览器下载的 zip 解出来的仓库，
# 每个文件都带"来自互联网"的标记，双击 .command 会被 Gatekeeper 拦成
# "无法验证开发者"。跟 Windows 上杀毒软件误报是同一类问题。
xattr -dr com.apple.quarantine "$SHORTCUT_DIR" 2>/dev/null || true
# ☠️ 按**确切文件名**清掉已退役的入口，不扫目录：这个文件夹是用户的，
# 他可能往里放了自己的东西。装了新版本的人桌面上还留着旧名字的启动器，
# 而旧脚本还在被调用，于是"合并入口"对老用户等于没发生。
for old in "启动并更新字幕.command" "更新字幕.command" "下载并加字幕.command"; do
    if [ -f "$SHORTCUT_DIR/$old" ]; then
        rm -f "$SHORTCUT_DIR/$old"
        say "  🧹 已移除退役入口: $old"
    fi
done
ok "  ✅ 启动器已生成：$SHORTCUT_DIR"

# ---------- 7. 装完自检 ----------
# 装完不验一下的话，环境问题要等用户双击启动、悬浮窗不出来、再去翻
# subtitle.err.log 才暴露。这里当场把最容易坏的几类依赖走一遍。
say ""
say "验证安装..."
smoke="$("$VENV_PY" -c '
import torch
import PyQt6.QtWidgets, yt_dlp, soxr
from realtime_subtitle import config
from realtime_subtitle.translate import translator_queue
translator_queue._ensure_ml_deps()
print("SMOKE_OK", config.WHISPER_MODEL, config.WHISPER_COMPUTE_TYPE, config.OLLAMA_MODEL)
' 2>&1)"
if printf '%s' "$smoke" | grep -qE "SMOKE_OK[[:space:]]+"; then
    ok "  ✅ 依赖自检通过：$(printf '%s' "$smoke" | grep 'SMOKE_OK' | tail -n 1)"
else
    warn "  ⚠️  依赖自检没通过，程序大概率起不来。报错如下："
    printf '%s\n' "$smoke" | tail -n 12 | while IFS= read -r line; do
        warn "     $line"
    done
    warn "     常见原因：装到了 Windows 版的轮子、缺 Xcode 工具链（swift 助手没编出来）、"
    warn "     网络装漏了包（重跑本脚本即可）。"
fi

say ""
say "=========================================="
say "🎉 安装完成！"
say "   双击桌面「德语实时字幕」文件夹里的「启动字幕.command」开始使用"
say "=========================================="
say ""
say "⚠️  首次启动前一定要做这一步（macOS 特有，Windows 不需要）："
say ""
say "   1. 打开「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」"
say "   2. 把开关打开——被请求的程序一般是「终端 / Terminal」"
say "      （你从哪儿启动的，就给哪个：终端 App、VS Code、或你自己的启动器）"
say "   3. 没看到开关？先双击一次「启动字幕.command」，系统会弹授权框；"
say "      弹框点过之后它才会出现在这个列表里"
say "   4. 授权后**重启字幕**（停一次再启一次），然后放一段德语音视频试"
say ""
say "   没给权限的症状：悬浮窗正常起来，但一个字都不出（subtitle.log 里会有"
say "   一行采集失败的提示）。"

