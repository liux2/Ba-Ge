# PTT Dictation

Push-to-talk voice dictation for Linux, powered by **[ElevenLabs Scribe](https://elevenlabs.io/speech-to-text)**.
Hold a key, speak, release — your words are transcribed and pasted at the cursor
in whatever app has focus. It also transcribes audio files with speaker labels and
timestamps.

```
[hold F9]  speak…  [release]  →  ElevenLabs Scribe  →  text pasted at the cursor
```

> **Platforms:** Linux (X11) — fully tested. The code is cross-platform (Qt +
> pynput), so macOS/Windows are in reach, but they're **unverified on hardware** —
> see [`docs/PORTING.md`](docs/PORTING.md).

## Why ElevenLabs Scribe?

I built this specifically around Scribe for one reason: **it's genuinely good at
professional Chinese–English bilingual speech.**

Most of my dictation is code-switching — technical English terms dropped into
Mandarin sentences, product names, domain jargon. In my own day-to-day testing,
Scribe handled that mixed-language, professional content noticeably better than the
alternatives:

- **OpenAI Whisper** — strong overall, but stumbles on dense Chinese↔English
  code-switching and tends to garble the switch points.
- **Qwen-ASR / FireRedASR** — excellent on *pure* Mandarin, but weaker once English
  terms and technical vocabulary are interleaved.
- **Scribe** — the cleanest on mixed-language sentences, and its **custom-vocabulary
  biasing** (keyterms) works across *both* languages, so product names and jargon
  come through correctly.

If your speech is monolingual, a local/open model may suit you better. But for
**bilingual professional dictation**, Scribe is why this project exists.

## Features

- **Hold-to-talk dictation** — hold F9 (configurable), speak, release; the
  transcript is pasted at the cursor.
- **Atomic paste injection** — inserts the whole transcript in one shot, so it
  can't drop characters or spaces; auto-uses **Ctrl+Shift+V** in terminals.
- **File transcription** — mp3 / wav / m4a / flac / ogg → full transcript with
  **speaker labels + timestamps** (ElevenLabs diarization).
- **Custom vocabulary** — bias recognition toward names, jargon, and product terms
  (applies to live dictation *and* files, across both languages).
- **Native Qt tray + settings UI**, desktop notifications, autostart-on-login.
- **Self-contained** — bundles its own Python + Qt. The `.deb` needs no system
  Python, `python3-tk`, or PyGObject.

## Requirements

- An **ElevenLabs API key** — create one at
  [elevenlabs.io](https://elevenlabs.io) (Profile → API Keys).
- Linux with X11 (GNOME/others). A tray host is needed for the indicator icon
  (on GNOME, the *AppIndicator/StatusNotifier* support extension).

## Install

```bash
git clone <your-repo-url> ptt-dictation
cd ptt-dictation
./install.sh
```

`install.sh` does everything and is idempotent (safe to re-run). It installs a few
CLI/X libraries via `apt`, then sets up a **self-contained Python** (a uv-managed
standalone interpreter) + **PySide6 (Qt)** in `.venv` — it never installs into or
touches your **system** Python, and needs no `python3-tk` or PyGObject. `uv` is
auto-installed if it's missing.

Then open **Activities → PTT Dictation** (or run `ptt-dictation`; `~/.local/bin` is
on PATH for most shells). Right-click the tray icon → **Settings** to paste your
API key.

**apt packages it installs:** `xdotool xclip ffmpeg alsa-utils libnotify-bin
libxcb-cursor0` — plain CLI/X tools (the last is what Qt's xcb plugin needs). The
Python packages (PySide6, pynput, …) go into the standalone `.venv`.

### Or a native package (`.deb`)

For an `apt`-managed install (and `apt remove` to uninstall), build a fully
self-contained `.deb` that bundles its own Python + Qt — no `python3-*` at all:

```bash
./build-deb.sh
sudo apt install ./dist/ptt-dictation_*.deb
```

It's ~55 MB (the price of bundling Python + Qt) and depends only on
`xdotool xclip alsa-utils ffmpeg libxcb-cursor0`.

## Usage

### Dictate

Focus any text field, **hold F9**, speak, **release**. After a short pause
(transcription runs on release), the text is pasted in. The tray icon shows state:
grey (idle) → red (recording) → yellow (transcribing).

### Settings

Right-click the tray icon → **Settings…**, or run `ptt-dictation --settings`. You
can set your **API key**, model, language, **custom vocabulary**, **hold-to-talk
key**, **microphone**, tap threshold, typing method, and **autostart on login**.
Saving applies live to a running app.

The API key can also come from the environment (takes precedence over the file):

```bash
export ELEVENLABS_API_KEY=sk-...
```

### Custom vocabulary (key terms)

Add domain jargon, product/brand names, or proper nouns (one per line or comma-
separated) to bias Scribe toward recognizing them — it applies to both live
dictation and file transcription, and across languages. It's context-aware biasing,
not a forced dictionary.

> ElevenLabs adds **~20% to each call's cost** while key terms are set. Limits:
> ≤1000 terms, ≤50 chars and ≤5 words each (the app validates and warns).

### Transcribe an audio file

Right-click the tray → **Transcribe file…** → pick a file. A window shows progress,
then the diarized transcript with **Copy** / **Save…** buttons:

```
[00:00] Speaker 1: Hello everyone, welcome to the show.
[00:12] Speaker 2: Thanks for having me.
```

Or headless from the CLI (writes `<name>.txt` next to the source, prints its path):

```bash
ptt-dictation --transcribe interview.m4a      # → interview.txt
```

`ffmpeg` down-mixes the file to a small 16 kHz mono clip before upload, so even long
recordings transfer quickly.

## Configuration

`~/.config/ptt-dictation/config.toml` (created on first run; see
`config.example.toml`):

| Setting | Default | Meaning |
|---|---|---|
| `elevenlabs.api_key` | placeholder | ElevenLabs API key (`ELEVENLABS_API_KEY` env var overrides) |
| `elevenlabs.model_id` | `scribe_v2` | Scribe model |
| `elevenlabs.language_code` | auto | force a language, e.g. `eng`, `zho` |
| `elevenlabs.keyterms` | (none) | bias vocabulary, e.g. `["Kubernetes", "小红书"]` (~20% cost) |
| `hotkey.key` | `f9` | hold-to-talk key (`f9`, `pause`, `ctrl_r`, …) |
| `audio.device` | `default` | input device (follows the PipeWire default mic) |
| `audio.min_duration` | `0.3` | ignore taps shorter than this (seconds) |
| `inject.backend` | `paste` | `paste` (atomic, recommended) / `xdotool` / `ydotool` |

## How it works

Recording runs while F9 is held; transcription happens on release (batch), so
expect a ~0.5–2 s pause before the text appears.

| Module | Role |
|---|---|
| `app.py` | state machine + threading wiring |
| `platform.py` | per-OS backend factory (the only place with `sys.platform`) |
| `hotkey.py` / `debounce.py` | global hotkey press/release (pynput) |
| `audio.py` / `audio_sd.py` | mic capture — `arecord` (Linux) / `sounddevice` (mac/win) |
| `transcribe.py` | audio → text / diarized via ElevenLabs Scribe (stdlib HTTP) |
| `filejob.py` | file → `ffmpeg` → Scribe (diarized) → speaker/timestamp text |
| `inject.py` / `inject_pynput.py` | insert text — paste/xdotool (Linux) / clipboard-paste (mac/win) |
| `ui.py` · `theme.py` · `ui_settings.py` · `ui_files.py` | **PySide6 (Qt)** tray + windows (self-contained, gi-free) |
| `config.py` · `paths.py` · `notify.py` · `singleton.py` · `autostart.py` | config, paths, notifications, single-instance, autostart |

**Design note:** the whole UI (tray + windows) is Qt. Qt's wheels are
self-contained and `QSystemTrayIcon` speaks StatusNotifier natively, which is what
lets the app ship its own Python with no `python3-tk`/PyGObject and still show a
GNOME tray. All threads marshal UI work onto the Qt main thread via a signal bridge.

## Troubleshooting

- **Nothing pastes into the cursor** — ensure `xdotool`, `xclip`, and `x11-utils`
  (for `xprop`) are installed (`install.sh` does this). The app reads the focused
  window's class and, in terminals (Ghostty, GNOME Terminal, kitty, …), pastes with
  **Ctrl+Shift+V** automatically instead of Ctrl+V.
- **Words run together / missing spaces** — keep **Settings → Typing method** on
  **paste** (the default): it inserts the transcript atomically, so spaces can't
  drop.
- **Silent recording / empty transcript** — the mic is muted or the wrong device is
  selected; the app warns you. Pick the right mic in **Settings → Microphone**.
- **No app logo after install** — log out and back in once (or `Alt+F2` → `r` on
  X11) to refresh GNOME Shell's icon cache.
- **No tray icon on GNOME** — enable the *AppIndicator / StatusNotifier* support
  extension.
- **Hotkey conflicts with another app** — change it in **Settings → Hold-to-talk
  key**.

## Development

```bash
# run from source (uses the .venv install.sh created)
.venv/bin/python -m ptt_dictation

# tests
.venv/bin/python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE) © 2026 Xingbang Liu
