#!/usr/bin/env bash
# One-command installer for Ba-Ge on Linux (Debian/Ubuntu).
#
# Fully self-contained: a uv-managed standalone Python + PySide6 (Qt) in .venv.
# It never installs into or touches the system Python — no python3-venv/pip/tk,
# no PyGObject. Only a handful of X/CLI tools come from apt. Idempotent.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PYVER=3.12

# --- 1. System tools (no python3-* at all). Injection uses the bundled Python
#        (Qt clipboard + pynput paste keystroke), so no X/clipboard CLI is needed.
PKGS=(ffmpeg alsa-utils libnotify-bin libxcb-cursor0)
MISSING=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ ${#MISSING[@]} -eq 0 ]; then
    echo "==> System packages: all present."
elif command -v apt-get >/dev/null; then
    echo "==> Installing system packages: ${MISSING[*]}"
    sudo apt-get update && sudo apt-get install -y "${MISSING[@]}" \
        || echo "!! apt failed — run: sudo apt-get install -y ${MISSING[*]}"
else
    echo "!! Non-apt distro — install equivalents of: ${MISSING[*]}"
fi

# --- 2. uv (bootstrap into ~/.local/bin if missing) ---
if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv (standalone package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "!! uv install failed"; exit 1; }
    export PATH="$HOME/.local/bin:$PATH"
fi

# --- 3. Standalone Python venv + deps (system Python untouched) ---
if [ -x .venv/bin/python ] && \
   .venv/bin/python -c 'import sys,os; sys.exit(0 if os.sep+"uv"+os.sep in sys.base_prefix else 1)' 2>/dev/null; then
    echo "==> Reusing existing .venv (managed Python)"
else
    echo "==> Creating .venv on a managed Python $PYVER"
    uv venv --clear --python "$PYVER" .venv
fi
uv pip install -q --python .venv/bin/python -r requirements.txt

# --- 4. Register the app (launcher + .desktop + icon), user-level, no sudo ---
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
LAUNCHER="$BIN/ba-ge"
mkdir -p "$BIN" "$APPS" "$ICONS"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
export PYTHONUTF8=1
export PYTHONPATH="$DIR\${PYTHONPATH:+:\$PYTHONPATH}"
PY="$DIR/.venv/bin/python"
[ -x "\$PY" ] || PY=python3
exec "\$PY" -m ba_ge "\$@"
EOF
chmod +x "$LAUNCHER"

cp "$DIR/packaging/ba-ge.svg" "$ICONS/ba-ge.svg"

cat > "$APPS/ba-ge.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Ba-Ge
GenericName=Voice Dictation
Comment=Hold a key to dictate with ElevenLabs Scribe
Exec=$LAUNCHER
Icon=$ICONS/ba-ge.svg
Terminal=false
Categories=Utility;
Keywords=dictation;voice;speech;transcribe;microphone;scribe;
StartupNotify=false
Actions=Settings;

[Desktop Action Settings]
Name=Settings
Exec=$LAUNCHER --settings
EOF

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

cat <<EOF

Installed.  "Ba-Ge" now appears in Activities / the app grid.
  1. Launch it (click the icon).
  2. Right-click the tray icon -> Settings -> paste your ElevenLabs API key.
  3. Hold your hotkey (F9) and speak.

Command line: ba-ge   (add ~/.local/bin to PATH if needed)
Uses its own bundled Python + Qt — nothing was installed into your system Python.
EOF
