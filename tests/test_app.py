"""UI tests with Textual's pilot, against the demo agent (a real subprocess)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import OptionList

from acptui.config import Settings
from acptui.ui.app import AcpTui
from acptui.ui.screens.dialogs import ConfigScreen, HelpScreen, SessionListScreen
from acptui.ui.screens.log import LogScreen
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.screens.session import ErrorPanel, SessionScreen
from acptui.ui.widgets.blocks import (
    AgentMessageView,
    NoticeView,
    PlanView,
    ThoughtView,
    ToolCallView,
    UserMessageView,
)
from acptui.ui.widgets.diff import DiffView
from acptui.ui.widgets.prompt import CompletionList
from acptui.ui.widgets.terminal import TerminalView
from tests.helpers import wait_for


if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot

    from acptui.config import AgentSpec


SIZE = (100, 40)


def session(app: AcpTui) -> SessionScreen:
    """The session screen (below any dialogs)."""
    return next(s for s in app.screen_stack if isinstance(s, SessionScreen))


async def until(pilot: Pilot[None], condition: object, timeout: float = 20) -> None:
    """Let the app run until ``condition()`` holds."""
    assert callable(condition)

    async def check() -> bool:
        await pilot.pause(0.05)
        return bool(condition())

    import asyncio

    async with asyncio.timeout(timeout):
        while not await check():
            pass


def stop_reason(screen: SessionScreen) -> str | None:
    """Why the last turn stopped."""
    return screen.state.stop_reason if screen.state is not None else None


async def connected(pilot: Pilot[None], app: AcpTui) -> SessionScreen:
    """Wait until the session screen has a session."""
    await until(
        pilot, lambda: isinstance(app.screen, SessionScreen) and app.screen.state is not None
    )
    return session(app)


async def send(pilot: Pilot[None], text: str) -> None:
    """Type a prompt and send it."""
    app = pilot.app
    assert isinstance(app, AcpTui)
    prompt = session(app).prompt
    prompt.load_text(text)
    prompt.move_cursor(prompt.document.end)
    await pilot.press("enter")


async def test_tour_renders_every_kind_of_block(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "show me the tour")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        dialog = app.screen
        assert isinstance(dialog, PermissionScreen)
        assert dialog.query(DiffView)
        await pilot.press("1")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.3)

        assert screen.query(UserMessageView)
        assert screen.query(ThoughtView)
        assert screen.query(PlanView)
        tools = list(screen.query(ToolCallView))
        assert [t.entry.status for t in tools] == ["completed"] * 3
        terminal = screen.query_one(TerminalView)
        assert terminal.terminal.exited
        assert screen.query(DiffView)
        answer = list(screen.query(AgentMessageView))[-1]
        assert "nothing was written to disk" in answer.source
        thought = screen.query_one(ThoughtView)
        assert thought.title == "Thought"
        assert thought.collapsed


async def test_reject_and_escape_in_permission_dialog(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "first")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.press("escape")  # rejects once
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        assert screen.state is not None
        assert screen.state.tool_calls["edit-1"].status == "failed"


async def test_slash_and_path_completion(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot, app)
        prompt = session(app).prompt
        completions = session(app).query_one(CompletionList)
        await until(pilot, lambda: bool(prompt.commands))

        await pilot.press("/", "e", "c")
        await until(pilot, lambda: completions.is_shown)
        assert [c.label for c in completions.completions] == ["/echo <text to echo>"]
        await pilot.press("tab")
        assert prompt.text == "/echo "

        prompt.load_text("")
        await pilot.press(*"read @sr")
        await until(pilot, lambda: completions.is_shown)
        await pilot.press("tab")  # a directory: completion continues
        assert prompt.text == "read @src/"
        await until(pilot, lambda: [c.label for c in completions.completions] == ["src/main.py"])
        await pilot.press("enter")  # accepts the completion, does not send
        assert prompt.text == "read @src/main.py "
        assert not completions.is_shown
        await pilot.press("escape")


async def test_echo_with_attachment_and_history(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo hello @README.md")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)

        user = screen.query_one(UserMessageView)
        assert "README.md" in str(user.render())
        answer = screen.query_one(AgentMessageView)
        assert answer.source.startswith("hello")
        prompt = session(app).prompt
        assert prompt.text == ""
        await pilot.press("up")
        assert prompt.text == "/echo hello @README.md"


async def test_cancel_turn(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/slow")
        await until(pilot, lambda: screen.state is not None and screen.state.running)
        await pilot.press("escape")
        await until(pilot, lambda: stop_reason(screen) == "cancelled")
        await until(
            pilot, lambda: any(n.entry.title == "Cancelled" for n in screen.query(NoticeView))
        )


async def test_failed_turn_shows_error(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/fail")
        await until(pilot, lambda: bool(screen.query(NoticeView)))
        notice = screen.query_one(NoticeView)
        assert notice.has_class("-error")
        assert notice.entry.description == "Demo failure: the model is unavailable"


async def test_crash_shows_exit_code_and_stderr(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/crash")
        await until(pilot, lambda: bool(screen.query(ErrorPanel)))
        text = str(screen.query_one(ErrorPanel).render())
        assert "exit code 3" in text
        assert "simulated crash" in text
        assert not screen.connected

        await pilot.press("ctrl+l")
        assert isinstance(app.screen, LogScreen)
        await pilot.press("escape")

        await pilot.press("f5")  # restart
        await until(pilot, lambda: screen.connected and screen.state is not None)
        assert not screen.query(ErrorPanel)


async def test_v1_agent_gets_a_clear_message(v1_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, v1_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = session(app)
        await until(pilot, lambda: bool(screen.query(ErrorPanel)))
        panel = screen.query_one(ErrorPanel)
        assert panel.border_title == "Unsupported agent"
        assert "old-agent speaks ACP v1" in str(panel.render())


async def test_missing_agent(missing_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, missing_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = session(app)
        await until(pilot, lambda: bool(screen.query(ErrorPanel)))
        assert "pip install it" in str(screen.query_one(ErrorPanel).render())
        await send(pilot, "hello")  # not connected: nothing is sent
        assert not screen.query(UserMessageView)


async def test_options_mode_and_palette(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await until(pilot, lambda: screen.state is not None and bool(screen.state.config_options))

        def mode() -> object:
            assert screen.state is not None
            return next(
                o for o in screen.state.config_options if o.config_id == "mode"
            ).model_dump()["current_value"]

        await pilot.press("shift+tab")
        await until(pilot, lambda: mode() == "auto")

        await pilot.press("ctrl+o")
        assert isinstance(app.screen, ConfigScreen)
        await pilot.press("enter")  # the mode option: shows its values
        await pilot.press("up", "enter")  # "Ask"
        await until(pilot, lambda: mode() == "ask")
        assert "Mode: Ask" in str(screen.status.query_one("#status-right").render())


async def test_new_and_resume_session(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo remember me")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        assert screen.state is not None
        first = screen.state.session_id

        await pilot.press("ctrl+n")
        await until(pilot, lambda: screen.state is not None and screen.state.session_id != first)
        assert not screen.query(UserMessageView)

        await pilot.press("ctrl+r")
        assert isinstance(app.screen, SessionListScreen)
        options = app.screen.query_one(OptionList)
        await until(pilot, lambda: options.option_count > 0)
        await pilot.press("enter")
        await until(pilot, lambda: screen.state is not None and screen.state.session_id == first)
        await until(pilot, lambda: len(screen.query(AgentMessageView)) == 1)
        assert screen.query_one(AgentMessageView).source.startswith("remember me")


async def test_picker_and_help(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir)
    async with app.run_test(size=(80, 24)) as pilot:
        assert isinstance(app.screen, AgentPicker)
        picker_list = app.screen.query_one(OptionList)
        ids: list[str | None] = [
            picker_list.get_option_at_index(i).id for i in range(picker_list.option_count)
        ]
        assert "demo" in ids
        picker_list.highlighted = ids.index("demo")
        await pilot.press("enter")
        await connected(pilot, app)
        await pilot.press("f1")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, SessionScreen)


async def test_quit_stops_the_agent(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        connection = screen.connection
        assert connection is not None
        await pilot.press("ctrl+q")
    await wait_for(lambda: connection.exit_code is not None)
