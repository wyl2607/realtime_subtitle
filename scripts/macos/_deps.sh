#!/usr/bin/env bash
# 依赖指纹 + 装依赖前把 venv 腾空。对应 scripts/windows/_update_deps.ps1 与
# _deps_guard.ps1，规则一一照抄。
#
# ☠️ 档位（tier）这件事在 macOS 上和 Windows 不一样，别照搬 nvidia-smi：
#   ctranslate2 没有 Metal/CUDA 后端，macOS 只有 cpu 一档。所以这里的 tier
#   永远是 "macos"，`filter_requirements_for_tier` 也不会因此过滤掉什么行
#   （它只对 "cpu" 生效）。指纹同时把 requirements-macos.txt 的**文本**算进去，
#   所以那个文件改了就一定会重装依赖。

# macOS 用的依赖清单。☠️ 没有就失败，不退回 requirements.txt——那份是
# Windows 的（CUDA/torch/pyaudiowpatch），在 macOS 上装不上或白占几个 GB。
deps_requirements_file() {
    [ -f "$RTS_REPO_ROOT/requirements-macos.txt" ] || return 1
    printf '%s
' "$RTS_REPO_ROOT/requirements-macos.txt"
}

deps_fingerprint_path() { printf '%s/venv/.deps_fingerprint\n' "$RTS_REPO_ROOT"; }

deps_fingerprint_current() {
    local py req
    py="$(venv_python)"
    [ -x "$py" ] || return 0
    req="$(deps_requirements_file 2>/dev/null)" || return 0
    RTS_REPO="$RTS_REPO_ROOT" RTS_REQ="$req" "$py" -c '
import os
from pathlib import Path
from realtime_subtitle.deps_fingerprint import fingerprint_requirements
text = Path(os.environ["RTS_REQ"]).read_text(encoding="utf-8")
print(fingerprint_requirements(text, "macos"))
' 2>/dev/null | tail -n 1
}

deps_fingerprint_stored() {
    local path
    path="$(deps_fingerprint_path)"
    [ -f "$path" ] || return 0
    tr -d ' \n\r' < "$path"
}

# pip 失败不得写这个文件，所以提交不变时仍能重试。
deps_fingerprint_write() {
    local fp path dir
    fp="$(deps_fingerprint_current)"
    [ -n "$fp" ] || return 0
    path="$(deps_fingerprint_path)"
    dir="$(dirname "$path")"
    [ -d "$dir" ] || return 0
    printf '%s\n' "$fp" > "$path"
}

deps_need_install() {
    local current stored
    current="$(deps_fingerprint_current)"
    stored="$(deps_fingerprint_stored)"
    [ -z "$current" ] && return 0     # 算不出来（venv 坏/文件缺）→ 当作要装
    [ "$current" != "$stored" ]
}

# ---------------------------------------------------------------- 装依赖

# pip 参数。--mirror 是国内网络加速，和 install.ps1 的 -Mirror 同一个意思。
pip_install_args() {
    if [ "${1:-}" = "--mirror" ]; then
        printf '%s\n' --index-url https://pypi.tuna.tsinghua.edu.cn/simple
    fi
}

deps_install() {
    local py req mirror="${1:-}" arg out
    py="$(venv_python)"
    [ -x "$py" ] || return 1
    req="$(deps_requirements_file)" || return 1
    # ☠️ 有 uv 就用 uv pip：解析快一大截，而且和 install.sh 建 venv 的方式一致
    #（同一个 uv 自己缓存 wheels）。没有 uv（用户后来把它卸了）退回 python -m pip。
    local -a extra=()
    while IFS= read -r arg; do [ -n "$arg" ] && extra+=("$arg"); done < <(pip_install_args "$mirror")
    if command -v uv >/dev/null 2>&1; then
        out="$(uv pip install --python "$py" "${extra[@]+"${extra[@]}"}" -r "$req" 2>&1)" \
            && printf '%s\n' "$out" && return 0
        printf '%s\n' "$out" >&2
        say "  （uv 装失败，退回 pip 再试一次）"
    fi
    "$py" -m pip install "${extra[@]+"${extra[@]}"}" -r "$req"
}

# ---------------------------------------------------------------- 腾 venv

# 装依赖前先确认没有别的 python 正在用这个 venv。
#
# ☠️ 只停**实时字幕**（pid 文件/进程身份认得出来的那种），同一个 venv 跑着的
# 别的东西——典型是 YouTube 下载加字幕的离线任务，可能已经跑了半小时——
# 绝不替用户杀，只报出来、这次不装依赖。
# macOS 上这一条比 Windows 轻：pip 装的是 .py 文件，真正装不换的只有
# 正在被 dlopen 的 dylib（ctranslate2/torch/PyQt6 那几个），所以这里仍然
# 按"先停干净再装"处理，不去赌 macOS 的 dyld 行为。
# 输出：deps_venv_users 的 PID（一行一个）
deps_venv_users() {
    local pid args
    while read -r pid; do
        [ -n "$pid" ] || continue
        args="$(proc_args "$pid")" || continue
        case "$args" in
            "$OUR_PYTHON "*|"$OUR_PYTHON") printf '%s\n' "$pid" ;;
        esac
    done < <(ps -A -o pid= 2>/dev/null | tr -d ' ')
}

# 请求腾空 venv。停掉实时字幕（有就停），然后等一小段宽限别被收尾中的进程误报。
# 返回 0 = 腾空了；非 0 = 还占着，占着的 PID 打在 stderr 上。
# 设 RTS_STOPPED_REALTIME=1 表示"这次确实停掉了字幕"（调用方要提示用户重启）。
deps_release_for_pip() {
    local settle="${1:-5}" running pid users i
    # shellcheck disable=SC2034  # shellcheck 认不出 `if x="$(cmd)"` 里 x 被读
    if running="$(get_running_realtime_pid)"; then
        say "依赖要更新，而字幕正在运行（它占着 ctranslate2/torch/PyQt6 等文件，pip 换不掉）——先停掉字幕..."
        if ! RTS_QUIET=1 "$RTS_SCRIPT_DIR/stop_subtitles.sh"; then
            warn "⚠️  停止字幕没成功，先不装依赖（多半是它正忙，稍后再试）"
            return 1
        fi
        # shellcheck disable=SC2034  # 调用方（update_subtitles.sh）读它
        RTS_STOPPED_REALTIME=1
    fi
    for ((i = 0; i < settle * 4; i++)); do
        users="$(deps_venv_users)"
        [ -z "$users" ] && return 0
        sleep 0.25
    done
    for pid in $users; do
        warn "   PID $pid 还在用这个 venv"
    done
    return 1
}

