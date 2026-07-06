from enum import Enum


class State(Enum):
    """The four states of a dictation session."""

    IDLE = "idle"            # waiting for the hotkey
    RECORDING = "recording"  # hotkey held, capturing audio
    BUSY = "busy"            # transcribing + injecting
    ERROR = "error"          # transient failure (auto-reverts to IDLE)
