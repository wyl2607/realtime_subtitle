# Repository layout

Graded layout (2026-08). Runtime code lives in the **`realtime_subtitle/` package** by domain. Root keeps the Windows entrypoints (`main.py` and `download_subtitle.py`) plus personal/runtime artifacts.

```text
realtime_subtitle/                 # install root (clone path)
├── main.py                        # thin entry → realtime_subtitle.app:main
├── download_subtitle.py           # batch download/subtitle entrypoint
├── realtime_subtitle/             # Python package
│   ├── config.py, version.py
│   ├── app.py                     # SubtitleApp orchestration
│   ├── offline.py                  # download → ASR → bilingual SRT → study guide
│   ├── capture/audio_capture.py   # WASAPI loopback
│   ├── asr/streaming_asr.py       # local-agreement streaming ASR
│   ├── translate/translator_queue.py
│   └── ui/                        # overlay, settings, popups, TV, chrome
├── scripts/windows/               # install / start / stop / pause / update / uninstall / download
├── tests/
├── docs/
├── README.md · README.en.md · README.de.md · README.zh.md
└── CLAUDE.md
```

## Grades

| Grade | Path | Purpose |
|-------|------|---------|
| **A – Runtime core** | `realtime_subtitle/` package | Required to run the app |
| **B – Windows ops** | `scripts/windows/` | Install and lifecycle |
| **C – Quality** | `tests/` | Pytest + GUI harnesses |
| **D – Docs** | `docs/`, `README*.md` | Human + AI documentation |

## Imports

```python
from realtime_subtitle import config
from realtime_subtitle.capture import AudioCapture
from realtime_subtitle.translate import WhisperQueueTranslator
from realtime_subtitle.ui.subtitle_window import SubtitleWindow
```

Personal overrides stay at **repo root**: `config_local.py` (loaded by `realtime_subtitle.config`).

## Script entrypoints

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# root shim still works:
powershell -ExecutionPolicy Bypass -File install.ps1
venv\Scripts\python -u main.py
# batch video subtitles:
venv\Scripts\python download_subtitle.py "https://www.youtube.com/watch?v=..."
```

The batch pipeline writes private media and subtitle work under `downloads/<video-id>/`.
It downloads up to 2160p, uses the project’s Faster-Whisper bootstrap, and translates
each segment with local Ollama. Chinese source language targets German; all other source
languages target Chinese. The bilingual SRT always places source text before the translation.

## Multi-language READMEs

See [README-i18n.md](README-i18n.md). GitHub landing (`README.md`) is Chinese; English is [`README.en.md`](../README.en.md).

## Windows operators

See [WINDOWS-RUNBOOK.md](WINDOWS-RUNBOOK.md).
