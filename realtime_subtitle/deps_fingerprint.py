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
