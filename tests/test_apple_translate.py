import queue
import sys
import textwrap
import threading
import types

import pytest

from realtime_subtitle.translate.apple_translate import AppleTranslator

# 假 helper 靠 shebang 被直接 exec，Windows 上是 WinError 193 起不来；
# 剩下几条只是因为「起不来也返回 None」才碰巧通过，等于什么都没测。
# Apple Translation 本来就只有 macOS 有，所以整份文件按平台关掉。
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Apple Translation helper 只在 macOS 上存在")


def _helper(tmp_path, body):
    path = tmp_path / "rstranslate"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "print(json.dumps({'ready': True}), flush=True)\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def test_apple_translator_translate_and_status(tmp_path):
    helper = _helper(tmp_path, """
        for line in sys.stdin:
            req = json.loads(line)
            if req["op"] == "status":
                print(json.dumps({"id": req["id"], "status": "installed"}), flush=True)
            else:
                print(json.dumps({"id": req["id"], "text": "译:" + req["text"]}, ensure_ascii=False), flush=True)
    """)
    tx = AppleTranslator(helper_path=helper)
    try:
        assert tx.status("de", "zh") == "installed"
        assert tx.translate("Hallo", "de", "zh", timeout=1) == "译:Hallo"
    finally:
        tx.close()


def test_apple_translator_timeout_returns_none(tmp_path):
    helper = _helper(tmp_path, """
        for line in sys.stdin:
            req = json.loads(line)
            time.sleep(2)
            print(json.dumps({"id": req["id"], "text": "late"}), flush=True)
    """)
    tx = AppleTranslator(helper_path=helper)
    try:
        assert tx.translate("Hallo", "de", "zh", timeout=0.1) is None
    finally:
        tx.close()


def test_apple_translator_error_returns_none(tmp_path):
    helper = _helper(tmp_path, """
        for line in sys.stdin:
            req = json.loads(line)
            print(json.dumps({"id": req["id"], "error": "boom"}), flush=True)
    """)
    tx = AppleTranslator(helper_path=helper)
    try:
        assert tx.translate("Hallo", "de", "zh", timeout=1) is None
    finally:
        tx.close()


def test_apple_translator_restarts_after_process_death(tmp_path):
    marker = tmp_path / "died_once"
    helper = _helper(tmp_path, f"""
        marker = {str(marker)!r}
        for line in sys.stdin:
            req = json.loads(line)
            if not os.path.exists(marker):
                open(marker, "w").close()
                sys.exit(0)
            print(json.dumps({{"id": req["id"], "text": "ok"}}, ensure_ascii=False), flush=True)
    """)
    tx = AppleTranslator(helper_path=helper)
    try:
        assert tx.translate("Hallo", "de", "zh", timeout=1) is None
        assert tx.translate("Hallo", "de", "zh", timeout=1) == "ok"
    finally:
        tx.close()


def test_stale_reader_eof_does_not_poison_new_generation():
    """硬约束 HC-M01：读取线程只许写启动时那一代的队列。

    违反的是什么：_read_stdout 每次 put 都去读 self._lines。为什么不行：
    helper 超时/崩溃后 _start_locked 会重建 self._lines，旧进程的读取线程收尾
    时会把 None（EOF）或旧响应塞进新队列，新一代第一句就被误判成 helper 已
    退出/协议错位。必须怎么改：启动线程时把这一代的 proc 和 queue 作为参数
    传进去，线程只写自己那份队列。
    """
    tx = AppleTranslator(helper_path="/nonexistent")
    release = threading.Event()

    class _GatedStdout:
        def __iter__(self):
            yield "stale-response\n"
            release.wait(5)
            # 迭代结束 → 旧进程 EOF，收尾会 put(None)

    old_queue = tx._lines
    stale_proc = types.SimpleNamespace(stdout=_GatedStdout())
    reader = threading.Thread(target=tx._read_stdout, args=(stale_proc, old_queue), daemon=True)
    reader.start()
    assert old_queue.get(timeout=1) == "stale-response"

    # 模拟超时重启：_start_locked 重建了队列，旧线程此时才收尾。
    new_queue: queue.Queue = queue.Queue()
    tx._lines = new_queue
    release.set()
    reader.join(2)

    assert not reader.is_alive()
    assert new_queue.empty(), (
        "违反 HC-M01：旧读取线程把 EOF/旧响应写进了新一代队列，"
        "新一代第一句会被误判为 helper 已退出或协议错位；"
        "必须把 proc 和 queue 作为参数钉进线程。"
    )
    assert old_queue.get(timeout=1) is None


def test_apple_translator_id_mismatch_returns_none(tmp_path):
    helper = _helper(tmp_path, """
        for line in sys.stdin:
            req = json.loads(line)
            print(json.dumps({"id": req["id"] + 1, "text": "wrong"}), flush=True)
    """)
    tx = AppleTranslator(helper_path=helper)
    try:
        assert tx.translate("Hallo", "de", "zh", timeout=1) is None
    finally:
        tx.close()
