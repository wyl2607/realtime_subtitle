"""F03：依赖更新必须能在提交不变时重试失败的 pip。"""
import hashlib

from realtime_subtitle.deps_fingerprint import (
    canonical_name,
    filter_requirements_for_tier,
    fingerprint_requirements,
    orphan_packages,
    should_reinstall_deps,
    update_status,
)


_REQ = "faster-whisper>=1.0\nnvidia-cublas-cu12>=1.0\nnumpy>=2.2\n"


def test_cpu_tier_strips_nvidia_packages():
    filtered = filter_requirements_for_tier(_REQ, "cpu")
    assert "nvidia-cublas-cu12" not in filtered
    assert "faster-whisper" in filtered
    assert "numpy" in filtered
    assert filter_requirements_for_tier(_REQ, "gpu") == _REQ


def test_fingerprint_includes_tier_so_cpu_gpu_do_not_collide():
    cpu = fingerprint_requirements(_REQ, "cpu")
    gpu = fingerprint_requirements(_REQ, "gpu")
    assert cpu != gpu
    assert cpu == fingerprint_requirements(_REQ, "cpu")
    payload = ("cpu\n" + filter_requirements_for_tier(_REQ, "cpu")).encode("utf-8")
    assert cpu == hashlib.sha256(payload).hexdigest()


def test_missing_fingerprint_requires_one_install_even_if_commit_unchanged():
    current = fingerprint_requirements(_REQ, "gpu")
    assert should_reinstall_deps(stored_fingerprint=None, current_fingerprint=current)
    assert should_reinstall_deps(stored_fingerprint="", current_fingerprint=current)
    assert not should_reinstall_deps(stored_fingerprint=current, current_fingerprint=current)


def test_failed_install_does_not_store_fingerprint():
    """安装成功才写标记；失败保留待修复状态。"""
    current = fingerprint_requirements(_REQ, "gpu")
    stored = None  # pip 失败，没写文件
    assert should_reinstall_deps(stored, current)
    stored_after_success = current
    assert not should_reinstall_deps(stored_after_success, current)


def test_update_status_distinguishes_code_and_deps():
    fp = fingerprint_requirements(_REQ, "cpu")
    assert update_status(old_commit="aaa", new_commit="aaa",
                         stored_fingerprint=fp, current_fingerprint=fp) == "already_latest"
    assert update_status(old_commit="aaa", new_commit="aaa",
                         stored_fingerprint=None, current_fingerprint=fp) == "deps_retry"
    assert update_status(old_commit="aaa", new_commit="bbb",
                         stored_fingerprint=fp, current_fingerprint=fp) == "code_only"
    assert update_status(old_commit="aaa", new_commit="bbb",
                         stored_fingerprint=None, current_fingerprint=fp) == "code_and_deps"


def test_update_script_retries_deps_when_commit_unchanged():
    """静态：更新脚本不能在 old==new 时无条件成功退出。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root
            / "scripts" / "windows" / "update_subtitles.ps1").read_text(encoding="utf-8-sig")
    helper = (root
              / "scripts" / "windows" / "_update_deps.ps1").read_text(encoding="utf-8-sig")
    assert "deps_fingerprint" in text or "Get-CurrentDepsFingerprint" in text
    assert "needDeps" in text
    assert "代码和依赖都无需更新" in text
    assert "即使提交不变也会重试依赖" in text
    assert "nvidia-" in text  # 延续 CPU 过滤
    assert "CUDA" in helper and "12.0" in helper  # 与 install.ps1 的 CPU 降级条件一致


# --- 多余依赖的识别（scripts/prune_venv.py 用） ---------------------------
# 指纹只认 requirements 的文本，`pip install -r` 又从不卸载东西，所以"某一行被
# 删掉"这件事在已装好的机器上是**无声**的：包留着、传递依赖也留着。

def test_canonical_name_follows_pep503():
    assert canonical_name("PyQt6_sip") == canonical_name("pyqt6-sip")
    assert canonical_name("PyQt6.Sip") == "pyqt6-sip"
    assert canonical_name("  Faster-Whisper  ") == "faster-whisper"
    assert canonical_name("") == ""


def test_orphan_packages_lists_only_what_closure_does_not_need():
    installed = ["faster-whisper", "numpy", "librosa", "numba", "transformers"]
    required = ["faster-whisper", "numpy"]
    assert orphan_packages(installed, required) == ["librosa", "numba", "transformers"]


def test_orphan_packages_matches_names_case_insensitively():
    """装的是 PyQt6_sip、清单里写的是 pyqt6-sip，不能因此被当成孤儿删掉。"""
    assert orphan_packages(["PyQt6_sip"], ["pyqt6-sip"]) == []


def test_orphan_packages_never_touches_venv_plumbing():
    """pip/setuptools 不会出现在任何 requirements 闭包里，删了 venv 就废了。"""
    assert orphan_packages(["pip", "setuptools", "wheel"], []) == []


def test_orphan_packages_dedupes_and_sorts():
    got = orphan_packages(["zeta", "Alpha", "alpha", "beta"], [])
    assert got == ["Alpha", "beta", "zeta"]
