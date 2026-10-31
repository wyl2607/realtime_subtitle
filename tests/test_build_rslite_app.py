import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

# build_rslite_app.sh 依赖 macOS 专用打包工具链（swift build / codesign / ditto），
# Windows 上 bash 是 WSL 存根，会直接失败（WinError 1920），所以只在 macOS 运行。
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="build_rslite_app.sh 只在 macOS 上用（打 rslite .app）",
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "macos" / "build_rslite_app.sh"
VERSION_FILE = REPO_ROOT / "realtime_subtitle" / "version.py"


def _bash(script: str, *args: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script, "bash", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _declared_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    assert match
    return match.group(1)


def test_build_script_parses_with_bash_n():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_read_version_reads_plain_version_py():
    result = _bash('source "$1"; read_version "$2"', SCRIPT, VERSION_FILE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _declared_version()


def test_app_min_macos_is_27_and_written_to_info_plist():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'APP_MIN_MACOS="27.0"' in text
    assert (
        'write_info_plist "${app_path}/Contents/Info.plist" "${version}" "${APP_MIN_MACOS}"' in text
    )


def test_self_check_runs_before_zipping():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--selftest" in text
    assert "SELFTEST OK" in text
    assert text.index("--selftest") < text.index("ditto -c -k --keepParent")


def test_generated_info_plist_contains_required_bundle_and_privacy_keys(tmp_path):
    plist_path = tmp_path / "Info.plist"
    result = _bash('source "$1"; write_info_plist "$2" "9.8.7" "26.0"', SCRIPT, plist_path)
    assert result.returncode == 0, result.stderr

    info = plistlib.loads(plist_path.read_bytes())
    assert info["CFBundleIdentifier"] == "com.realtimesubtitle.rslite"
    assert info["CFBundleExecutable"] == "rslite"
    assert info["CFBundleShortVersionString"] == "9.8.7"
    assert info["CFBundleVersion"] == "9.8.7"
    assert info["LSMinimumSystemVersion"] == "26.0"
    assert info["LSUIElement"] is True
    assert "麦克风" in info["NSMicrophoneUsageDescription"]
    assert "语音识别" in info["NSSpeechRecognitionUsageDescription"]
    assert "屏幕与系统音频录制" in info["NSScreenCaptureUsageDescription"]
    assert "系统播放声音" in info["NSAudioCaptureUsageDescription"]


def test_packaging_steps_are_ad_hoc_signed_and_zipped_with_parent():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'swift build -c release --package-path "${PACKAGE_DIR}" --product "${PRODUCT}"' in text
    assert 'codesign --force --deep --sign - "${app_path}"' in text
    assert 'ditto -c -k --keepParent "${APP_BUNDLE}" "${zip_name}"' in text
    assert 'shasum -a 256 "${zip_name}" > "${zip_name}.sha256"' in text
