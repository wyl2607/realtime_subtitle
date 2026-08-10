# Repository layout

Graded layout (2026-08). Runtime Python modules stay at the **repository root** so the Windows install path, `main.py` entrypoint, and import graph stay simple (`import config`, `from audio_capture import …`). Ops scripts and tests are separated by responsibility.

```text
realtime_subtitle/
├── main.py                 # Application entry (keep at root)
├── config.py, version.py   # Config + version single source of truth
├── audio_capture.py        # WASAPI loopback capture
├── streaming_asr.py        # Local-agreement streaming ASR
├── translator_queue.py     # Whisper + Ollama pipeline
├── subtitle_*.py, window_*, tv_window.py, popups.py, settings_window.py
├── install.ps1             # Thin shim → scripts/windows/install.ps1
├── scripts/
│   └── windows/            # Install / start / stop / pause / update / uninstall
├── tests/                  # Pytest + standalone GUI script suites
├── docs/
│   ├── STRUCTURE.md        # This file
│   ├── zh/                 # Chinese notes & user templates
│   ├── design/             # Design specs / plans
│   ├── en/                 # English extra docs (optional)
│   └── de/                 # German extra docs (optional)
├── README.md               # English (primary public README)
├── README.de.md            # German
├── README.zh.md            # Chinese
└── CLAUDE.md               # AI install / ops playbook
```

## Grades

| Grade | Path | Purpose |
|-------|------|---------|
| **A – Runtime core** | Root `*.py` | Required to run the subtitle app |
| **B – Windows ops** | `scripts/windows/` | Install, update, lifecycle on Windows |
| **C – Quality** | `tests/` | Unit tests + manual GUI harnesses |
| **D – Docs** | `docs/`, `README*.md` | Human + AI documentation |

## Why core stays at root

Moving Python modules into `src/` would force a large import rewrite and break:

- desktop shortcuts that run `python main.py` from the install directory
- `install.ps1` import smoke tests
- the critical **torch-before-PyQt5** import order documented in `main.py`

A future package layout is tracked as a separate issue.

## Script entrypoints

```powershell
# Preferred (after clone)
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1

# Still works (root shim)
powershell -ExecutionPolicy Bypass -File install.ps1
```

Inside `scripts/windows/*.ps1`, `$RepoRoot` is the repository root (`Parent.Parent` of the script directory).

## Multi-language READMEs

See [README-i18n.md](README-i18n.md) (EN / DE / ZH sync policy).
