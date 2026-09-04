# Scripts

## Windows (`scripts/windows/`)

| Script | Role |
|--------|------|
| `install.ps1` | One-shot setup (Python, venv, GPU tier, Ollama, desktop shortcuts) |
| `download_subtitle.ps1` | Download a video, create bilingual SRT files, and write a study guide |
| `start_subtitles.ps1` | Start app (single-instance, Ollama health wait) |
| `stop_subtitles.ps1` | Graceful stop then force-kill fallback |
| `pause_subtitles.ps1` | Toggle pause flag without unloading models |
| `update_subtitles.ps1` | `git pull` + dependency sync |
| `uninstall.ps1` | Interactive cleanup (optional `-CleanCache`) |

All scripts resolve `$RepoRoot` two levels above this folder and run with the repository as working directory.

Root `install.ps1` and `download_subtitle.ps1` are **compatibility shims** that forward to
the canonical scripts in this folder.
