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
import time
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
          'if [ "${1:-}" = "venv" ]; then mkdir -p venv/bin; ln -sf "$STUB_FAKE_PYTHON" venv/bin/python; fi\n'
          'exit 0\n',
    "tailscale": '#!/bin/bash\n'
                 'if [ "${1:-}" = "ip" ]; then [ -n "${TS_IP-100.64.0.7}" ] && echo "${TS_IP-100.64.0.7}"; exit 0; fi\n'
                 'if [ "${1:-}" = "status" ]; then echo "{\\"Self\\":{\\"DNSName\\":\\"${TS_DNS:-mini2.tail1234.ts.net}.\\"}}"; exit 0; fi\n'
                 'exit 1\n',
    # launchctl：只记录调用；STUB_BOOTSTRAP_FAIL=1 时 bootstrap 一律失败
    "launchctl": '#!/bin/bash\n'
                 'echo "$*" >> "$STUB_LOG_DIR/launchctl.log"\n'
                 'if [ "${1:-}" = "print" ]; then printf "gui/501/x = {\\n\\tstate = running\\n\\tpid = 4242\\n}\\n"; exit 0; fi\n'
                 'if [ "${1:-}" = "bootstrap" ] && [ -n "${STUB_BOOTSTRAP_FAIL:-}" ]; then exit 5; fi\n'
                 'exit 0\n',
    "plutil": "#!/bin/bash\nexit 0\n",
    # curl：模拟 gateway 的 /v1/info；STUB_INFO_FAIL=1 时拿不到响应
    "curl": '#!/bin/bash\n'
            '[ -n "${STUB_INFO_FAIL:-}" ] && exit 22\n'
            'nid=$(cat "$HOME/Library/Application Support/rs-node/node_id")\n'
            'echo "{\\"v\\":2,\\"node_id\\":\\"$nid\\"}"\n',
    # macOS 16.x：$PATH 前面的 bin 目录没有 say/afconvert，全走真系统工具会真跑 TTS/转码；
    # 桩成无输出，让自检走「没有可用德语声音」分支，什么都不真的执行
    "say": "#!/bin/bash\nexit 0\n",
    "afconvert": "#!/bin/bash\nexit 0\n",
    # lsof：v1 的 cwd 由测试指定；`-iTCP` 查询（第 10 步 TCP 监听校验）返回 STUB_TCP_LISTEN（-Fpn 格式，默认 gateway=4242 在 100.64.0.7:8791）
    "lsof": '#!/bin/bash\n'
            'case "$*" in *-iTCP*)\n'
            '  if [ -n "${STUB_TCP_LISTEN+x}" ]; then printf "%s" "$STUB_TCP_LISTEN"; else printf "p4242\\nn100.64.0.7:8791\\n"; fi\n'
            '  exit 0;;\n'
            'esac\n'
            'printf \'p1\\nfcwd\\nn%s\\n\' "${STUB_V1_CWD:-}"\n',
    # nohup：不真的起进程，只记下「在哪个目录、跑什么」
    # STUB_NOHUP_SPAWN=1 时再起一个 argv 与入参一致的假进程（模拟「v1 真的起来了」），pid 记下供清理
    "nohup": '#!/bin/bash\necho "PWD=$PWD ARGS=$*" >> "$STUB_LOG_DIR/nohup.log"\n'
             'if [ -n "${STUB_NOHUP_SPAWN:-}" ]; then\n'
             '  ( exec -a "$*" "$STUB_REAL_SLEEP" 30 </dev/null >/dev/null 2>&1 ) &\n'
             '  echo $! >> "$STUB_LOG_DIR/nohup.pids"\n'
             'fi\n',
    "sleep": "#!/bin/bash\nexit 0\n",
    # pgrep（C2 测试隔离）：只在「本测试自己起的进程」里找（pid 清单由 spawn / nohup 桩写），
    # 仍用真 ps 取命令行、按脚本传来的模式做真实 grep -E；永远看不到测试之外的进程
    # （例如 mini2 上正在服务的真 v1）。ps / kill 用真的，匹配与复核逻辑照样被真实输出验证。
    "pgrep": '#!/bin/bash\n'
             'pat="${@: -1}"\n'
             'for f in "$STUB_LOG_DIR/spawned.pids" "$STUB_LOG_DIR/nohup.pids"; do\n'
             '  [ -f "$f" ] || continue\n'
             '  while read -r p; do\n'
             '    c=$(ps -o command= -p "$p" 2>/dev/null) || continue\n'
             '    printf "%s\\n" "$c" | grep -Eq -- "$pat" && echo "$p"\n'
             '  done < "$f"\n'
             'done\n'
             'exit 0\n',
    # mv：STUB_MV_FAIL_PREV=1 时 rs-node → rs-node.prev 失败（F7）
    "mv": '#!/bin/bash\n'
          'if [ -n "${STUB_MV_FAIL_PREV:-}" ] && [ "${1:-}" = "$HOME/rs-node" ] && [ "${2:-}" = "$HOME/rs-node.prev" ]; then exit 1; fi\n'
          'exec "$STUB_REAL_MV" "$@"\n',
}

# 假 venv python：自检那段（`python - <wav>`，脚本走 stdin）按环境变量给出可控结果，
# 其余调用（/v1/info 校验的 -c 单行）转给真 python
_FAKE_PYTHON = r"""#!/bin/bash
if [ "${1:-}" = "-" ]; then
    cat >/dev/null
    if [ -n "${STUB_SELFCHECK_FAIL:-}" ]; then echo "ENGINE_LOAD_FAILED=Boom"; exit 3; fi
    echo "TRANSLATION_STATUS=${STUB_TRANSLATION:-installed}"
    echo "RTF=0.1"
    exit 0
fi
exec "$STUB_PYTHON" "$@"
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in _STUBS.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    fake = tmp_path / "fakepython"
    fake.write_text(_FAKE_PYTHON)
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    logdir = tmp_path / "log"
    logdir.mkdir()
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        "PATH": f"{bindir}:{e['PATH']}",
        # install_node.sh 会把这条前缀 export 到 PATH 最前面；指回桩目录可保证真
        # tailscale/uv/ollama（在 /opt/homebrew/bin 等 System PATH 里）永远不会被优先命中
        "RS_TOOL_PATH_PREFIX": str(bindir),
        "STUB_LOG_DIR": str(logdir),
        "STUB_PYTHON": sys.executable,
        "STUB_FAKE_PYTHON": str(fake),
        "STUB_REAL_MV": shutil.which("mv"),
        "STUB_REAL_SLEEP": shutil.which("sleep"),
        "RS_V1_WAIT": "2",
        "RS_TCP_WAIT": "2",
    })
    yield e, home, logdir
    pids = logdir / "nohup.pids"
    if pids.exists():
        for line in pids.read_text().split():
            try:
                os.kill(int(line), 9)
            except OSError:
                pass


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

    e["STUB_SELFCHECK_FAIL"] = "1"  # 假 python 的自检段报 ENGINE_LOAD_FAILED=Boom
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode != 0, r.stdout
    assert "目标机自检失败" in r.stderr
    assert "ENGINE_LOAD_FAILED=Boom" in r.stdout + r.stderr

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


# ------------------------------------------------------------ CR-005 修复：成功 / 回滚路径（桩）
#
# 目标机行为（launchctl / plutil / curl / lsof / nohup）全是桩；venv python 是假的，
# 自检与 /v1/info 的结果由环境变量控制，所以每个失败都有「具体原因」可断言。
# v1 进程是真进程（bash exec -a 伪装 argv），pgrep / ps / kill 用真的，
# 这样 S-F1 的匹配规则测的是真实行为而不是桩。

V1_ARGS = "--host 100.105.163.59 --port 8791 --token-file /Users/x/.config/rs-remote/token"
V1_CMD = f"/Users/x/rs-remote/venv/bin/python3.13 -m realtime_subtitle.remote.server {V1_ARGS}"


MINI2_DECOY = ("venv/bin/python -u -m realtime_subtitle.remote.server --host 100.105.163.59 "
               "--port 8791 --token-file /Users/yilinwang/.config/rs-remote/token")


@pytest.fixture(autouse=True)
def _real_v1_decoy():
    """C2 守卫：每个用例期间都有一个「会命中模式的无关进程」（逐字模拟 mini2 上真 v1），
    它不在任何 pid 清单里；用例结束时必须还活着，否则说明测试碰到了测试之外的进程。"""
    d = subprocess.Popen(["bash", "-c", 'exec -a "$0" cat', MINI2_DECOY], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    yield d
    alive = d.poll() is None
    d.kill()
    d.wait()
    d.stdin.close()
    assert alive, "测试杀掉了测试之外的（模拟真 v1 的）进程"


@pytest.fixture
def spawn(env):
    procs = []
    pidfile = env[2] / "spawned.pids"

    def _spawn(cmdline):
        # 用 os.environ 而不是桩 PATH：这里要真的 sleep
        # cat 没有额外参数：ps 看到的命令行就恰好是 cmdline（sleep 300 会多出一个 "300"，
        # 白名单解析会把它当未知参数拒掉）；stdin 开着管道，cat 就一直阻塞
        p = subprocess.Popen(["bash", "-c", 'exec -a "$0" cat', cmdline], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        with pidfile.open("a") as f:
            f.write(f"{p.pid}\n")
        time.sleep(0.3)  # 等 exec 完成，否则 argv 还是 bash 的
        return p

    yield _spawn
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()
        p.stdin.close()


def _launchctl_calls(logdir: Path):
    f = logdir / "launchctl.log"
    return f.read_text().splitlines() if f.exists() else []


def _make_v1_cwd(tmp_path: Path) -> Path:
    """v1 的工作目录：带 venv/bin/python（重拉的固定入口）。"""
    cwd = tmp_path / "rs-remote"
    py = cwd / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    return cwd


def _wait_for(path: Path, needle: str, secs=5.0) -> str:
    end = time.time() + secs
    while time.time() < end:
        if path.exists() and needle in path.read_text():
            return path.read_text()
        time.sleep(0.05)
    return path.read_text() if path.exists() else ""


def test_success_twice_keeps_single_nodes_json_entry_and_updates_it(env):
    e, home, logdir = env
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "gateway-ok" in r.stdout

    nodes = home / ".config" / "rslite" / "nodes.json"
    first = json.loads(nodes.read_text())
    assert len(first) == 1 and first[0]["id"] == "mini2"
    assert first[0]["url"] == "ws://mini2.tail1234.ts.net:8791"

    plist = home / "Library" / "LaunchAgents" / "com.realtimesubtitle.node.plist"
    assert plist.exists() and str(home) in plist.read_text()
    assert _mode(home / "Library" / "Logs" / "rs-node") == 0o700  # S-F4
    assert not (home / "rs-node.prev").exists() and not (home / "rs-node.failed").exists()

    e["TS_DNS"] = "mini2-b.tail1234.ts.net"
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    second = json.loads(nodes.read_text())
    assert len(second) == 1, "重跑不许重复追加"
    assert second[0]["url"] == "ws://mini2-b.tail1234.ts.net:8791", "同 id 原位更新"
    assert second[0]["node_id"] == first[0]["node_id"]
    assert (home / "rs-node.prev").is_dir(), "第二次成功后上一版保留为 .prev"
    assert _mode(nodes) == 0o600
    assert not (home / "Library/Application Support/rs-node/v1.restart").exists()


def test_first_install_does_not_claim_prev_and_second_does(env):
    e, home, logdir = env
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rs-node.prev" not in r.stdout.split("节点安装完成")[1]
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "上一版保留在 ~/rs-node.prev" in r.stdout


def test_preflight_non_ip_first_line_fails_before_any_change(env):
    """GUI 模式的报错行（rc=0、stdout 非 IP）不能被当成 Tailscale IP。"""
    e, home, logdir = env
    for bad in ("The Tailscale GUI failed to start: boom (Tailscale.CLIError error 3.)",
                "8.8.8.8", "100.200.1.1", "100.63.255.255"):
        e["TS_IP"] = bad
        r = _run(["mini2"], e)
        assert r.returncode != 0, bad
        assert "Tailscale IPv4" in r.stdout + r.stderr, bad
        assert not (home / "rs-node.new").exists()
        assert not (home / ".config").exists()


@pytest.mark.parametrize("ts_ip, should_pass", [
    ("100.64.999.999", False),
    ("100.64.1", False),
    ("100.64.1.2.3", False),
    ("100.064.1.2", False),
    ("100.105.163.59", True),
])
def test_preflight_tailscale_ip_validation(env, ts_ip, should_pass):
    """参数化测试预检的 Tailscale IPv4 严格格式校验（100.64.0.0/10）。

    前导零（如 100.064.1.2）视为不合法：标准 IPv4 严禁多余前导零以杜绝八进制歧义。
    """
    e, home, logdir = env
    e["TS_IP"] = ts_ip
    e["STUB_TCP_LISTEN"] = f"p4242\nn{ts_ip}:8791\n"
    r = _run(["mini2"], e, timeout=300)
    if should_pass:
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"TCP 已在 {ts_ip}:8791 监听" in r.stdout
    else:
        assert r.returncode != 0, f"非法的 TS_IP={ts_ip} 应该导致预检失败"
        assert "Tailscale IPv4" in r.stdout + r.stderr, ts_ip
        assert not (home / "rs-node.new").exists()
        assert not (home / ".config").exists()


def test_tailscale_is_called_with_be_cli_env(env):
    e, home, logdir = env
    ts = Path(e["PATH"].split(":")[0]) / "tailscale"
    ts.write_text('#!/bin/bash\necho "BE_CLI=${TAILSCALE_BE_CLI:-unset} $*" >> "$STUB_LOG_DIR/ts.log"\n'
                  'if [ "${1:-}" = "ip" ]; then echo 100.64.0.7; exit 0; fi\n'
                  'if [ "${1:-}" = "status" ]; then echo "{\\"Self\\":{\\"DNSName\\":\\"m.tail1.ts.net.\\"}}"; exit 0; fi\n'
                  'exit 1\n')
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [ln for ln in (logdir / "ts.log").read_text().splitlines() if " ip -4" in ln]
    assert len(calls) >= 2, "预检与第 10 步都要取 IP"
    assert all(ln.startswith("BE_CLI=1 ") for ln in calls)


def test_tcp_not_listening_rolls_back(env, spawn, tmp_path):
    """UDS 自检通过但 TCP 没起来 → 判失败并回滚（含按记录重拉 v1）。"""
    e, home, logdir = env
    spawn(V1_CMD)
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    e["STUB_NOHUP_SPAWN"] = "1"
    e["STUB_TCP_LISTEN"] = ""  # lsof 看不到任何 8791 监听
    r = _run(["mini2"], e, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "ROLLBACK：gateway 2 秒内没有在 100.64.0.7:8791 上监听 TCP" in out
    assert "v2 安装失败，已恢复 v1" in out
    assert not (home / "Library/LaunchAgents/com.realtimesubtitle.node.plist").exists()
    assert (home / "rs-node.failed").is_dir()
    assert not (home / ".config" / "rslite" / "nodes.json").exists()


def test_tcp_listening_on_wrong_pid_or_ip_rolls_back(env):
    e, home, logdir = env
    for listen in ("p999\nn100.64.0.7:8791\n",      # 不是 launchd 给的 gateway 进程
                   "p4242\nn127.0.0.1:8791\n"):     # 不是 Tailscale IP
        e["STUB_TCP_LISTEN"] = listen
        r = _run(["mini2"], e, timeout=300)
        assert r.returncode != 0, listen
        assert "没有在 100.64.0.7:8791 上监听 TCP" in r.stdout + r.stderr


@pytest.mark.parametrize("wild", ["*:8791", "0.0.0.0:8791", "[::]:8791"])
def test_wildcard_listener_rolls_back_even_if_correct_ip_also_listens(env, wild):
    e, home, logdir = env
    e["STUB_TCP_LISTEN"] = f"p4242\nn100.64.0.7:8791\np4242\nn{wild}\n"
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode != 0
    assert "通配" in r.stdout + r.stderr
    assert not (home / ".config" / "rslite" / "nodes.json").exists()


def test_tcp_listening_on_correct_ip_succeeds(env):
    e, home, logdir = env
    e["STUB_TCP_LISTEN"] = "p4242\nn100.64.0.7:8791\n"
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TCP 已在 100.64.0.7:8791 监听" in r.stdout


def test_no_prev_bootstrap_failure_removes_plist_keeps_failed_and_relaunches_v1(env, spawn, tmp_path):
    e, home, logdir = env
    v1 = spawn(V1_CMD)
    v1_cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(v1_cwd)
    e["STUB_BOOTSTRAP_FAIL"] = "1"
    e["STUB_NOHUP_SPAWN"] = "1"

    r = _run(["mini2"], e, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "ROLLBACK：launchctl bootstrap 失败" in out
    assert "没有上一版可回滚" in out
    assert "v2 安装失败，已恢复 v1" in out

    plist = home / "Library" / "LaunchAgents" / "com.realtimesubtitle.node.plist"
    assert not plist.exists(), "留着指向不存在 venv 的 plist 会在每次登录反复拉起失败"
    assert not (home / "rs-node").exists()
    assert (home / "rs-node.failed" / "venv" / "bin" / "python").exists(), "失败版要留着排查"
    assert not (home / ".config" / "rslite" / "nodes.json").exists()

    # v1 在 bootstrap 之前被停（抢同一个 8791 端口），然后按记录重拉
    assert v1.wait(timeout=5) is not None
    calls = _launchctl_calls(logdir)
    assert any(c.startswith("bootstrap") for c in calls)
    log = _wait_for(logdir / "nohup.log", "realtime_subtitle.remote.server")
    assert f"PWD={v1_cwd}" in log
    # 固定入口 <cwd>/venv/bin/python，而不是 ps 里记到的解释器路径
    assert f"ARGS={v1_cwd}/venv/bin/python -m realtime_subtitle.remote.server {V1_ARGS}" in log
    assert not (home / "Library/Application Support/rs-node/v1.restart").exists()


def test_failed_dir_from_earlier_attempt_is_replaced(env):
    e, home, _l = env
    old_failed = home / "rs-node.failed"
    old_failed.mkdir()
    (old_failed / "OLD").write_text("x")
    e["STUB_BOOTSTRAP_FAIL"] = "1"
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode != 0
    assert not (old_failed / "OLD").exists(), "旧的 .failed 先删再改名"
    assert (old_failed / "venv").is_dir()


def test_with_prev_info_failure_restores_old_dir_and_rebootstraps(env, spawn, tmp_path):
    e, home, logdir = env
    assert _run(["mini2"], e, timeout=300).returncode == 0
    (home / "rs-node" / "MARK").write_text("good-v2")
    nodes = home / ".config" / "rslite" / "nodes.json"
    nodes_before = nodes.read_text()

    v1 = spawn(V1_CMD)
    v1_cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(v1_cwd)
    e["STUB_INFO_FAIL"] = "1"
    e["STUB_NOHUP_SPAWN"] = "1"
    (logdir / "launchctl.log").unlink()

    r = _run(["mini2"], e, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "ROLLBACK：gateway 40 秒内没有通过 /v1/info 自检" in out
    assert "已回滚到上一版并重新启动" in out

    assert (home / "rs-node" / "MARK").read_text() == "good-v2", "旧目录要原样回来"
    assert not (home / "rs-node.prev").exists()
    assert not (home / "rs-node.failed").exists()
    assert (home / "Library" / "LaunchAgents" / "com.realtimesubtitle.node.plist").exists()
    assert nodes.read_text() == nodes_before, "失败不许改清单"

    calls = _launchctl_calls(logdir)
    boot_idx = [i for i, c in enumerate(calls) if c.startswith("bootstrap")]
    assert len(boot_idx) == 2, calls  # 新版一次 + 回滚后旧版一次
    last_bootout = max(i for i, c in enumerate(calls) if c.startswith("bootout"))
    assert last_bootout < boot_idx[-1], "回滚要先 bootout 失败版再 bootstrap 旧版"

    assert v1.wait(timeout=5) is not None
    assert "PWD=" + str(v1_cwd) in _wait_for(logdir / "nohup.log", "remote.server")


def test_prev_rename_failure_rebootstraps_old_plist(env):
    """F7：bootout 旧版之后 mv rs-node → .prev 失败，不能让旧版就此停着。"""
    e, home, logdir = env
    assert _run(["mini2"], e, timeout=300).returncode == 0
    (home / "rs-node" / "MARK").write_text("good-v2")
    (logdir / "launchctl.log").unlink()
    e["STUB_MV_FAIL_PREV"] = "1"

    r = _run(["mini2"], e, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "无法把旧版换名为 .prev" in out
    assert "已重新启动旧版 LaunchAgent" in out
    assert (home / "rs-node" / "MARK").read_text() == "good-v2"

    calls = _launchctl_calls(logdir)
    kinds = [c.split()[0] for c in calls]
    assert kinds.index("bootout") < kinds.index("bootstrap"), calls
    assert "kickstart" in kinds


@pytest.mark.parametrize("status,must,must_not", [
    ("None", "rstranslate 不可用", "系统设置"),
    ("supported", "重跑 install_node.sh", "kickstart -k"),
])
def test_language_pack_hints(env, status, must, must_not):
    """F4 / F5：status 为 None 是构建问题，不是语言包问题；语言包提示是「重跑」而不是 kickstart。"""
    e, _home, _l = env
    e["STUB_TRANSLATION"] = status
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert must in r.stderr
    assert must_not not in r.stderr
    if status == "None":
        assert "重跑 install_node.sh" in r.stderr


# ---- token 校验与权限（F3 / S-F2 / S-F3）


def test_existing_token_reuse_tightens_permissions(env):
    e, home, _l = env
    d = home / ".config" / "rs-node"
    d.mkdir(parents=True)
    tok = d / "token"
    existing = "0f" * 32
    tok.write_text(existing)
    d.chmod(0o755)
    tok.chmod(0o644)
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert tok.read_text() == existing
    assert _mode(d) == 0o700 and _mode(tok) == 0o600


@pytest.mark.parametrize("bad", ["x" * 40, "ab" * 32 + "\n" + "cd" * 32, "A" * 64, "ab" * 31])
def test_malformed_existing_token_is_regenerated_on_both_sides(env, bad):
    e, home, logdir = env
    tgt = home / ".config" / "rs-node" / "token"
    tgt.parent.mkdir(parents=True)
    tgt.write_text(bad)
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    new = tgt.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", new), "新 token 必须是 64 位小写十六进制"
    assert new != bad
    assert (home / ".config/rslite/tokens/mini2.token").read_text() == new
    assert new not in r.stdout + r.stderr
    assert new.encode() not in (logdir / "ssh.argv").read_bytes()
    assert _mode(tgt) == 0o600 and _mode(tgt.parent) == 0o700


def test_malformed_client_token_is_not_adopted(env):
    e, home, _l = env
    cli = home / ".config" / "rslite" / "tokens" / "mini2.token"
    cli.parent.mkdir(parents=True)
    cli.write_text("short-but-" + "z" * 40)
    r = _run(["mini2"], e, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", cli.read_text())
    assert (home / ".config/rs-node/token").read_text() == cli.read_text()


# ---- 停 v1 的精确匹配（S-F1）


def _run_stop_v1(e):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; printf "%s\\n" "$STOP_V1_SH" | bash -s', "x", str(SCRIPT)],
        env=e, capture_output=True, text=True, timeout=60)


def test_stop_v1_matches_only_real_v1_and_records_it(env, spawn, tmp_path):
    e, home, _l = env
    cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(cwd)
    decoys = [
        spawn("tail -f /x/realtime_subtitle/remote/server.py"),          # `.` 通配 `/` 的旧 bug
        spawn("vim /x/realtime_subtitle/remote/server.py"),
        spawn("python3 -m realtime_subtitle.remote.serverx --x"),         # pgrep 会命中，ps 复核必须拒绝
        spawn("python3 -m realtime_subtitleXremote.server"),
        spawn("grep realtime_subtitle.remote.server"),
    ]
    mac_v1 = spawn("/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks/Python.framework/"
                   "Versions/3.13/Resources/Python.app/Contents/MacOS/Python "
                   "-m realtime_subtitle.remote.server --host 100.105.163.59 --port 8791 "
                   "--token-file /Users/x/.config/rs-remote/token")
    r = _run_stop_v1(e)
    assert r.returncode == 0, r.stderr
    assert mac_v1.wait(timeout=5) is not None, "框架版 Python（大写 P）的 v1 要能被识别并停掉"
    assert all(p.poll() is None for p in decoys), "无关进程不许被杀"

    rec = home / "Library" / "Application Support" / "rs-node" / "v1.restart"
    assert rec.read_text().splitlines() == [
        f"cwd={cwd}", "host=100.105.163.59", "port=8791",
        "token_file=/Users/x/.config/rs-remote/token"], "记录只存校验过的字段，不存原始命令行"
    assert _mode(rec) == 0o600 and _mode(rec.parent) == 0o700
    assert "已记录 v1" in r.stdout and "已停止" in r.stdout


def test_stop_v1_with_only_decoys_does_nothing(env, spawn):
    e, home, _l = env
    d = spawn("tail -f /x/realtime_subtitle/remote/server.py")
    stale = home / "Library" / "Application Support" / "rs-node" / "v1.restart"
    stale.parent.mkdir(parents=True)
    stale.write_text("/old\nold-cmd\n")
    r = _run_stop_v1(e)
    assert r.returncode == 0
    assert "未在运行" in r.stdout
    assert d.poll() is None
    assert not stale.exists(), "v1 没在跑就不该留着过期记录"


def test_stop_v1_pattern_is_anchored_in_source():
    text = SCRIPT.read_text(encoding="utf-8")
    assert r"realtime_subtitle\.remote\.server" in text
    assert 'pgrep -u "$uid_n"' in text
    assert "pgrep -f '[r]ealtime_subtitle.remote.server'" not in text


# ---- v1 记录/重拉：固定入口 + 白名单参数（CR-005 第 2 轮：R2-S1 / R2-D1 / R2-D2 / R2-S2）

REAL_V1_ARGS = "--host 100.105.163.59 --port 8791 --token-file /Users/x/.config/rs-remote/token"
MAC_FRAMEWORK_PY = ("/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks/Python.framework/"
                    "Versions/3.13/Resources/Python.app/Contents/MacOS/Python")


def _state_dir(home: Path) -> Path:
    return home / "Library" / "Application Support" / "rs-node"


def _run_restore(e, home: Path):
    """只跑 SWAP_SH 里的 restore_v1（连同共用校验库），观察它的输出与副作用。"""
    prog = (
        'source "$1"; eval "$V1_LIB_SH"\n'
        f'state="{_state_dir(home)}"; rec="$state/v1.restart"\n'
        'eval "$(printf "%s\\n" "$SWAP_SH" | sed -n "/^restore_v1()/,/^}/p")"\n'
        'restore_v1\n')
    return subprocess.run(["bash", "-c", prog, "x", str(SCRIPT)], env=e,
                          capture_output=True, text=True, timeout=60)


def _nohup_log(logdir: Path) -> str:
    f = logdir / "nohup.log"
    return f.read_text() if f.exists() else ""


MINI2_REAL_V1 = ("venv/bin/python -u -m realtime_subtitle.remote.server --host 100.105.163.59 "
                 "--port 8791 --token-file /Users/yilinwang/.config/rs-remote/token")


@pytest.mark.parametrize("interp", ["/Users/x/rs-remote/venv/bin/python3.13", MAC_FRAMEWORK_PY,
                                    "venv/bin/python -u", "/x/venv/bin/python -uB"])
def test_v1_record_then_restore_uses_fixed_venv_entry(env, spawn, tmp_path, interp):
    e, home, logdir = env
    cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(cwd)
    e["STUB_NOHUP_SPAWN"] = "1"
    v1 = spawn(f"{interp} -m realtime_subtitle.remote.server {REAL_V1_ARGS}")
    r = _run_stop_v1(e)
    assert r.returncode == 0 and "已记录 v1" in r.stdout, r.stdout + r.stderr
    assert v1.wait(timeout=5) is not None
    rec_text = (_state_dir(home) / "v1.restart").read_text()
    assert "python" not in rec_text.lower() and "-m" not in rec_text, "记录里不许有原始命令行"

    r = _run_restore(e, home)
    assert r.returncode == 0, r.stderr
    assert "已恢复 v1" in r.stdout and "未能自动恢复" not in r.stdout
    log = _nohup_log(logdir)
    # 固定 <cwd>/venv/bin/python（框架版的 Python.app 路径不会被重放），参数逐个原样
    assert log.strip() == (f"PWD={cwd} ARGS={cwd}/venv/bin/python "
                           f"-m realtime_subtitle.remote.server {REAL_V1_ARGS}")
    assert not (_state_dir(home) / "v1.restart").exists()


@pytest.mark.parametrize("tail", [
    "--port 8791 $(touch {tmp}/PWNED)",
    "--port 8791 `touch {tmp}/PWNED`",
    "--port 8791 ;touch {tmp}/PWNED",
    "--port 8791;id",
    "--port 8791 --evil 1",
    "--port 8791 extra",
    "--port 8791 --token-file",
    "--port 70000",
    "--port 0",
    "--port abc",
    "--host 0x7f.1 --port 1",
    "--port 8791 --host 100.105.163.59",
    "--port 8791 >/tmp/x",
])
def test_v1_unsafe_cmdline_is_not_recorded_and_never_relaunched(env, spawn, tmp_path, tail):
    e, home, logdir = env
    cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(cwd)
    e["STUB_NOHUP_SPAWN"] = "1"
    base = "--host 100.105.163.59 --token-file /Users/x/.config/rs-remote/token"
    if tail.startswith("--host"):
        base = "--token-file /Users/x/t"
    if tail.endswith("--token-file"):
        base = "--host 100.105.163.59"
    if "--port 8791 --host" in tail:
        base = "--host 100.105.163.59 --token-file /Users/x/t"
    v1 = spawn("/x/venv/bin/python -m realtime_subtitle.remote.server "
               f"{base} {tail.format(tmp=tmp_path)}")
    r = _run_stop_v1(e)
    assert r.returncode == 0, r.stderr
    assert "不记录 v1" in r.stdout, r.stdout
    assert v1.wait(timeout=5) is not None, "拒绝记录不影响停掉 v1"
    rec = _state_dir(home) / "v1.restart"
    assert rec.read_text().strip() == "unrecorded=1"

    r = _run_restore(e, home)
    assert "没能恢复 v1，请手动重新启动" in r.stdout, r.stdout
    assert "已恢复" not in r.stdout
    assert _nohup_log(logdir) == "", "回滚不许重拉"
    assert not (tmp_path / "PWNED").exists(), "注入的命令不许被执行"
    assert not rec.exists()


def test_v1_spaced_path_is_rejected_and_pwned_never_created(env, spawn, tmp_path):
    e, home, logdir = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    v1 = spawn("/x/python -m realtime_subtitle.remote.server --host 100.105.163.59 "
               "--token-file /Users/a b/token")
    r = _run_stop_v1(e)
    assert "不记录 v1" in r.stdout
    v1.wait(timeout=5)
    _run_restore(e, home)
    assert _nohup_log(logdir) == ""


def test_v1_record_hides_nothing_secret_and_accepts_ipv6_and_default_port(env, spawn, tmp_path):
    e, home, _l = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    v1 = spawn("/x/python -m realtime_subtitle.remote.server --host=fd7a:115c::1 "
               "--token-file=/Users/x/.config/rs-remote/token")
    r = _run_stop_v1(e)
    assert "已记录 v1" in r.stdout, r.stdout
    v1.wait(timeout=5)
    lines = (_state_dir(home) / "v1.restart").read_text().splitlines()
    assert "host=fd7a:115c::1" in lines and "port=" in lines


def test_restore_reports_failure_when_v1_does_not_come_back(env, spawn, tmp_path):
    e, home, logdir = env
    cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(cwd)
    e.pop("STUB_NOHUP_SPAWN", None)  # 桩 nohup 不会真起进程 = 重拉后进程没起来
    e["RS_V1_WAIT"] = "1"
    v1 = spawn(V1_CMD)
    assert "已记录 v1" in _run_stop_v1(e).stdout
    v1.wait(timeout=5)
    t0 = time.time()
    r = _run_restore(e, home)
    assert time.time() - t0 < 5, "轮询必须有界"
    assert "已恢复" not in r.stdout
    assert "v1 未能自动恢复，请手动启动：" in r.stdout
    manual = r.stdout.split("请手动启动：", 1)[1].strip()
    assert manual == (f"cd {cwd} && ./venv/bin/python -m realtime_subtitle.remote.server {V1_ARGS}")
    assert "ARGS=" in _nohup_log(logdir), "确实尝试过重拉"


def test_restore_without_venv_python_gives_manual_command(env, spawn, tmp_path):
    e, home, logdir = env
    cwd = tmp_path / "rs-remote"
    cwd.mkdir()
    e["STUB_V1_CWD"] = str(cwd)
    v1 = spawn(V1_CMD)
    assert "已记录 v1" in _run_stop_v1(e).stdout
    v1.wait(timeout=5)
    r = _run_restore(e, home)
    assert "未能自动恢复，请手动启动：" in r.stdout and "已恢复" not in r.stdout
    assert _nohup_log(logdir) == ""


def test_tampered_record_is_revalidated_on_restore(env, tmp_path):
    e, home, logdir = env
    cwd = _make_v1_cwd(tmp_path)
    rec = _state_dir(home) / "v1.restart"
    rec.parent.mkdir(parents=True)
    for body in [
        f"cwd={cwd}\nhost=100.105.163.59;id\nport=8791\ntoken_file=/x/t\n",
        f"cwd={cwd}\nhost=100.105.163.59\nport=8791\ntoken_file=/x/t y\n",
        f"cwd={cwd}\nhost=100.105.163.59\nport=8791\ntoken_file=/x/t\ncmd=touch PWNED\n",
        f"cwd={tmp_path}/nope\nhost=100.105.163.59\nport=8791\ntoken_file=/x/t\n",
    ]:
        rec.write_text(body)
        r = _run_restore(e, home)
        assert "记录无效或不完整" in r.stdout, (body, r.stdout)
    assert _nohup_log(logdir) == ""


def test_state_dir_symlink_refuses_record_and_restore(env, spawn, tmp_path):
    e, home, logdir = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    evil = tmp_path / "evil"
    evil.mkdir()
    sd = _state_dir(home)
    sd.parent.mkdir(parents=True)
    sd.symlink_to(evil)
    v1 = spawn(V1_CMD)
    r = _run_stop_v1(e)
    assert r.returncode == 0
    assert "符号链接" in r.stdout and "已记录 v1" not in r.stdout
    assert v1.wait(timeout=5) is not None, "拒绝记录不影响停 v1"
    assert list(evil.iterdir()) == [], "不许写进符号链接指向的目录"
    (evil / "v1.restart").write_text(f"cwd={tmp_path}\nhost=1.2.3.4\ntoken_file=/x\n")
    r = _run_restore(e, home)
    assert "拒绝读取" in r.stdout
    assert _nohup_log(logdir) == ""
    assert (evil / "v1.restart").exists(), "拒绝读取也不能顺手删目标里的文件"


def test_record_file_symlink_refused_on_write_and_read(env, spawn, tmp_path):
    e, home, logdir = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    sd = _state_dir(home)
    sd.mkdir(parents=True)
    target = tmp_path / "target.txt"
    target.write_text("keep\n")
    (sd / "v1.restart").symlink_to(target)
    v1 = spawn(V1_CMD)
    r = _run_stop_v1(e)
    assert "符号链接" in r.stdout and "已记录 v1" not in r.stdout
    v1.wait(timeout=5)
    assert target.read_text() == "keep\n"
    r = _run_restore(e, home)
    assert "拒绝读取" in r.stdout and _nohup_log(logdir) == ""


def test_v1_restore_never_goes_through_sh_c():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/bin/sh -c" not in text and "sh -c \"exec" not in text
    assert '"${v1_args[@]}"' in text


def test_mini2_real_v1_cmdline_is_found_stopped_recorded_and_relaunched(env, spawn, tmp_path):
    """C1：mini2 上真实的 v1 命令行带 -u，必须能被找到、停掉、记录，回滚时用固定入口重拉。"""
    e, home, logdir = env
    cwd = _make_v1_cwd(tmp_path)
    e["STUB_V1_CWD"] = str(cwd)
    e["STUB_NOHUP_SPAWN"] = "1"
    v1 = spawn(MINI2_REAL_V1)
    r = _run_stop_v1(e)
    assert "已记录 v1" in r.stdout and "已停止" in r.stdout, r.stdout
    assert v1.wait(timeout=5) is not None
    r = _run_restore(e, home)
    assert "已恢复 v1" in r.stdout, r.stdout
    assert _nohup_log(logdir).strip() == (
        f"PWD={cwd} ARGS={cwd}/venv/bin/python -m realtime_subtitle.remote.server "
        "--host 100.105.163.59 --port 8791 --token-file /Users/yilinwang/.config/rs-remote/token")


@pytest.mark.parametrize("cmdline", [
    'python -c "import x  # -m realtime_subtitle.remote.server"',
    "python -X dev -m realtime_subtitle.remote.server --host 100.105.163.59 --token-file /x/t",
    "python -u -c x -m realtime_subtitle.remote.server --host 100.105.163.59 --token-file /x/t",
])
def test_interpreter_options_with_values_are_not_matched(env, spawn, tmp_path, cmdline):
    """带值/-c 的解释器选项一律不匹配（选「不匹配」而非「匹配但拒绝记录」：
    那不是我们的 v1 启动形状，宁可不碰别人的进程）。"""
    e, home, _l = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    p = spawn(cmdline)
    r = _run_stop_v1(e)
    assert "未在运行" in r.stdout
    assert p.poll() is None


def test_decoy_real_v1_survives_stop_and_rollback(env, _real_v1_decoy, spawn, tmp_path):
    """C2：跑一遍 stop + 回滚重拉，与模式相符的无关进程（模拟 mini2 真 v1）必须毫发无损。"""
    e, home, _l = env
    e["STUB_V1_CWD"] = str(_make_v1_cwd(tmp_path))
    e["STUB_NOHUP_SPAWN"] = "1"
    mine = spawn(V1_CMD)
    assert "已记录 v1" in _run_stop_v1(e).stdout
    assert mine.wait(timeout=5) is not None
    assert "已恢复 v1" in _run_restore(e, home).stdout
    assert _real_v1_decoy.poll() is None


def test_ssh_passes_dashdash_before_host(env):
    e, _home, logdir = env
    host = "mini2"
    r = _run([host], e, timeout=300)
    assert r.returncode == 0, r.stderr
    raw = (logdir / "ssh.argv").read_bytes()
    calls = [c for c in raw.split(b"\n---\n") if c.strip()]
    assert len(calls) >= 1
    for call in calls:
        args = [part.decode("utf-8") for part in call.split(b"\0") if part]
        assert host in args, f"{host} not found in ssh call: {args}"
        idx = args.index(host)
        assert idx > 0
        assert args[idx - 1] == "--", f"argument before {host} is {args[idx - 1]}, expected '--'"
