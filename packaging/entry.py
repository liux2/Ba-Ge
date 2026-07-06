"""Single entry point for compiled builds (PyInstaller/Nuitka)."""

import os
import sys

# Windowed builds (pythonw.exe / PyInstaller --windowed) leave sys.stdout and
# sys.stderr as None; any print()/stderr write then raises AttributeError and the
# app dies silently (see docs/PORTING.md, Windows blocker). Guard BEFORE importing
# anything that might write to them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from ptt_dictation.app import main  # noqa: E402

if __name__ == "__main__":
    main()
