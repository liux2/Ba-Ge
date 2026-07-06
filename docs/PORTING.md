# Cross-platform porting notes (macOS + Windows)

**Read this before writing or testing any macOS/Windows code.** It is the recorded
risk log for porting this Linux-first app to macOS and Windows. Sourced from a
web-verified research pass (2026: macOS Tahoe 26.x, Windows 11 25H2). Severities:
**9 blockers, 24 high, 22 medium**.

> **The one thing to internalize:** on macOS and Windows, almost every failure in
> this app's problem domain is **SILENT** — no exception, no log. Permissions,
> mic access, and injection all "succeed" while doing nothing. Detection must be
> **signal-based** (does audio have energy? did text actually land?), never
> "did the API call raise?". Carry `peak_amplitude()`/silence-detection forward.

## Who tests what
- **Linux:** fully built + tested in the dev environment.
- **macOS / Windows:** the dev instance **cannot run these**. A person/instance on
  real hardware runs the per-item `TEST` checks below and relays results. Every
  concern has a concrete on-hardware test.

---

## Target architecture (keep Linux behind interfaces — do NOT regress it)

Define narrow interfaces and pick a backend by `sys.platform` in ONE factory
(`platform.py`). Core `app.py` imports interfaces + the factory only — **no inline
`sys.platform` branches anywhere else** (grep-enforceable). Today's
arecord/xdotool/GTK/notify-send/abstract-socket classes become the **Linux
backends, unchanged**. Regression gate: the existing pytest suite (which injects
fakes via `recorder=`/`injector=`/`indicator=`) must stay green.

Interfaces: `Recorder`, `Injector` (`type_text`, `backend`), `Indicator`
(`owns_main_loop`, `set_state`, `run_main_loop`), `Notifier`, `Clipboard`,
`SingleInstance.acquire`, `Autostart` (`set_enabled`/`is_enabled`), `Permissions`
(no-op on Linux/Win), `Paths` (via `platformdirs`), `FfmpegResolver`.

### Recommended stack
| Interface | Linux (keep) | macOS | Windows |
|---|---|---|---|
| Hotkey | pynput | pynput (Input Monitoring perm) | pynput (UIPI caveats) |
| Audio | `arecord` | `sounddevice`/PortAudio (Mic TCC) | `sounddevice`/WASAPI (privacy toggle) |
| Type at cursor | xdotool/paste | **clipboard-paste default** (Cmd+V) | **clipboard-paste default** (Ctrl+V) |
| Tray | GTK AppIndicator | **rumps** (not pystray) | pystray (win32) |
| Windows (UI) | GTK (for now) | tkinter | tkinter |
| Notifications | notify-send | rumps/UserNotifications | Windows-Toasts + AUMID |
| Config/cache dirs | `~/.config` | `platformdirs` → `~/Library/...` | `platformdirs` → `%LOCALAPPDATA%` |
| Clipboard | xclip (keep) | pyperclip (pbcopy) | pyperclip (built-in) |
| Single-instance | abstract unix socket | flock lockfile | named mutex (`CreateMutexW`) |
| Autostart | XDG .desktop | LaunchAgent plist | HKCU `Run` key |
| ffmpeg | system PATH | bundle (imageio-ffmpeg) | bundle (imageio-ffmpeg) |

---

## 🔴 THE #1 RISK — event-loop / main-thread ownership (BLOCKER, all platforms)

On **macOS**, the pystray tray (Cocoa NSApp runloop), the tkinter Tcl interpreter,
AND pynput all want the process **main thread**. You cannot give three things the
main thread. Naive "tray in a daemon thread, `tk.mainloop()` on main" (the Linux
instinct) **silently fails or crashes** on macOS (`RuntimeError: Calling Tcl from
different apartment`; pystray issue #138 GIL crash on Apple Silicon).

**Canonical pattern (use on ALL platforms so Linux doesn't diverge):**
1. `tkinter` `root.mainloop()` **owns the main thread** everywhere.
2. Tray started **before** `mainloop()`:
   - Windows/Linux: `threading.Thread(target=icon.run, daemon=True).start()`.
   - macOS: `icon.run_detached(darwin_nsapplication=<tk's NSApplication>)`, **or
     use `rumps`** as the macOS tray owner (safer — pystray `run_detached` has a
     documented fatal GIL crash on Apple Silicon).
3. pynput `Listener` on its **own daemon thread**.
4. **One `queue.Queue`.** pynput callbacks, tray-menu callbacks, and the
   transcription worker only `.put()` closures. A repeating `root.after(50, drain)`
   on the tk thread pops+runs them. **This is the ONLY place tk widgets / tray
   state are touched.** This is the direct re-expression of today's
   `GLib.idle_add()` marshalling in `indicator.py set_state()`.
5. Keep the hotkey callback trivial (enqueue + return) — on **Windows** a blocking
   hotkey callback **freezes ALL system keyboard input** and Windows silently
   removes the hook after `LowLevelHooksTimeout`.

**TEST (mac + win):** tray icon appears and is clickable; hold hotkey → icon goes
IDLE→RECORDING→BUSY→IDLE; rapidly toggle hotkey 10× while opening the tray menu;
watch stderr for `Calling Tcl from different apartment` / SIGILL / segfault; tray
survives >60s idle.

---

## macOS concerns

### 🔴 Blockers
- **Input Monitoring permission** (TCC) — the global F9 listener gets ZERO events
  without it; **no error**. Grant: System Settings → Privacy & Security → Input
  Monitoring. Must quit+relaunch after granting. *Mitigation:* check
  `Listener.IS_TRUSTED`/`CGPreflightListenEventAccess` at startup; if untrusted,
  show a dialog deep-linking `x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent`.
  *TEST:* clean Mac, F9 does nothing → grant → relaunch → F9 works.
- **Accessibility permission** (TCC, **separate** from Input Monitoring) —
  `keyboard.Controller.type()` silently no-ops without it (pynput #389). Grant:
  Privacy & Security → Accessibility. *TEST:* with only Input Monitoring granted,
  typing produces nothing; grant Accessibility → text appears. Verify the two are
  independent.
- **Microphone permission** (TCC) — `sounddevice`/PortAudio records **pure silence
  (zeros), not an error**, without `NSMicrophoneUsageDescription` + granted
  consent. Device list still populates (enumeration ≠ capture). *Mitigation:* ship
  `.app` with `NSMicrophoneUsageDescription`; validate non-silence
  (`peak_amplitude`); message → Privacy → Microphone. *TEST:* `tccutil reset
  Microphone <id>`; deny → app detects silence + helpful error (not empty
  transcript); grant → works; works when double-clicked from Finder.
- **Tray/tkinter main-thread conflict** — see THE #1 RISK above.

### 🟠 High
- **Code-signing / TCC identity across rebuilds** — grants bind to the code
  signature + bundle id + path. Unsigned/ad-hoc rebuild = new identity ⇒
  permissions silently stop working. *Mitigation:* sign with a stable Apple
  Development / Developer ID cert; fixed install path (`/Applications/...`);
  recovery = `tccutil reset Accessibility/ListenEvent/Microphone <bundle-id>`.
- **Character/space dropping on fast `type()`** (macOS analogue of the Linux pain)
  — CGEvent typing drops spaces/unicode when pushed too fast (pynput #569). *Fix:*
  **clipboard-paste default** (atomic), or chunk ~20 chars w/ 2–4 ms delay.
- **Secure Input (`EnableSecureEventInput`)** — password fields / 1Password /
  terminals in "Secure Keyboard Entry" block BOTH capture and injection; a bug can
  leave it stuck **globally**. Treat as expected (don't dictate into password
  fields); detect stuck state via `IsSecureEventInputEnabled()`; never leave it
  asserted.
- **pystray run_detached GIL crash on Apple Silicon** (#138) — prefer **rumps**
  for the macOS tray.
- **Wrong Tk build** — system Tk is deprecated (warning + broken file dialogs). Use
  the **python.org universal2** build (bundles private Tcl/Tk 8.6.x); verify
  `root.tk.call('info','patchlevel')`; `TK_SILENCE_DEPRECATION=1` only as fallback.
- **Notifications need a bundle id** — `osascript` shows as "Script Editor";
  terminal-notifier/pync need a signed bundle on Sonoma+/Tahoe. Use rumps /
  `UNUserNotificationCenter` from inside the `.app`; treat notifications as
  best-effort (mirror state in tray/window).
- **Gatekeeper/notarization** — unsigned/un-notarized `.app` is blocked on
  Sequoia/Tahoe (right-click-Open bypass removed). Hardened-runtime signing needs
  entitlements `allow-jit`, `allow-unsigned-executable-memory`,
  `disable-library-validation` + `codesign -o runtime`; **re-verify mic prompt
  still appears after signing** (classic regression). Don't gate on
  `platform.mac_ver()` (reports 16.x on Tahoe).
- **ffmpeg** — absent on stock macOS; bundle a **universal2** signed binary
  (invoke by absolute path); review GPL vs LGPL.
- **pynput hotkey/typing = same TCC class** (dup of blockers, flagged for audio dev).

### 🟡 Medium
- **CGEventTap silent disable at runtime** (`kCGEventTapDisabledByTimeout`) — keep
  the callback fast (offload work); health-check the listener every ~5s and
  re-create if dead.
- **Grant goes to python3/Terminal, not the app** when run from source — test with
  the packaged signed `.app`, the same artifact users get.
- **Dock icon / focus stealing** — set `LSUIElement=1` / `NSApplicationActivationPolicyAccessory`
  so it's menu-bar-only and doesn't steal focus from the dictation target.
- **Autostart** = `~/Library/LaunchAgents/<id>.plist` (`RunAtLoad`); use `launchctl
  bootstrap gui/$UID` (not deprecated load/unload); user can disable under Login
  Items — reflect real state.
- **Single-instance** = flock lockfile (abstract socket is Linux-only).
- **Device enumeration** works pre-permission; AirPods/Bluetooth change default
  input + sample rate — use device default samplerate, store device by name.

---

## Windows concerns

### 🔴 Blockers
- **Mic privacy toggle → pure silence** (the Linux-mute analogue) — the master
  "Microphone access" + "**Let desktop apps access your microphone**"
  (`ConsentStore\microphone\NonPackaged`) toggles: when off, WASAPI opens fine and
  returns **pure silence, no error, no prompt** for an unpackaged app. No reliable
  query API ⇒ **detection must be signal-based** (carry `peak_amplitude()==0`).
  Message → Settings → Privacy & security → Microphone → "Let desktop apps…".
  *TEST:* turn it off, record → app detects silence + specific message (not empty
  transcript); turn on → works.
- **PyInstaller `--windowed`: `sys.stdout`/`sys.stderr` are `None`** (since 5.7.0)
  — any `print()`/stderr write raises `AttributeError` and the windowed app **dies
  silently**. *Fix (very top of entry point):* if `sys.stdout is None:
  sys.stdout=open(os.devnull,'w')` (same for stderr); route logging to a file under
  `platformdirs` log dir; install a top-level excepthook → messagebox + log.

### 🟠 High
- **UIPI inbound** — F9 is silently ignored while an **elevated/admin window** is
  focused (Task Manager, admin apps, UAC desktop) unless the app is elevated
  (pynput #375). Decide policy; surface a one-time hint; don't corrupt state on a
  missed event.
- **UIPI outbound** — can't type into an elevated window from a non-elevated
  process (SendInput blocked). *Fallback:* clipboard + "paste with Ctrl+V; target is
  elevated" notification.
- **Blocking hotkey callback freezes ALL keyboard input** system-wide (pynput runs
  it on the OS input thread) and Windows removes the hook after
  `LowLevelHooksTimeout`. Callback must enqueue + return only.
- **Layout/Unicode mistyping** — `type()` maps via `VkKeyScan` on the current
  layout; AltGr/dead-key/non-US layouts mistype; **use clipboard-paste** (layout-
  independent) as default.
- **tkinter DPI-unaware → blurry windows** — on Windows call
  `ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)` (Per-Monitor v2, fallback
  `SetProcessDpiAwareness(2)`) once **before** creating `Tk()`, then
  `root.tk.call('tk','scaling', GetScaleFactorForDevice(0)/100)`.
- **Console window** — run via `pythonw`/windowed build (no console) but then
  tracebacks vanish → file logging + excepthook. **Every** `subprocess` (ffmpeg)
  needs `creationflags=subprocess.CREATE_NO_WINDOW` or it flashes a console.
- **Toasts need a registered AUMID** — a Win32 app can't reliably toast / persist
  in Action Center without an AppUserModelID. Use **Windows-Toasts**
  (`register_hkey_aumid`) with a stable reverse-DNS id; ship a real `.ico`.
- **First-open WASAPI latency clips short taps** — first `InputStream.start()` drops
  leading ms ⇒ short push-to-talk taps lose their first word / hit `is_too_short()`. Keep a
  **persistent stream** open (warm up once) rather than per-tap.
- **PortAudio + ffmpeg must be bundled + resolved via `sys._MEIPASS`** — sounddevice
  may look for PortAudio at the dev path; ffmpeg isn't auto-collected. Bundle both;
  invoke ffmpeg by absolute path (prefer `imageio-ffmpeg.get_ffmpeg_exe()`).
- **SmartScreen / Defender false positives** — global hook + SendInput + PyInstaller
  onefile ≈ keylogger heuristic; unsigned = "Windows protected your PC". Sign
  (OV/EV); prefer `--onedir`; no UPX; consider Nuitka.
- **Regression discipline** — without disciplined interfaces, `sys.platform` leaks
  into core and breaks Linux (see architecture section). High-priority process risk.

### 🟡 Medium
- **Toast callbacks need asyncio** which conflicts with tk/pystray threads — fire
  toasts non-blocking; marshal any click callback via `root.after()`. For info
  toasts, skip activation listeners.
- **Autostart** = HKCU `...\Run` value = `"<sys.executable>" --start-hidden`
  (quoted!); resolve via `sys.frozen`/`sys.executable`; reflect state by reading
  back.
- **Single-instance** = named mutex (`CreateMutexW` + `ERROR_ALREADY_EXISTS`),
  `Local\` prefix, created early, handle held for lifetime (auto-releases on crash,
  unlike a lock file).
- **sounddevice device indices unstable** (per host API MME/DirectSound/WASAPI, and
  across reboot/hotplug) — store device by **name + host API**, prefer WASAPI,
  resolve at open time.
- **`type()` drops chars / raises on non-BMP / types into wrong window if focus
  stolen** — clipboard-paste default; don't type while Settings window focused.
- **Tray startup timing / HiDPI blur** — set `icon.visible` in `run(setup=...)`;
  gate on `HAS_NOTIFICATION`; supply a ≥256×256 icon.
- **Missing hidden imports (winsdk/WinRT, pystray backend) in onefile; slow onefile
  extraction** — add `--hidden-import`; prefer `--onedir` for fast tray startup.

---

## Cross-cutting (all platforms)
- **paths** — replace hard-coded `~/.config`/`~/.cache` with `platformdirs`
  (`PlatformDirs('ba-ge', author)`); Linux still resolves to XDG (no
  regression). Print the resolved config path in "no API key" messages.
- **clipboard** — pyperclip: built-in on Win, pbcopy on mac; Linux keeps xclip
  (Wayland needs `wl-clipboard` or it raises `PyperclipException`). Wrap + fallback.
- **notifications** — `notify-send` is Linux-only (FileNotFound no-ops silently
  elsewhere) → Notifier interface; also mirror critical messages in tray/window so
  nothing is silent.
- **ffmpeg discovery** — prefer bundled (`imageio-ffmpeg`) → fall back to
  `shutil.which`; pass absolute path.
- **hotkey semantics** — re-validate `HoldDebouncer` press/release pairing per OS
  (Windows key-repeat differs; macOS differs again); `resolve_key` map is
  X11-flavored (cmd vs super, fn+F-keys reserved on macOS); pynput doesn't suppress
  the key by default (may leak into focused app).

---

## On-hardware testing checklist (hand to the Mac/Windows tester)

### macOS
1. Fresh Mac, **no permissions**: launch → F9 does nothing → app shows a dialog
   naming the missing permission (Input Monitoring) and deep-links to it.
2. Grant **Input Monitoring only** → hotkey fires but typing produces nothing (app
   says Accessibility missing). Grant **Accessibility** → text lands at cursor.
3. `tccutil reset Microphone <id>` → record → OS mic prompt appears with custom
   reason → Deny → app detects silence + helpful message → Grant → real audio.
4. Tray icon appears + menu opens; open Settings + Transcript windows while tray
   stays alive; open/close 10×; watch for `Calling Tcl from different apartment`.
5. Long paragraph w/ spaces + `café naïve — hello 世界 🙂` into TextEdit, Notes,
   Chrome, 10×: **zero** dropped spaces/chars (paste backend).
6. No Dock icon (`LSUIElement`); focus not stolen from the dictation target.
7. Second instance exits; `kill -9` first → relaunch recovers (no stale lock).
8. Signed `.app`: `spctl -a -vvv` = "accepted, Notarized"; mic prompt still appears
   post-signing; rebuild+reinstall keeps grants.

### Windows
1. Non-elevated: Notepad F9 → works. Task Manager focused F9 → nothing (UIPI). Run
   as admin → works over Task Manager.
2. Cursor in admin PowerShell → typing doesn't land → clipboard fallback + Ctrl+V
   works.
3. Turn off "Let desktop apps access your microphone" → record → app detects
   silence + specific message → turn on → works.
4. Long transcript (spaces, curly quotes, em-dash, accents, euro, emoji) into
   Notepad, browser, VS Code, Windows Terminal on **US + German** layouts: paste
   backend lossless; `type()` shows drops.
5. Tray menu items (Settings / Transcribe file… / Quit) clicked rapidly → no "main
   thread is not in main loop"; Quit fully exits (no leftover `pythonw.exe`).
6. HiDPI 150%/200%: Settings + transcript windows crisp, correctly sized; drag
   across monitors re-scales.
7. Shipped **windowed** exe: doesn't vanish silently; ffmpeg file-transcription runs
   with **no console flash**; forced exception → dialog + log file written.
8. Clean machine (no Python/PortAudio/ffmpeg): recording + file transcription work
   (bundled binaries via `_MEIPASS`).
9. Toast appears + persists in Action Center under the app's name (not "Python");
   `reg query HKCU\Software\Classes\AppUserModelId` shows the AUMID.
10. Second instance exits (named mutex); `kill` first → relaunch clean (no stale
    false-positive).
11. Download exe via browser (Mark-of-the-Web) → note SmartScreen wording;
    VirusTotal detection count; test signed vs unsigned, onedir vs onefile.

### Linux (regression gate — run in the dev env after each refactor step)
- Full pytest suite green (proves Linux backends unchanged behind interfaces).
- Manual hotkey → record → transcribe → type (xdotool/paste) identical to pre-port.
- `grep -rn sys.platform ba_ge/` → appears ONLY in the backend factory.
