# Ba-Ge — project guide (auto-loaded)

Push-to-talk voice dictation + audio-file transcription using **ElevenLabs
Scribe**. Hold a hotkey → record → transcribe → paste at the cursor. Also
transcribes audio files (with speaker labels + timestamps) and biases recognition
with a custom-vocabulary (`keyterms`) dictionary.

## Status
- **Linux: working and tested.** pynput hotkey · `arecord` audio · **paste**
  injection (X11; Qt clipboard manager preserves the board + keeps history, and the
  Ctrl+(Shift+)V keystroke is sent via **uinput** — see the note below) · Qt tray +
  settings/transcript windows · ElevenLabs Scribe.
  - **Injection note (hard-won):** split by target in `inject.py::type_text`.
    GTK terminals (Ghostty) intermittently **encode even a real-device (uinput)
    Ctrl+Shift+V as a CSI-u key** instead of firing the paste keybind — the modifiers
    ARE present (mod code 6), so no key-timing trick fixes it; it's Ghostty's call.
    So **terminals are TYPED** (`_type_via_uinput`: uinput char events reach the PTY
    normally — reliable + clipboard-free) for ASCII-mappable text; **GUI apps and
    non-typeable text (CJK — Scribe is bilingual!) are PASTED** (Qt clipboard + uinput
    Ctrl+V, sent OFF the Qt main thread so the loop can serve the SelectionRequest).
    uinput via `evdev` (`/dev/uinput`, udev `uaccess` ACL — no `input` group/re-login).
    pynput/XTEST is the last-resort fallback (GUI only).
- **Cross-platform port: Linux + macOS run on hardware; Windows UNVERIFIED.**
  - ✅ `platform.py` factory (the only `sys.platform` in core) — `make_recorder`,
    `make_injector`, `list_input_devices`, `ffmpeg_exe`, `missing_permissions`.
  - ✅ `paths.py` (platformdirs), `singleton.py`, `notify.py`, `autostart.py` — all
    per-OS (Linux behaviour byte-identical, tests green).
  - ✅ `ui.py` — **PySide6 (Qt)** runtime (the #1-risk event-loop pattern:
    QApplication owns main thread, `QSystemTrayIcon` on it, cross-thread work
    marshalled via a QObject `Signal` bridge — queued to main). `theme.py` sets a
    dark Fusion palette; `ui_settings.py` / `ui_files.py` / `ui_clipboard.py` are the Qt windows
    (`QFileDialog` replaces the old zenity picker). **Why Qt:** the uv-standalone
    Python's Tk aborts on window ops (static libxcb) AND pystray needs PyGObject
    for the GNOME tray — Qt's self-contained wheels + native `QSystemTrayIcon`
    (StatusNotifier) fix both, with no gi/Tk. **GTK/tkinter/pystray are gone.**
  - ✅ audio: `audio.py` (arecord, Linux) + `audio_sd.py` (sounddevice, mac/win).
  - ✅ inject: `inject.py` + `clipboard.py` (paste at cursor, Linux X11 — the Qt
    clipboard manager preserves the board + keeps a history stack; the keystroke is
    sent via **uinput/evdev**, XTEST/pynput fallback) + `inject_pynput.py` (mac/win).
  - ✅ **Linux verified end-to-end** (runs on the new stack, 78 tests green).
  - ✅ **macOS (arm64) brought up on hardware** — 78 tests green, app launches, Qt
    event loop + `QSystemTrayIcon` come up, sd/pynput backends construct, mic
    enumerates, TCC `Accessibility` is detected. Fixed on macOS along the way:
    (1) `hotkey.py` referenced `Key.pause`/`scroll_lock`/`menu`/`insert` that
    pynput's darwin backend doesn't define → crash at import; now built defensively
    via `getattr`. (2) `singleton.py` POSIX `flock` backend ignored `name` and
    couldn't fail a same-process re-acquire → tracks held names + per-name lock file.
    (3) **`platform.ensure_qt_plugins()`** clears the `UF_HIDDEN` flag PySide6's
    macOS wheel puts on its Qt plugin dirs/dylibs (Qt's `getattrlistbulk`
    enumeration skips hidden entries → "Could not find the Qt platform plugin cocoa
    in ''"; see `docs/PORTING.md`). Called before every `QApplication`.
    (4) **`platform.prewarm_macos_input_source()`** — Settings **Save** crashed the
    app (SIGTRAP): pynput's `Listener._run()` (background thread) calls the
    main-thread-only Text Input Source API (`TISCopyCurrentKeyboardInputSource`),
    which macOS 14+ aborts off-main once a window is shown, so restarting the
    listener mid-event-loop died. Now the keyboard-layout context is cached once on
    the main thread (from `HotkeyListener.start()`) and pynput's `keycode_context`
    is rebound to reuse it; the listener thread never touches TIS.
  - ⬜ **macOS dictation flow still needs manual TCC grants to fully verify** — the
    hold-to-talk path (pynput logs "process is not trusted"), mic capture, and
    paste all require Input Monitoring + Accessibility + Microphone grants (a human
    action); code path is wired but the end-to-end dictation wasn't exercised here.
  - ⬜ **Windows on-hardware testing** — NOTHING win is verified.
  - ✅ Packaging (macOS): `build-macos.sh` rewritten to the PySide6 stack
    (PyInstaller onedir `.app`, QML/Quick/Designer trimmed, `NSMicrophoneUsageDescription`
    + `LSUIElement`) and **verified: builds `dist/Ba-Ge.app` (187 MB, ad-hoc
    signed) that launches with tray + event loop on arm64**. Unsigned/un-notarized
    (needs a Developer ID cert). ⚠️ Must be built from a NON-iCloud path — iCloud
    "Optimize Mac Storage" evicts the Qt tree mid-build (Errno 60 / truncated
    Mach-O); build from a `/tmp` copy (see `build-macos.sh` header + PORTING.md).
  - ⬜ Packaging (Windows): `build-windows.ps1` UNVERIFIED (signing/AUMID in PORTING.md).
  - Possible follow-ups from PORTING.md: rumps for the macOS tray (pystray #138),
    Windows AUMID toast registration.

## How to run / test (Linux dev env)
- Run: `.venv/bin/python -m ba_ge`  (or the `.pyz` / app icon)
- Tests: `.venv/bin/python -m unittest discover -s tests -t .`  (must stay green)
- The `.venv` runs a **uv-managed standalone Python** (`uv venv --python 3.12`) +
  **PySide6** — NOT the system Python, and no gi/Tk. Qt's wheels are self-contained,
  so nothing from apt is needed for the UI. `./install.sh` sets this up.
- Packaging: `./build-deb.sh` → a fully self-contained `.deb` bundling the standalone
  Python + trimmed PySide6 + app (~56 MB; Depends only on `alsa-utils ffmpeg
  libxcb-cursor0` — **no `python3-*`, no gi**). Qt's xcb plugin needs
  `libxcb-cursor0`. `build-deb.sh` trims the QML/Quick/Designer stack a widget app
  never uses.

## Modules
`app.py` (state machine + threading) · `hotkey.py` (pynput) · `debounce.py`
(collapse X11 auto-repeat) · `audio.py` (arecord → WAV; `peak_amplitude` silence
guard) · `transcribe.py` (Scribe HTTP; `_base_fields` incl. keyterms) · `filejob.py`
(ffmpeg → diarized transcript) · `inject.py` + `clipboard.py` (**paste**; X11, Qt
clipboard manager + uinput keystroke) · `inject_pynput.py` (mac/win) · `ui.py` + `theme.py` +
`ui_settings.py` +
`ui_files.py` (**PySide6/Qt**: tray + windows) · `platform.py` (backend factory) ·
`paths.py` · `config.py` · `notify.py` · `singleton.py` · `autostart.py`.

## Cross-platform port — READ `docs/PORTING.md` FIRST
It is the full, web-verified risk log (9 blockers / 24 high / 22 medium). The
essentials:

1. **The dev instance can only run Linux.** All macOS/Windows behavior must be
   verified by a human/instance on real hardware using the `TEST` steps in
   `docs/PORTING.md`. Don't claim Mac/Win code "works" — say "unverified, needs
   on-hardware test".
2. **Almost every failure on Mac/Win is SILENT.** macOS TCC (Input Monitoring +
   Accessibility + Microphone — three separate grants, all no-op silently),
   Windows mic-privacy toggle (records pure silence), UIPI (drops hotkey/typing
   near elevated windows). **Detect via signals, not exceptions** — keep and extend
   `peak_amplitude()`/silence detection; verify text actually landed.
3. **Event-loop rule (the #1 architectural risk):** `tkinter root.mainloop()` owns
   the main thread on ALL platforms; tray runs detached/threaded and started before
   mainloop (use **rumps** on macOS, not pystray); pynput on its own thread; a
   single `queue.Queue` drained by `root.after(50,…)` is the ONLY place UI/tray
   state is touched (the cross-platform form of today's `GLib.idle_add`).
4. **Keep Linux behind interfaces.** One `platform.py` factory selects backends by
   `sys.platform`; today's arecord/xdotool/GTK/notify-send/socket become the
   **unchanged Linux backends**. `sys.platform` must appear ONLY in that factory.
   The pytest suite is the Linux regression gate.
5. **Typing:** clipboard-paste is the DEFAULT injection backend on Mac/Win
   (layout-independent, atomic — avoids the dropped-space bug that plagued Linux).

## Conventions
- Don't write summary/plan `.md` files unless asked (this file and `docs/PORTING.md`
  were explicitly requested).
- After changing code that the packaged app runs, rebuild `dist/ba-ge.pyz`.
- `docs/PORTING.md` carries a per-platform on-hardware **testing checklist** — hand
  it to whoever tests on Mac/Windows.
