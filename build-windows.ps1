# Build a Windows .exe — RUN ON Windows 11 in PowerShell. UNVERIFIED starting
# point; details/gotchas are in docs/PORTING.md (Windows).
#
# Notes baked in from PORTING.md:
#   --windowed  -> no console (but sys.stdout/stderr become None; the app installs
#                  an os.devnull guard + file logging at startup — keep that).
#   --onedir    -> faster tray startup + fewer AV false positives than --onefile.
#   hidden imports for pystray's win32 backend + the toast lib.
#   Bundle ffmpeg + PortAudio; resolve via sys._MEIPASS (imageio-ffmpeg helps).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -U pyinstaller pynput sounddevice pyperclip pystray Pillow `
    platformdirs imageio-ffmpeg windows-toasts pywin32

pyinstaller --noconfirm --clean --windowed --onedir --name "Ba-Ge" `
    --hidden-import "pystray._win32" `
    --collect-all "pystray" `
    --collect-all "windows_toasts" `
    --collect-binaries "sounddevice" `
    packaging\entry.py

Write-Host ""
Write-Host "Built dist\Ba-Ge\ (UNVERIFIED). Per docs/PORTING.md (Windows):"
Write-Host "  * First run: a startup guard must set sys.stdout/stderr if None (windowed)."
Write-Host "  * Mic 'Let desktop apps access your microphone' OFF => silent capture."
Write-Host "  * Toasts need a registered AppUserModelID (windows-toasts register_hkey_aumid)."
Write-Host "  * Sign the exe (OV/EV) to avoid SmartScreen/Defender false positives."
Write-Host "  * ffmpeg must be bundled + resolved from _MEIPASS (imageio-ffmpeg)."
Write-Host "Run the docs/PORTING.md Windows testing checklist on real hardware."
