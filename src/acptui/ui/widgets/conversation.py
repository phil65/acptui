"""The scrolling conversation of one session: renders :class:`SessionState` changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.message import Message

from acptui.protocol.state import EntryAdded, EntryChanged, SessionChanged, TerminalChanged
from acptui.ui.widgets.blocks import (
    AgentMessageView,
    ThoughtView,
    ToolCallView,
    make_view,
)
from acptui.ui.widgets.terminal import TerminalView


if TYPE_CHECKING:
    from textual.widget import Widget

    from acptui.config import DiffViewMode
    from acptui.protocol.state import Change, Entry, SessionField, SessionState
    from acptui.ui.agent_log import AgentLog


class ConversationView(VerticalScroll):
    """Shows a session's timeline and applies changes in order, one at a time."""

    DEFAULT_CSS = """
    ConversationView {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    """

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
        id: str | None = None,  # noqa: A002 - Textual's name
    ) -> None:
        super().__init__(id=id)
        self.agent_log = agent_log
        self.diff_mode: DiffViewMode = diff_mode
        self.expand_thoughts = expand_thoughts
        self.state: SessionState | None = None
        self._views: dict[Entry, Widget] = {}
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
                view = make_view(entry, state, self.diff_mode, expand_thoughts=self.expand_thoughts)
                self._views[entry] = view
                await self.mount(view)
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

    def set_diff_mode(self, mode: DiffViewMode) -> None:
        """Switch how diffs are shown."""
        self.diff_mode = mode
        for view in self.query(ToolCallView):
            view.set_diff_mode(mode)
