#!/usr/bin/env bash
# 下载网络视频、或导入本地视频/音频，并制作字幕（可选单语/双语、英语/德语）。
#
# 用法：
#   bash scripts/macos/download_subtitle.sh                      # 读剪贴板 / 交互问
#   bash scripts/macos/download_subtitle.sh "视频地址"
#   bash scripts/macos/download_subtitle.sh "/path/to/video.mp4"
#   bash scripts/macos/download_subtitle.sh --no-summary "视频地址"
#   bash scripts/macos/download_subtitle.sh --target-language de "视频地址"
#
# 对应 scripts/windows/download_subtitle.ps1，语义逐条一致：
# 剪贴板优先、抠链接只有 Python 那一份规则、两个问题各自的默认值、输出目录
# 默认仓库 downloads/、结束停一下等用户看完。
set -uo pipefail

RTS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RTS_REPO_ROOT="$(cd "$RTS_SCRIPT_DIR/../.." && pwd -P)"
# shellcheck source=scripts/macos/_common.sh
. "$RTS_SCRIPT_DIR/_common.sh"

VENV_PY="$(venv_python)"
[ -x "$VENV_PY" ] || die "❌ 还没有安装运行环境（找不到 venv）。请先运行 bash scripts/macos/install.sh。"

url=""
output_dir=""
source_language=""
target_language=""
subtitle_mode=""
no_summary=0

while [ $# -gt 0 ]; do
    case "$1" in
        --url) url="${2:-}"; shift 2 ;;
        --output-dir) output_dir="${2:-}"; shift 2 ;;
        --source-language) source_language="${2:-}"; shift 2 ;;
        --target-language) target_language="${2:-}"; shift 2 ;;
        --subtitle-mode) subtitle_mode="${2:-}"; shift 2 ;;
        --no-summary) no_summary=1; shift ;;
        -h|--help)
            say "用法：bash scripts/macos/download_subtitle.sh [选项] [视频地址或本地文件]"
            say "  --url 地址              等同于把地址作为位置参数（拖文件进来时用得上）"
            say "  --output-dir 目录       输出目录，默认仓库 downloads/"
            say "  --source-language 代码  Whisper 语言代码，默认自动识别"
            say "  --target-language 代码  译文语言 en / de"
            say "  --subtitle-mode 形式    bilingual（默认）/ target / source"
            say "  --no-summary            不生成学习笔记"
            exit 0 ;;
        --*) die "不认识的选项：${1}（--help 看用法）" ;;
        *) url="$1"; shift ;;
    esac
done

# 分享文案里常常不止一个链接（短链 + 活动页 + 下载页）。以前直接取第一个匹配，
# 等于替用户随机决定处理哪个视频；现在超过一个就让他选。
choose_url() {
    local candidates="$1" pick="" i=0 n
    n="$(printf '%s\n' "$candidates" | grep -c .)"
    if [ "$n" -eq 1 ]; then
        printf '%s\n' "$candidates" | grep .
        return 0
    fi
    # ☠️ 菜单和提示一律走 **stderr**，只有选中的那一个链接走 stdout。
    # 调用方是 `url="$(resolve_input_text …)"`——命令替换会吃掉整个函数的
    # stdout，所以菜单哪怕 printf 到 stdout，用户在屏幕上也是看不见的，只会
    # 看着程序自己往下跑。Windows 那边的 Write-Host 是直接显示的，没有这个
    # 问题，这里得手动对齐。
    {
        say "这段文字里有多个链接，请选择要处理的那一个："
        while IFS= read -r candidate; do
            [ -n "$candidate" ] || continue
            i=$((i + 1))
            say "  $i. $candidate"
        done <<EOF
$candidates
EOF
        printf '输入序号（直接回车放弃）'
    } >&2
    IFS= read -r pick || pick=""
    case "$pick" in
        ''|*[!0-9]*) return 0 ;;
    esac
    [ "$pick" -ge 1 ] && [ "$pick" -le "$n" ] || return 0
    printf '%s\n' "$candidates" | grep . | sed -n "${pick}p"
}

# ☠️ 剪贴板和手动输入走**同一个**解析入口。以前只有剪贴板分支会从分享文案里
# 抠链接，手动粘贴同一段文案则被整段当成地址交给下载器，直接失败。
resolve_input_text() {
    local text="$1" found
    [ -n "$text" ] || return 0
    # Finder 拖进来的路径里空格会被转义成"\ "，先按原样试，再按去转义试
    if [ -f "$text" ]; then printf '%s\n' "$text"; return 0; fi
    local unescaped
    unescaped="$(printf '%s' "$text" | sed 's/\\\(.\)/\1/g')"
    if [ -f "$unescaped" ]; then printf '%s\n' "$unescaped"; return 0; fi
    # ☠️ 抠链接的规则只有 Python 那一份（offline.extract_share_urls），这里调过去。
    # 别在这儿再写一套正则：`https?://\S+` 对中文分享文案是错的——中文标点不是
    # 空白字符，`http://xhslink.com/a/xxx，快去看` 会把后半句一起当成 URL。
    found="$("$VENV_PY" "$OFFLINE_ENTRY" "$text" --list-urls 2>/dev/null | grep -v '^[[:space:]]*$')"
    [ -z "$found" ] && { printf '%s\n' "$text"; return 0; }
    choose_url "$found"
}

if [ -z "$url" ]; then
    clipboard="$(pbpaste 2>/dev/null || true)"
    url="$(resolve_input_text "$(printf '%s' "$clipboard" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')")"
    [ -n "$url" ] && say "已从剪贴板读取视频地址。"
fi
if [ -z "$url" ]; then
    printf '请粘贴视频地址或分享文案，或把本地视频文件拖到这里后回车：' >&2
    typed=""
    IFS= read -r typed || typed=""
    url="$(resolve_input_text "$typed")"
else
    # 命令行给的地址也走同一个解析入口：启动器里粘一整段分享文案同样要能用
    url="$(resolve_input_text "$url")"
fi
[ -n "$url" ] || die "❌ 需要一个视频地址或本地文件才能继续。"

is_local=0
case "$url" in
    [a-zA-Z]*://*) ;;
    *) if [ -f "$url" ]; then is_local=1; url="$(cd "$(dirname "$url")" && pwd -P)/$(basename "$url")"; fi ;;
esac
if [ "$is_local" -eq 1 ]; then
    say "导入本地文件：$url"
fi

# ☠️ 选项编号只是菜单序号，不能直接当语言码用（"1" 不是 en）。
if [ -z "$subtitle_mode" ]; then
    say ""
    say "字幕形式："
    say "  1 双语（原文 + 译文，默认）"
    say "  2 单语（只要译文）"
    printf '输入序号（直接回车用默认）' >&2
    pick=""
    IFS= read -r pick || pick=""
    case "$pick" in
        2) subtitle_mode="target" ;;
        1|"") subtitle_mode="bilingual" ;;
        *) say "无法识别的选项「${pick}」，按默认（双语）处理。"; subtitle_mode="bilingual" ;;
    esac
fi
if [ -z "$target_language" ]; then
    say ""
    say "译文语言："
    say "  1 英语（中文视频默认，直接回车即可）"
    say "  2 德语"
    printf '输入序号（直接回车用默认）' >&2
    pick=""
    IFS= read -r pick || pick=""
    case "$pick" in
        2) target_language="de" ;;
        1|"") target_language="" ;;   # 交给 Python 按源语言定默认
        *) say "无法识别的选项「${pick}」，按默认处理。"; target_language="" ;;
    esac
fi

case "$subtitle_mode" in
    target) mode_label="单语（只要译文）" ;;
    source) mode_label="只要原文" ;;
    *) mode_label="双语（原文 + 译文）" ;;
esac
case "$target_language" in
    "") lang_label="按源语言自动决定（中文视频→英语）" ;;
    de) lang_label="德语" ;;
    en) lang_label="英语" ;;
    *) lang_label="$target_language" ;;
esac
if [ -n "$source_language" ]; then source_label="$source_language"; else source_label="自动识别"; fi
if [ "$is_local" -eq 1 ]; then input_label="本地文件"; else input_label="网络地址"; fi
if [ -n "$output_dir" ]; then output_label="$output_dir"; else output_label="$RTS_REPO_ROOT/downloads"; fi
say ""
say "本次任务：$input_label / 源语言 $source_label / 译文 $lang_label / $mode_label"
say "输出目录：$output_label"
say ""

# ☠️ 一律用参数数组传给 Python，不拼命令行字符串、不用 eval：
# 视频地址来自剪贴板，拼字符串等于把它交给 shell 再解析一遍。
cli_args=("$url")
[ -n "$output_dir" ] && cli_args+=(--output-dir "$output_dir")
[ -n "$source_language" ] && cli_args+=(--source-language "$source_language")
[ -n "$target_language" ] && cli_args+=(--target-language "$target_language")
[ -n "$subtitle_mode" ] && cli_args+=(--subtitle-mode "$subtitle_mode")
[ "$no_summary" -eq 1 ] && cli_args+=(--no-summary)

"$VENV_PY" "$OFFLINE_ENTRY" "${cli_args[@]}"
exit_code=$?
if [ "$exit_code" -eq 0 ]; then
    printf '完成。按回车关闭'
else
    printf '失败。按回车关闭'
fi
IFS= read -r _ || true
exit "$exit_code"

