# Realtime Subtitle

**Offline-Live-Untertitel für Windows (Deutsch / Englisch)** — Systemton aufnehmen, Sprache lokal erkennen, lokal übersetzen und als zweisprachiges Always-on-Top-Overlay anzeigen.

Erkennung und Übersetzung laufen offline; Audio und Transkripte verlassen den Rechner nicht.

> **Das wird durchgesetzt, nicht nur auf dem Papier.** Beim Start löst das Programm
> `OLLAMA_BASE_URL` auf und bricht den Start ab, wenn nicht alle aufgelösten Adressen
> Loopback-Adressen sind — sonst würde schon ein Tippfehler in `config_local.py` dazu
> führen, dass deine Transkripte unbemerkt den Rechner verlassen, während die Untertitel
> ganz normal weiterlaufen. Für Ollama auf einem anderen Rechner:
> `ALLOW_REMOTE_OLLAMA = True` in `config_local.py` setzen.

> **Eine Ausnahme, und nur wenn du sie anklickst:** Der Button `🌐 Stärkere KI fragen`
> öffnet den Systembrowser mit einer Frage aus den letzten Minuten des erkannten Textes
> (≤300 Zeichen) an eine Web-KI (standardmäßig grok.com,
> `config.AI_ANALYSIS_WEB_URL_TEMPLATE`). Ohne Klick wird nichts gesendet. Template auf
> einen leeren String setzen entfernt den Button. Achtung: Das Programm nimmt **den
> gesamten Systemton** auf, also ggf. auch Sprachanrufe.

[English](README.md) · [中文](README.zh.md) · [Repository-Struktur](docs/STRUCTURE.md) · [Windows-Anleitung](docs/WINDOWS-RUNBOOK.md)

```text
Systemton ──WASAPI-Loopback──▶ Faster-Whisper (CUDA oder CPU)
                                    │ Local-Agreement Streaming
                                    ▼
                             Quelltext ──▶ Ollama (lokales LLM) Übersetzung
                                    │
                                    ▼
                    Overlay: Quelle zuerst + Entwurf + finale Zweizeiler
```

## Funktionen

- **Quelltext zuerst** — Teilerkennung erscheint sofort (grau = noch änderbar); Übersetzung folgt
- **Entwurfsübersetzung** — hellblau kursiv mitten im Satz; wird durch die Endfassung ersetzt
- **Local-Agreement-ASR** — stabilere Präfixe, weniger Fragment-Duplikate (angelehnt an [whisper_streaming](https://github.com/ufal/whisper_streaming), MIT)
- **Glossar** — Fach-/Politikbegriffe in `config.py` (`GLOSSARY`)
- **Halluzinationsfilter** — typische TV-Untertitel-Phrasen bei Stille/Musik
- **GPU-freundlich unter Last** — bei Spielen eher Verzögerung als dauerhafter Wortverlust
- **Skalierbares Overlay** — Kanten/Ecken ziehen; Position und Größe bleiben erhalten
- **Wortklick** — Lemma / Wortart / Bedeutung per lokalem LLM
- **Beide Richtungen (DE↔ZH)** — `Ctrl+Alt+L` wechselt das *Sprachpaar*, bei
  chinesischem Ton also deutsche Untertitel. Optionale automatische
  Sprachumschaltung (`AUTO_DETECT_LANGUAGE`, standardmäßig aus) greift erst,
  wenn dreimal hintereinander dieselbe neue Sprache erkannt wird; bis dahin sind
  die Untertitel rund 12 Sekunden lang unbrauchbar, weil der Ton in diesem
  Zeitraum noch mit der alten Sprache transkribiert wird.
- **Klick-durchlässig** — `Ctrl+Alt+M` für Vollbild-Video/Spiele
- **Tages-Archiv** — Ordner `transcripts/`, standardmäßig unbegrenzt aufbewahrt. Klartext, und diese App nimmt **sämtlichen** System-Ton auf: `TRANSCRIPT_KEEP_DAYS = 30` löscht Älteres automatisch, `SAVE_TRANSCRIPT = False` zeichnet gar nichts auf
- **Hotkeys** — Pause, Sprache, Leistungsmodus (siehe Nutzung)

## Systemvoraussetzungen

| | Empfohlen | Minimum |
|---|---|---|
| OS | Windows 10/11 64-Bit | Windows 10/11 (**nur Windows** — WASAPI) |
| GPU | NVIDIA ≥ 8 GB VRAM | CPU möglich (höhere Latenz) |
| RAM | 16 GB | 8 GB |
| Speicher | ca. 10 GB | ca. 6 GB |
| Python | 3.10–3.13 | gleich |
| Sonstiges | [Ollama](https://ollama.com/) | gleich |

## Installation

In einen **rein ASCII-Pfad** klonen (z. B. `C:\realtime_subtitle`). Desktop-Verknüpfungen betten absolute Pfade ein; nicht-ASCII-Benutzerprofile zerstören die generierten `.bat`-Dateien.

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# Optional Spiegel (China): ... install.ps1 -Mirror

# Kompatibler Einstieg im Repo-Root:
# powershell -ExecutionPolicy Bypass -File install.ps1
```

Das Setup prüft Pfad und Python, erkennt NVIDIA-VRAM (sonst CPU-Profil), legt ein venv an, installiert Abhängigkeiten, führt durch Ollama und legt Desktop-Shortcuts an.

Fehlt Ollama, bietet das Setup **`winget install --id Ollama.Ollama -e`** an (Suche über übliche Installationspfade). Ablehnen öffnet die Download-Seite.

Beim ersten Start wird das Whisper-Modell geladen (~1,6 GB).

**KI-gestützte Installation:** Agent anweisen, dieses Repo zu klonen und [CLAUDE.md](CLAUDE.md) (Hardware-Stufen, Fallstricke) zu folgen.

## Update & Deinstallation

| Aktion | Befehl |
|---|---|
| Update | Desktop-Shortcut oder `scripts\windows\update_subtitles.ps1` |
| Deinstallieren / Platz | `scripts\windows\uninstall.ps1` (pro Komponente nachfragen) |
| Nur Cache | `scripts\windows\uninstall.ps1 -CleanCache` |

Persönliche Dateien bleiben unversioniert: `config_local.py`, Fensterzustand, `transcripts/`.

<details>
<summary>Manuelle Installation</summary>

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
# Ollama: https://ollama.com/download
ollama pull qwen3.5:9b
venv\Scripts\python -u main.py
```
</details>

## Nutzung

| Aktion | Bedienung |
|---|---|
| Verschieben | Beliebige Stelle im Overlay ziehen |
| Größe | Kanten/Ecken (größeres Fenster = mehr Verlauf) |
| Wort nachschlagen | Deutsch-Wort anklicken |
| Klick-durchlässig | `Ctrl+Alt+M` |
| Pause / Weiter | `Ctrl+Alt+P` |
| Sprachpaar wechseln | `Ctrl+Alt+L` (de→zh / zh→de / en→zh, siehe `config.LANGUAGE_PAIRS`) |
| Leistungsmodus | `Ctrl+Alt+G` |
| Verlauf | 📜 |
| Einstellungen | ⚙️ |
| Beenden | ❌ oder Stopp-Shortcut |

**Farben:** weiß = feste Quelle · grau kursiv = vorläufiges Quellenende · hellblau kursiv = Entwurfsübersetzung · hellgrau = finale Übersetzung.

## Konfiguration

Standardwerte in [config.py](config.py). Persönliche Overrides in **`config_local.py`** (gitignored):

```python
WHISPER_MODEL = "large-v3-turbo"   # oder "medium" / "small"
OLLAMA_MODEL = "qwen3.5:9b"        # schwächere PCs: "qwen3.5:2b"
SOURCE_LANGUAGE = "de"
DRAFT_TRANSLATION = True
GLOSSARY = {...}
```

VRAM-Stufen und Modellwahl: [CLAUDE.md](CLAUDE.md).

## Repository-Struktur

| Pfad | Rolle |
|---|---|
| `main.py` | Einstieg (`python -u main.py`) |
| `realtime_subtitle/` | Paket: `capture/`, `asr/`, `translate/`, `ui/`, `app.py` |
| `scripts/windows/` | Install, Start, Stop, Pause, Update, Uninstall |
| `tests/` | Pytest + manuelle GUI-Suites |
| `docs/` | Design, chinesische Notizen, Struktur |

Details: [docs/STRUCTURE.md](docs/STRUCTURE.md).

## Tests

```powershell
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
```

## Fehlerbehebung

| Symptom | Maßnahme |
|---|---|
| `cublas64_12.dll` fehlt | CUDA-Pakete aus `requirements.txt` neu installieren |
| Nur Quelle, keine Übersetzung | Ollama starten; Modell laut `config.OLLAMA_MODEL` pullen |
| Häufig „GPU busy“ | Commit-Intervall erhöhen oder kleineres Whisper-Modell |
| Kein Ton nach Headset-Wechsel | Gerätename in den Einstellungen / `LOOPBACK_DEVICE_NAME` |

Logs: `subtitle.log`, `subtitle.err.log`, Rotation unter `logs/`.

## Danksagung & Lizenz

- Inspiriert von [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle) (Apache-2.0); Erkennungspipeline neu aufgebaut
- Streaming-Ideen aus [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming) (MIT)
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

Lizenz: [Apache-2.0](LICENSE).
