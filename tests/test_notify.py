"""Attention: bell, sounds, desktop and terminal notifications, the terminal title."""

from typing import TYPE_CHECKING

import pytest

from acptui import notify
from acptui.config import NotificationSettings, Settings
from acptui.ui import app as app_module
from acptui.ui.app import AcpTui
from acptui.ui.screens.permission import PermissionScreen
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    from acptui.config import AgentSpec


def test_terminal_notification_cleans_agent_text() -> None:
    text = notify.terminal_notification("Agent: done", "evil\x1b]0;pwned\x07 text")
    assert "\x1b]0;pwned" not in text
    assert "\x1b]777;notify;Agent: done;evil ]0,pwned  text\x1b\\" in text
    assert "\x1b]9;Agent: done: evil ]0;pwned  text\x1b\\" in text
    assert notify.terminal_title("a\nb") == "\x1b]0;a b\x1b\\"


def test_desktop_is_not_used_over_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert notify.desktop_notifier() is None
    assert not notify.use_desktop("auto")
    assert notify.use_desktop("desktop")
    assert not notify.use_desktop("terminal")


@pytest.mark.skipif(not notify.sys.platform.startswith("linux"), reason="Linux tools")
def test_linux_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands: list[list[str]] = []
    monkeypatch.setattr(notify, "_run", lambda command: commands.append(command) or True)
    assert notify.desktop_notify("Title", "-n message")
    assert commands[-1] == ["notify-send", "--app-name=acptui", "--", "Title", "-n message"]
    assert notify.play_sound("question")
    assert commands[-1][0] == "paplay"
    assert commands[-1][1].endswith("question.wav")


def test_sounds_are_packaged() -> None:
    from importlib.resources import files

    for sound in ("question", "turn-over"):
        assert files("acptui.data").joinpath(f"sounds/{sound}.wav").is_file()


async def test_questions_and_turn_ends_raise_alerts(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(AcpTui, "is_headless", property(lambda _: False))
    monkeypatch.setattr(app_module, "use_desktop", lambda _: True)

    async def fake_desktop(title: str, message: str) -> bool:
        sent.append(("desktop", title, message))
        return True

    monkeypatch.setattr(app_module, "notify_desktop", fake_desktop)
    monkeypatch.setattr(app_module, "play_sound", lambda s: sent.append(("sound", s)))
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "edit something")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        question = app.alerts[-1]
        assert question.kind == "question"
        assert question.title == "Demo agent: Allow the demo agent to edit this file?"
        assert not question.outside  # the user is looking at it
        assert screen.waiting
        assert app._terminal_title.startswith("● acptui · Demo agent")

        app.app_focus = False  # the terminal lost focus (a key press would give it back)
        dialog = app.screen
        assert isinstance(dialog, PermissionScreen)
        dialog.action_pick_index(0)
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await until(pilot, lambda: app.alerts[-1].kind == "turn_end")
        turn = app.alerts[-1]
        assert turn.outside
        assert turn.message == "end turn"
        await until(pilot, lambda: ("sound", "turn-over") in sent)
        assert ("desktop", "Demo agent: Turn ended", "end turn") in sent
        assert not app._terminal_title.startswith("●")


async def test_notifications_can_be_turned_off(demo_spec: AgentSpec, workdir: Path) -> None:
    settings = Settings(notifications=NotificationSettings(when="never", title=False))
    app = AcpTui(settings, workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo hi")
        app.app_focus = False
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await until(pilot, lambda: bool(app.alerts))
        assert not any(alert.outside for alert in app.alerts)
        assert app._terminal_title == ""


async def test_desktop_notifier_is_used_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import desktop_notifier

    shown: list[tuple[str, str]] = []

    class FakeNotifier:
        def __init__(self, app_name: str, app_icon: object) -> None:
            assert app_name == "acptui"

        async def send(self, title: str, message: str) -> str:
            shown.append((title, message))
            return "id"

    monkeypatch.setattr(desktop_notifier, "DesktopNotifier", FakeNotifier)
    monkeypatch.setattr(notify, "_native", None)
    monkeypatch.setattr(notify, "desktop_notify", lambda t, m: pytest.fail("no fallback"))
    assert await notify.notify_desktop("Agent: done", "end turn")
    assert shown == [("Agent: done", "end turn")]


async def test_a_failing_desktop_notifier_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import desktop_notifier

    class Broken:
        def __init__(self, app_name: str, app_icon: object) -> None:
            raise RuntimeError("no D-Bus session")

    commands: list[tuple[str, str]] = []
    monkeypatch.setattr(desktop_notifier, "DesktopNotifier", Broken)
    monkeypatch.setattr(notify, "_native", None)
    monkeypatch.setattr(notify, "desktop_notify", lambda t, m: commands.append((t, m)) or True)
    assert await notify.notify_desktop("T", "M")
    assert commands == [("T", "M")]
