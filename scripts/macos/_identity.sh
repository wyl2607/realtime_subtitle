#!/usr/bin/env bash
# 实时字幕进程身份：精确解释器 + 入口命令行 + 创建时间。供 start/stop/update source。
#
# ☠️ 和 scripts/windows/_identity.ps1 是同一件事的两套实现，规则必须逐条对得上：
#   - 解释器必须是**本仓库 venv 里那个**（不是系统 python、不是 venv_backup）
#   - 入口必须是 main.py，且**不能**是 download_subtitle.py（离线任务）
#   - 记了创建时间就必须逐字对上（PID 会被系统回收复用，光"这个 PID 有进程"不算数）
# macOS 上的对应物：
#   exe      → `ps -o args=` 的第一个 token 就是 exec 时的 argv[0]（绝对路径）
#   start    → `ps -o lstart=`（"Thu Oct  1 22:49:00 2026"，同一进程不会变）
#   ☠️ 不要用 `ps -o comm=`：macOS 上它按 p_comm 的 16 字节上限截断，
#     实测 `/Users/yilinwang/dev/rs-venv/bin/python` 会被截成 `/Users/yilinwang`。
#   ☠️ 也不要用 `ps -o command=`：默认会把参数按空格切开再拼，引号信息全丢。
#     命令行里可能有路径含空格（仓库放在 "My Subtitle" 下），
#     所以下面一律用**整串前缀比较**而不是按空白切词。

# 本仓库的 venv 解释器和入口脚本（绝对路径，启动时也是这么调的）
OUR_PYTHON="${RTS_PYTHON:-$RTS_REPO_ROOT/venv/bin/python}"
OUR_MAIN="$RTS_REPO_ROOT/main.py"

proc_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

proc_args() {
    # 整条命令行（含参数、空格），只对活着的进程问
    proc_alive "${1:-}" || return 1
    ps -o args= -p "$1" 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//'
}

proc_start_time() {
    proc_alive "${1:-}" || return 1
    ps -o lstart= -p "$1" 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//'
}

# 这条命令行是不是"本仓库的实时字幕"？
# 启动契约固定是 `<venv python> -u <main.py>`，所以整串比较比任何前缀规则都严，
# 顺带对路径里的空格免疫。裸 main.py 那条是老格式（cwd 在仓库根），照样认。
is_realtime_args() {
    local args="$1"
    case "$args" in
        "$OUR_PYTHON -u $OUR_MAIN"|"$OUR_PYTHON -u main.py") return 0 ;;
    esac
    return 1
}

# ------------------------------------------------------- subtitle.pid

# 写身份文件。格式和 Windows 那边的 JSON 一致（key 同名），这样两边都能读懂
# 对方留下的文件；pid 字段供 Python 侧 parse_pid_file 解析。
# ☠️ 自己手拼 JSON、不用 python/jq：stop.sh 必须在 venv 已经删掉之后还能跑
# （卸载流程最后一步就是删 venv），那时候两个解释器都没了。
_json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

write_identity() {
    local pid="$1" args start exe
    args="$(proc_args "$pid")" || return 1
    start="$(proc_start_time "$pid")"
    exe="${args%% *}"
    printf '{"pid":%s,"start_time":"%s","exe":"%s","command_line":"%s","kind":"realtime","repo_root":"%s"}\n' \
        "$pid" "$(_json_escape "$start")" "$(_json_escape "$exe")" \
        "$(_json_escape "$args")" "$(_json_escape "$RTS_REPO_ROOT")" > "$PID_FILE"
}

# 读 pid 文件里某个**字符串**字段（只认 JSON 格式；裸 PID 那条老格式由
# read_identity_pid 单独处理）
#
# ☠️ 冒号后面**必须容得下空格**。Windows 那边的 Write-SubtitleIdentity 用
# ConvertTo-Json，输出的是 `"pid": 1234`（冒号后带空格）；只认 `"pid":123` 的
# 正则读到 Windows 写的文件、或任何 Python json.dumps 写的文件时会**静默**取
# 不到值——表现是"停止脚本认不出自己的进程"，而日志里什么异常都没有。
read_identity_field() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 1
    sed -n "s/.*\"$key\":[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$file" | head -n 1
}

read_identity_pid() {
    local raw
    raw="$(sed -n 's/.*"pid":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n 1)"
    if [ -z "$raw" ]; then
        raw="$(tr -d ' \n' < "$1" 2>/dev/null)"
        case "$raw" in ''|*[!0-9]*) return 1 ;; esac
    fi
    printf '%s\n' "$raw"
}

# pid 文件里那个 PID 现在**确实**是本仓库的实时实例吗？
# 判据三项全中：进程活着 + 命令行是我们启动的那条 + （记了创建时间就必须对上）。
identity_is_ours() {
    local pid="${1:-}" recorded_start="${2:-}" live_args live_start
    proc_alive "$pid" || return 1
    live_args="$(proc_args "$pid")" || return 1
    is_realtime_args "$live_args" || return 1
    if [ -n "$recorded_start" ]; then
        live_start="$(proc_start_time "$pid")" || return 1
        [ "$live_start" = "$recorded_start" ] || return 1
    fi
    return 0
}

# 按进程身份找正在运行的实时字幕（不依赖 subtitle.pid）。
# ☠️ pid 文件会丢（跑测试误删过、用户手删、异常退出没写成），所以 start 也要用
# 这个查，否则 pid 文件一丢就会"判定没在运行"又起一个、还把正在跑的日志截断。
# macOS 上比 Windows 简单一档：venv/bin/python 就是真的解释器（没有 Windows 那种
# 启动器存根→子进程的间接），起进程拿到的 PID 直接就是程序本身。
find_realtime_instances() {
    local pid args
    while read -r pid; do
        [ -n "$pid" ] || continue
        args="$(proc_args "$pid")" || continue
        if is_realtime_args "$args"; then printf '%s\n' "$pid"; fi
    done < <(ps -A -o pid= 2>/dev/null | tr -d ' ')
}

# 当前运行着的实时实例 PID（先认 pid 文件，丢了再按身份找）。没有输出。
get_running_realtime_pid() {
    local pid recorded
    if [ -f "$PID_FILE" ]; then
        pid="$(read_identity_pid "$PID_FILE")" || pid=""
        recorded="$(read_identity_field "$PID_FILE" start_time)"
        if [ -n "$pid" ] && identity_is_ours "$pid" "$recorded"; then
            printf '%s\n' "$pid"
            return 0
        fi
    fi
    find_realtime_instances | head -n 1
}

