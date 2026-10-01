#!/usr/bin/env bash
# ============================================================
# 实时字幕翻译系统 卸载 / 清理脚本（macOS）
#
# 用法：bash scripts/macos/uninstall.sh
#       bash scripts/macos/uninstall.sh --clean-cache
#
# 为什么需要这个脚本：这套系统装完摊在好几个位置，其中三个在仓库目录之外
# （HuggingFace 模型缓存、Ollama 模型、Ollama 程序本体）。只删仓库目录的话
# 十几 GB 里只能收回 venv 那几 GB，剩下的会静默留在硬盘上——而想卸载的人
# 通常正是嫌它占地方。所以这里逐项列出、逐项问。
#
# 原则（和 scripts/windows/uninstall.ps1 一致）：
#   - 每一步先报体积再问 y/N，默认 N，不做"一把梭全删"
#   - 只碰这个项目自己产生的东西（HuggingFace 缓存里只动 faster-whisper 的模型）
#   - 用户数据（字幕存档/下载视频、字幕和学习笔记/个人配置/窗口位置）默认保留，只告诉在哪
# ============================================================
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"

CLEAN_CACHE=0
for arg in "$@"; do
    case "$arg" in
        --clean-cache) CLEAN_CACHE=1 ;;
        -h|--help)
            say "用法：bash scripts/macos/uninstall.sh [--clean-cache]"
            say "  --clean-cache  只清垃圾不卸载（删中断下载的残文件 + 空壳模型目录）"
            exit 0 ;;
        *) die "不认识的参数：${arg}（--help 看用法）" ;;
    esac
done

# 默认 N：回车 = 不删。卸载脚本手滑的代价是重下十几 GB，不值得图省事
confirm_step() {
    local msg="$1" ans=""
    printf '%s  [y/N] ' "$msg" >&2
    IFS= read -r ans || ans=""
    case "$ans" in y|Y) return 0 ;; *) return 1 ;; esac
}

remove_path() {
    local path="$1" label="$2"
    if rm -rf "$path" 2>/dev/null; then
        ok "     ✅ 已删除 $label"
    else
        warn "     ❌ 删除失败 $label"
        warn "        （文件可能被占用：先关掉字幕程序再重试）"
    fi
}

HUB_DIR="$(hf_hub_dir)"
SHORTCUT_DIR="$HOME/Desktop/德语实时字幕"

say ""
say "=========================================="
if [ "$CLEAN_CACHE" -eq 1 ]; then
    say "   实时字幕 - 缓存清理（不卸载）"
else
    say "   实时字幕 - 卸载"
fi
say "=========================================="
say ""

# ---------- 清理模式：只删中断下载的残文件和空壳目录 ----------
if [ "$CLEAN_CACHE" -eq 1 ]; then
    say "[1/2] 扫描中断的下载残文件（*.incomplete）..."
    freed=0
    if [ -d "$HUB_DIR" ]; then
        incomplete="$(find "$HUB_DIR" -type f -name '*.incomplete' 2>/dev/null)"
        if [ -n "$incomplete" ]; then
            # 先量体积再列清单：顺序反过来的话，"合计多大" 会打在明细前面
            bytes=0
            count=0
            while IFS= read -r f; do
                [ -f "$f" ] || continue
                count=$((count + 1))
                bytes=$((bytes + $(stat -f '%z' "$f")))
            done <<EOF
$incomplete
EOF
            say "  发现 $count 个残文件，合计 $(format_size_kb $((bytes / 1024)))："
            while IFS= read -r f; do
                [ -f "$f" ] || continue
                printf '     %s\t%s\n' \
                    "$(format_size_kb $(( $(stat -f '%z' "$f") / 1024 )))" "$(basename "$f")"
            done <<EOF
$incomplete
EOF
            if confirm_step "  删掉这些残文件？（下次要用会重新下载）"; then
                while IFS= read -r f; do
                    [ -f "$f" ] && rm -f "$f"
                done <<EOF
$incomplete
EOF
                freed=$((freed + bytes))
                ok "     ✅ 已清理"
            fi
        else
            ok "  ✅ 没有残文件"
        fi
    else
        say "  ℹ️ 没有 HuggingFace 缓存目录，跳过"
    fi

    say "[2/2] 扫描没有完整模型的空壳目录..."
    # 中断的下载会留下一个有 blobs/refs 但没有 snapshots/model.bin 的目录，
    # 占几 GB 却完全没用，faster-whisper 下次启动会绕过它重新下载
    shells=0
    for d in "$HUB_DIR"/models--*faster-whisper*; do
        [ -d "$d" ] || continue
        if ! find "$d" -type f -name 'model.bin' -print -quit 2>/dev/null | grep -q .; then
            shells=1
            kb="$(dir_size_kb "$d")"
            say "  $(basename "$d") —— $(format_size_kb "$kb")，没有 model.bin（下载没完成）"
            if confirm_step "  删掉它？"; then
                remove_path "$d" "$(basename "$d")"
                freed=$((freed + kb * 1024))
            fi
        fi
    done
    [ "$shells" -eq 0 ] && ok "  ✅ 没有空壳目录"

    say ""
    say "🎉 清理完成，共收回约 $(format_size_kb $((freed / 1024)))。程序不受影响，照常用。"
    exit 0
fi

# ---------- 卸载模式 ----------
# ☠️ 模型名必须在删 venv 之前读：venv 一没，import config 就跑不了了
tx_model=""
if [ -x "$RTS_REPO_ROOT/venv/bin/python" ]; then
    tx_model="$(rts_read_config OLLAMA_MODEL | sed -n '1p')"
fi
[ -n "$tx_model" ] || say "  ℹ️ 读不到配置里的翻译模型名（venv 可能已删），到时候手动选"

say "先看看各部分占多少地方："
say ""
venv_kb="$(dir_size_kb "$RTS_REPO_ROOT/venv")"
whisper_kb=0
for d in "$HUB_DIR"/models--*faster-whisper*; do
    [ -d "$d" ] && whisper_kb=$((whisper_kb + $(dir_size_kb "$d")))
done
ollama_prog_kb=0
for candidate in /opt/homebrew/Cellar/ollama /usr/local/Cellar/ollama \
                 "$HOME/.ollama" /Applications/Ollama.app; do
    [ -e "$candidate" ] && ollama_prog_kb="$(dir_size_kb "$candidate")"
done

say "   venv（Python 依赖）        $(printf '%10s' "$(format_size_kb "$venv_kb")")"
say "   Whisper 识别模型缓存       $(printf '%10s' "$(format_size_kb "$whisper_kb")")"
say "   Ollama 翻译模型               见下（ollama list）"
say "   Ollama 程序本体            $(printf '%10s' "$(format_size_kb "$ollama_prog_kb")")  ← 要单独卸"
say ""

# 1. 先停掉在跑的程序，否则 venv 里的文件删不掉
say "[1/6] 停止正在运行的字幕..."
if [ -f "$(repo_file subtitle.pid)" ]; then
    if bash "$RTS_SCRIPT_DIR/stop_subtitles.sh" >/dev/null 2>&1; then
        ok "  ✅ 已停止"
    else
        warn "  ⚠️  停止脚本报错，继续（如果后面删 venv 失败，先手动关掉字幕）"
    fi
else
    say "  ✅ 没在运行"
fi

# 2. 桌面启动器文件夹
say "[2/6] 桌面启动器文件夹..."
if [ -d "$SHORTCUT_DIR" ]; then
    if confirm_step "  删除「${SHORTCUT_DIR}」？"; then
        remove_path "$SHORTCUT_DIR" "桌面启动器"
    fi
else
    say "  ✅ 不存在"
fi

# 3. venv
say "[3/6] venv（Python 依赖，$(format_size_kb "$venv_kb")）..."
if [ -d "$RTS_REPO_ROOT/venv" ]; then
    if confirm_step "  删除 venv？（想再用就重跑 install.sh）"; then
        remove_path "$RTS_REPO_ROOT/venv" "venv"
    fi
else
    say "  ✅ 不存在"
fi

# 4. Whisper 模型缓存（单独问：可能别的项目也在用 faster-whisper）
say "[4/6] Whisper 识别模型缓存（$(format_size_kb "$whisper_kb")）..."
found_model=0
for d in "$HUB_DIR"/models--*faster-whisper*; do
    [ -d "$d" ] || continue
    found_model=1
    say "     $(basename "$d") —— $(format_size_kb "$(dir_size_kb "$d")")"
done
if [ "$found_model" -eq 1 ]; then
    say "  ℹ️ 只列出 faster-whisper 的模型，缓存里别的模型不会动"
    if confirm_step "  删除这些模型？（重装后首次启动要重新下 1-3GB）"; then
        for d in "$HUB_DIR"/models--*faster-whisper*; do
            [ -d "$d" ] && remove_path "$d" "$(basename "$d")"
        done
    fi
else
    say "  ✅ 没有找到"
fi

# 5. Ollama 翻译模型
say "[5/6] Ollama 翻译模型..."
if ollama_exe >/dev/null 2>&1; then
    ollama list
    if [ -z "$tx_model" ]; then
        say "  ℹ️ 读不到配置里的模型名，需要的话自己 ollama rm <名字>"
    elif confirm_step "  删除本项目用的模型 $tx_model ？"; then
        ollama rm "$tx_model"
        ok "     ✅ 已删除 $tx_model"
    fi
else
    say "  ✅ 没装 Ollama"
fi
say "  ℹ️ 上面列表里别的模型可能是你其它项目在用的，这里不动"

# 6. Ollama 程序本体：不代劳，因为很可能有别的项目在用它
say "[6/6] Ollama 程序本体（$(format_size_kb "$ollama_prog_kb")）..."
if [ "$ollama_prog_kb" -gt 0 ]; then
    say "  ℹ️ Ollama 是独立安装的服务，可能有别的项目在用，本脚本不代劳。"
    say "     确定不用了就自己跑： brew uninstall ollama"
else
    say "  ✅ 没装"
fi

# 用户数据一律保留，只告诉在哪
say ""
say "------------------------------------------"
say "以下是你的数据，本脚本一律不动，要删自己删："
say "   字幕存档   $RTS_REPO_ROOT/transcripts/"
say "   下载字幕   $RTS_REPO_ROOT/downloads/"
say "   个人配置   $RTS_REPO_ROOT/config_local.py"
say "   窗口位置   $RTS_REPO_ROOT/window_state.json"
say "   运行日志   $RTS_REPO_ROOT/subtitle.log / subtitle.err.log / logs/"
say ""
say "整个仓库目录（含以上数据）在："
say "   $RTS_REPO_ROOT"
say "确认都不要了，把它整个删掉即可。"
say "=========================================="

