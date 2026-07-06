#!/usr/bin/env bash
# Build a fully SELF-CONTAINED .deb: a uv-managed standalone CPython + PySide6 (Qt)
# + the app under /opt/ptt-dictation. Qt's wheels bundle their own libraries and
# QSystemTrayIcon speaks StatusNotifier natively, so this needs NO system Python,
# NO python3-tk, and NO PyGObject/gi — only a few X client libs from apt.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PKG=ptt-dictation
VERSION="${VERSION:-1.0.0}"
ARCH=amd64
PYVER=3.12
PREFIX=/opt/ptt-dictation

# Linux runtime deps (no sounddevice/imageio-ffmpeg — those are macOS/Windows only).
DEPS=(PySide6-Essentials pynput platformdirs pyperclip)

command -v uv >/dev/null || { echo "!! uv is required (https://astral.sh/uv)"; exit 1; }
command -v dpkg-deb >/dev/null || { echo "!! dpkg-deb is required (apt install dpkg)"; exit 1; }

echo "==> Locating standalone CPython $PYVER"
uv python install "$PYVER" >/dev/null 2>&1 || true
STORE="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
RT_PY="$(ls -d "$STORE"/cpython-"$PYVER".*-linux-*/bin/python"$PYVER" 2>/dev/null | sort -V | tail -1 || true)"
[ -x "$RT_PY" ] || { echo "!! no standalone CPython $PYVER under $STORE"; exit 1; }
RT_ROOT="$(dirname "$(dirname "$RT_PY")")"
echo "    runtime: $RT_ROOT"

STAGE="$DIR/build/deb"
rm -rf "$STAGE"
APPDIR="$STAGE$PREFIX"
mkdir -p "$APPDIR" "$STAGE/DEBIAN" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps"

echo "==> Copying + slimming bundled runtime"
cp -r "$RT_ROOT" "$APPDIR/runtime"
RTLIB="$APPDIR/runtime/lib/python$PYVER"
rm -rf "$RTLIB"/test "$RTLIB"/idlelib "$RTLIB"/turtledemo "$RTLIB"/lib2to3 \
       "$RTLIB"/ensurepip "$RTLIB"/tkinter "$APPDIR/runtime/include" 2>/dev/null || true
find "$APPDIR/runtime" -name "*.a" -delete 2>/dev/null || true
find "$APPDIR/runtime" -depth -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Installing Python deps into $PREFIX/site-packages"
uv pip install --python "$APPDIR/runtime/bin/python$PYVER" \
    --target "$APPDIR/site-packages" -q "${DEPS[@]}"

echo "==> Trimming Qt (drop the QML/Quick/Designer stack a widget app never uses)"
Q="$APPDIR/site-packages/PySide6"
rm -rf "$Q"/Qt/qml "$Q"/Qt/translations
rm -f "$Q"/Qt/lib/libQt6Quick*.so.6 "$Q"/Qt/lib/libQt6Qml*.so.6 \
      "$Q"/Qt/lib/libQt6Designer*.so.6 "$Q"/Qt/lib/libQt6QuickControls2*.so.6 \
      "$Q"/Qt/lib/libQt6QuickTemplates2*.so.6 "$Q"/Qt/lib/libQt6QuickWidgets*.so.6
rm -f "$Q"/QtQml*.so "$Q"/QtQuick*.so "$Q"/QtDesigner*.so "$Q"/QtOpenGL*.so "$Q"/QtQuick3D*.so
rm -rf "$Q"/Qt/plugins/qmltooling "$Q"/Qt/plugins/sqldrivers "$Q"/Qt/plugins/multimedia
find "$APPDIR/site-packages" -depth -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Copying app source"
cp -r "$DIR/ptt_dictation" "$APPDIR/ptt_dictation"
find "$APPDIR/ptt_dictation" -depth -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cp "$DIR/packaging/ptt-dictation.svg" \
   "$STAGE/usr/share/icons/hicolor/scalable/apps/ptt-dictation.svg"

cat > "$STAGE/usr/bin/ptt-dictation" <<EOF
#!/usr/bin/env bash
export PYTHONUTF8=1
export PYTHONPATH="$PREFIX:$PREFIX/site-packages\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PREFIX/runtime/bin/python$PYVER" -m ptt_dictation "\$@"
EOF
chmod +x "$STAGE/usr/bin/ptt-dictation"

cat > "$STAGE/usr/share/applications/ptt-dictation.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=PTT Dictation
GenericName=Voice Dictation
Comment=Hold a key to dictate with ElevenLabs Scribe
Exec=/usr/bin/ptt-dictation
Icon=/usr/share/icons/hicolor/scalable/apps/ptt-dictation.svg
Terminal=false
Categories=Utility;
Keywords=dictation;voice;speech;transcribe;microphone;scribe;
StartupNotify=false
Actions=Settings;

[Desktop Action Settings]
Name=Settings
Exec=/usr/bin/ptt-dictation --settings
EOF

INSTALLED_KB="$(du -sk "$STAGE$PREFIX" "$STAGE/usr" | awk '{s+=$1} END {print s}')"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: PTT Dictation <ptt-dictation@localhost>
Installed-Size: $INSTALLED_KB
Depends: xdotool, xclip, x11-utils, alsa-utils, ffmpeg, libxcb-cursor0
Recommends: libnotify-bin, ydotool
Section: utils
Priority: optional
Description: Push-to-talk voice dictation with ElevenLabs Scribe
 Hold a hotkey, speak, release — your words are transcribed by ElevenLabs
 Scribe and pasted at the cursor. Also transcribes audio files with speaker
 labels and timestamps. Bundles its own Python + Qt, so it does not touch the
 system Python and needs no python3-tk or PyGObject.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database /usr/share/applications 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst"

mkdir -p "$DIR/dist"
OUT="$DIR/dist/${PKG}_${VERSION}_${ARCH}.deb"
rm -f "$DIR/dist/${PKG}_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "==> Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "    Install:  sudo apt install $OUT"
echo "    Remove:   sudo apt remove $PKG"
