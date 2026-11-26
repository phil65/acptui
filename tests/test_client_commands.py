"""acptui's own slash commands and the Markdown export."""

from typing import TYPE_CHECKING

from acptui.config import Settings
from acptui.protocol.export import fence, session_markdown
from acptui.protocol.state import SessionState
from acptui.ui.app import AcpTui
from acptui.ui.screens import session as session_module
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.widgets.blocks import NoticeView, UserMessageView
from acptui.ui.widgets.prompt import CompletionList
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


def test_fence_outgrows_backticks() -> None:
    assert fence("a ``` b") == "````\na ``` b\n````"


def test_markdown_export_of_a_local_timeline() -> None:
    from aclientp.schema.v2 import TextContent

    state = SessionState("s1", "/work")
    state.title = "Fix_the_bug"
    state.add_local_user_message([TextContent(text="please **fix** it")])
    state.add_notice("warning", "Careful_now", "a *plain* note")
    text = session_markdown(state, "Demo agent")
    assert text.startswith("# Fix\\_the\\_bug\n\n*Demo agent · /work*")
    assert "## You\n\nplease **fix** it" in text
    assert "**Warning:** Careful\\_now\n\n> a \\*plain\\* note" in text


async def test_client_commands(
    demo_spec: AgentSpec, workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(session_module.platformdirs, "user_downloads_path", lambda: tmp_path)
    app = AcpTui(Settings(), workdir, demo_spec)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        completions = screen.query_one(CompletionList)
        await until(pilot, lambda: bool(screen.prompt.commands))
        await pilot.press(*"/acptui:ex")
        await until(pilot, lambda: completions.is_shown)
        assert [c.label for c in completions.completions] == ["/acptui:export"]
        assert "(acptui)" in str(completions.get_option_at_index(0).prompt)
        screen.prompt.load_text("")

        await send(pilot, "/echo hello")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await send(pilot, "/acptui:export")
        [path] = tmp_path.glob("acptui_*.md")
        exported = path.read_text(encoding="utf-8")
        assert "## You\n\n/echo hello" in exported
        assert "## Agent" in exported
        assert copied == [exported]
        assert len(screen.query(UserMessageView)) == 1  # the command was not sent to the agent

        await send(pilot, "/acptui:rename Better title")
        await until(
            pilot, lambda: screen.state is not None and screen.state.title == "Better title"
        )

        await send(pilot, "/acptui:about")
        await until(
            pilot, lambda: any(n.entry.title == "About acptui" for n in screen.query(NoticeView))
        )
        about = next(n for n in screen.query(NoticeView) if n.entry.title == "About acptui")
        assert "aclientp" in (about.entry.description or "")

        await send(pilot, "/acptui:clear")
        await until(pilot, lambda: not screen.query(UserMessageView))
        assert screen.state is not None
        assert screen.state.entries  # the history is kept

        await send(pilot, "/acptui:tab")
        await until(pilot, lambda: isinstance(app.screen, AgentPicker))
        await pilot.press("escape")
        assert app.screen is screen
        await send(pilot, "/acptui:close")
        await until(pilot, lambda: not app.tabs)
