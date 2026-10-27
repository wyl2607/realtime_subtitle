# Realtime Subtitle

**Live-Untertitel für Windows und macOS** — Systemton aufnehmen, Sprache erkennen, lokal übersetzen und als zweisprachiges Always-on-Top-Overlay anzeigen. Standardmäßig vollständig lokal; Audio wird nur an den Cloud-Erkennungsdienst gesendet, wenn ein Groq-Schlüssel in den Programmeinstellungen konfiguriert ist. Aktuelle Version **v3.2.0**.

> **Datenschutzversprechen & Datenwege außerhalb des Rechners:**
>
> Standardmäßig laufen Erkennung und Übersetzung vollständig lokal; weder Audio noch Transkripte verlassen den Rechner. Es gibt nur zwei optionale Wege, auf denen Audio den Rechner verlässt, und beide erfordern eine explizite Konfiguration:
> 1. **Eigener Rechenknoten:** Audio wird an einen Rechner im eigenen Tailscale-Netzwerk (`100.64.0.0/10`) gesendet, ohne Beteiligung Dritter;
> 2. **Cloud-Groq (Drittanbieter):** Audio wird nur dann an den Groq-Cloud-Dienst gesendet, wenn ein Groq-Schlüssel in den Programmeinstellungen hinterlegt ist. Liest keine Umgebungsvariablen (verhindert versehentliches Hochladen durch systemweite Variablen); kann im Einstellungsfenster oder per CLI mit einem Klick deaktiviert werden.
>
> Die Statuszeile im Overlay zeigt den aktuellen Erkennungsursprung eindeutig an: „云端·Groq“ / „混合·<Knotenname>“ / „本机“.
> Bei Verwendung eines Netzwerk-Proxys kann der Proxy die hochgeladenen **Roh-Audiofragmente** (WAV) sehen.
> Bezüglich der Datenspeicherung durch Drittanbieter: Laut der [offiziellen Groq-Datenschutzrichtlinie](https://console.groq.com/docs/your-data) speichert Groq Inferenzanfragen standardmäßig nicht; zur Behebung von Zuverlässigkeitsproblemen oder Missbrauch können Daten vorübergehend bis zu 30 Tage gespeichert werden. Bei Bedenken aktivieren Sie Zero Data Retention (ZDR) unter Data Controls in der Groq-Konsole.

> **Das wird durchgesetzt, nicht nur auf dem Papier.** Beim Start löst das Programm
> `OLLAMA_BASE_URL` auf und bricht den Start ab, wenn nicht alle aufgelösten Adressen
> Loopback-Adressen sind — sonst würde schon ein Tippfehler in `config_local.py` dazu
> führen, dass deine Transkripte unbemerkt den Rechner verlassen, während die Untertitel
> ganz normal weiterlaufen. Für Ollama auf einem anderen Rechner:
> `ALLOW_REMOTE_OLLAMA = True` in `config_local.py` setzen.

> **Eine Ausnahme, und nur wenn du sie anklickst:** Der Button `🌐 Stärkere KI fragen`
> öffnet den Systembrowser mit einer Frage aus den letzten Minuten des erkannten Textes
> (≤300 Zeichen) an eine Web-KI (standardmäßig grok.com,
> `config.AI_ANALYSIS_WEB_URL_TEMPLATE`). Ohne Klick wird nichts gesendet — und selbst dann
> zeigt dir zuerst ein Dialog, **was genau gesendet würde** (Zielhost, Gesamtlänge, erste
> 300 Zeichen), und wartet auf deine Bestätigung; mit `AI_WEB_CONFIRM = False` abschaltbar.
> Template auf einen leeren String setzen entfernt den Button. Achtung: Das Programm nimmt
> **den gesamten Systemton** auf, also ggf. auch Sprachanrufe.

[中文](README.md) · [English](README.en.md) · [Repository-Struktur](docs/STRUCTURE.md) · [Windows-Anleitung](docs/de/WINDOWS-RUNBOOK.md)

```text
Systemton ──WASAPI-Loopback──▶ Faster-Whisper (CUDA oder CPU)
                                    │ Local-Agreement Streaming
                                    ▼
                             Quelltext ──▶ Ollama (lokales LLM) Übersetzung
                                    │
                                    ▼
                    Overlay: Quelle zuerst + Entwurf + finale Zweizeiler
```

## Plattformstatus

Stand 2026-10-25. Aktuelle Release-Version für Windows / macOS ist v3.2.0 (2026-10-25).

Legende: ✅ stabil · 🟢 nutzbar · 🧪 experimentell · ⬜ nicht begonnen.

| Plattform | Form | Stufe | Schon nutzbar | Noch offen |
|---|---|---|---|---|
| Windows | Desktop-App | ✅ Stabil | Lokale Erkennung und Übersetzung; dreistufiges automatisches Routing Cloud (Groq) → Rechenknoten → lokal; die Statuszeile zeigt die aktuelle Quelle. Vollständiges pytest auf echter Windows-Hardware (RTX 4070): 1269 bestanden, 0 fehlgeschlagen; Smoke-Test und 150-%-HiDPI-Prüfung bestanden | Trennt gelegentlich einen Satz zu früh (zwei Zeilen; Übersetzung nicht betroffen) |
| macOS | Desktop-App + rslite (natives Swift) | 🟢 Nutzbar | Lokale Erkennung und Übersetzung; dreistufiges automatisches Routing Cloud → Rechenknoten → lokal. Cloud: Segmentende → Anzeige im Median ca. 1,1 s, Median-Latenz der Hybrid-Verfeinerung 1,47 s | Weiterhin nur als Quellcode (rslite-.app-Paket ist für v3.3 geplant); rslite-Release-Build mit Swift 6.3 hängt in der CI, wird untersucht |
| Android | Native App (separates privates Repo) | 🧪 Experimentell | v0.4.0-quality veröffentlicht (alte Architektur); die neue Architektur ist in main zusammengeführt: Erkennung auf dem Gerät mit Live-Entwurf auf Deutsch, Cloud-Stufe, Knoten-Stufe, automatische Umschaltung, Keystore-verschlüsselte Schlüsselspeicherung, alle Unit-Tests bestanden | Neue Architektur noch nicht auf echter Hardware verifiziert; noch kein neues Release |
| iOS | Native App (Prototyp) | 🧪 Experimentell | Broadcast-Erweiterung für Bildschirmaufnahme nimmt Ton anderer Apps auf, alternativ direkt das Mikrofon; dreistufiges Routing Cloud → Knoten → lokal; Kopplung per QR-Code / `rsnode://pair`; Untertitel-Rückblick für heute | Noch nicht auf echter Hardware; selbst mit Xcode bauen und installieren; Anzeige nur per Bild-in-Bild, kein Desktop-Overlay |
| Linux | — | ⬜ Nicht begonnen | — | Keine Pläne |

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
  wenn dreimal hintereinander dieselbe neue Sprache erkannt wird. Während der
  Bestätigung werden neue Untertitel zurückgehalten (`LANGUAGE_HOLD_OUTPUT`,
  standardmäßig an): verworfen, wenn umgeschaltet wird (sie wurden noch mit der
  alten Sprache transkribiert), unverändert nachgereicht, wenn nicht. Die wenigen
  Sekunden vor der ersten Erkennung (höchstens ein Erkennungsintervall) können
  weiterhin unbrauchbar sein.
- **Klick-durchlässig** — `Ctrl+Alt+M` für Vollbild-Video/Spiele
- **Kinoleiste** — transparenter Netflix-ähnlicher Streifen am unteren Bildrand (`Ctrl+Alt+C` / 🎞); immer klick-durchlässig
- **TV-Vollbild** — undurchsichtiges Großschrift-Fenster auf einem zweiten Bildschirm (📺)
- **KI-Analyse** — 🤖 fasst die letzten Minuten lokal zusammen; 🌐 öffnet eine Web-KI erst nach Bestätigung
- **Tages-Archiv** — Ordner `transcripts/`, standardmäßig unbegrenzt aufbewahrt. Klartext, und diese App nimmt **sämtlichen** System-Ton auf: `TRANSCRIPT_KEEP_DAYS = 30` löscht Älteres automatisch, `SAVE_TRANSCRIPT = False` zeichnet gar nichts auf
- **Video herunterladen und Untertitel erzeugen** — Desktop-Shortcut `YouTube下载加字幕.bat` oder `scripts\windows\download_subtitle.ps1` lädt bis 4K, transkribiert lokal, erzeugt SRT-Dateien, übersetzt jede Zeile mit lokalem Ollama und schreibt einen chinesischen Lernleitfaden. Chinesische Quelle wird Chinesisch + Deutsch; Deutsch, Englisch und andere Quellen werden Quelle + Chinesisch.
- **Hotkeys** — Pause, Sprache, Leistungsmodus, Kinoleiste (siehe Nutzung)

## Systemvoraussetzungen

| | Empfohlen | Minimum |
|---|---|---|
| OS | Windows 10/11 64-Bit | Windows 10/11 (**nur Windows** — WASAPI) |
| GPU | NVIDIA ≥ 8 GB VRAM | CPU möglich (höhere Latenz) oder Windows-Liveuntertitel (siehe „Optionale Erkennungsquellen“) |
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

Das Setup prüft Pfad und Python, erkennt NVIDIA-VRAM (sonst CPU-Profil), legt ein venv an, installiert Abhängigkeiten, führt durch Ollama und legt Desktop-Shortcuts an (Start, Stopp, Pause, Update, Deinstallation sowie Download und Untertitel).

Fehlt Ollama, bietet das Setup **`winget install --id Ollama.Ollama -e`** an (Suche über übliche Installationspfade). Ablehnen öffnet die Download-Seite.

Beim ersten Start wird das Whisper-Modell geladen (~1,6 GB).

**KI-gestützte Installation:** Agent anweisen, dieses Repo zu klonen und [CLAUDE.md](CLAUDE.md) (Hardware-Stufen, Fallstricke) zu folgen.

## macOS (Apple Silicon)

Die macOS-Version unterstützt **nur Apple Silicon (M1 und neuer)**. Intel-Macs werden nicht unterstützt. Die Erkennung nutzt MLX auf der Apple-GPU, kein CUDA; die Satzübersetzung bevorzugt die eingebaute macOS-Übersetzung (install.sh baut rstranslate; Sie müssen zuerst Deutsch und vereinfachtes Chinesisch in Systemeinstellungen → Allgemein → Sprache & Region → Übersetzungssprachen laden); wenn nicht verfügbar, fällt sie automatisch auf das lokale Ollama zurück. Wortnachschlagen und 🤖 KI-Analyse nutzen weiterhin Ollama. Audio und Text bleiben auf dem Rechner.

Voraussetzungen:

```bash
brew install uv ollama
```

Installation:

```bash
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
bash scripts/macos/install.sh
# Spiegel für Festlandchina: bash scripts/macos/install.sh --mirror
# Wenig Speicherplatz: bash scripts/macos/install.sh --lean
```

`--lean` nutzt die 4-Bit-quantisierte Whisper-Version. Die Genauigkeit sinkt leicht (deutsche WER 4,05% → 4,84%), spart aber etwa 1,1 GB. Das Installationsskript wählt nach Arbeitsspeicher:

| RAM | Whisper | Übersetzungsmodell |
|---|---|---|
| <12 GB | turbo-q4 | qwen3.5:2b |
| 12–31 GB | turbo | qwen3.5:4b |
| ≥32 GB | turbo | qwen3.5:9b |

Rechnerspezifische Einstellungen landen in `config_local.py`; `config.py` nicht ändern. Das ist derselbe Mechanismus wie unter Windows. Wenn der Arbeitsspeicher während des Betriebs knapp wird, stuft das Programm nach Genauigkeitskosten herunter: zuerst die Spracherkennung von fp16 auf 8 Bit (gleiche Genauigkeit, pro Aktualisierung etwa 1 s langsamer), dann das Übersetzungsmodell um eine Stufe (z. B. 4b → 2b) und erst zuletzt die Erkennung auf 4 Bit. Bei länger normalem Druck geht es stufenweise zurück, aber nie über die bei der Installation gewählte Stufe hinaus.

macOS hat keine eingebaute Systemton-Aufnahme. Für Systemton:

```bash
brew install blackhole-2ch
```

Danach in Audio-MIDI-Setup ein Multi-Output-Gerät erstellen, Lautsprecher und BlackHole 2ch anhaken und die Systemausgabe darauf umstellen. Ohne BlackHole fällt das Programm auf das Standardmikrofon zurück und zeigt einen Hinweis. Im ⚙️-Panel kann "device name contains" ein Eingabegerät auswählen.

Start / Stopp:

```bash
bash scripts/macos/start.sh
bash scripts/macos/stop.sh
```

`start.sh` holt vor jedem Start Updates von GitHub; einmalig überspringen mit `bash scripts/macos/start.sh --no-update` oder per `RS_NO_UPDATE=1`.

Logs: `subtitle.log` / `subtitle.err.log`. Die Hotkeys sind wie unter Windows: `Ctrl+Alt+P/L/M/G/C`; auf dem Mac sind das die physischen Tasten **Control + Option**.

Bekannte Unterschiede: Auf dem Mac aktualisieren Untertitel etwa alle 2–3 Sekunden (M2-Messung: Erkennung pro Lauf ca. 2,4 Sekunden), langsamer als mit NVIDIA-GPU. Eine einzelne Übersetzung dauert mit 4b ca. 4 Sekunden, mit 2b ca. 1,6 Sekunden. Erwarteter Speicherbedarf: Python-Umgebung ca. 1,5 GB + Whisper 1,6 GB (`--lean`: 0,5 GB) + Übersetzungsmodell 2,7–6,6 GB.

### rslite (experimentell, native minimal Mac-Version)

Kleines natives Programm mit Systemerkennung und Systemübersetzung; kein Python oder Ollama nötig; bei Akkubetrieb nur etwa 0,1 W über Leerlauf. Bauen: `cd macos-native && swift build -c release`; ausführen: `.build/release/rslite` (Oberflächenmodus). Details: `macos-native/README.md`.

### Rechenknoten (experimentell)

Erkennung an einen anderen Apple-Silicon-Mac oder NVIDIA-Windows-PC im Tailnet auslagern: dieses Gerät zeigt Untertitel zuerst, dann ersetzen genauere Ergebnisse des Knotens sie; bei Verbindungsabbruch laufen Untertitel weiter und fallen automatisch auf lokale Verarbeitung zurück, bei Rückkehr des Knotens wieder verbunden. Installation: Mac-Knoten `bash scripts/node/install_node.sh <ssh-Alias>`, Windows-Knoten `bash scripts/node/install_node_windows.sh <ssh-Alias>` (beide auf dem Mac ausführen). Datenschutzgrenze: Knoten lauscht nur auf Tailscale-Adressen (`100.64.0.0/10`), Token-Authentifizierung, nur Dateien, Audio bleibt im Speicher (keine Festplatte), Logs enthalten keinen Untertiteltext; Audio verlässt dieses Gerät nur zu Ihrem eigenen Knoten. Sowohl rslite als auch das Windows-Desktop-Programm unterstützen Rechenknoten (siehe unten „Windows: Rechenknoten nutzen“). Protokolldetails: `docs/protocol-v2.md`.

## Cloud-Erkennung (optional)

- **Mac rslite**: Schlüssel über `rslite --set-groq-key` von Standard-Input einlesen, mit `rslite --clear-groq-key` löschen und Cloud-Modus mit `rslite --cloud on|off` umschalten.
- **Windows**: Groq-Schlüssel im Passwortfeld unter „Cloud-Erkennung“ im ⚙️-Einstellungsfenster eingeben und Schalter umlegen.
- **Kostenloses Kontingent**: 20 Anfragen pro Minute, 7.200 Sekunden Audio pro Stunde, 2.000 Anfragen / 28.800 Sekunden pro Tag, mindestens 10 Sekunden pro Anfrage.
- **Automatischer Fallback**: Bei fast vollem Kontingent automatischer Wechsel auf eigene Knoten oder lokal; nach Erholung Umschaltung an Sprechpause.
- **Fallback-Reihenfolge**: Cloud → Eigener Knoten → Lokal.

## Optionale Erkennungsquellen und Untertitel-Export

Alles in `config_local.py` eintragen und die Untertitel danach neu starten.

- **Windows-Liveuntertitel (ohne GPU)**: `ASR_SOURCE = "livecaptions"`, danach
  `venv\Scripts\python -m pip install uiautomation`. Die Spracherkennung übernimmt die in Windows 11 (22H2+)
  eingebaute Funktion „Liveuntertitel“; das Programm liest deren Text und übersetzt ihn mit Ollama. Whisper wird
  nicht geladen, die GPU nicht genutzt – passend für Rechner ohne NVIDIA-Karte. Die Erkennungssprache wählst du unter
  **Windows-Einstellungen → Barrierefreiheit → Untertitel**; sie muss zur Quellsprache des Programms passen. Das
  Liveuntertitel-Fenster wird während des Betriebs minimiert (`LIVE_CAPTIONS_HIDE_WINDOW = False` lässt es sichtbar)
  und beim Beenden wiederhergestellt. Die Liveuntertitel halten die letzten Wörter bis zum nächsten Satz zurück, daher
  kommt der letzte Satz vor einer Pause verspätet (spätestens nach ca. 12 s). Sind die Voraussetzungen nicht erfüllt
  (kein Windows 11, uiautomation fehlt, Liveuntertitel nicht gefunden), wird automatisch Whisper verwendet.
- **SenseVoice (Rechenknoten, optional)**: `ASR_ENGINE = "sensevoice"` und auf dem Knoten `pip install funasr`.
  Nur Chinesisch / Englisch / Japanisch / Koreanisch / Kantonesisch – **kein Deutsch**; andere Sprachen fallen auf
  Whisper zurück. Die lokale Streaming-Erkennung bleibt Whisper.
- **SRT / CSV pro Sitzung**: standardmäßig an. Neben der täglichen txt-Datei schreibt jeder Lauf unter
  `transcripts\sessions\` eine zweisprachige SRT (direkt in den Videoplayer ziehen) und eine CSV (öffnet in Excel).
  Abschalten mit `SAVE_SESSION_SRT = False` / `SAVE_SESSION_CSV = False`; `SAVE_TRANSCRIPT = False` schaltet sie mit
  ab. `TRANSCRIPT_KEEP_DAYS` gilt auch für sie.

## Windows: Rechenknoten nutzen

- Knotenschalter standardmäßig aus.
- Vom Mac mit `bash scripts/node/install_node_windows.sh <Knoten> --client <windows-ssh-alias>` (oder `install_node.sh` mit gleichen Parametern) in einem Schritt bereitstellen.
- Ist ein Rechner gleichzeitig Knoten und Client, überspringt er sich automatisch selbst.

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
| Kinoleiste | `Ctrl+Alt+C` oder 🎞 (nötig, wenn das Video schon Vollbild ist) |
| TV-Vollbild | 📺 |
| KI-Analyse | 🤖 |
| Verlauf | 📜 |
| Einstellungen | ⚙️ (Takt, Schriftgröße, Kinoleiste — live) |
| Beenden | ❌ oder Stopp-Shortcut |

### Video herunterladen und zweisprachige SRT-Dateien erstellen

`YouTube下载加字幕.bat` doppelklicken. Wenn die Zwischenablage eine Video-Adresse enthält, wird sie automatisch verwendet; sonst erscheint eine Eingabeaufforderung zum Einfügen oder Tippen. Standardmäßig werden bis zu 2160p (4K) geladen. Danach extrahiert das Programm die Tonspur, nutzt das lokale Faster-Whisper-Modell und übersetzt jede Untertitelzeile einzeln mit lokalem Ollama, damit lange Videos keinen doppelten Kontext aufbauen.

Die Dateien liegen unter `downloads\<video-id>\`:

- `<video-id>.<quelle>-<ziel>.bilingual.srt` — zuerst Quelle, danach Übersetzung; direkt im Player nutzbar
- `<video-id>.<quelle>-<ziel>.target.srt` — nur die Übersetzung (einsprachig)
- `<video-id>.<quelle>.source.srt` — nur die erkannte Quelle
- `<video-id>.<quelle>-<ziel>.learning.md` — chinesische Übersicht, Gesprächsablauf, Ausdrücke und Lernschritte

Das Skript fragt nach der Untertitelform (1 zweisprachig, Standard; 2 nur Übersetzung) und der Zielsprache (1 Englisch, Standard für chinesische Videos; 2 Deutsch). Standardziele: chinesische Quelle → Englisch, alle anderen → Chinesisch; eine ausdrücklich gewählte Zielsprache wird von der Spracherkennung nie überschrieben. Alle drei SRT-Varianten entstehen bei jedem Lauf, und die Dateinamen tragen das Sprachpaar — ein Wechsel von Ziel oder Form überschreibt also nichts und startet die Spracherkennung nicht neu. Das Paar Chinesisch → Deutsch der Echtzeit-Pipeline (`LANGUAGE_PAIRS`) bleibt unverändert. Kommandozeile:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\download_subtitle.ps1 -Url "Video-Adresse"
# Mit -NoSummary den Lernleitfaden überspringen
```

**Farben:** weiß = feste Quelle · grau kursiv = vorläufiges Quellenende · hellblau kursiv = Entwurfsübersetzung · hellgrau = finale Übersetzung.

## Konfiguration

Standardwerte in [config.py](realtime_subtitle/config.py). Persönliche Overrides in **`config_local.py`** (gitignored):

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
| `scripts/windows/` | Install, Start, Stop, Pause, Update, Uninstall, Download und Untertitel |
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
| Download oder Untertitel schlagen fehl | `ffmpeg` im PATH prüfen, Ollama starten und die Abhängigkeiten inklusive `yt-dlp` neu installieren |

Logs: `subtitle.log`, `subtitle.err.log`, Rotation unter `logs/`.

## Danksagung & Lizenz

- Inspiriert von [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle) (Apache-2.0); Erkennungspipeline neu aufgebaut
- Streaming-Ideen aus [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming) (MIT)
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

Lizenz: [Apache-2.0](LICENSE).
