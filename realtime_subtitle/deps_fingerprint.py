"""Requirements fingerprint for retryable dependency updates.

The update script stores the last *successful* install fingerprint in
``venv/.deps_fingerprint``. A failed pip must not write that file, so the
next run retries even when git HEAD did not change.
"""
from __future__ import annotations

import hashlib
import re

_NVIDIA_LINE = re.compile(r"^\s*nvidia-", re.I)


def filter_requirements_for_tier(text: str, tier: str) -> str:
    """CPU installs skip nvidia-* wheels, matching install.ps1."""
    if (tier or "").lower() != "cpu":
        return text
    return "".join(
        line for line in text.splitlines(True)
        if not _NVIDIA_LINE.match(line)
    )


def fingerprint_requirements(text: str, tier: str) -> str:
    body = filter_requirements_for_tier(text, tier)
    payload = f"{(tier or '').lower()}\n{body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def should_reinstall_deps(stored_fingerprint: str | None, current_fingerprint: str) -> bool:
    return (stored_fingerprint or "") != (current_fingerprint or "")


# ☠️ 指纹只认 requirements.txt 的**文本**，而 `pip install -r` 从不卸载东西。
# 于是从 requirements.txt 里删掉一行之后，那个包（以及它拖来的一串传递依赖）
# 会在所有已经装好的机器上永远留着：本机 2026-09-20 实测 venv 4.0GB，其中
# 33 个包不在 requirements 的依赖闭包里——librosa 是 faster-whisper ≤1.0
# 时代的依赖，transformers/optimum 是某次实验的残留。它们不会被 import，
# 但会让 `pip list` 和排障时的判断持续跑偏。
# 下面这组函数给 scripts/prune_venv.py 用，算"装了但闭包里没有"的那部分。
_PROTECTED = frozenset({"pip", "setuptools", "wheel", "uv"})


def canonical_name(name: str) -> str:
    """PEP 503 规范化：PyQt6_sip / pyqt6-sip / PyQt6.Sip 是同一个包。"""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def orphan_packages(installed, required, protected=_PROTECTED) -> list[str]:
    """装了但不在依赖闭包里的包（返回原样包名，按规范名排序）。

    protected 里的是 venv 自身的管道（pip/setuptools/...），它们不会出现在任何
    requirements 闭包里，但删了 venv 就废了。
    """
    keep = {canonical_name(n) for n in required}
    keep |= {canonical_name(n) for n in protected}
    seen: set[str] = set()
    out: list[str] = []
    for name in installed:
        c = canonical_name(name)
        if not c or c in keep or c in seen:
            continue
        seen.add(c)
        out.append(name)
    return sorted(out, key=canonical_name)


def update_status(
    old_commit: str,
    new_commit: str,
    stored_fingerprint: str | None,
    current_fingerprint: str,
) -> str:
    """Distinguish 'code is latest' from 'deps still need a retry'."""
    deps = should_reinstall_deps(stored_fingerprint, current_fingerprint)
    code = old_commit != new_commit
    if not code and not deps:
        return "already_latest"
    if not code and deps:
        return "deps_retry"
    if code and not deps:
        return "code_only"
    return "code_and_deps"
