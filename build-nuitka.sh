#!/usr/bin/env bash
# Build a single compiled binary with Nuitka: dist/ptt-dictation
# Produces a self-contained native executable (no Python needed on the target),
# far smaller than an Electron app. The target still needs the system tools
# `ydotool` and `arecord` (alsa-utils) installed — they talk to the kernel and
# cannot be bundled.
#
# Needs sudo for the one-time build dependencies. GTK/PyGObject bundling with
# Nuitka usually works out of the box; if the first run complains about an
# unknown "gi" plugin, drop the --enable-plugin=gi line (recent Nuitka auto-
# detects it). If you hit a missing typelib at runtime, see the README notes.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> Build dependencies (sudo apt)…"
sudo apt-get update
sudo apt-get install -y \
    python3-dev python3-venv python3-pip patchelf build-essential \
    python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1

echo "==> Installing Nuitka in a build venv (sees system PyGObject)…"
python3 -m venv --system-site-packages build/venv
build/venv/bin/pip install -U pip nuitka

echo "==> Compiling (a few minutes the first time)…"
mkdir -p dist
build/venv/bin/python -m nuitka \
    --standalone --onefile \
    --include-package=ptt_dictation \
    --enable-plugin=gi \
    --assume-yes-for-downloads \
    --output-filename=ptt-dictation \
    --output-dir=build/nuitka \
    --remove-output \
    packaging/entry.py

cp build/nuitka/ptt-dictation dist/ptt-dictation
chmod +x dist/ptt-dictation
echo
echo "Built dist/ptt-dictation ($(du -h dist/ptt-dictation | cut -f1))"
echo "Re-run ./install.sh to point the app launcher at the compiled binary."
echo "Reminder: target machines still need 'ydotool' and 'alsa-utils' installed."
