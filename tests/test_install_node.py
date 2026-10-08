"""scripts/node/install_node.sh 的 Linux 侧测试（TK-003）。

目标机（macOS）上的真实行为——launchctl、say、MLX、Apple Translation——这里
一概测不到，留给 Mac 真机。能在 Linux 上钉住的是：控制端的参数校验（S6）、
dry-run 的无副作用、nodes.json 合并（P6）、token 不进 argv（S5），以及
「自检失败 → 旧版不受影响」这条主干。

方法：PATH 里放一组桩命令。桩 ssh 不联网，而是把「目标机命令」直接在本机
`bash -c` 执行（HOME 指向 tmp_path），并把收到的完整 argv / stdin 记下来，
测试据此断言 token 的去向。
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "node" / "install_node.sh"
PLIST_TEMPLATE = REPO_ROOT / "scripts" / "node" / "com.realtimesubtitle.node.plist.template"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")

_STUB_SSH = r"""#!/bin/bash
# 记录 argv（NUL 分隔）与 stdin；然后把目标机命令在本机执行
log="$STUB_LOG_DIR"
printf '%s\0' "$@" >> "$log/ssh.argv"
printf '\n---\n' >> "$log/ssh.argv"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    if [ "$1" = "-o" ]; then shift 2; else shift; fi
done
[ "${1:-}" = "--" ] || { echo "stub ssh: 缺少 --" >&2; exit 97; }
shift   # --
shift   # host
tee -a "$log/ssh.stdin" | bash -c "$1"
"""

_STUBS = {
    "ssh": _STUB_SSH,
    "uname": '#!/bin/bash\n[ "${1:-}" = "-m" ] && echo arm64 || echo Darwin\n',
    "df": '#!/bin/bash\necho "Filesystem 1K-blocks Used Available Capacity Mounted"\n'
          'echo "/dev/x 999999999 1 999999999 1% /"\n',
    "brew": "#!/bin/bash\nexit 0\n",
    "ollama": "#!/bin/bash\nexit 0\n",
    "swift": "#!/bin/bash\nexit 0\n",
    "sysctl": "#!/bin/bash\necho 17179869184\n",
    "uv": '#!/bin/bash\n'
          'if [ "${1:-}" = "venv" ]; then mkdir -p venv/bin; ln -sf "$STUB_PYTHON" venv/bin/python; fi\n'
          'exit 0\n',
    "tailscale": '#!/bin/bash\n'
                 'if [ "${1:-}" = "ip" ]; then [ -n "${TS_IP-100.64.0.7}" ] && echo "${TS_IP-100.64.0.7}"; exit 0; fi\n'
                 'if [ "${1:-}" = "status" ]; then echo \'{"Self":{"DNSName":"mini2.tail1234.ts.net."}}\'; exit 0; fi\n'
                 'exit 1\n',
}


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in _STUBS.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    logdir = tmp_path / "log"
    logdir.mkdir()
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        "PATH": f"{bindir}:{e['PATH']}",
        "STUB_LOG_DIR": str(logdir),
        "STUB_PYTHON": sys.executable,
    })
    return e, home, logdir


def _run(args, e, input_=None, timeout=120):
    return subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True,
                          text=True, input=input_, timeout=timeout)


def _snapshot(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ------------------------------------------------------------ 语法 / 静态


def test_bash_syntax_ok():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="没装 shellcheck")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("var", ["PREFLIGHT_SH", "NODE_ID_SH", "STOP_V1_SH",
                                  "BUILD_SH", "SELFCHECK_SH", "SWAP_SH"])
def test_target_side_scripts_have_valid_syntax(var):
    """交给目标机的脚本是字符串，shellcheck 看不到，这里单独过一遍语法。"""
    r = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; printf "%s\\n" "${var}" | bash -n'],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_bash4_only_features():
    """macOS 自带 bash 3.2（CLAUDE.md 第 7 节）：没有关联数组 / mapfile / ${x,,} 等。"""
    text = SCRIPT.read_text(encoding="utf-8")
    for pat in [r"declare\s+-A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+(,,|\^\^)\}",
                r"\[\[\s+-v\s", r"&>>", r"\|&"]:
        assert not re.search(pat, text), f"bash 3.2 不支持：{pat}"


def test_plist_template_contract():
    text = PLIST_TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), "plist 里不许写死 IP（S2）"
    assert "--token-file" in text and "--token-stdin" not in text
    assert "@HOME@/.config/rs-node/token" in text
    d = plistlib.loads(text.replace("@HOME@", "/Users/x").encode())
    assert d["Label"] == "com.realtimesubtitle.node"
    assert d["KeepAlive"] is True
    assert d["WorkingDirectory"] == "/Users/x/rs-node"
    assert d["ProgramArguments"][:3] == ["/Users/x/rs-node/venv/bin/python", "-m",
                                         "realtime_subtitle.node.gateway"]
    assert "Library/Logs/rs-node" in d["StandardOutPath"]
    assert not any("token" in k.lower() for k in d.get("EnvironmentVariables", {}))


def test_script_constants_match_node_code():
    """文件名 / 端口 / 状态目录必须与节点代码一字不差。"""
    from realtime_subtitle.node import gateway, info

    text = SCRIPT.read_text(encoding="utf-8")
    assert f"NODE_PORT={gateway.PORT}" in text
    assert info.NODE_ID_FILENAME == "node_id" and '"$d/node_id"' in text
    assert str(info.STATE_DIR).endswith("Library/Application Support/rs-node")
    assert 'Library/Application Support/rs-node' in text
    assert info.ASR_STATE_FILENAME in text
    assert gateway.UDS_FILENAME in text


# ------------------------------------------------------------ 主机名白名单（S6）

BAD_HOSTS = [
    "-oProxyCommand=touch /tmp/pwned",
    "-oProxyCommand=x",
    "a b",
    "a;b",
    "mini2;id",
    "mini2$(id)",
    "mini2`id`",
    "mini2\nevil",
    "",
    "-F/etc/passwd",
    "-x",
    "mini2/../x",
    "mini2:22",
]


@pytest.mark.parametrize("host", BAD_HOSTS)
@pytest.mark.parametrize("via_dashdash", [False, True])
def test_invalid_host_rejected_without_ssh(env, host, via_dashdash):
    e, _home, logdir = env
    args = ["--dry-run"] + (["--"] if via_dashdash else []) + [host]
    r = _run(args, e)
    assert r.returncode != 0
    assert not (logdir / "ssh.argv").exists(), "非法主机名不该走到 ssh"


@pytest.mark.parametrize("host", ["mini2", "user@mini2", "mini2.local", "10.0.0.5",
                                  "my_host-1", "a.b-c@d_e.f"])
def test_valid_host_accepted(env, host):
    e, _home, logdir = env
    r = _run(["--dry-run", host], e)
    assert r.returncode == 0, r.stderr
    assert not (logdir / "ssh.argv").exists()


def test_arg_errors(env):
    e, _h, _l = env
    assert _run([], e).returncode != 0
    assert _run(["--local", "mini2"], e).returncode != 0
    assert _run(["a", "b"], e).returncode != 0
    assert _run(["--bogus"], e).returncode != 0


# ------------------------------------------------------------ dry-run


def test_dry_run_prints_plan_and_touches_nothing(env, tmp_path):
    e, home, logdir = env
    secret = "f" * 64
    tok = home / ".config" / "rslite" / "tokens" / "mini2.token"
    tok.parent.mkdir(parents=True)
    tok.write_text(secret)
    before_home = _snapshot(home)
    before_tmp = _snapshot(tmp_path)
    r = _run(["--dry-run", "mini2"], e)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for key in ["预检", "node_id", "<redacted>", "git archive", "install.sh --skip-models",
                "rtf", "rs-remote", "rs-node.prev", "bootstrap", "kickstart", "/v1/info",
                "nodes.json", "回滚"]:
        assert key in out, key
    assert secret not in out + r.stderr
    assert not re.search(r"\b[0-9a-f]{64}\b", out)
    assert _snapshot(home) == before_home
    assert _snapshot(tmp_path) == before_tmp
    assert not (logdir / "ssh.argv").exists()


def test_dry_run_local_skips_nodes_json(env):
    e, home, _l = env
    r = _run(["--dry-run", "--local"], e)
    assert r.returncode == 0
    assert "不写 nodes.json" in r.stdout
    assert "rslite/tokens" not in r.stdout
    assert _snapshot(home) == []


# ------------------------------------------------------------ nodes.json 合并（P6）


def _merge(e, path, nid, node_id, url, token_file):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; merge_nodes_json "$@"', "x", str(SCRIPT),
         str(path), nid, node_id, url, token_file],
        env=e, capture_output=True, text=True)


def test_merge_nodes_json_create_dedupe_and_perms(env):
    e, home, _l = env
    path = home / ".config" / "rslite" / "nodes.json"
    r = _merge(e, path, "mini2", "nid-aaaaaaaa", "ws://mini2.ts.net:8791", "/t/mini2.token")
    assert r.returncode == 0, r.stderr
    assert json.loads(path.read_text()) == [
        {"id": "mini2", "node_id": "nid-aaaaaaaa", "url": "ws://mini2.ts.net:8791",
         "token_file": "/t/mini2.token"}]
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700

    # 其它条目保留；放宽权限后重写仍回到 0600
    assert _merge(e, path, "air", "nid-bbbbbbbb", "ws://air.ts.net:8791", "/t/air.token").returncode == 0
    path.chmod(0o644)
    r = _merge(e, path, "mini2", "nid-cccccccc", "ws://mini2-new.ts.net:8791", "/t/mini2.token")
    assert r.returncode == 0, r.stderr
    data = json.loads(path.read_text())
    assert [d["id"] for d in data] == ["mini2", "air"]  # 原位更新，不挪位置、不重复
    assert data[0]["node_id"] == "nid-cccccccc" and data[0]["url"].startswith("ws://mini2-new")
    assert data[1]["node_id"] == "nid-bbbbbbbb"
    assert _mode(path) == 0o600
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_merge_nodes_json_refuses_to_clobber_non_list(env):
    e, home, _l = env
    path = home / "nodes.json"
    path.write_text('{"oops": 1}')
    r = _merge(e, path, "mini2", "n", "ws://h:8791", "/t")
    assert r.returncode != 0
    assert path.read_text() == '{"oops": 1}'
    path.write_text("not json")
    assert _merge(e, path, "mini2", "n", "ws://h:8791", "/t").returncode != 0
    assert path.read_text() == "not json"


# ------------------------------------------------------------ 全流程（桩）：token 去向、失败不碰旧版


def test_preflight_failure_stops_before_any_change(env):
    e, home, logdir = env
    e["TS_IP"] = ""  # tailscale ip -4 无输出
    r = _run(["mini2"], e)
    assert r.returncode != 0
    assert "Tailscale" in r.stdout + r.stderr
    assert not (home / "rs-node.new").exists()
    assert not (home / ".config").exists()


def test_token_stays_out_of_argv_and_selfcheck_failure_keeps_old_version(env):
    e, home, logdir = env
    old = home / "rs-node"
    old.mkdir()
    (old / "MARK").write_text("old")

    r = _run(["mini2"], e, timeout=300)
    # Linux 上没有 MLX / rstranslate，自检必然失败：要的就是这条回滚主干
    assert r.returncode != 0, r.stdout
    assert "自检失败" in r.stderr

    assert (old / "MARK").read_text() == "old", "自检失败不许碰旧版"
    assert not (home / "rs-node.new").exists(), ".new 要清理掉"
    assert not (home / "rs-node.prev").exists()

    tgt_token = home / ".config" / "rs-node" / "token"
    cli_token = home / ".config" / "rslite" / "tokens" / "mini2.token"
    token = tgt_token.read_text()
    assert len(token) >= 32
    assert cli_token.read_text() == token
    assert _mode(tgt_token) == 0o600 and _mode(cli_token) == 0o600
    assert _mode(tgt_token.parent) == 0o700 and _mode(cli_token.parent) == 0o700

    node_id_file = home / "Library" / "Application Support" / "rs-node" / "node_id"
    assert _mode(node_id_file) == 0o600 and node_id_file.read_text().strip()

    argv = (logdir / "ssh.argv").read_bytes()
    stdin = (logdir / "ssh.stdin").read_bytes()
    assert argv.count(b"\0--\0mini2\0") >= 5, "每次 ssh 都必须在主机名前加 --"
    assert token.encode() not in argv, "token 出现在 ssh 的 argv 里"
    assert token.encode() in stdin, "对照：token 应当经 stdin 送达"
    assert token not in r.stdout + r.stderr
    assert not (home / ".config" / "rslite" / "nodes.json").exists(), "失败时不写清单"


def test_existing_target_token_is_reused(env):
    e, home, logdir = env
    tgt = home / ".config" / "rs-node" / "token"
    tgt.parent.mkdir(parents=True)
    existing = "a1" * 32
    tgt.write_text(existing)
    _run(["mini2"], e, timeout=300)
    assert tgt.read_text() == existing
    assert (home / ".config" / "rslite" / "tokens" / "mini2.token").read_text() == existing
    # 沿用时目标机那份不需要再传一遍
    assert existing.encode() not in (logdir / "ssh.argv").read_bytes()


def test_node_id_is_reused_on_rerun(env):
    e, home, _l = env
    _run(["mini2"], e, timeout=300)
    f = home / "Library" / "Application Support" / "rs-node" / "node_id"
    first = f.read_text()
    _run(["mini2"], e, timeout=300)
    assert f.read_text() == first
