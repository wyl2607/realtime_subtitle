# Windows runbook

[中文](../zh/WINDOWS-RUNBOOK.md) · **English** · [Deutsch](../de/WINDOWS-RUNBOOK.md)

Everything an operator needs once the code is on GitHub `master`. Windows only — audio
capture uses WASAPI loopback.

## A. First-time install (new PC)

```powershell
# 1) Clone to an ASCII-only path (required)
cd C:\
git clone https://github.com/wyl2607/realtime_subtitle.git
cd C:\realtime_subtitle

# 2) Install (venv, GPU tier, Ollama, desktop shortcuts)
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# China network:
# powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror
```

> **Why ASCII-only?** The generated desktop `.bat` files embed this path, and under
> `chcp 65001` cmd mis-parses lines containing non-ASCII characters — the launcher
> breaks outright. `install.ps1` refuses to continue on such a path.

If Ollama is missing, accept the winget prompt (`Y`). The first launch also downloads the
Whisper model (1–3 GB); that is expected, not a hang.

The desktop folder **德语直播实时字幕** ends up with five entries:

| File | What it does |
|------|--------------|
| `启动字幕.bat` | **The only one you need day to day.** Pulls the latest version, then starts |
| `YouTube下载加字幕.bat` | Subtitle a video after the fact (also accepts local files) |
| `停止字幕.bat` | Stop |
| `暂停继续字幕.bat` | Pause / resume (same as `Ctrl+Alt+P`) |
| `卸载字幕.bat` | Uninstall; asks per component, default is keep |

## B. Existing install — update

```powershell
cd C:\realtime_subtitle   # your real clone path
powershell -ExecutionPolicy Bypass -File scripts\windows\update_subtitles.ps1

# Once, to pick up the new five-entry desktop layout:
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

`install.ps1` is idempotent: it never wipes `config_local.py`, transcripts or downloads.
It does remove desktop entries that have been retired (`启动并更新字幕.bat`,
`更新字幕.bat`, `下载并加字幕.bat`) — their behaviour moved into `启动字幕.bat`.

Upgrading from a pre-v3.0.0 (PyQt5) build leaves the old Qt packages in the venv, because
`pip install -r` never uninstalls anything. Harmless, just disk:

```powershell
venv\Scripts\python scripts\prune_venv.py --yes
```

## C. Daily use

| Action | How |
|--------|-----|
| Start (pulls first) | Desktop **启动字幕.bat**; still starts if the pull fails, and restarts a running instance only when new code actually landed |
| Stop | **停止字幕.bat** |
| Pause | **暂停继续字幕.bat** or `Ctrl+Alt+P` |
| Download + bilingual SRT | **YouTube下载加字幕.bat**; outputs land in `downloads\<video-id>\` |
| Switch language pair | `Ctrl+Alt+L` |
| Mouse click-through | `Ctrl+Alt+M` |
| Performance mode (give the GPU back to a game) | `Ctrl+Alt+G` |
| Cinema bar | `Ctrl+Alt+C` — needed once the video is already fullscreen and the buttons are covered |
| Update without starting | `scripts\windows\update_subtitles.ps1` (deliberately has no desktop entry) |

Manual start (debugging):

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -u main.py
# Video download + bilingual subtitles:
venv\Scripts\python download_subtitle.py "https://www.youtube.com/watch?v=..."
```

Logs: `subtitle.log`, `subtitle.err.log`, `logs\`.

## D. Smoke checks after an update

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -c "from realtime_subtitle import config, version_string; print(version_string(), config.OLLAMA_MODEL)"
venv\Scripts\python -c "import torch; from realtime_subtitle.translate import translator_queue; translator_queue._ensure_ml_deps(); print('SMOKE_OK')"
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
```

## E. Things that stay true

| Item | Notes |
|------|-------|
| `config_local.py` | Always at **repo root**, never inside the package. Machine-specific settings go here — never edit `config.py`, that causes merge conflicts on every update |
| Root `*.ps1` | Compatibility shims for pre-2026-08 installs. **Do not delete** — old desktop shortcuts point at them, including the updater itself, so removing them leaves those users with no way to recover |
| Standalone GUI tests | `tests\test_hittest.py`, `test_resize_freedom.py`, `test_wordclick.py` open real windows; run them manually with `PYTHONPATH=.` |
| Ollama | A separately installed service that auto-updates; its port briefly disappears while it does |

## F. Common failures

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: realtime_subtitle` | Run from the repo root; re-pull; the venv must be this clone's |
| `ModuleNotFoundError: PyQt5` | You are running code newer than your venv — let the updater reinstall dependencies (v3.0.0 moved to PyQt6) |
| `import config` fails in old notes | Use `from realtime_subtitle import config` |
| Source language only, no translation | Ollama not running or the model was never pulled: compare `ollama list` with `config.OLLAMA_MODEL` |
| No translation for the first minute after install | The model is loading while ASR works through the backlog; it settles on its own |
| Shortcut says "file not found" | Re-run `scripts\windows\install.ps1` to regenerate the `.bat` files |
| Non-ASCII install path | Move the clone to e.g. `C:\realtime_subtitle` and re-run the installer |
| No audio picked up | Capture follows the **default playback device**; swapping headsets re-binds within ~5 s. To pin one device, set a substring of its name in the ⚙ panel |

---

Deeper material — hardware tiers, tuning knobs, and the full list of things that have
already bitten this project — lives in [CLAUDE.md](../../CLAUDE.md) (Chinese).
