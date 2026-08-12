# Realtime Subtitle

**Offline German (and English) live subtitles for Windows** — capture system audio, recognize speech, translate locally, and show a bilingual always-on-top overlay.

No audio or transcript is sent to the cloud. Recognition and translation run on your machine.

> **This is enforced, not just documented.** At startup the app resolves
> `OLLAMA_BASE_URL` and refuses to run unless every resolved address is a loopback
> address — otherwise one typo in `config_local.py` would silently ship your transcripts
> off the machine while subtitles kept working normally. To use an Ollama on another
> machine, set `ALLOW_REMOTE_OLLAMA = True` in `config_local.py`.

> **One exception, and only if you click it:** the `🌐 Ask a stronger AI` button in the
> popups opens your system browser with a question built from the last few minutes of
> recognized source text (≤300 chars), sent to a web AI (grok.com by default,
> `config.AI_ANALYSIS_WEB_URL_TEMPLATE`). Nothing leaves the machine unless you press it.
> Set that template to an empty string to remove the button entirely. Note this app
> captures **all system audio**, which may include voice calls.

[Deutsch](README.de.md) · [中文](README.zh.md) · [Repository layout](docs/STRUCTURE.md) · [Windows runbook](docs/WINDOWS-RUNBOOK.md)

```text
System audio ──WASAPI loopback──▶ Faster-Whisper (CUDA or CPU)
                                      │ local-agreement streaming
                                      ▼
                               Source text ──▶ Ollama (local LLM) translation
                                      │
                                      ▼
                         Overlay: source first + draft + final bilingual lines
```

## Features

- **Source text first** — partial recognition appears immediately (grey = still revisable); translation follows
- **Draft translation** — mid-sentence draft in light blue italic; replaced by the final line when ready
- **Local-agreement streaming ASR** — prefix-stable commits, fewer fragment duplicates (ported from [whisper_streaming](https://github.com/ufal/whisper_streaming), MIT)
- **Glossary** — political and domain terms in `config.py` (`GLOSSARY`)
- **Hallucination filter** — drops typical TV-subtitle ghost phrases on silence/music
- **GPU preemption friendly** — under game load, subtitles lag instead of dropping forever
- **Resizable overlay** — drag edges/corners; size and position persist across restarts
- **Word lookup** — click a German word for lemma / POS / meaning (local LLM)
- **Click-through mode** — `Ctrl+Alt+M` so the overlay ignores mouse hits over video/games
- **Daily transcript archive** — `transcripts/` (timestamp + source + translation), kept
  forever by default. Plain text, and this app captures **all** system audio: set
  `TRANSCRIPT_KEEP_DAYS = 30` to auto-prune, or `SAVE_TRANSCRIPT = False` to record nothing.
- **Hotkeys** — pause, language cycle, performance mode (see Usage)

## Requirements

| | Recommended | Minimum |
|---|---|---|
| OS | Windows 10/11 64-bit | Windows 10/11 (**Windows only** — WASAPI loopback) |
| GPU | NVIDIA 8 GB+ VRAM | CPU-only works (higher latency) |
| RAM | 16 GB | 8 GB |
| Disk | ~10 GB (deps + models) | ~6 GB |
| Python | 3.10–3.13 | same |
| Other | [Ollama](https://ollama.com/) for translation | same |

## Install

Clone into an **ASCII-only path** (e.g. `C:\realtime_subtitle`). Desktop shortcuts embed absolute paths; non-ASCII user profiles (common on Chinese Windows) break the generated `.bat` files.

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# China mirror: powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror

# Root shim still works:
# powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer checks the path and Python, detects NVIDIA VRAM (or falls back to CPU), creates a venv, installs dependencies, guides Ollama setup, and writes desktop shortcuts under **“德语直播实时字幕”** (start / stop / pause / update / uninstall).

If Ollama is missing, the installer offers **`winget install --id Ollama.Ollama -e`** (finds the binary via common paths; PATH need not refresh). Decline to open the download page instead.

First launch downloads the Whisper model (~1.6 GB).

**AI-assisted install:** ask an agent to clone this repo and follow [CLAUDE.md](CLAUDE.md) for hardware tiers and known pitfalls.

## Update & uninstall

| Action | Command |
|---|---|
| Update | Desktop `更新字幕.bat` or `scripts\windows\update_subtitles.ps1` (`git pull` + deps) |
| Uninstall / free space | `scripts\windows\uninstall.ps1` (asks per component; default is keep) |
| Cache only | `scripts\windows\uninstall.ps1 -CleanCache` |

Personal files never go into git: `config_local.py`, window state, `transcripts/`.

<details>
<summary>Manual install (no scripts)</summary>

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
# Install Ollama: https://ollama.com/download
ollama pull qwen3.5:9b
venv\Scripts\python -u main.py
```
</details>

## Usage

| Action | How |
|---|---|
| Move overlay | Drag anywhere on the window |
| Resize | Drag edges / corners (larger window = more history) |
| Word lookup | Single-click a German word |
| Click-through | `Ctrl+Alt+M` |
| Pause / resume | `Ctrl+Alt+P` (works over fullscreen games) |
| Cycle recognition language | `Ctrl+Alt+L` (default de ↔ en; extend `config.LANGUAGE_CYCLE`) |
| Performance mode | `Ctrl+Alt+G` (lighter ASR + model; free GPU for games) |
| Session history | 📜 on the overlay |
| Settings | ⚙️ (timing, font size, line count — live) |
| Quit | ❌ or desktop stop shortcut |

**Colours:** white = committed source · grey italic = provisional source tail · light-blue italic = draft translation · light grey = final translation.

## Configuration

Defaults live in [config.py](config.py). Prefer overrides in **`config_local.py`** (gitignored):

```python
WHISPER_MODEL = "large-v3-turbo"   # or "medium" / "small"
OLLAMA_MODEL = "qwen3.5:9b"        # weaker machines: "qwen3.5:2b"
SOURCE_LANGUAGE = "de"
DRAFT_TRANSLATION = True
GLOSSARY = {...}
```

VRAM tiers and model picks are documented in [CLAUDE.md](CLAUDE.md).

## Repository layout

| Path | Role |
|---|---|
| `main.py` | Thin entrypoint (`python -u main.py`) |
| `realtime_subtitle/` | Package: `capture/`, `asr/`, `translate/`, `ui/`, `app.py` |
| `scripts/windows/` | Install, start, stop, pause, update, uninstall |
| `tests/` | Pytest + standalone GUI harnesses |
| `docs/` | Design specs, Chinese notes, structure guide |

See [docs/STRUCTURE.md](docs/STRUCTURE.md).

## Tests

```powershell
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
# GUI harnesses (not collected by pytest):
# venv\Scripts\python tests\test_hittest.py
```

## Troubleshooting

| Symptom | What to try |
|---|---|
| `cublas64_12.dll` missing | Reinstall `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` from `requirements.txt` |
| Source only, no translation | Start Ollama; `ollama pull` the model printed by `from realtime_subtitle import config; print(config.OLLAMA_MODEL)` |
| Frequent “GPU busy” | Settings → slower commit interval, or smaller Whisper model in `config_local.py` |
| No audio after headset change | Settings → device name contains… or `LOOPBACK_DEVICE_NAME` in `config_local.py` |
| Tiny UI on laptop | Windows display scaling; `Ctrl+scroll` for subtitle font size |

Logs: `subtitle.log` / `subtitle.err.log` (and rotated files under `logs/`).

## Credits & license

- Originally inspired by [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle) (Apache-2.0); recognition pipeline rewritten
- Streaming agreement ideas from [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming) (MIT)
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

Licensed under [Apache-2.0](LICENSE).
