"""The block cursor: select a conversation block, copy it, save it, maximize it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from acptui.config import Settings
from acptui.ui.app import AcpTui
from acptui.ui.block_actions import block_svg, block_text
from acptui.ui.widgets import conversation as conversation_module
from acptui.ui.widgets.blocks import AgentMessageView, ToolCallView, UserMessageView
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


async def test_block_cursor_actions(
    demo_spec: AgentSpec, workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(conversation_module.platformdirs, "user_downloads_path", lambda: tmp_path)
    app = AcpTui(Settings(), workdir, demo_spec)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo hello *world*")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)
        conversation = screen.conversation

        await pilot.press("alt+up")
        block = conversation.block_cursor
        assert isinstance(block, AgentMessageView)
        assert block.has_class("-block-cursor")
        assert conversation.has_focus
        await pilot.press("c")
        assert copied == [block.source.strip()]
        assert "hello *world*" in copied[0]  # Markdown, not the rendered text

        await pilot.press("alt+up")
        assert isinstance(conversation.block_cursor, UserMessageView)
        await pilot.press("alt+down")
        assert conversation.block_cursor is block

        await pilot.press("v")
        [svg] = tmp_path.glob("acptui_block_*.svg")
        assert svg.read_text(encoding="utf-8").startswith("<svg")

        await pilot.press("m")
        assert screen.maximized is block
        await pilot.press("escape")
        assert screen.maximized is None
        await pilot.press("p")
        assert "hello *world*" in screen.prompt.text
        assert screen.prompt.has_focus
        assert conversation.block_cursor is None
        await pilot.press("c")  # no block selected: the key is typed
        assert screen.prompt.text.endswith("c")

        await pilot.press("alt+up", "escape")
        assert conversation.block_cursor is None
        assert screen.prompt.has_focus


async def test_tool_call_text(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/media")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await until(pilot, lambda: bool(screen.query(ToolCallView)))
        tool = screen.query(ToolCallView).first()
        assert block_text(tool).startswith(tool.entry.display_title)
        assert "<svg" in block_svg(tool)


async def test_old_blocks_are_pruned(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(max_blocks=3), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        for word in ("one", "two", "three"):
            assert screen.state is not None
            screen.state.stop_reason = None
            await send(pilot, f"/echo {word}")
            await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.2)
        assert len(screen.conversation.children) == 3
        assert screen.state is not None
        assert len(screen.state.entries) >= 6  # the session keeps everything
        assert "one" not in "".join(v.source for v in screen.query(AgentMessageView))
