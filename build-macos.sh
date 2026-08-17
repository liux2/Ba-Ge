#!/usr/bin/env bash
# Build a macOS .app — RUN ON macOS (Apple Silicon or Intel). Produces an UNSIGNED
# bundle; signing/notarization/plist details are in docs/PORTING.md (macOS).
#
# Stack matches the app: PySide6 (Qt) + pynput + sounddevice + pyperclip +
# platformdirs. NO pystray / GTK / Tk. Builds from the project's uv-managed
# standalone .venv (created by the macOS install steps / `uv venv --python 3.12`).
#
# ⚠️ iCloud: if the project lives in an iCloud-synced folder (~/Documents or
# ~/Desktop) with "Optimize Mac Storage" ON, the OS evicts file bodies to dataless
# placeholders and re-evicts them faster than PyInstaller can copy the (large) Qt
# tree — builds fail with `TimeoutError: [Errno 60]` or truncated Mach-O
# (`struct.error: 4 bytes`). The workarounds below (pre-materialize + disable
# shutil fcopyfile) help but aren't bulletproof. RELIABLE fix: build from a copy in
# a NON-synced dir, e.g.:
#   cp -R . /tmp/ba-ge-build && cd /tmp/ba-ge-build && PY=/tmp/bage-venv/bin/python ./build-macos.sh
# Copy .git along with it (cp -R . does; an rsync with --exclude .git does not) so
# the build can stamp its source ref — otherwise pass BAGE_SOURCE_REF=... instead.
# (or turn off "Optimize Mac Storage" for the volume).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || { echo "!! $PY not found — create the venv first: uv venv --python 3.12 .venv && uv pip install -r requirements.txt"; exit 1; }

# App deps into the venv (idempotent) + PyInstaller. imageio-ffmpeg bundles a
# universal2 ffmpeg for file transcription (no system ffmpeg on a clean Mac). The
# venv is uv-managed (no bundled pip), so install via `uv pip` when available.
if command -v uv >/dev/null 2>&1; then
    PIP=(uv pip install --python "$PY")
else
    PIP=("$PY" -m pip install)
fi
"${PIP[@]}" -U pyinstaller imageio-ffmpeg
"${PIP[@]}" -r requirements.txt

# iCloud "Optimize Mac Storage" evicts file bodies to dataless placeholders; a
# straight copy then stalls (Errno 60) materializing them. Force-download the Qt
# tree before PyInstaller reads it. (Harmless no-op when files are already local.)
find .venv/lib/python*/site-packages/PySide6 -type f -print0 2>/dev/null \
    | xargs -0 -P 8 -I{} sh -c 'cat "{}" > /dev/null 2>&1' || true

# A .spec injects the Info.plist keys the CLI can't set:
#   NSMicrophoneUsageDescription -> or the mic silently records ZEROS (blocker)
#   LSUIElement = True           -> menu-bar-only, no Dock icon / focus stealing
# collect_all('PySide6') pulls the Qt libs + plugins (incl. platforms/cocoa); we
# then drop the QML/Quick/Designer/3D stack a widget app never uses (mirrors
# build-deb.sh) — smaller bundle, and far fewer files to copy.
cat > bage-macos.spec <<'SPEC'
# -*- mode: python ; coding: utf-8 -*-
# Building from an iCloud-managed folder (~/Documents with "Optimize Mac Storage"):
# macOS `fcopyfile` (shutil's fast clone path) times out (Errno 60) cloning Qt files
# off the synced volume. Force shutil's plain read/write copy, which works fine.
import shutil
shutil._HAS_FCOPYFILE = False

from PyInstaller.utils.hooks import collect_all

qt_datas, qt_bins, qt_hidden = collect_all('PySide6')

# Path fragments (in the on-disk source path OR the bundle dest) to exclude.
_DROP = ('/qml/', '/translations/', '/include/', 'plugins/qmltooling',
         'plugins/sqldrivers', 'plugins/designer', 'QtQuick', 'QtQml',
         'QtDesigner', 'QtQuick3D', 'Qt3D', 'QtMultimedia', 'QtWebEngine',
         'QtCharts', 'QtDataVisualization', 'QtGraphs', 'QtPdf', 'QtSensors',
         'QtWebSockets', 'QtWebChannel', 'QtRemoteObjects', 'QtScxml', 'QtSql',
         'QtSpatialAudio', 'QtBluetooth', 'QtNfc', 'QtPositioning', 'QtSerialPort')
def _keep(entry):
    joined = (str(entry[0]) + '|' + str(entry[1]))
    return not any(frag in joined for frag in _DROP)

qt_datas = [e for e in qt_datas if _keep(e)]
qt_bins = [e for e in qt_bins if _keep(e)]
qt_hidden = [m for m in qt_hidden if not any(m.endswith('.' + f) for f in
             ('QtQml', 'QtQuick', 'QtDesigner', 'QtMultimedia', 'QtWebEngineCore'))]

a = Analysis(['packaging/entry.py'], pathex=['.'],
             hiddenimports=qt_hidden + ['ba_ge'],
             datas=qt_datas, binaries=qt_bins,
             excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtDesigner',
                       'PySide6.QtQuick3D', 'PySide6.QtMultimedia', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Ba-Ge',
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Ba-Ge')  # onedir: fast tray startup
app = BUNDLE(coll, name='Ba-Ge.app',
             icon='packaging/ba-ge.icns',  # Finder/Dock/DMG icon (matches Linux)
             bundle_identifier='com.ba-ge.app',
             info_plist={
                 'NSMicrophoneUsageDescription':
                     'Ba-Ge records your voice to transcribe it.',
                 'LSUIElement': True,
                 'CFBundleName': 'Ba-Ge',
             })
SPEC

"$PY" -m PyInstaller --noconfirm --clean bage-macos.spec

# --- Sign + deploy ------------------------------------------------------------
# TCC (Input Monitoring / Accessibility / Microphone) keys grants to the app's
# code signature. An ad-hoc/unsigned app gets a NEW identity on every rebuild, so
# the grants reset each time and dictation silently breaks. Signing with a stable
# identity (an Apple Development cert is enough for your own machine) makes the
# designated requirement — and therefore the grants — persist across rebuilds.
#
# Pick the identity from $SIGN_ID, else the first "Apple Development" one. Signing
# also needs the mic entitlement (com.apple.security.device.audio-input) + hardened
# runtime, or macOS never shows the mic prompt. iCloud stamps un-strippable xattrs
# on files under ~/Documents that make codesign refuse, so we sign a copy in a
# non-iCloud temp dir and deploy the signed bundle to ~/Applications (a stable,
# non-iCloud home — the app must live there for grants to stick).
SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
    | grep -m1 'Apple Development' | sed -E 's/.*"(.*)"$/\1/')}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/Applications}"
ENTITLEMENTS="$(pwd)/packaging/entitlements.plist"

# The deployed bundle is meant to be the ONLY copy on the machine, so nothing else
# records what was built — hence provenance. $BAGE_SOURCE_REF overrides for builds
# from a tree without .git (the /tmp recipe above copies it, so this normally
# resolves on its own).
BUILT_AT="$(date '+%Y-%m-%d %H:%M:%S')"
SOURCE_REF="${BAGE_SOURCE_REF:-$(git describe --tags --always --dirty 2>/dev/null \
    || echo unknown)}"

if [ -n "$SIGN_ID" ] && [ -f "$ENTITLEMENTS" ]; then
    echo "==> Signing with: $SIGN_ID"
    STAGE="$(mktemp -d)/Ba-Ge.app"
    ditto dist/Ba-Ge.app "$STAGE"
    find "$STAGE" -print0 | xargs -0 xattr -c 2>/dev/null || true  # iCloud/Finder detritus
    # Stamp provenance INTO the bundle, before signing so it's covered by the
    # signature and can't be edited without breaking it. Since dist/ is deleted
    # below, this is how the installed app stays self-describing:
    #   cat ~/Applications/Ba-Ge.app/Contents/Resources/build-info.txt
    cat > "$STAGE/Contents/Resources/build-info.txt" <<INFO
source_ref  $SOURCE_REF
built_at    $BUILT_AT
built_by    $(id -un)@$(hostname -s)
identity    $SIGN_ID
INFO
    codesign --force --deep --options runtime --entitlements "$ENTITLEMENTS" \
        -s "$SIGN_ID" "$STAGE"
    codesign --verify "$STAGE" >/dev/null 2>&1 \
        && echo "    signature OK" \
        || echo "    note: --verify warns on bundled Qt sub-apps (harmless; app runs)"
    mkdir -p "$DEPLOY_DIR"
    pkill -f "Ba-Ge.app/Contents/MacOS/Ba-Ge" 2>/dev/null || true
    rm -rf "$DEPLOY_DIR/Ba-Ge.app"
    ditto "$STAGE" "$DEPLOY_DIR/Ba-Ge.app"
    rm -rf "$(dirname "$STAGE")"

    # ---- compilation history: keep the record, not the binary ----
    # A build usually runs from a throwaway copy (see the iCloud note at the top),
    # so this lives outside the build tree or it would be deleted with it. One line
    # per deploy is enough to tie the installed app to a commit and rebuild it —
    # which is the whole point of not keeping a second 200 MB bundle around.
    HISTORY="${BAGE_HISTORY:-$HOME/Library/Application Support/ba-ge/build-history.tsv}"
    mkdir -p "$(dirname "$HISTORY")"
    [ -s "$HISTORY" ] \
        || printf 'built_at\tsource_ref\texe_sha256\tsize\tidentity\n' > "$HISTORY"
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$BUILT_AT" "$SOURCE_REF" \
        "$(shasum -a 256 "$DEPLOY_DIR/Ba-Ge.app/Contents/MacOS/Ba-Ge" | cut -d' ' -f1)" \
        "$(du -sh "$DEPLOY_DIR/Ba-Ge.app" | cut -f1)" \
        "$SIGN_ID" >> "$HISTORY"

    # Drop PyInstaller's staging trees (~430 MB). The deployed bundle is then the
    # only copy of the app on the machine, so a stale dist/Ba-Ge.app can never be
    # launched by mistake or drift out of sync with what's installed. KEEP_BUILD=1
    # retains them when you need to inspect the raw PyInstaller output.
    if [ -z "${KEEP_BUILD:-}" ]; then
        rm -rf dist build
    fi

    cat <<EOF

Built + signed: $DEPLOY_DIR/Ba-Ge.app  (the only copy — dist/ removed)
  Source   : $SOURCE_REF
  History  : $HISTORY
  Identity : $SIGN_ID
  Runtime  : hardened, entitlements = packaging/entitlements.plist (incl. mic)
Its signature is stable, so your Input Monitoring / Accessibility / Microphone
grants persist across rebuilds. First build with a new cert: grant the three
permissions once (tray → Permissions…), then never again.
The bundle carries its own provenance — Contents/Resources/build-info.txt — and
every deploy is appended to the history file above; rebuild any past version with
\`git checkout <source_ref>\` then re-run this script.
For Gatekeeper on other Macs: notarytool submit + stapler staple.
EOF
else
    cat <<EOF

Built dist/Ba-Ge.app (UNSIGNED — no signing identity found).
Set one up so TCC grants survive rebuilds:
  * Have an "Apple Development" cert (Xcode › Settings › Accounts › Manage
    Certificates › +), then re-run — it's auto-detected. Or: SIGN_ID="..." ./build-macos.sh
Without it, Input Monitoring/Accessibility/Microphone reset on every rebuild.
EOF
fi
