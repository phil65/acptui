"""Conversation blocks: messages, thoughts, tool calls, plans and notices."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, ClassVar

from aclientp.schema.v2 import (
    Content as ToolContent,
    Diff,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    SessionReference,
    Terminal,
    TextContent,
    TextResourceContents,
)
from aclientp.schema.v2.base import is_set
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Collapsible, Markdown, Static

from acptui.protocol.prompt import describe_block, relative_display
from acptui.protocol.state import (
    CompactionEntry,
    MessageEntry,
    NoticeEntry,
    PlanEntry,
    ToolCallEntry,
    blocks_text,
)
from acptui.ui.render import pill, status_icon, tool_icon
from acptui.ui.widgets.diff import DiffView
from acptui.ui.widgets.terminal import TerminalView


if TYPE_CHECKING:
    from aclientp.schema.v2 import ContentBlock, ToolCallContent
    from textual.app import ComposeResult
    from textual.events import Click
    from textual.widget import Widget
    from textual.widgets.markdown import MarkdownStream

    from acptui.config import DiffViewMode
    from acptui.protocol.state import Entry, SessionState


MAX_TEXT_LINES = 200
"""Lines of tool output text shown; the rest is cut (with a note)."""

COLLAPSED_KINDS = frozenset({"read", "search", "fetch", "think"})
"""Tool kinds whose content starts collapsed (it is mostly noise)."""


def _content_markdown(blocks: list[ContentBlock], cwd: str) -> str:
    """Markdown for message content: text as is, other blocks as short notes."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif (desc := describe_block(block)) is not None:
            parts.append(f"\n\n`[{relative_display(desc, cwd)}]`\n\n")
    return "".join(parts)


class UserMessageView(Static):
    """A message the user sent."""

    DEFAULT_CSS = """
    UserMessageView {
        height: auto;
        background: $boost;
        border-left: thick $primary;
        padding: 0 1;
        margin: 1 0 1 0;
    }
    """

    def __init__(self, entry: MessageEntry, cwd: str) -> None:
        super().__init__()
        self.entry = entry
        self.cwd = cwd
        self.update(self._message_content())

    def _message_content(self) -> Content:
        lines: list[Content] = [Content(blocks_text(self.entry.blocks) or " ")]
        lines.extend(
            Content.assemble(
                ("  + ", "$text-muted"), (relative_display(desc, self.cwd), "italic $text-muted")
            )
            for block in self.entry.blocks
            if (desc := describe_block(block)) is not None
        )
        return Content("\n").join(lines)

    def refresh_entry(self) -> None:
        """Re-render after the entry changed."""
        self.update(self._message_content())


class StreamingMarkdown(Markdown):
    """Markdown that streams appended text, starting only after its initial content is shown.

    ``Markdown`` renders its initial content in an async mount handler; text streamed
    before that finished would be overwritten.
    """

    def __init__(self, markdown: str, *, classes: str | None = None) -> None:
        super().__init__(markdown, classes=classes)
        self._ready = asyncio.Event()
        self._stream: MarkdownStream | None = None

    def on_mount(self) -> None:
        # Markdown's own mount handler (which shows the initial content) runs after
        # this one, in the same message: become ready once the message is processed.
        self.call_next(self._ready.set)

    async def append_text(self, text: str) -> None:
        """Append streamed text."""
        await self._ready.wait()
        if self._stream is None:
            self._stream = Markdown.get_stream(self)
        await self._stream.write(text)

    async def replace_text(self, text: str) -> None:
        """Replace all text."""
        await self._ready.wait()
        await self.finish()
        await self.update(text)

    async def finish(self) -> None:
        """Flush streamed text."""
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()


class AgentMessageView(StreamingMarkdown):
    """A message of the agent, streamed as Markdown."""

    DEFAULT_CSS = """
    AgentMessageView {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        background: transparent;
    }
    """

    def __init__(self, entry: MessageEntry, cwd: str) -> None:
        super().__init__(_content_markdown(entry.blocks, cwd))
        self.entry = entry
        self.cwd = cwd

    async def refresh_entry(self) -> None:
        """Re-render all content (after a replacement)."""
        await self.replace_text(_content_markdown(self.entry.blocks, self.cwd))


class ThoughtView(Collapsible):
    """The agent's reasoning, collapsible."""

    DEFAULT_CSS = """
    ThoughtView {
        height: auto;
        background: transparent;
        border: none;
        padding: 0;
        margin: 0 0 1 0;
        CollapsibleTitle { color: $text-muted; text-style: italic; padding: 0 1; }
        Contents { padding: 0 0 0 2; }
        Markdown { color: $text-muted; background: transparent; margin: 0; padding: 0; }
    }
    """

    def __init__(self, entry: MessageEntry, cwd: str, *, expanded: bool) -> None:
        self.entry = entry
        self.cwd = cwd
        self.markdown = StreamingMarkdown(_content_markdown(entry.blocks, cwd))
        super().__init__(self.markdown, title="Thinking…", collapsed=not expanded)

    async def append_text(self, text: str) -> None:
        """Append streamed text."""
        await self.markdown.append_text(text)

    async def refresh_entry(self) -> None:
        """Re-render all content."""
        await self.markdown.replace_text(_content_markdown(self.entry.blocks, self.cwd))

    async def finish(self) -> None:
        """Flush streamed text; the title then says the thinking is done."""
        await self.markdown.finish()
        self.title = "Thought"


def _text_widget(text: str) -> Widget:
    """A widget for tool output text: ANSI, Markdown or plain (guessed, as agents vary)."""
    lines = text.splitlines()
    note = ""
    if len(lines) > MAX_TEXT_LINES:
        note = f"\n… {len(lines) - MAX_TEXT_LINES} more lines"
        text = "\n".join(lines[:MAX_TEXT_LINES])
    if "\x1b[" in text:
        return Static(Text.from_ansi(text + note), classes="tool-text")
    if "```" in text or re.search(r"^#{1,6}\s", text, re.MULTILINE):
        return Markdown(text + note, classes="tool-markdown")
    return Static(Content(text.rstrip("\n") + note), classes="tool-text")


class ToolCallHeader(Static, can_focus=True):
    """The one-line summary of a tool call; enter or click toggles the details."""

    BINDINGS: ClassVar = [
        Binding("enter,space", "toggle_details", "Toggle details", show=False),
        Binding("x", "cancel", "Cancel tool call", show=False),
    ]

    DEFAULT_CSS = """
    ToolCallHeader {
        height: auto;
        padding: 0 1;
        &:focus { background: $boost; text-style: bold; }
        &:hover { background: $boost; }
    }
    """

    def action_toggle_details(self) -> None:
        parent = self.parent
        if isinstance(parent, ToolCallView):
            parent.expanded = not parent.expanded

    def action_cancel(self) -> None:
        parent = self.parent
        if (
            isinstance(parent, ToolCallView)
            and parent.entry.can_cancel
            and not parent.entry.finished
        ):
            parent.post_message(ToolCallView.CancelRequested(parent.entry.tool_call_id))

    def on_click(self, event: Click) -> None:
        event.stop()
        self.action_toggle_details()


class ToolCallView(Vertical):
    """A tool call: status, kind, title, locations and content."""

    DEFAULT_CSS = """
    ToolCallView {
        height: auto;
        margin: 0 0 1 0;
        #tool-body { height: auto; padding: 0 0 0 3; }
        &.-collapsed #tool-body { display: none; }
        .tool-text { height: auto; max-height: 30; color: $text-muted; }
        .tool-markdown { height: auto; margin: 0; padding: 0; background: transparent; }
        .tool-locations { color: $text-muted; height: auto; }
    }
    """

    expanded: reactive[bool] = reactive(True)

    class CancelRequested(Message):
        """The user asked to cancel the tool call."""

        def __init__(self, tool_call_id: str) -> None:
            super().__init__()
            self.tool_call_id = tool_call_id

    def __init__(self, entry: ToolCallEntry, state: SessionState, diff_mode: DiffViewMode) -> None:
        super().__init__()
        self.entry = entry
        self.state = state
        self.diff_mode: DiffViewMode = diff_mode
        self._content_key: tuple[int, ...] = ()
        self._user_toggled = False
        self.set_reactive(ToolCallView.expanded, self._should_expand())

    def _should_expand(self) -> bool:
        if not self.entry.content:
            return True
        has_rich = any(isinstance(item, Diff | Terminal) for item in self.entry.content)
        return has_rich or self.entry.kind not in COLLAPSED_KINDS

    def compose(self) -> ComposeResult:
        yield ToolCallHeader(self._header())
        with Vertical(id="tool-body"):
            yield from self._body()

    def on_mount(self) -> None:
        self.set_class(not self.expanded, "-collapsed")

    def watch_expanded(self, expanded: bool) -> None:
        self.set_class(not expanded, "-collapsed")
        if self.is_mounted:
            self.query_one(ToolCallHeader).update(self._header())

    def _header(self) -> Content:
        entry = self.entry
        arrow = "▼" if self.expanded else "▶"
        has_body = bool(entry.content or entry.locations)
        parts: list[Content | str | tuple[str, str]] = [
            (f"{arrow} " if has_body else "  ", "$text-muted"),
            status_icon(entry.status),
            " ",
            (tool_icon(entry.kind) + " ", "bold $secondary"),
            Content(entry.display_title),
        ]
        if entry.status == "failed":
            parts += ["  ", pill("failed", "$error-muted", "$text-error")]
        elif entry.status == "cancelled":
            parts += ["  ", pill("cancelled", "$warning-muted", "$text-warning")]
        if entry.can_cancel and not entry.finished:
            parts += ["  ", ("x: cancel", "$text-muted italic")]
        return Content.assemble(*parts)

    def _body(self) -> list[Widget]:
        widgets: list[Widget] = []
        entry = self.entry
        if entry.locations:
            lines = []
            for location in entry.locations:
                path = relative_display(location.path, self.state.cwd)
                line = location.line if is_set(location.line) and location.line else None
                lines.append(f"↳ {path}:{line}" if line else f"↳ {path}")
            widgets.append(Static(Content("\n".join(lines)), classes="tool-locations"))
        widgets.extend(self._content_widget(item) for item in entry.content)
        return widgets

    def _content_widget(self, item: ToolCallContent) -> Widget:
        match item:
            case ToolContent(content=block):
                return self._block_widget(block)
            case Diff():
                return DiffView(item, self.state.cwd, self.diff_mode)
            case Terminal(terminal_id=terminal_id):
                terminal = self.state.terminals.get(terminal_id)
                if terminal is None:
                    from acptui.protocol.state import TerminalState

                    terminal = self.state.terminals.setdefault(
                        terminal_id, TerminalState(terminal_id)
                    )
                return TerminalView(terminal)
            case SessionReference(session_id=session_id):
                return Static(Content(f"↳ session {session_id}"), classes="tool-locations")
            case _:
                return Static(Content(f"[{item.type}]"), classes="tool-locations")

    def _block_widget(self, block: ContentBlock) -> Widget:
        match block:
            case TextContent(text=text):
                return _text_widget(text)
            case EmbeddedResource(resource=TextResourceContents(text=text)):
                return _text_widget(text)
            case ResourceLink() | ImageContent() | EmbeddedResource():
                desc = describe_block(block) or block.type
                return Static(
                    Content(f"[{relative_display(desc, self.state.cwd)}]"), classes="tool-locations"
                )
            case _:
                return Static(Content(f"[{block.type}]"), classes="tool-locations")

    async def refresh_entry(self) -> None:
        """Update after the tool call changed (re-renders content only if it changed)."""
        self.query_one(ToolCallHeader).update(self._header())
        key = (*(id(item) for item in self.entry.content), len(self.entry.locations))
        if key != self._content_key:
            self._content_key = key
            body = self.query_one("#tool-body", Vertical)
            await body.remove_children()
            await body.mount_all(self._body())
            if not self._user_toggled:
                self.expanded = self._should_expand() or self.expanded

    def set_diff_mode(self, mode: DiffViewMode) -> None:
        """Switch how diffs are shown."""
        self.diff_mode = mode
        for view in self.query(DiffView):
            view.mode = mode


class PlanView(Static):
    """The agent's plan (adapted from toad's plan widget: status icons, priority pills)."""

    DEFAULT_CSS = """
    PlanView {
        height: auto;
        border: round $secondary 60%;
        border-title-color: $secondary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    PlanView.-removed { display: none; }
    """

    PRIORITIES: ClassVar[dict[str, tuple[str, str, str]]] = {
        "high": ("H", "$error-muted", "$text-error"),
        "medium": ("M", "$warning-muted", "$text-warning"),
        "low": ("L", "$primary-muted", "$text-primary"),
    }

    def __init__(self, entry: PlanEntry) -> None:
        super().__init__()
        self.entry = entry
        self.border_title = "Plan"
        self.refresh_entry()

    def refresh_entry(self) -> None:
        """Re-render the plan."""
        from aclientp.schema.v2 import PlanItems

        self.set_class(self.entry.removed, "-removed")
        content = self.entry.content
        if not isinstance(content, PlanItems):
            self.update(Content(content.content))
            return
        lines: list[Content] = []
        for item in content.entries:
            label, bg, fg = self.PRIORITIES.get(
                item.priority, (item.priority[:1].upper(), "$panel", "$text")
            )
            text_style = "$text-muted strike" if item.status in {"completed", "cancelled"} else ""
            lines.append(
                Content.assemble(
                    status_icon(item.status),
                    " ",
                    pill(label, bg, fg),
                    " ",
                    (item.content, text_style),
                )
            )
        done = sum(1 for item in content.entries if item.status == "completed")
        self.border_subtitle = f"{done}/{len(content.entries)}"
        self.update(Content("\n").join(lines) if lines else Content("(empty plan)"))


class NoticeView(Static):
    """A notice: info, warning or error."""

    DEFAULT_CSS = """
    NoticeView {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border-left: wide $primary;
        &.-warning { border-left: wide $warning; color: $text-warning; }
        &.-error { border-left: wide $error; color: $text-error; background: $error 10%; }
    }
    """

    def __init__(self, entry: NoticeEntry) -> None:
        super().__init__(classes=f"-{entry.severity}")
        self.entry = entry
        text = Content.assemble((entry.title, "bold"))
        if entry.description:
            text = Content.assemble(text, "\n", entry.description)
        self.update(text)


class CompactionView(Static):
    """A context compaction."""

    DEFAULT_CSS = """
    CompactionView { height: auto; color: $text-muted; padding: 0 1; margin: 0 0 1 0; }
    """

    def __init__(self, entry: CompactionEntry) -> None:
        super().__init__()
        self.entry = entry
        self.refresh_entry()

    def refresh_entry(self) -> None:
        """Re-render."""
        entry = self.entry
        text = f"⟲ Context compaction: {entry.status}"
        if entry.error:
            text += f" ({entry.error})"
        summary = blocks_text(entry.summary)
        self.update(Content(text + (f"\n{summary}" if summary else "")))


def make_view(  # noqa: PLR0911
    entry: Entry, state: SessionState, diff_mode: DiffViewMode, *, expand_thoughts: bool
) -> Widget:
    """The widget for a timeline entry."""
    match entry:
        case MessageEntry(role="user"):
            return UserMessageView(entry, state.cwd)
        case MessageEntry(role="thought"):
            return ThoughtView(entry, state.cwd, expanded=expand_thoughts)
        case MessageEntry():
            return AgentMessageView(entry, state.cwd)
        case ToolCallEntry():
            return ToolCallView(entry, state, diff_mode)
        case PlanEntry():
            return PlanView(entry)
        case NoticeEntry():
            return NoticeView(entry)
        case CompactionEntry():
            return CompactionView(entry)


def raw_json(value: object) -> str:
    """Pretty JSON for raw tool input/output."""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
