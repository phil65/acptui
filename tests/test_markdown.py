"""Markdown where ACP v2 allows it (descriptions, plan entries), plain text elsewhere."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2 import Command, ResourceLink, Skill

from acptui.completion import complete_command
from acptui.config import Settings
from acptui.protocol.content import block_markdown, code_span, escape_markdown
from acptui.ui.app import AcpTui, session_commands
from acptui.ui.markdown import AgentMarkdown, inline_markdown, link_is_web, summary_markdown
from acptui.ui.screens.dialogs import ConfigScreen, ConfirmScreen, LoginScreen
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.widgets.blocks import PlanView, SubagentView
from acptui.ui.widgets.prompt import CompletionList, completion_prompt
from tests.test_app import SIZE, connected, send, session, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


def test_inline_markdown_renders_code_emphasis_and_links() -> None:
    content = inline_markdown("Run `claude /login` in **the** [docs](https://example.com)")
    assert content.plain == "Run claude /login in the docs (https://example.com)"
    styles = {str(span.style) for span in content.spans}
    assert "$text-accent" in styles
    assert "bold" in styles


def test_inline_markdown_flattens_blocks() -> None:
    assert inline_markdown("- one\n- two").plain == "• one\n• two"
    assert inline_markdown("1. a\n2. b").plain == "1. a\n2. b"
    assert inline_markdown("# Title\n\nBody", single_line=True).plain == "Title Body"
    assert summary_markdown("First *para*\ncontinues\n\nSecond").plain == "First para continues"


def test_raw_html_is_shown_not_interpreted() -> None:
    assert inline_markdown("a <b>bold</b>").plain == "a <b>bold</b>"
    assert AgentMarkdown("x")._parser_factory is not None


def test_a_link_whose_text_is_its_target_is_not_repeated() -> None:
    assert inline_markdown("<https://example.com>").plain == "https://example.com"


def test_link_is_web() -> None:
    assert link_is_web("https://example.com")
    assert link_is_web("HTTP://example.com")
    assert not link_is_web("file:///etc/passwd")
    assert not link_is_web("vscode://open")


def test_plain_text_is_escaped_in_generated_markdown() -> None:
    assert escape_markdown("my_file*.md") == r"my\_file\*\.md"
    assert code_span("a`b") == "``a`b``"
    link = ResourceLink(name="notes_v2", uri="https://example.com/a_b", description="see *this*")
    text = block_markdown(link, "/work")
    assert r"[notes\_v2](<https://example.com/a_b>)" in text
    assert r"see \*this\*" in text


def test_command_descriptions_are_markdown() -> None:
    commands = [
        Command(name="login", description="Run `x` **now**"),
        Skill(name="s", description="d"),
    ]
    first = complete_command("lo", commands)[0]
    assert first.markdown
    assert completion_prompt(first).plain == "/login  Run x now"


async def test_markdown_descriptions_in_the_ui(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        completions = screen.query_one(CompletionList)
        await until(pilot, lambda: bool(screen.prompt.commands))
        await pilot.press("/", "t", "o")
        await until(pilot, lambda: completions.is_shown)
        assert "Show what acptui renders" in app.screen_text()
        screen.prompt.load_text("")

        await send(pilot, "show me the tour")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.pause(0.2)
        description = app.screen.query_one("#description", AgentMarkdown)
        assert description.source == (
            "Nothing is written: the demo agent only *pretends* (`dry run`)."
        )
        text = app.screen_text()
        assert "only pretends (dry run)." in text
        assert "`dry run`" not in text
        await pilot.press("1")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)
        plan = screen.query_one(PlanView)
        assert "Propose an edit to README.md" in str(plan.items.render())

        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, ConfigScreen))
        config = app.screen
        panel = config.query_one("#config-description", AgentMarkdown)
        await until(pilot, lambda: panel.source == "How the agent edits: **ask** first, or `auto`.")
        assert "How the agent edits: ask first, or auto." in app.screen_text()
        await pilot.press("enter")  # the mode's values
        await until(pilot, lambda: panel.source == "Ask before *editing* files")
        await pilot.press("escape", "escape")

        await send(pilot, "/subagent")
        await until(pilot, lambda: bool(screen.query(SubagentView)))
        view = screen.query_one(SubagentView)
        await until(
            pilot, lambda: view.description.source == "Looks things up for the **main** agent"
        )


async def test_login_dialog_shows_markdown_descriptions(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPTUI_DEMO_REQUIRE_AUTH", "1")
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = session(app)
        await until(pilot, lambda: screen.connection is not None and screen.connected)
        await until(pilot, lambda: "needs you to log in" in app.screen_text())
        assert "Pretend to log in (demo login)" in app.screen_text()
        helps = {name: help_text for name, help_text, _ in session_commands(screen)}
        assert (
            helps["Log in: Demo login"]
            == "Pretend to log in ([$text-accent]demo login[/$text-accent])"
        )
        screen.action_login()
        await until(pilot, lambda: isinstance(app.screen, LoginScreen))
        panel = app.screen.query_one("#login-description", AgentMarkdown)
        await until(pilot, lambda: panel.source == "Pretend to log in (`demo login`)")
        await pilot.press("enter")
        await until(pilot, lambda: screen.state is not None)


async def test_links_other_than_web_links_need_a_confirmation(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    app = AcpTui(Settings(), workdir, demo_spec)
    monkeypatch.setattr(app, "open_url", lambda url, **_: opened.append(url))
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        markdown = AgentMarkdown("[web](https://example.com) [local](vscode://file/x)")
        await screen.conversation.mount(markdown)
        markdown.post_message(AgentMarkdown.LinkClicked(markdown, "https://example.com"))
        await until(pilot, lambda: opened == ["https://example.com"])
        markdown.post_message(AgentMarkdown.LinkClicked(markdown, "vscode://file/x"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "vscode://file/x" in app.screen_text()
        assert opened == ["https://example.com"]
        await pilot.press("y")
        await until(pilot, lambda: opened == ["https://example.com", "vscode://file/x"])
