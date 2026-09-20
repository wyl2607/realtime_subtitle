"""桌面快捷方式这件事有三处副本，必须对得上。

install.ps1 的 `$batTemplate` 决定桌面上**真的**会有哪几个 .bat，而
`docs/zh/user-guide-template.txt`（生成「操作说明.txt」的单一真相源）告诉
用户有哪几个。这两份各写各的，于是：

- 加了入口忘了写说明 → 用户桌面上多个图标，没人知道它干嘛的；
- 改了名忘了改说明 → 说明里让人双击一个**不存在**的文件；
- 退役了一个入口忘了从说明里删 → 更糟，老用户桌面上那个文件还在
  （install.ps1 只写不删过），说明还教他去点，于是"合并入口"等于没发生。

第三条就是 2026-09-20 合并「启动/更新/启动并更新」时真踩到的形状，所以
`$RETIRED` 那一条断言是有来历的，别当成洁癖删掉。

纯文本解析，不需要 PowerShell，两个 CI job 都会跑到（同 test_ps1_encoding）。
"""
import re
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "scripts" / "windows" / "install.ps1"
GUIDE = REPO_ROOT / "docs" / "zh" / "user-guide-template.txt"

# 已经退役的桌面入口。它们的 .ps1 通常还留着（被别的入口调用），所以光看
# "文件还在不在"判断不了——只能维护这张名单。
_RETIRED = ("启动并更新字幕.bat", "更新字幕.bat", "下载并加字幕.bat")


def _bat_template():
    """从 install.ps1 里抠出 $batTemplate，返回 [(bat 名, ps1 相对路径)]。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    block = re.search(r"\$batTemplate\s*=\s*@\((.*?)\n\)", text, re.S)
    assert block, "install.ps1 里找不到 $batTemplate——它被改名或改写法了，同步改本测试"
    pairs = re.findall(r'@\("([^"]+)",\s*"([^"]+)"\)', block.group(1))
    assert pairs, "$batTemplate 解析出来是空的"
    return pairs


def test_every_shortcut_points_at_a_real_script():
    """☠️ 指向不存在的 ps1 = 双击报"找不到文件"，而且只有装完才发现。"""
    missing = [f"{bat} → {rel}" for bat, rel in _bat_template()
               if not (REPO_ROOT / rel.replace("\\", "/")).is_file()]
    assert not missing, "这些桌面入口指向的脚本不存在：\n  " + "\n  ".join(missing)


def test_every_shortcut_is_explained_in_the_user_guide():
    guide = GUIDE.read_text(encoding="utf-8")
    undocumented = [bat for bat, _ in _bat_template() if bat not in guide]
    assert not undocumented, (
        "这些桌面入口在操作说明模板里没有说明，用户桌面上会多出不知道干嘛的图标：\n  "
        + "\n  ".join(undocumented))


def test_user_guide_does_not_mention_retired_shortcuts():
    """☠️ 说明里留着退役入口，等于教用户去点一个我们已经不再生成的文件。"""
    guide = GUIDE.read_text(encoding="utf-8")
    stale = [bat for bat in _RETIRED if bat in guide]
    assert not stale, (
        "操作说明里还在提这些已经退役的入口：\n  " + "\n  ".join(stale))


def test_retired_shortcuts_are_actually_gone_from_the_template():
    """退役名单和在用名单不能有交集——否则说明有人改了一半。"""
    active = {bat for bat, _ in _bat_template()}
    overlap = active & set(_RETIRED)
    assert not overlap, f"这些名字既在 $batTemplate 里又在退役名单里：{sorted(overlap)}"


def test_installer_cleans_up_retired_shortcuts():
    """install.ps1 必须主动删掉退役入口，光靠"不再生成"对老用户没用。

    老用户桌面上那几个 .bat 是上一次 install 写下的，而它们指向的 ps1 还在，
    所以双击照样能跑——不删的话，合并入口对他等于没发生。
    """
    text = INSTALL_PS1.read_text(encoding="utf-8")
    block = re.search(r"\$retiredBats\s*=\s*@\((.*?)\)", text, re.S)
    assert block, "install.ps1 里找不到 $retiredBats 清理逻辑"
    listed = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert set(_RETIRED) <= listed, (
        f"install.ps1 的清理名单漏了：{sorted(set(_RETIRED) - listed)}")
    assert "Remove-Item" in text, "找到了 $retiredBats 但没看到 Remove-Item"
