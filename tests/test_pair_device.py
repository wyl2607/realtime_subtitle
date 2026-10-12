"""scripts/node/pair_device.sh 的控制端测试。

不连真实 ssh。PATH 里放桩 ssh / curl / openssl / qrencode / tailscale / launchctl。
桩 ssh 把 argv 和 stdin 记下来：Mac 命令在本机 bash 里执行（HOME 换成临时目录），
Windows 命令交给桩解释器，按 EncodedCommand 里的 RsOp 模拟 tokens 目录。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "node" / "pair_device.sh"
GATEWAY = REPO_ROOT / "realtime_subtitle" / "node" / "gateway.py"
INSTALL = REPO_ROOT / "scripts" / "node" / "install_node.sh"

NODE_ID = "0123456789abcdef"
DNS = "mini2.tail1234.ts.net"
TOKEN = "ab" * 32
OTHER = "cd" * 32
PORT = 8791

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="依赖 bash，仅在类 Unix 上运行")

_STUB_SSH = r"""#!/bin/bash
set -u
log="$STUB_LOG_DIR"
mkdir -p "$log"
printf '%s\0' "$@" >> "$log/ssh.argv"
printf '\n---\n' >> "$log/ssh.argv"
stdin_tmp="$(mktemp)"
cat > "$stdin_tmp"
cat "$stdin_tmp" >> "$log/ssh.stdin"
printf '\n---\n' >> "$log/ssh.stdin"
cmd=""
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  if [ "${args[$i]}" = "--" ]; then
    cmd="${args[$((i+2))]}"
    break
  fi
  i=$((i+1))
done
if [ -z "${cmd}" ]; then
  echo "stub ssh: 缺少命令" >&2
  rm -f "$stdin_tmp"
  exit 97
fi
if [ "$cmd" = "uname -s" ]; then
  rm -f "$stdin_tmp"
  if [ "${STUB_OS:-Darwin}" = "Darwin" ]; then
    printf '%s\n' Darwin
    exit 0
  fi
  echo "uname: not found" >&2
  exit 1
fi
if [ "${STUB_OS:-Darwin}" != "Darwin" ]; then
  "$STUB_PYTHON" "$STUB_WIN_SIM" "$cmd" < "$stdin_tmp"
  rc=$?
  rm -f "$stdin_tmp"
  exit "$rc"
fi
HOME="$STUB_HOME" bash -c "$cmd" < "$stdin_tmp"
rc=$?
rm -f "$stdin_tmp"
exit "$rc"
"""

_STUB_WIN = r'''
import base64, os, re, sys
from pathlib import Path

cmd = sys.argv[1]
data = sys.stdin.buffer.read()
root = Path(os.environ["STUB_WIN"])
tokens = root / "tokens"
tokens.mkdir(parents=True, exist_ok=True)
log = Path(os.environ["STUB_LOG_DIR"])

def finish(code):
    raise SystemExit(code)

if "tailscale" in cmd and "ip" in cmd and "-EncodedCommand" not in cmd:
    sys.stdout.write(os.environ.get("STUB_TS_IP", "100.64.0.7") + "\n")
    finish(0)

m = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", cmd)
if not m:
    sys.stderr.write("win sim: unrecognized\n")
    finish(97)
script = base64.b64decode(m.group(1)).decode("utf-16le")
with (log / "win.ps1").open("a", encoding="utf-8") as fh:
    fh.write(script)
    fh.write("\n# -----\n")
op = re.search(r"\$RsOp = '([a-z]+)'", script).group(1)
dev = re.search(r"\$RsDevice = '([^']*)'", script)
device = dev.group(1) if dev else ""
rotate = "1" if "$RsRotate = '1'" in script else "0"

def path():
    return tokens / (device + ".token")

if op == "list":
    names = sorted(p.stem for p in tokens.glob("*.token") if p.is_file())
    sys.stdout.write("".join(n + "\n" for n in names))
    finish(0)
if op == "exists":
    finish(0 if path().is_file() else 1)
if op == "read":
    if not path().is_file():
        sys.stderr.write("missing\n")
        finish(1)
    sys.stdout.buffer.write(path().read_bytes())
    finish(0)
if op == "delete":
    if path().is_file():
        path().unlink()
    finish(0)
if op == "write":
    tok = data.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", tok):
        sys.stderr.write("token shape\n")
        finish(1)
    if path().exists() and rotate != "1":
        sys.stderr.write("exists\n")
        finish(2)
    path().write_text(tok, encoding="ascii")
    finish(0)
sys.stderr.write("bad op\n")
finish(97)
'''

_STUB_CURL = r'''
import json, os, sys
from pathlib import Path

log = Path(os.environ["STUB_LOG_DIR"])
argv = sys.argv[1:]
with (log / "curl.argv").open("ab") as fh:
    fh.write(b"\0".join(a.encode() for a in argv) + b"\0\n---\n")
cfg = None
i = 0
while i < len(argv):
    if argv[i] == "--config" and i + 1 < len(argv):
        cfg = argv[i + 1]
        i += 2
    else:
        i += 1
if not cfg:
    sys.stderr.write("stub curl: no config\n")
    raise SystemExit(2)
text = Path(cfg).read_text(encoding="utf-8")
with (log / "curl.config").open("a", encoding="utf-8") as fh:
    fh.write(text)
    fh.write("\n# -----\n")

def field(name):
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "="):
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    raise SystemExit("missing " + name)

out = field("output")
code = os.environ.get("STUB_HTTP_CODE", "200")
nid = os.environ.get("STUB_NODE_ID", "")
body = "" if code != "200" else json.dumps({"v": 2, "node_id": nid})
Path(out).write_text(body, encoding="utf-8")
sys.stdout.write(code)
'''


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    logdir = tmp_path / "log"
    logdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    win = tmp_path / "win"
    win.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    win_sim = tmp_path / "win_sim.py"
    win_sim.write_text(_STUB_WIN, encoding="utf-8")
    curl_py = tmp_path / "curl.py"
    curl_py.write_text(_STUB_CURL, encoding="utf-8")
    _write_exec(bindir / "ssh", _STUB_SSH)
    _write_exec(
        bindir / "openssl",
        "#!/bin/bash\n"
        'printf "%s\\0" "$@" >> "$STUB_LOG_DIR/openssl.argv"\n'
        "printf '\\n---\\n' >> \"$STUB_LOG_DIR/openssl.argv\"\n"
        'if [ "${1:-}" = "rand" ] && [ "${2:-}" = "-hex" ] && [ "${3:-}" = "32" ]; then\n'
        '  printf "%s\\n" "${STUB_TOKEN:-' + TOKEN + '}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "stub openssl" >&2\n'
        "exit 1\n",
    )
    _write_exec(
        bindir / "qrencode",
        "#!/bin/bash\n"
        'printf "%s\\0" "$@" >> "$STUB_LOG_DIR/qr.argv"\n'
        "printf '\\n---\\n' >> \"$STUB_LOG_DIR/qr.argv\"\n"
        'cat >> "$STUB_LOG_DIR/qr.stdin"\n'
        "printf 'QRCODE\\n'\n",
    )
    _write_exec(
        bindir / "tailscale",
        "#!/bin/bash\n"
        'if [ "${1:-}" = "ip" ] && [ "${2:-}" = "-4" ]; then\n'
        '  printf "%s\\n" "${STUB_TS_IP:-100.64.0.7}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_exec(
        bindir / "launchctl",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$STUB_LOG_DIR/launchctl.log"\n'
        "exit 0\n",
    )
    _write_exec(
        bindir / "curl",
        "#!/bin/bash\n"
        'exec "$STUB_PYTHON" "$STUB_CURL_PY" "$@"\n',
    )
    py_dir = str(Path(sys.executable).resolve().parent)
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "PATH": f"{bindir}:{py_dir}:/usr/bin:/bin",
        "STUB_LOG_DIR": str(logdir),
        "STUB_HOME": str(remote),
        "STUB_WIN": str(win),
        "STUB_PYTHON": sys.executable,
        "STUB_WIN_SIM": str(win_sim),
        "STUB_CURL_PY": str(curl_py),
        "STUB_OS": "Darwin",
        "STUB_NODE_ID": NODE_ID,
        "STUB_HTTP_CODE": "200",
        "STUB_TOKEN": TOKEN,
        "STUB_TS_IP": "100.64.0.7",
        "RS_INFO_WAIT": "0",
        "RS_INFO_SLEEP": "0",
    })
    yield {
        "env": e,
        "home": home,
        "log": logdir,
        "remote": remote,
        "win": win,
        "scratch": scratch,
        "bin": bindir,
    }


def _nodes(home: Path, port: int = PORT) -> None:
    folder = home / ".config" / "rslite"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "nodes.json").write_text(json.dumps([
        {"id": "other", "node_id": "z" * 16, "url": "ws://other.tail.ts.net:8791", "token_file": "/x"},
        {"id": "mini2", "node_id": NODE_ID, "url": f"ws://{DNS}:{port}", "token_file": "/nope"},
    ], ensure_ascii=False), encoding="utf-8")


def _run(box, args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=box["env"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _link(name="phone", port=PORT, token=TOKEN) -> str:
    return f"rsnode://pair?v=1&id={NODE_ID}&host={DNS}&port={port}&tok={token}&name={name}"


def _decoded_ps(argv: bytes) -> list[str]:
    text = argv.decode("latin1")
    out = []
    for match in re.finditer(r"-EncodedCommand[\0 ]+([A-Za-z0-9+/=]+)", text):
        out.append(base64.b64decode(match.group(1)).decode("utf-16le"))
    return out


def _mac_token(box, name="phone") -> Path:
    return box["remote"] / ".config" / "rs-node" / "tokens" / f"{name}.token"


def _win_token(box, name="phone") -> Path:
    return box["win"] / "tokens" / f"{name}.token"


def test_bash_syntax_ok():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="没装 shellcheck")
def test_shellcheck_clean():
    shellcheck = "shellcheck"
    result = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_bash4_only_features():
    text = SCRIPT.read_text(encoding="utf-8")
    for pat in [r"declare\s+-A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+(,,|\^\^)\}",
                r"\[\[\s+-v\s", r"&>>", r"\|&"]:
        assert not re.search(pat, text), pat


def test_device_and_host_patterns_match_sources():
    script = SCRIPT.read_text(encoding="utf-8")
    gateway = GATEWAY.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    assert '_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")' in gateway
    assert "^[A-Za-z0-9_.-]{1,64}$" in script
    assert "^[A-Za-z0-9._@-]+$" in script
    assert "^[A-Za-z0-9._@-]+$" in install


def test_mac_pair_prints_link_and_qr(env):
    _nodes(env["home"])
    result = _run(env, ["user@mini2", "phone"])
    assert result.returncode == 0, result.stderr
    link = _link()
    assert link in result.stdout.splitlines()
    assert "QRCODE" in result.stdout
    assert (env["log"] / "qr.stdin").read_text(encoding="utf-8") == link
    qr_argv = (env["log"] / "qr.argv").read_bytes()
    assert b"ANSIUTF8" in qr_argv
    assert TOKEN.encode() not in qr_argv
    path = _mac_token(env)
    assert path.read_text(encoding="utf-8") == TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert "HUP" in (env["log"] / "launchctl.log").read_text(encoding="utf-8")
    assert "com.realtimesubtitle.node" in (env["log"] / "launchctl.log").read_text(encoding="utf-8")
    cfg = (env["log"] / "curl.config").read_text(encoding="utf-8")
    assert f'url = "http://100.64.0.7:{PORT}/v1/info"' in cfg
    assert f"Authorization: Bearer {TOKEN}" in cfg
    assert "noproxy" in cfg
    assert DNS not in cfg
    argv = (env["log"] / "ssh.argv").read_bytes()
    assert TOKEN.encode() not in argv
    assert TOKEN.encode() not in (env["log"] / "curl.argv").read_bytes()
    assert TOKEN.encode() in (env["log"] / "ssh.stdin").read_bytes()
    assert b"\0--\0user@mini2\0" in argv
    assert TOKEN not in result.stderr
    assert "<redacted>" in result.stderr
    assert list(env["scratch"].iterdir()) == []


def test_windows_pair_uses_localappdata_and_icacls(env):
    env["env"]["STUB_OS"] = "Windows_NT"
    _nodes(env["home"])
    result = _run(env, ["mini2", "phone"])
    assert result.returncode == 0, result.stderr
    assert _link() in result.stdout.splitlines()
    assert _win_token(env).read_text(encoding="utf-8") == TOKEN
    argv = (env["log"] / "ssh.argv").read_bytes()
    scripts = _decoded_ps(argv)
    write_ps = [s for s in scripts if "$RsOp = 'write'" in s]
    assert len(write_ps) == 1
    text = write_ps[0]
    assert "LOCALAPPDATA" in text
    assert "rs-node\\tokens" in text
    assert "icacls" in text
    assert "inheritance:r" in text
    assert "$RsDevice = 'phone'" in text
    assert TOKEN not in text
    assert TOKEN.encode() not in argv
    assert TOKEN not in result.stderr
    assert "<redacted>" in result.stderr
    assert not (env["log"] / "launchctl.log").exists()


def test_duplicate_device_rejected(env):
    _nodes(env["home"])
    path = _mac_token(env)
    path.parent.mkdir(parents=True)
    path.write_text(OTHER, encoding="utf-8")
    result = _run(env, ["mini2", "phone"])
    assert result.returncode != 0
    assert "已存在" in result.stderr
    assert "--rotate" in result.stderr
    assert path.read_text(encoding="utf-8") == OTHER
    assert not (env["log"] / "openssl.argv").exists()
    assert "rsnode://" not in result.stdout
    assert TOKEN not in result.stderr
    assert OTHER not in result.stderr


def test_windows_duplicate_rejected(env):
    env["env"]["STUB_OS"] = "Windows_NT"
    _nodes(env["home"])
    path = _win_token(env)
    path.parent.mkdir(parents=True)
    path.write_text(OTHER, encoding="utf-8")
    result = _run(env, ["mini2", "phone"])
    assert result.returncode != 0, result.stderr
    assert "已存在" in result.stderr
    assert path.read_text(encoding="utf-8") == OTHER
    assert not (env["log"] / "openssl.argv").exists()


def test_rotate_replaces_token(env):
    _nodes(env["home"])
    path = _mac_token(env)
    path.parent.mkdir(parents=True)
    path.write_text(OTHER, encoding="utf-8")
    path.chmod(0o600)
    result = _run(env, ["--rotate", "mini2", "phone"])
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == TOKEN
    assert _link() in result.stdout
    assert OTHER not in result.stdout
    assert TOKEN not in result.stderr
    assert OTHER not in result.stderr


def test_info_node_id_mismatch_deletes_token(env):
    env["env"]["STUB_NODE_ID"] = "ffffffffffffffff"
    _nodes(env["home"])
    result = _run(env, ["mini2", "phone"])
    assert result.returncode != 0
    assert "node_id" in result.stderr
    assert not _mac_token(env).exists()
    assert "rsnode://" not in result.stdout
    assert TOKEN not in result.stderr
    assert TOKEN.encode() not in (env["log"] / "ssh.argv").read_bytes()


def test_revoke_then_401(env):
    env["env"]["STUB_HTTP_CODE"] = "401"
    _nodes(env["home"])
    path = _mac_token(env)
    path.parent.mkdir(parents=True)
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    result = _run(env, ["--revoke", "phone", "mini2"])
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert "401" in result.stderr
    cfg = (env["log"] / "curl.config").read_text(encoding="utf-8")
    assert f"Authorization: Bearer {TOKEN}" in cfg
    assert f"http://100.64.0.7:{PORT}/v1/info" in cfg
    assert TOKEN not in result.stderr
    assert TOKEN not in result.stdout
    assert TOKEN.encode() not in (env["log"] / "ssh.argv").read_bytes()
    assert "<redacted>" in result.stderr


def test_revoke_non_401_restores_and_fails(env):
    env["env"]["STUB_HTTP_CODE"] = "200"
    _nodes(env["home"])
    path = _mac_token(env)
    path.parent.mkdir(parents=True)
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    result = _run(env, ["mini2", "--revoke", "phone"])
    assert result.returncode != 0
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == TOKEN
    assert "401" in result.stderr
    assert TOKEN not in result.stderr
    assert TOKEN.encode() not in (env["log"] / "ssh.argv").read_bytes()


def test_no_qrencode_prints_link_and_hint(env):
    (env["bin"] / "qrencode").unlink()
    _nodes(env["home"])
    result = _run(env, ["mini2", "phone"])
    assert result.returncode == 0, result.stderr
    assert _link() in result.stdout.splitlines()
    assert "brew install qrencode" in result.stdout.splitlines()
    assert "QRCODE" not in result.stdout
    assert TOKEN not in result.stderr


def test_token_not_in_ssh_argv_or_stderr(env):
    _nodes(env["home"])
    result = _run(env, ["mini2", "phone"])
    assert result.returncode == 0, result.stderr
    blob = (env["log"] / "ssh.argv").read_bytes()
    assert TOKEN.encode() not in blob
    assert TOKEN.encode() not in (env["log"] / "curl.argv").read_bytes()
    assert TOKEN.encode() not in (env["log"] / "qr.argv").read_bytes()
    assert TOKEN.encode() not in (env["log"] / "openssl.argv").read_bytes()
    assert TOKEN not in result.stderr
    assert "<redacted>" in result.stderr
    scripts = _decoded_ps(blob)
    assert scripts == []


@pytest.mark.parametrize("name", ["", "a b", "a/b", "phone;rm", "手机", "a" * 65, "foo:bar", "bad*name", "has\nline"])
def test_bad_device_name_rejected(env, name):
    _nodes(env["home"])
    args = ["mini2"] if name == "" else ["mini2", name]
    result = _run(env, args)
    assert result.returncode != 0
    assert not (env["log"] / "ssh.argv").exists()
    assert "rsnode://" not in result.stdout


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/pwned", "a b", "mini2;id", "mini2$(id)", "mini2`id`", ""])
def test_bad_host_rejected(env, host):
    _nodes(env["home"])
    args = ["--dry-run", "phone"] if host == "" else ["--dry-run", host, "phone"]
    result = _run(env, args)
    assert result.returncode != 0
    assert not (env["log"] / "ssh.argv").exists()


def test_dry_run_does_not_ssh(env):
    _nodes(env["home"])
    result = _run(env, ["--dry-run", "--rotate", "mini2", "phone"])
    assert result.returncode == 0, result.stderr
    assert "<redacted>" in result.stdout
    assert "ANSIUTF8" in result.stdout
    assert "0600" in result.stdout
    assert "icacls" in result.stdout
    assert not (env["log"] / "ssh.argv").exists()
    assert not (env["log"] / "openssl.argv").exists()
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


def test_list_names_only(env):
    _nodes(env["home"])
    for name, secret in (("phone", TOKEN), ("pad", OTHER)):
        path = _mac_token(env, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        path.chmod(0o600)
    result = _run(env, ["--list", "mini2"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["pad", "phone"]
    assert TOKEN not in result.stdout
    assert OTHER not in result.stdout
    assert TOKEN not in result.stderr
    assert OTHER not in result.stderr
    argv = (env["log"] / "ssh.argv").read_bytes()
    assert TOKEN.encode() not in argv
    assert OTHER.encode() not in argv
    assert b"ReadAll" not in argv
    assert b"Get-Content" not in argv


def test_windows_list_names_only(env):
    env["env"]["STUB_OS"] = "Windows_NT"
    for name, secret in (("phone", TOKEN), ("pad", OTHER)):
        path = _win_token(env, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
    result = _run(env, ["win-desktop", "--list"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["pad", "phone"]
    scripts = _decoded_ps((env["log"] / "ssh.argv").read_bytes())
    listed = [s for s in scripts if "$RsOp = 'list'" in s]
    assert len(listed) == 1
    assert "Get-Content" not in listed[0]
    assert "ReadAll" not in listed[0]
    assert "BaseName" in listed[0]
    assert TOKEN not in result.stdout + result.stderr
    assert OTHER not in result.stdout + result.stderr


def test_non_tailnet_ip_refuses_before_write(env):
    env["env"]["STUB_TS_IP"] = "8.8.8.8"
    _nodes(env["home"])
    result = _run(env, ["mini2", "phone"])
    assert result.returncode != 0
    assert "100.64.0.0/10" in result.stderr
    assert not _mac_token(env).exists()
    assert not (env["log"] / "openssl.argv").exists()
    assert not (env["log"] / "curl.argv").exists()
    assert TOKEN not in result.stderr


def test_help_lists_actions():
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0
    assert "--revoke" in result.stdout
    assert "--list" in result.stdout
    assert "--rotate" in result.stdout
