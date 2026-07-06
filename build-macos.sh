#!/usr/bin/env bash
# Build a macOS .app — RUN ON macOS (Apple Silicon or Intel). UNVERIFIED starting
# point; the signing/notarization/plist details are in docs/PORTING.md (macOS).
#
# Prereqs: the python.org universal2 Python (NOT system/Homebrew Tk — see
# PORTING.md "Wrong Tk build"), and Xcode command line tools.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -U pyinstaller pynput sounddevice pyperclip pystray Pillow \
    platformdirs imageio-ffmpeg pyobjc-framework-ApplicationServices

# A .spec is required to inject the Info.plist keys PyInstaller's CLI can't set:
#   NSMicrophoneUsageDescription  -> or the mic silently records ZEROS (blocker)
#   LSUIElement = True            -> menu-bar-only, no Dock icon / focus stealing
cat > bage-macos.spec <<'SPEC'
# -*- mode: python ; coding: utf-8 -*-
a = Analysis(['packaging/entry.py'], pathex=['.'],
             hiddenimports=['pystray._darwin'], datas=[], binaries=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='Ba-Ge',
          console=False, disable_windowed_traceback=False)
app = BUNDLE(exe, name='Ba-Ge.app',
             bundle_identifier='com.ba-ge.app',
             info_plist={
                 'NSMicrophoneUsageDescription':
                     'Ba-Ge records your voice to transcribe it.',
                 'LSUIElement': True,
                 'CFBundleName': 'Ba-Ge',
             })
SPEC

pyinstaller --noconfirm --clean bage-macos.spec

cat <<EOF

Built dist/Ba-Ge.app (UNVERIFIED).
Next, per docs/PORTING.md (macOS):
  1. Grant Input Monitoring + Accessibility + Microphone (three separate TCC
     grants; all fail SILENTLY without them).
  2. Sign with a STABLE cert (grants reset on every unsigned rebuild):
       codesign --deep -o runtime --entitlements entitlements.plist \\
         -s "Developer ID Application: ..." "dist/Ba-Ge.app"
     Entitlements: com.apple.security.cs.allow-jit,
       allow-unsigned-executable-memory, disable-library-validation.
  3. Re-verify the mic prompt STILL appears after signing (classic regression).
  4. notarytool submit + stapler staple for Gatekeeper.
  5. Bundle a universal2 ffmpeg (imageio-ffmpeg or your own), signed.
  6. Consider swapping pystray for rumps on macOS (PORTING.md: pystray #138 GIL
     crash on Apple Silicon).
Run the docs/PORTING.md macOS testing checklist on real hardware.
EOF
