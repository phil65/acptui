"""The scrolling conversation of one session: renders :class:`SessionState` changes."""

import asyncio
import datetime as dt
from typing import TYPE_CHECKING, ClassVar

import platformdirs
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message

from acptui.protocol.state import EntryAdded, EntryChanged, SessionChanged, TerminalChanged
from acptui.ui.block_actions import block_svg, block_text
from acptui.ui.widgets.blocks import (
    AgentMessageView,
    ThoughtView,
    ToolCallView,
    make_view,
)
from acptui.ui.widgets.terminal import TerminalView


if TYPE_CHECKING:
    from textual.widget import Widget

    from acptui.config import DiffViewMode, ToolExpand
    from acptui.protocol.state import Change, Entry, SessionField, SessionState
    from acptui.ui.agent_log import AgentLog


class ConversationView(VerticalScroll):
    """Shows a session's timeline and applies changes in order, one at a time."""

    DEFAULT_CSS = """
    ConversationView {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        & > .-block-cursor { outline-left: thick $accent; background: $boost; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("alt+up", "block_cursor(-1)", "Block up", show=False),
        Binding("alt+down", "block_cursor(1)", "Block down", show=False),
        Binding("c", "copy_block", "Copy"),
        Binding("p", "block_to_prompt", "To prompt"),
        Binding("v", "save_block", "Save SVG"),
        Binding("m", "maximize_block", "Maximize"),
        Binding("escape", "leave_blocks", "Back", show=False),
    ]

    BLOCK_ACTIONS: ClassVar = frozenset({
        "copy_block",
        "block_to_prompt",
        "save_block",
        "maximize_block",
        "leave_blocks",
    })

    class ToPrompt(Message):
        """Put a block's text into the prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class LeftBlocks(Message):
        """The block cursor was put away (the prompt takes the focus again)."""

    class SessionFieldChanged(Message):
        """A session-level property of the shown session changed."""

        def __init__(self, field: SessionField) -> None:
            super().__init__()
            self.field: SessionField = field

    def __init__(
        self,
        agent_log: AgentLog,
        *,
        diff_mode: DiffViewMode = "auto",
        expand_thoughts: bool = False,
        tool_expand: ToolExpand = "auto",
        id: str | None = None,  # noqa: A002 - Textual's name
    ) -> None:
        super().__init__(id=id)
        self.agent_log = agent_log
        self.diff_mode: DiffViewMode = diff_mode
        self.expand_thoughts = expand_thoughts
        self.tool_expand: ToolExpand = tool_expand
        self.max_blocks = 0
        """Blocks kept on screen (0: all); older ones are removed (after toad's pruning)."""
        self.state: SessionState | None = None
        self._views: dict[Entry, Widget] = {}
        self.block_cursor: Widget | None = None
        """The block the block cursor is on."""
        # ``None`` clears the view.
        self._queue: asyncio.Queue[list[Change] | None] = asyncio.Queue()

    def on_mount(self) -> None:
        self.anchor()
        self.run_worker(self._render_loop(), name="render", group="render")

    def show(self, state: SessionState | None) -> None:
        """Show a session (rendering what it already has), or nothing."""
        self.state = state
        self._queue.put_nowait(None)
        if state is not None:
            self._queue.put_nowait([EntryAdded(entry) for entry in state.entries])

    async def _prune(self) -> None:
        if not self.max_blocks or len(self.children) <= self.max_blocks:
            return
        old = list(self.children[: len(self.children) - self.max_blocks])
        if self.block_cursor in old:
            self._set_cursor(None)
        gone = set(old)
        for entry in [entry for entry, view in self._views.items() if view in gone]:
            del self._views[entry]
        await self.remove_children(old)

    def clear_view(self) -> None:
        """Remove what is shown; later entries appear as usual (the state is kept)."""
        self._queue.put_nowait(None)

    def push(self, changes: list[Change]) -> None:
        """Queue changes of the shown session for rendering."""
        if changes:
            self._queue.put_nowait(changes)

    def stop(self) -> None:
        """Stop rendering."""
        self.workers.cancel_group(self, "render")

    async def _render_loop(self) -> None:
        while True:
            changes = await self._queue.get()
            if changes is None:
                self._views.clear()
                await self.remove_children()
                continue
            for change in changes:
                try:
                    await self._render_change(change)
                except Exception as exc:  # noqa: BLE001 - one bad update must not kill the view
                    self.agent_log.add(
                        "error", f"render failed for {type(change).__name__}: {exc!r}"
                    )

    async def _render_change(self, change: Change) -> None:
        state = self.state
        if state is None:
            return
        match change:
            case EntryAdded(entry=entry):
                if entry in self._views:
                    return
                view = make_view(
                    entry,
                    state,
                    self.diff_mode,
                    expand_thoughts=self.expand_thoughts,
                    tool_expand=self.tool_expand,
                )
                self._views[entry] = view
                await self.mount(view)
                await self._prune()
            case EntryChanged(entry=entry):
                view = self._views.get(entry)
                if isinstance(view, AgentMessageView | ThoughtView | ToolCallView):
                    await view.refresh_entry()
                elif view is not None:
                    refresh = getattr(view, "refresh_entry", None)
                    if callable(refresh):
                        refresh()
            case TerminalChanged(terminal=terminal):
                for terminal_view in self.query(TerminalView):
                    if terminal_view.terminal is terminal:
                        terminal_view.refresh_terminal()
            case SessionChanged(field=field):
                if field == "activity" and not state.running:
                    for message in self.query(AgentMessageView):
                        await message.finish()
                    for thought in self.query(ThoughtView):
                        await thought.finish()
                self.post_message(self.SessionFieldChanged(field))

    # --- Block cursor (after toad's): select a block, copy it, save it, maximize it --

    def blocks(self) -> list[Widget]:
        """The blocks shown, top to bottom."""
        return [child for child in self.children if child.display]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.BLOCK_ACTIONS:
            return self.block_cursor is not None and self.block_cursor.is_attached
        return True

    def move_block_cursor(self, step: int) -> Widget | None:
        """Move the cursor ``step`` blocks (from the last block when there is none)."""
        blocks = self.blocks()
        if not blocks:
            return None
        current = self.block_cursor
        if current in blocks:
            index = max(0, min(len(blocks) - 1, blocks.index(current) + step))
        else:
            index = len(blocks) - 1 if step < 0 else 0
        self._set_cursor(blocks[index])
        return self.block_cursor

    def _set_cursor(self, block: Widget | None) -> None:
        if self.block_cursor is not None:
            self.block_cursor.remove_class("-block-cursor")
        self.block_cursor = block
        if block is not None:
            block.add_class("-block-cursor")
            self.scroll_to_widget(block, animate=False)
            self.focus()
        self.refresh_bindings()

    def action_block_cursor(self, step: int) -> None:
        """Select the previous / next block."""
        self.move_block_cursor(step)

    def action_leave_blocks(self) -> None:
        """Put the cursor away."""
        if self.screen.maximized is not None:
            self.screen.minimize()
            return
        self._set_cursor(None)
        self.post_message(self.LeftBlocks())

    def action_copy_block(self) -> None:
        """Copy the selected block's text."""
        if self.block_cursor is not None:
            self.app.copy_to_clipboard(block_text(self.block_cursor))
            self.notify("Copied to the clipboard", timeout=2)

    def action_block_to_prompt(self) -> None:
        """Put the selected block's text into the prompt."""
        if self.block_cursor is not None:
            self.post_message(self.ToPrompt(block_text(self.block_cursor)))
            self._set_cursor(None)

    def action_save_block(self) -> None:
        """Save the selected block as an SVG picture (in Downloads)."""
        if self.block_cursor is None:
            return
        folder = platformdirs.user_downloads_path()
        path = folder / f"acptui_block_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.svg"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(block_svg(self.block_cursor), encoding="utf-8")
        except OSError as exc:
            self.notify(f"Couldn't save the block: {exc}", severity="error")
            return
        self.notify(str(path), title="Block saved", markup=False)

    def action_maximize_block(self) -> None:
        """Show the selected block on the whole screen (escape: back)."""
        if self.block_cursor is not None:
            self.screen.maximize(self.block_cursor, container=False)

    def set_diff_mode(self, mode: DiffViewMode) -> None:
        """Switch how diffs are shown."""
        self.diff_mode = mode
        for view in self.query(ToolCallView):
            view.set_diff_mode(mode)
