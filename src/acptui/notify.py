"""Getting the user's attention outside the app: sounds, desktop and terminal notifications.

No Textual here: :class:`~acptui.ui.app.AcpTui` decides *when* (the agent asks for
permission or input, a turn ends, the agent fails) and calls these to do it. The sounds
are toad's (see ``NOTICE``), resampled to keep the package small.
"""

import asyncio
from importlib.resources import as_file, files
from importlib.util import find_spec
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from desktop_notifier import DesktopNotifier
import unicodedata


Sound = Literal["question", "turn-over"]
"""``question``: the agent waits for an answer; ``turn-over``: the agent finished."""

NotifyMethod = Literal["auto", "terminal", "desktop"]
"""Where notifications go: terminal escape sequences, the desktop's notification service
(desktop-notifier from ``acptui[notify]``, else ``notify-send`` / ``osascript``), or the
desktop when it is reachable (a local session with a notifier), else the terminal."""

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


def is_remote() -> bool:
    """Whether acptui runs over SSH (a desktop notification would pop up over there)."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def has_native_notifier() -> bool:
    """Whether desktop-notifier is installed (``pip install acptui[notify]``)."""
    return find_spec("desktop_notifier") is not None


def desktop_notifier() -> list[str] | None:
    """The command that shows desktop notifications here (``None``: none, or remote)."""
    if is_remote():
        return None
    if sys.platform == "darwin":
        return ["osascript"] if shutil.which("osascript") else None
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send"]
    return None


_native: DesktopNotifier | None = None


async def native_notify(title: str, message: str) -> bool:
    """Show a notification with desktop-notifier (D-Bus, macOS notification center, WinRT).

    Whether it was sent; ``False`` when the extra is not installed or the backend fails
    (the caller falls back to :func:`desktop_notify`).
    """
    global _native
    try:
        from desktop_notifier import DesktopNotifier
    except ImportError:
        return False
    try:
        if _native is None:
            _native = DesktopNotifier(app_name="acptui", app_icon=None)
        await _native.send(title=title, message=message)
    except Exception:  # noqa: BLE001 - any backend failure: use the fallback
        return False
    return True


async def notify_desktop(title: str, message: str) -> bool:
    """A desktop notification: desktop-notifier if installed, else the notifier command."""
    if await native_notify(title, message):
        return True
    return await asyncio.to_thread(desktop_notify, title, message)


def desktop_notify(title: str, message: str) -> bool:
    """Show a desktop notification with a command; whether it was shown (blocks)."""
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
            return not is_remote() and (has_native_notifier() or desktop_notifier() is not None)


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
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0
