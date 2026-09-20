# Repository layout

Runtime code lives in the **`realtime_subtitle/` package**, split by domain. The repo root
keeps only what has to be there: the two entrypoints, packaging/config files, and the
compatibility shims.

```text
realtime_subtitle/                  # clone root
├── main.py                         # thin entry → realtime_subtitle.app:main
├── download_subtitle.py            # offline: download → ASR → bilingual SRT → study guide
├── config_local.py                 # NOT in git; machine-specific overrides (installer writes it)
├── requirements.txt · requirements-dev.txt   # dependency lists (the only source of truth)
├── pyproject.toml                  # pytest + ruff config; deliberately has no [project] table
├── *.ps1                           # compat shims → scripts/windows/ (see "Root shims")
│
├── realtime_subtitle/              # the package
│   ├── app.py                      # SubtitleApp: wiring, hotkeys, mode switching
│   ├── config.py                   # all defaults (repo file — never edit per machine)
│   ├── version.py                  # single source of truth for the version (zero imports!)
│   ├── paths.py                    # single source of truth for runtime file locations
│   ├── language_policy.py          # single parse point for language pairs (zero Qt deps)
│   ├── offline.py                  # import layer (network) + processing layer (local only)
│   ├── deps_fingerprint.py         # "did requirements change?" + orphan detection
│   ├── instance_identity.py        # single-instance mutex identity
│   ├── migrate_legacy.py           # moves runtime files left by pre-package versions
│   ├── capture/audio_capture.py    # WASAPI loopback + device hot-swap
│   ├── asr/streaming_asr.py        # local-agreement streaming ASR (word-level commits)
│   ├── translate/
│   │   ├── translator_queue.py     # Whisper/Ollama owner: sentence split, queue, drafts
│   │   ├── lookup.py               # click-a-word lookup + AI analysis   (mixin)
│   │   ├── transcript.py           # daily archive + retention           (mixin)
│   │   └── runtime_stats.py        # per-minute performance summary      (mixin)
│   └── ui/
│       ├── subtitle_window.py      # the overlay (main class)
│       ├── window_frame.py         # dragging + WM_NCHITTEST native hit-test
│       ├── window_chrome.py        # button bar, hover behaviour
│       ├── window_geometry.py      # screen placement, clamping, HiDPI/DPR rescaling
│       ├── subtitle_render.py      # layout of sentence pairs, word hit-testing
│       ├── settings_window.py      # ⚙ panel
│       ├── popups.py               # history / word lookup / AI analysis popups
│       ├── tv_window.py            # 📺 fullscreen large-type window
│       └── cinema_bar.py           # 🎞 transparent caption strip
│
├── scripts/
│   ├── prune_venv.py               # remove orphan packages + pip leftover dirs
│   └── windows/                    # install / start / stop / pause / update / uninstall / download
├── tests/                          # pytest + three standalone GUI harnesses
└── docs/                           # see below
```

## Docs tree

```text
docs/
├── STRUCTURE.md                    # this file
├── TODO.md                         # open items, each with "how to call it done"
├── README-i18n.md                  # which README is the source of truth for what
├── zh/  WINDOWS-RUNBOOK.md · user-guide-template.txt · optimization-notes.md
├── en/  WINDOWS-RUNBOOK.md
├── de/  WINDOWS-RUNBOOK.md
└── design/
    ├── plans/                      # dated implementation plans (historical record)
    └── specs/                      # dated design specs (historical record)
```

`docs/zh/user-guide-template.txt` is the **single source of truth** for the
「操作说明.txt」 that `install.ps1` drops on the desktop. Edit the template, never the
generated file — the next install overwrites it.

Files under `docs/design/` are dated snapshots of what was decided *at that time*. They
are deliberately **not** kept current; present-day behaviour lives in `CLAUDE.md`.

## Layers

| Layer | Path | Purpose |
|-------|------|---------|
| **A – Runtime core** | `realtime_subtitle/` | Required to run the app |
| **B – Windows ops** | `scripts/` | Install and lifecycle |
| **C – Quality** | `tests/` | Pytest suite + GUI harnesses |
| **D – Docs** | `docs/`, `README*.md`, `CLAUDE.md` | Human + AI documentation |

## Imports

```python
from realtime_subtitle import config
from realtime_subtitle.capture import AudioCapture
from realtime_subtitle.translate import WhisperQueueTranslator
from realtime_subtitle.ui.subtitle_window import SubtitleWindow
```

☠️ Plain `import config` no longer works. When moving code, remember this project has
~30 **deliberate function-body imports** (to keep torch/Qt out of import time and to dodge
the single-instance mutex) — search indented `import`/`from` too, not just top-level ones.

## Root shims

`install.ps1`, `start_subtitles.ps1`, `stop_subtitles.ps1`, `pause_subtitles.ps1`,
`update_subtitles.ps1`, `uninstall.ps1` and `download_subtitle.ps1` at the repo root are
thin forwarders into `scripts/windows/`.

☠️ **Do not delete them.** Installs from before 2026-08 have desktop `.bat` files with the
root path baked in — including the updater itself. Removing the shims leaves those users
with every shortcut broken and no in-product way to recover.

## Entrypoints

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
venv\Scripts\python -u main.py
venv\Scripts\python download_subtitle.py "https://www.youtube.com/watch?v=..."
```

Runtime artifacts (all gitignored) land at the repo root via `realtime_subtitle/paths.py`:
`config_local.py`, `window_state.json`, `transcripts/`, `downloads/`, `subtitle.log`,
`subtitle.err.log`, `logs/`.

## See also

- Windows operators: [中文](zh/WINDOWS-RUNBOOK.md) · [English](en/WINDOWS-RUNBOOK.md) · [Deutsch](de/WINDOWS-RUNBOOK.md)
- README language policy: [README-i18n.md](README-i18n.md)
- Hardware tiers, tuning, and every pitfall this project has hit: [CLAUDE.md](../CLAUDE.md)
