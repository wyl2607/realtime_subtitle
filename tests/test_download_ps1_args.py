"""download_subtitle.ps1 到底把什么参数交给了 Python（真跑 PowerShell）。

☠️ 这不是"启动一次真实下载"：Python 被换成一个只把 sys.argv 写进 argv.json 的
桩，整个测试在 tmp_path 里搭一份最小仓库布局。要验的是交互菜单 → 参数数组这
一段——它以前只在剪贴板分支抽链接，手动粘贴同一段分享文案会把整段文字当成
地址交给下载器。

跳过条件：没有 powershell.exe，或本仓库还没建 venv（拿不到 python.exe 当桩）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PS1 = REPO_ROOT / "scripts" / "windows" / "download_subtitle.ps1"
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(
    not POWERSHELL or not VENV_PYTHON.is_file(),
    reason="需要 Windows PowerShell 和本仓库的 venv")

# 桩：处理请求只记 argv；--list-urls 转给**真实**的 extract_share_urls
# （ps1 就是这么调的，抠链接的规则只有那一份）。
_STUB = """import json, os, sys
# tmp_path 里那份 venv 只有 python.exe，没有 site-packages（复制整个 venv 太重），
# 所以把真 venv 的包目录和仓库根一起接上——这是测试台的事，真环境不需要。
sys.path.insert(0, {site!r})
sys.path.insert(0, {repo!r})
if "--list-urls" in sys.argv[1:]:
    from realtime_subtitle.offline import extract_share_urls
    text = [a for a in sys.argv[1:] if a != "--list-urls"]
    for url in extract_share_urls(text[0] if text else ""):
        print(url)
    raise SystemExit(0)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh, ensure_ascii=False)
"""


@pytest.fixture
def harness(tmp_path):
    """tmp_path 里搭一份最小仓库：venv/Scripts/python.exe + 桩脚本 + 真 ps1。"""
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "windows").mkdir(parents=True)
    shutil.copyfile(VENV_PYTHON, tmp_path / "venv" / "Scripts" / "python.exe")
    cfg = REPO_ROOT / "venv" / "pyvenv.cfg"
    if cfg.is_file():
        shutil.copyfile(cfg, tmp_path / "venv" / "pyvenv.cfg")
    shutil.copyfile(PS1, tmp_path / "scripts" / "windows" / "download_subtitle.ps1")
    (tmp_path / "download_subtitle.py").write_text(
        _STUB.format(repo=str(REPO_ROOT),
                     site=str(REPO_ROOT / "venv" / "Lib" / "site-packages")),
        encoding="utf-8")
    # ☠️ 不套这层的话，PowerShell 按 OEM 代码页往 stdout 写，中文提示到了
    # pytest 这边全是问号，对提示文案的断言就永远是假的
    (tmp_path / "run.ps1").write_text(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "& (Join-Path $PSScriptRoot 'scripts\\windows\\download_subtitle.ps1') @args\r\n",
        encoding="utf-8-sig")
    return tmp_path


def _run(harness, answers, *params):
    """跑一次脚本，返回 (argv 列表, stdout)。answers 是依次回答 Read-Host 的行。"""
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(harness / "run.ps1"), *params],
        input="\n".join(list(answers) + [""]) + "\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    target = harness / "argv.json"
    argv = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
    return argv, (done.stdout or "")


def test_pressing_enter_twice_uses_the_defaults(harness):
    argv, _ = _run(harness, ["", ""], "-Url", "https://example.test/v1")

    assert argv[0] == "https://example.test/v1"
    assert argv[argv.index("--subtitle-mode") + 1] == "bilingual"
    # 没显式选目标语言时不传该参数，交给 Python 按源语言定（中文→英语）
    assert "--target-language" not in argv


def test_choosing_german(harness):
    argv, _ = _run(harness, ["", "2"], "-Url", "https://example.test/v1")

    assert argv[argv.index("--target-language") + 1] == "de"
    assert argv[argv.index("--subtitle-mode") + 1] == "bilingual"


def test_choosing_monolingual_english(harness):
    argv, _ = _run(harness, ["2", "1"], "-Url", "https://example.test/v1")

    assert argv[argv.index("--subtitle-mode") + 1] == "target"
    assert argv[argv.index("--target-language") + 1] == "en"


def test_local_path_with_spaces_and_chinese(harness):
    video = harness / "我的 视频 目录"
    video.mkdir()
    clip = video / "第一集 片段.mp4"
    clip.write_bytes(b"media")

    argv, out = _run(harness, ["", ""], "-Url", str(clip))

    assert argv[0] == str(clip), argv
    assert "本地文件" in out


def test_a_pasted_share_blurb_is_parsed_not_swallowed(harness):
    """手动粘贴分享文案要和剪贴板走同一条解析路径。"""
    blurb = "我在小红书发现了个宝藏视频 https://example.test/abc123 快去看看吧"

    argv, _ = _run(harness, ["", ""], "-Url", blurb)

    assert argv[0] == "https://example.test/abc123", argv


def test_several_links_ask_which_one(harness):
    blurb = ("活动页 https://example.test/campaign "
             "视频 https://example.test/video42 下载 https://example.test/app")

    argv, out = _run(harness, ["2", "", ""], "-Url", blurb)

    assert "多个链接" in out
    assert argv[0] == "https://example.test/video42", argv


def test_unknown_menu_input_falls_back_to_the_default_out_loud(harness):
    argv, out = _run(harness, ["9", "abc"], "-Url", "https://example.test/v1")

    assert argv[argv.index("--subtitle-mode") + 1] == "bilingual"
    assert "--target-language" not in argv
    assert out.count("无法识别") == 2, "别默默把看不懂的输入当默认值咽下去"


def test_no_summary_switch_is_passed_through(harness):
    argv, _ = _run(harness, ["", ""], "-Url", "https://example.test/v1", "-NoSummary")

    assert "--no-summary" in argv
