"""UI tests for the rest of the v2 surface: subagents, elicitation, sessions, auth, providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Input, OptionList, Switch

from acptui.config import Settings
from acptui.protocol.connection import SessionSetup
from acptui.protocol.state import MessageEntry
from acptui.ui.app import AcpTui, session_commands
from acptui.ui.screens.dialogs import ConfirmScreen, ForkScreen, SessionListScreen, TextInputScreen
from acptui.ui.screens.elicitation import FormElicitationScreen, UrlElicitationScreen
from acptui.ui.screens.log import LogScreen
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.screens.providers import ProviderEditScreen, ProvidersScreen, parse_headers
from acptui.ui.screens.session import ErrorPanel, SessionScreen, SubagentScreen
from acptui.ui.widgets.blocks import (
    AgentMessageView,
    CompactionView,
    NoticeView,
    PlanView,
    SessionMessageView,
    SubagentView,
    ToolCallHeader,
    ToolCallView,
    UserMessageView,
)
from acptui.ui.widgets.diff import DiffView
from tests.test_app import SIZE, connected, send, session, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


def agent_texts(screen: SessionScreen) -> list[str]:
    """Sources of the agent messages."""
    return [view.source for view in screen.query(AgentMessageView)]


async def test_subagent_view_and_screen(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/subagent")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.3)

        view = screen.query_one(SubagentView)
        assert view.entry.title == "Researcher"
        messages = list(screen.query(SessionMessageView))
        assert len(messages) == 2
        assert "this session → Researcher" in str(messages[0].render())
        assert agent_texts(screen)[-1] == "The researcher is done."  # streamed once, no duplicates

        view.focus()
        await pilot.press("enter")
        assert isinstance(app.screen, SubagentScreen)
        child = app.screen
        await until(pilot, lambda: bool(child.query(AgentMessageView)))
        assert child.query_one(AgentMessageView).source == "I found the README in the project root."
        await pilot.press("escape")
        assert app.screen is screen


async def test_form_elicitation(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/elicit")
        await until(pilot, lambda: isinstance(app.screen, FormElicitationScreen))
        form = app.screen
        assert isinstance(form, FormElicitationScreen)

        await pilot.press("ctrl+s")  # name is required
        assert isinstance(app.screen, FormElicitationScreen)
        assert "name is required" in str(form.query_one("#errors").render())

        name = form.fields["name"]
        assert isinstance(name, Input)
        name.value = "acptui"
        ratio = form.fields["ratio"]
        assert isinstance(ratio, Input)
        ratio.value = "5"
        await pilot.press("ctrl+s")
        assert "ratio: at most 1" in str(form.query_one("#errors").render())
        ratio.value = "0.5"
        public = form.fields["public"]
        assert isinstance(public, Switch)
        public.value = False
        await pilot.press("ctrl+s")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)

        text = agent_texts(screen)[-1]
        assert '"action":"accept"' in text
        assert '"name":"acptui"' in text
        assert '"ratio":0.5' in text
        assert '"public":false' in text
        assert '"license":"MIT"' in text
        assert '"count":3' in text


async def test_url_elicitation_closes_on_completion(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/url")  # the dialog closes itself when the agent completes it
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionScreen)
        assert agent_texts(screen)[-1] == "URL elicitation: accept"


async def test_command_permission_and_cancel_tool(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/command")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        assert "rm -rf build" in str(
            app.screen.query_one("#details").query("Static").first().render()
        )
        await pilot.press("n")  # first reject option: "Never" (reject_always)
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)
        assert agent_texts(screen)[-1] == "Permission outcome: never"

        assert screen.state is not None
        screen.state.stop_reason = None
        await send(pilot, "/cancel-tool")
        await until(pilot, lambda: screen.state is not None and "long-1" in screen.state.tool_calls)
        await until(pilot, lambda: bool(screen.query(ToolCallView)))
        header = list(screen.query(ToolCallHeader))[-1]
        header.focus()
        await pilot.press("x")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        assert screen.state.tool_calls["long-1"].status == "cancelled"


async def test_media_plans_notices_compaction_suggestions(
    demo_spec: AgentSpec, workdir: Path
) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        for command in ("/media", "/plans", "/notice", "/compact", "/ext", "/suggest"):
            assert screen.state is not None
            screen.state.stop_reason = None
            await send(pilot, command)
            await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.3)

        media = agent_texts(screen)[0]
        assert "image: image/png" in media
        assert "**Project readme** `README.md`" in media
        assert "only for the model" not in media
        assert len(screen.query(DiffView)) == 2
        tool = next(t for t in screen.query(ToolCallView) if t.entry.tool_call_id == "media-tool")
        tool.query_one(ToolCallHeader).focus()
        await pilot.press("r")  # raw input / output
        await pilot.pause(0.1)
        assert '"prompt": "a dot"' in "".join(str(w.render()) for w in tool.query(".tool-text"))

        plans = list(screen.query(PlanView))
        assert str(plans[0].border_subtitle).endswith("PLAN.md")
        assert plans[1].has_class("-removed")
        assert [n.entry.severity for n in screen.query(NoticeView)] == ["info", "warning", "error"]
        assert "completed" in str(screen.query_one(CompactionView).render())

        assert screen.prompt.suggestion == "/echo try the suggestion"
        screen.prompt.focus()
        await pilot.press("tab")
        assert screen.prompt.text == "/echo try the suggestion"
        assert any("_demo/hello" in line.text for line in app.agent_log.lines)


async def test_fork_rename_and_session_list(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo forkable")
        await until(
            pilot,
            lambda: (
                stop_reason(screen) == "end_turn" and bool(screen.state and screen.state.cursors)
            ),
        )
        assert screen.state is not None
        original = screen.state.session_id

        await pilot.press("f2")
        assert isinstance(app.screen, TextInputScreen)
        app.screen.query_one(Input).value = "My title"
        await pilot.press("enter")
        await until(pilot, lambda: screen.state is not None and screen.state.title == "My title")

        await pilot.press("ctrl+f")
        assert isinstance(app.screen, ForkScreen)
        options = app.screen.query_one(OptionList)
        assert options.option_count == 2  # whole session + one cursor
        options.highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.state is not None and screen.state.session_id != original)
        # The fork shows its history up to the cursor
        assert any(
            isinstance(e, MessageEntry) and "forkable" in e.text for e in screen.state.entries
        )
        await until(pilot, lambda: bool(screen.query(UserMessageView)))

        await pilot.press("ctrl+r")
        assert isinstance(app.screen, SessionListScreen)
        listing = app.screen
        await until(pilot, lambda: len(listing.sessions) >= 1)
        assert any(info.title == "My title" for info in listing.sessions.values())
        await pilot.press("a")  # archive the highlighted session
        await until(
            pilot,
            lambda: (
                len(listing.sessions) == 0 or all(not s.archived for s in listing.sessions.values())
            ),
        )
        await pilot.press("h")  # show archived
        await until(pilot, lambda: any(info.archived for info in listing.sessions.values()))
        await pilot.press("d")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await until(pilot, lambda: original not in listing.sessions)
        await pilot.press("escape")


async def test_auth_required_then_login(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPTUI_DEMO_REQUIRE_AUTH", "1")
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = session(app)
        await until(pilot, lambda: bool(screen.query(ErrorPanel)))
        panel = screen.query_one(ErrorPanel)
        assert panel.border_title == "Authentication required"
        assert "Demo login" in str(panel.render())
        await until(pilot, lambda: screen.status.account == "not logged in")

        login = next(cb for name, _, cb in session_commands(screen) if name == "Log in: Demo login")
        login()
        await until(pilot, lambda: screen.state is not None)
        await until(pilot, lambda: screen.status.account == "Demo account")
        logout = next(cb for name, _, cb in session_commands(screen) if name == "Log out")
        logout()
        await until(pilot, lambda: screen.status.account == "not logged in")


async def test_providers_dialog(demo_spec: AgentSpec, workdir: Path) -> None:
    assert parse_headers("A: 1\nbad\n B : 2 ") == {"A": "1", "B": "2"}
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        screen.action_providers()
        await until(pilot, lambda: isinstance(app.screen, ProvidersScreen))
        providers = app.screen
        assert isinstance(providers, ProvidersScreen)
        await until(pilot, lambda: len(providers.providers) == 2)
        options = providers.query_one(OptionList)
        options.highlighted = 1  # backup
        await pilot.press("enter")
        assert isinstance(app.screen, ProviderEditScreen)
        edit = app.screen
        edit.query_one("#base-url", Input).value = "https://openai.example.com"
        from textual.widgets import Select

        edit.query_one("#api-type", Select).value = "openai"
        await pilot.click("#save")
        await until(pilot, lambda: app.screen is providers)
        await until(pilot, lambda: providers.providers["backup"].current is not None)


async def test_session_setup_from_settings(demo_spec: AgentSpec, workdir: Path) -> None:
    from acptui.config import McpServerConfig
    from acptui.setup import build_setup

    settings = Settings(
        system_prompt="Be nice.",
        additional_directories=[str(workdir / "src")],
        mcp_servers=[McpServerConfig(name="web", url="https://example.com/mcp")],
        agent_options={"demo": {"model": "demo-smart"}},
    )
    spec = settings.find_agent("demo")
    assert spec is not None
    setup = build_setup(settings, spec, options={"mode": "auto"})
    assert isinstance(setup, SessionSetup)
    app = AcpTui(settings, workdir, spec, setup=setup)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/setup")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        assert screen.state is not None
        text = next(
            e.text
            for e in reversed(screen.state.entries)
            if isinstance(e, MessageEntry) and e.role == "agent"
        )
        assert "MCP servers: web" in text
        assert "system prompt: append: Be nice." in text
        assert "mode: auto, model: demo-smart" in text
        await pilot.press("ctrl+l")
        assert isinstance(app.screen, LogScreen)
        await pilot.press("t")  # protocol traffic
        await pilot.press("escape")


async def test_agent_cancelling_a_permission_request_closes_the_dialog(
    demo_spec: AgentSpec, workdir: Path
) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "tour")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        screen.action_cancel_turn()  # the agent then cancels its request ($/cancel_request)
        await until(pilot, lambda: stop_reason(screen) == "cancelled")
        await until(pilot, lambda: app.screen is screen)


async def test_url_dialog_closes_on_complete(workdir: Path) -> None:
    from aclientp.schema.v2 import AcceptElicitationResponse, UrlElicitationRequest
    from textual.app import App

    results: list[object] = []
    request = UrlElicitationRequest(
        message="Sign in", session_id="s", elicitation_id="e", url="https://x"
    )

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(UrlElicitationScreen(request), callback=results.append)

    host = Host()
    async with host.run_test() as pilot:
        dialog = host.screen
        assert isinstance(dialog, UrlElicitationScreen)
        assert "https://x" in str(dialog.query_one("#url").render())
        dialog.complete()
        await pilot.pause()
    assert results == [AcceptElicitationResponse()]


async def test_resume_keeps_the_title(demo_spec: AgentSpec, workdir: Path) -> None:
    """The resume response has no title: the session list's stays."""
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo keep me")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        assert screen.state is not None
        original = screen.state.session_id
        await pilot.press("f2")
        assert isinstance(app.screen, TextInputScreen)
        app.screen.query_one(Input).value = "Kept title"
        await pilot.press("enter")
        await until(pilot, lambda: screen.state is not None and screen.state.title == "Kept title")

        await pilot.press("ctrl+n")
        await until(pilot, lambda: screen.state is not None and screen.state.session_id != original)
        screen.sessions.pop(original)  # Only the session list knows the title now
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, SessionListScreen)
        listing = app.screen
        await until(pilot, lambda: original in listing.sessions)
        options = listing.query_one(OptionList)
        options.highlighted = list(listing.sessions).index(original)
        await pilot.press("enter")
        await until(pilot, lambda: bool(screen.state and screen.state.session_id == original))
        # The replayed history names the first title; the list's (current) one is kept
        await until(pilot, lambda: screen.state is not None and screen.state.title == "Kept title")
        await pilot.pause(0.3)
        assert screen.state is not None
        assert screen.state.title == "Kept title"
