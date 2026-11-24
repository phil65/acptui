"""Getting the user's attention outside the app: sounds, desktop and terminal notifications.

No Textual here: :class:`~acptui.ui.app.AcpTui` decides *when* (the agent asks for
permission or input, a turn ends, the agent fails) and calls these to do it. The sounds
are toad's (see ``NOTICE``), resampled to keep the package small.
"""

from __future__ import annotations

from importlib.resources import as_file, files
import os
import shutil
import subprocess
import sys
from typing import Literal
import unicodedata


Sound = Literal["question", "turn-over"]
"""``question``: the agent waits for an answer; ``turn-over``: the agent finished."""

NotifyMethod = Literal["auto", "terminal", "desktop"]
"""Where notifications go: terminal escape sequences, the desktop's notification service, or
the desktop when it is reachable (a local session with a notifier), else the terminal."""

TIMEOUT = 15
"""Seconds a sound player or notifier may take."""


def sound_player() -> list[str] | None:
    """A command line that plays a WAV file (the file is appended), if one is installed."""
    if sys.platform == "darwin":
        return ["afplay"] if shutil.which("afplay") else None
    for player in (["paplay"], ["pw-play"], ["aplay", "-q"]):
        if shutil.which(player[0]):
            return player
    return None


def play_sound(sound: Sound) -> bool:
    """Play a sound and wait until it ended (call it from a thread). Whether it played."""
    with as_file(files("acptui.data").joinpath(f"sounds/{sound}.wav")) as path:
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return True
        player = sound_player()
        if player is None:
            return False
        return _run([*player, str(path)])


def desktop_notifier() -> list[str] | None:
    """The command that shows desktop notifications here (``None``: none, or remote)."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None  # a notification would pop up on the remote machine
    if sys.platform == "darwin":
        return ["osascript"] if shutil.which("osascript") else None
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send"]
    return None


def desktop_notify(title: str, message: str) -> bool:
    """Show a desktop notification (call it from a thread). Whether it was shown."""
    notifier = desktop_notifier()
    if notifier is None:
        return False
    if notifier == ["osascript"]:
        script = f"display notification {_applescript(message)} with title {_applescript(title)}"
        return _run(["osascript", "-e", script])
    return _run(["notify-send", "--app-name=acptui", "--", title, message])


def use_desktop(method: NotifyMethod) -> bool:
    """Whether notifications go to the desktop (else to the terminal)."""
    match method:
        case "desktop":
            return True
        case "terminal":
            return False
        case _:
            return desktop_notifier() is not None


def terminal_notification(title: str, message: str) -> str:
    """Escape sequences that make the terminal show a notification.

    OSC 777 (foot, WezTerm, Ghostty, Konsole, urxvt, VTE), OSC 9 (iTerm2, Windows
    Terminal, kitty, Ghostty) and OSC 99 (kitty); terminals ignore the ones they do not
    know. Agent text is cleaned of control characters first.
    """
    title, message = clean(title), clean(message)
    body = f"{title}: {message}" if message else title
    return (
        f"\x1b]777;notify;{title.replace(';', ',')};{message.replace(';', ',')}\x1b\\"
        f"\x1b]9;{body}\x1b\\"
        f"\x1b]99;i=acptui:d=0;{title}\x1b\\"
        f"\x1b]99;i=acptui:d=1:p=body;{message}\x1b\\"
    )


def terminal_title(title: str) -> str:
    """The escape sequence that sets the terminal window (and tab) title."""
    return f"\x1b]0;{clean(title)}\x1b\\"


def clean(text: str) -> str:
    """Text without control characters (they could end an escape sequence early)."""
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
