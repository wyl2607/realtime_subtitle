import sys
import textwrap

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
