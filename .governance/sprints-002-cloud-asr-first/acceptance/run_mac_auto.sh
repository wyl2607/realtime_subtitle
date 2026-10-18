#!/bin/bash
# macOS 自动验收脚本 for v3.2.0
# 兼容 macOS 自带 bash 3.2：set -u 下不要展开空数组

set -u

# 默认参数
OUT_DIR=""
ONLY=""
SKIP_PTEST=false

# 解析命令行参数
while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            OUT_DIR="$2"
            shift 2
            ;;
        --only)
            ONLY="$2"
            shift 2
            ;;
        --skip-pytest)
            SKIP_PTEST=true
            shift
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

# 设置默认输出目录
if [ -z "$OUT_DIR" ]; then
    TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
    OUT_DIR="$HOME/projects/rs-mac-native-data/accept-v320/$TIMESTAMP"
fi

# 创建输出目录
mkdir -p "$OUT_DIR"
SUMMARY_FILE="$OUT_DIR/summary.tsv"
echo -e "项⽬名\t结果\t原因" > "$SUMMARY_FILE"

# 前置检查：确保没有 rslite 或 Python main.py 在运行
if pgrep -f "macos-native/.build/release/rslite" >/dev/null 2>&1 || \
   pgrep -f "python.*main.py" >/dev/null 2>&1; then
    echo "错误: 检测到正在运行的 rslite 或 Python 主程序，请先停止它们"
    echo "rslite\tFAIL\t检测到正在运行的 rslite 或 Python 主程序" >> "$SUMMARY_FILE"
    exit 2
fi

# 定义日志函数
log_result() {
    local test_name="$1"
    local result="$2"  # PASS|FAIL|SKIP
    local reason="$3"
    
    echo -e "$test_name\t$result\t$reason" | tee -a "$SUMMARY_FILE"
}

# 创建假ホーム目录
setup_fake_home() {
    local fakehome="$OUT_DIR/fakehome"
    mkdir -p "$fakehome"
    chmod 700 "$fakehome"
    
    # 创建必要的目录结构
    mkdir -p "$fakehome/.config/rslite"
    chmod 700 "$fakehome/.config/rslite"
    
    # 如果存在真实的 nodes.json 和 tokens，只读复制进去
    if [ -d "$HOME/.config/rslite" ]; then
        if [ -f "$HOME/.config/rslite/nodes.json" ]; then
            cp -p "$HOME/.config/rslite/nodes.json" "$fakehome/.config/rslite/nodes.json"
            chmod 600 "$fakehome/.config/rslite/nodes.json"
        fi
        
        if [ -d "$HOME/.config/rslite/tokens" ]; then
            cp -p "$HOME/.config/rslite/tokens/"* "$fakehome/.config/rslite/tokens/" 2>/dev/null || true
            chmod -R 600 "$fakehome/.config/rslite/tokens" 2>/dev/null || true
            chmod 700 "$fakehome/.config/rslite/tokens" 2>/dev/null || true
        fi
    fi
    
    echo "$fakehome"
}

# 清理假ホーム目录
cleanup_fake_home() {
    local fakehome="$1"
    if [ -n "$fakehome" ] && [ -d "$fakehome" ]; then
        # 删除 groq.key（如果存在）
        rm -f "$fakehome/.config/rslite/groq.key"
        # 删除整个假ホーム目录
        rm -rf "$fakehome"
    fi
}

# 运行质量门禁 (Done 7)
run_quality_gates() {
    local fakehome="$1"
    local result_file="$OUT_DIR/quality_gates.log"
    
    {
        echo "=== 运行 pytest ==="
        if [ "$SKIP_PTEST" = false ]; then
            TMPDIR=/tmp HOME="$fakehome" ~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/rs-accept-pytest
            pytest_result=$?
        else
            echo "跳过 pytest (--skip-pytest)"
            pytest_result=0
        fi
        
        echo "=== 运行 ruff ==="
        TMPDIR=/tmp HOME="$fakehome" ~/projects/rs-mac-native/venv/bin/ruff check .
        ruff_result=$?
        
        echo "=== 编译 Swift release ==="
        (cd macos-native && swift build -c release)
        swift_build_result=$?
        
        echo "=== 运行 rslite 自检 ==="
        TMPDIR=/tmp HOME="$fakehome" macos-native/.build/release/rslite --selftest
        selftest_result=$?
        
        # 检查 CI 中 rslite-macos job 实际做的事情：swift build + selftest + 设计令牌检查
        echo "=== 检查设计令牌 ==="
        python3 scripts/design/gen_tokens.py --check
        design_tokens_result=$?
        
        # 汇总结果
        if [ "$SKIP_PTEST" = false ] && [ $pytest_result -ne 0 ]; then
            echo "质量门禁 FAIL: pytest 失败 (退出码 $pytest_result)"
            return 1
        elif [ $ruff_result -ne 0 ]; then
            echo "质量门禁 FAIL: ruff 检查失败 (退出码 $ruff_result)"
            return 1
        elif [ $swift_build_result -ne 0 ]; then
            echo "质量门禁 FAIL: Swift 编译失败 (退出码 $swift_build_result)"
            return 1
        elif [ $selftest_result -ne 0 ]; then
            echo "质量门禁 FAIL: rslite 自检失败 (退出码 $selftest_result)"
            return 1
        elif [ $design_tokens_result -ne 0 ]; then
            echo "质量门禁 FAIL: 设计令牌检查失败 (退出码 $design_tokens_result)"
            return 1
        else
            echo "质量门禁 PASS"
            return 0
        fi
    } > "$result_file" 2>&1
}

# 检查文档 (Done 8)
check_documentation() {
    local fakehome="$1"
    
    # 检查 version.py
    if ! grep -q '__version__ = "3.2.0"' realtime_subtitle/version.py; then
        log_result "文档检查" "FAIL" "realtime_subtitle/version.py 中未找到 __version__ = \"3.2.0\""
        return 1
    fi
    
    # 检查 README 文件中是否提到 Groq、开关方法、隐私/数据保留、key 配置
    local readme_files="README.md README.en.md README.de.md"
    local all_passed=0
    
    for readme in $readme_files; do
        if [ ! -f "$readme" ]; then
            log_result "文档检查" "FAIL" "找不到文件 $readme"
            return 1
        fi
        
        # 检查 Groq 提及
        if ! grep -qi "groq" "$readme"; then
            log_result "文档检查" "FAIL" "$readme 中未提到 Groq"
            return 1
        fi
        
        # 检查开关方法（关闭云端的方法）
        if ! grep -qi "\-\-cloud.*off\|云端.*关闭\|关闭.*云端" "$readme"; then
            log_result "文档检查" "FAIL" "$readme 中未提到关闭云端的方法"
            return 1
        fi
        
        # 检查隐私/数据保留
        if ! grep -qi "隐私\|数据保留\|data retention\|不向任何云端发送\|默认完全本地" "$readme"; then
            log_result "文档检查" "FAIL" "$readme 中未提到隐私/数据保留"
            return 1
        fi
        
        # 检查 key 配置方法
        if ! grep -qi "\-\-set\-groq\-key\|设置.*key\|key.*配置" "$readme"; then
            log_result "文档检查" "FAIL" "$readme 中未提到 key 配置方法"
            return 1
        fi
    done
    
    log_result "文档检查" "PASS" "所有 README 文件符合要求"
    return 0
}

# 运行 fallback 测试
run_fallback_test() {
    local scenario_name="$1"
    local extra_args="$2"
    local expected_log_pattern="$3"  # 需要在 stderr 中找到的模式
    local timeout_sec=240
    
    local fakehome="$OUT_DIR/fakehome"
    local events_file="$OUT_DIR/${scenario_name}_events.jsonl"
    local stderr_file="$OUT_DIR/${scenario_name}.stderr.log"
    local score_file="$OUT_DIR/${scenario_name}/score.json"
    
    # 创建 score 目录
    mkdir -p "$(dirname "$score_file")"
    
    # 设置环境变量
    export HOME="$fakehome"
    export TMPDIR=/tmp
    
    # 运行 rslite 并捕获输出，使用后台进程和 kill 实现超时
    {
        # 如果是 bad-key 场景，先设置假 key
        if [ "$scenario_name" = "bad-key" ]; then
            # 生成 40 个随机字母数字
            local random_string=$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 40)
            printf 'gsk_%s\n' "$random_string" | macos-native/.build/release/rslite --set-groq-key
        fi
        
        # 运行 rslite
        macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless $extra_args
    } > "$events_file" 2> "$stderr_file" &
    local rslite_pid=$!
    
    # 等待进程完成或超时
    local elapsed=0
    while [ $elapsed -lt $timeout_sec ]; do
        if ! kill -0 $rslite_pid 2>/dev/null; then
            # 进程已经结束
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    
    # 如果超时，杀死进程
    if kill -0 $rslite_pid 2>/dev/null; then
        kill $rslite_pid 2>/dev/null
        wait $rslite_pid 2>/dev/null
        log_result "fallback_$scenario_name" "FAIL" "测试超时 (${timeout_sec}s)"
        return 1
    fi
    
    # 检查 stderr 中是否包含期望的日志模式
    if ! grep -q "$expected_log_pattern" "$stderr_file"; then
        log_result "fallback_$scenario_name" "FAIL" "stderr 中未找到期望的日志模式: $expected_log_pattern"
        return 1
    fi
    
    # 运行 hybrid_score.py 评估
    if ! python3 scripts/bench/hybrid_score.py "$events_file" --refs $HOME/projects/rs-mac-native-data/refs.jsonl --json > "$score_file"; then
        log_result "fallback_$scenario_name" "FAIL" "hybrid_score.py 运行失败"
        return 1
    fi
    
    # 检查评估结果中是否没有重复或丢句
    if ! grep -q '"problem_counts":{"duplicates":0,"dropped":0}' "$score_file"; then
        log_result "fallback_$scenario_name" "FAIL" "hybrid_score.py 评估显示有重复或丢句"
        return 1
    fi
    
    # 对于 bad-key 场景，额外检查假 key 字符串不出现在 stdout/stderr 任何位置
    if [ "$scenario_name" = "bad-key" ]; then
        if grep -q "$random_string" "$events_file" || grep -q "$random_string" "$stderr_file"; then
            log_result "fallback_bad-key" "FAIL" "假 key 字符串出现在了输出中"
            return 1
        fi
    fi
    
    log_result "fallback_$scenario_name" "PASS" "所有检查通过"
    return 0
}

# 主函数
main() {
    # 设置假ホーム目录
    local fakehome=$(setup_fake_home)
    
    # 根据 --only 参数决定运行哪些检查
    if [ -z "$ONLY" ] || [ "$ONLY" = "gates" ]; then
        # 运行质量门禁
        if run_quality_gates "$fakehome"; then
            log_result "gates" "PASS" "质量门禁所有检查通过"
        else
            log_result "gates" "FAIL" "质量门禁检查失败，详见 $OUT_DIR/quality_gates.log"
        fi
    fi
    
    if [ -z "$ONLY" ] || [ "$ONLY" = "docs" ]; then
        # 检查文档
        if check_documentation "$fakehome"; then
            log_result "docs" "PASS" "文档检查通过"
        else
            log_result "docs" "FAIL" "文档检查失败"
        fi
    fi
    
    if [ -z "$ONLY" ] || [ "$ONLY" = "fallback" ]; then
        # 运行 fallback 测试
        if [ -z "$ONLY" ]; then
            # 只有在非 --only 模式下才运行 fallback 测试
            mkdir -p "$OUT_DIR"
        fi
        
        # 场景 1: bad-key
        if run_fallback_test "bad-key" "--cloud on" "mark_key_invalid\|disabled_invalid_key"; then
            :
        else
            # 失败已经在函数内部记录了
            :
        fi
        
        # 场景 2: cloud-off
        if run_fallback_test "cloud-off" "--cloud off" "disabled\|disabled_switch_off"; then
            :
        else
            :
        fi
        
        # 场景 3: node-down
        if run_fallback_test "node-down" "" "no_available_node\|consecutive_failure_backoff"; then
            :
        else
            :
        fi
    fi
    
    # 清理假ホーム目录
    cleanup_fake_home "$fakehome"
    
    # 打印汇总
    echo
    echo "=== 验收汇总 ==="
    column -t -s $'\t' "$SUMMARY_FILE"
    
    # 检查是否有任何 FAIL
    if grep -q $'\tFAIL\t' "$SUMMARY_FILE"; then
        echo "验收失败：存在不通过的检查项"
        exit 1
    else
        echo "所有检查项均通过"
        exit 0
    fi
}

# 执行主函数
main "$@"
