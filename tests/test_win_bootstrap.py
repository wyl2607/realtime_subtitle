"""Windows 引导安装器（scripts/windows/bootstrap.ps1 / bootstrap.cmd）静态测试。

本机是 Mac，没有 Windows，所以这里只做**静态检查**：参数、编码、有没有引入
危险模式、有没有去复制 install.ps1 的逻辑。真机验收步骤写在 RESULT-WINBOOT.md。

☠️ 编码那条不是洁癖：含中文的 .ps1 没有 UTF-8 BOM 时，Windows PowerShell 5.1
会按 ANSI(GBK) 读，多字节序列被拆开、引号被吃掉，直接变成语法错误——引导器
是用户解压后第一个双击的东西，坏在这里等于全盘装不上。test_ps1_encoding.py
全局扫了一遍，这里再对本文件显式钉一次（它是发布附件，值得单独守）。
"""
import re
import shutil
import subprocess
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
PS1 = WIN / "bootstrap.ps1"
CMD = WIN / "bootstrap.cmd"
DOC = REPO_ROOT / "docs" / "zh" / "install-windows-bootstrap.md"

REPO_URL = "https://github.com/wyl2607/realtime_subtitle.git"
UTF8_BOM = b"\xef\xbb\xbf"


def _ps1_text():
    return PS1.read_text(encoding="utf-8-sig")


def test_files_exist():
    assert PS1.is_file(), f"缺少 {PS1}"
    assert CMD.is_file(), f"缺少 {CMD}"
    assert DOC.is_file(), f"缺少 {DOC}"


def test_ps1_has_required_parameters():
    text = _ps1_text()
    block = re.search(r"\bparam\s*\((.*?)\n\)", text, re.S)
    assert block, "bootstrap.ps1 里找不到 param() 块"
    body = block.group(1)
    for name in ("$Branch", "$Dir", "$SkipOllama", "$DryRun"):
        assert re.search(r"\$" + name.lstrip("$") + r"\b", body), \
            f"bootstrap.ps1 缺少参数 {name}"


def test_ps1_has_no_dynamic_code_execution():
    """引导器不许把下载回来的文本当代码执行（防供应链/防手滑）。"""
    text = _ps1_text()
    for pat in (r"\bInvoke-Expression\b", r"(?<![A-Za-z0-9_\-])iex(?![A-Za-z0-9_\-])"):
        assert not re.search(pat, text, re.I), \
            f"bootstrap.ps1 里出现了动态执行（{pat}）——引导器只许显式拼命令"


def test_ps1_clones_the_canonical_repo():
    assert REPO_URL in _ps1_text(), "clone 地址必须是固定的官方仓库"


def test_ps1_delegates_to_install_ps1_instead_of_copying_it():
    """只许调用仓库里的 install.ps1，不许把它的安装逻辑抄一份。"""
    text = _ps1_text()
    assert r"scripts\windows\install.ps1" in text, "没有引用 scripts\\windows\\install.ps1"
    assert re.search(r"-File\s+\$installer", text), "必须以子进程 -File 方式调用 install.ps1"
    # install.ps1 的活：建 venv、装依赖、拉模型。引导器都不该自己干。
    for forbidden in ("-m venv", "pip install", "ollama pull"):
        assert forbidden not in text, f"引导器自己做了 install.ps1 的活：{forbidden}"


def test_cmd_points_at_bootstrap_ps1():
    raw = CMD.read_bytes()
    text = raw.decode("ascii")  # 非 ASCII 会在这里直接抛错，等于断言纯 ASCII
    assert "%~dp0bootstrap.ps1" in text, "bootstrap.cmd 没有指向同目录的 bootstrap.ps1"
    assert "powershell" in text.lower()
    assert "-NoProfile" in text
    assert "-ExecutionPolicy Bypass" in text
    assert "-File" in text
    assert "pause" in text.lower(), "结束要 pause，否则双击后窗口一闪而过看不到报错"


def test_cmd_is_pure_ascii_without_bom():
    """cmd 混进 BOM/中文时，cmd.exe 会在 @echo off 之前就解析出错。"""
    raw = CMD.read_bytes()
    assert not raw.startswith(UTF8_BOM), "bootstrap.cmd 不能带 BOM"
    assert all(b < 128 for b in raw), "bootstrap.cmd 必须是纯 ASCII"


def test_ps1_chinese_output_is_utf8_with_bom():
    raw = PS1.read_bytes()
    assert raw.startswith(UTF8_BOM), "bootstrap.ps1 含中文，必须是 UTF-8 with BOM"
    text = raw[len(UTF8_BOM):].decode("utf-8")  # 顺带断言是合法 UTF-8
    assert any("一" <= ch <= "鿿" for ch in text), "引导器应该有中文输出"


def test_doc_mentions_both_release_files():
    text = DOC.read_text(encoding="utf-8")
    assert "bootstrap.cmd" in text and "bootstrap.ps1" in text, \
        "安装文档要讲清下载哪两个文件"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="本机没有 pwsh，跳过语法检查")
def test_ps1_parses_under_pwsh():
    """有 pwsh 就用它把脚本整份 parse 一遍，语法错误在这里就红。"""
    pwsh = shutil.which("pwsh")
    cmd = (
        f"[ScriptBlock]::Create((Get-Content -Raw '{PS1}')) | Out-Null; "
        "Write-Output 'PARSE_OK'"
    )
    done = subprocess.run(
        [pwsh, "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert done.returncode == 0 and "PARSE_OK" in done.stdout, \
        f"bootstrap.ps1 语法错误：\n{done.stdout}\n{done.stderr}"
