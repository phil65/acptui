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

from acptui.protocol.content import content_markdown, describe_block, relative_display
from acptui.protocol.state import (
    CompactionEntry,
    MessageEntry,
    NoticeEntry,
    PlanEntry,
    ShellEntry,
    SubagentEntry,
    ToolCallEntry,
    blocks_text,
)
from acptui.ui.markdown import AgentMarkdown, inline_markdown
from acptui.ui.render import pill, status_icon, tool_icon
from acptui.ui.widgets.diff import DiffView
from acptui.ui.widgets.terminal import ShellView, TerminalView


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


_content_markdown = content_markdown


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
            Content.assemble(("  + ", "$text-muted"), (desc, "italic $text-muted"))
            for block in self.entry.blocks
            if (desc := describe_block(block, self.cwd)) is not None
        )
        return Content("\n").join(lines)

    def refresh_entry(self) -> None:
        """Re-render after the entry changed."""
        self.update(self._message_content())


class StreamingMarkdown(AgentMarkdown):
    """Markdown that streams appended text, starting only after its initial content is shown.

    ``Markdown`` renders its initial content in an async mount handler; text streamed
    before that finished would be overwritten.
    """

    def __init__(self, markdown: str, *, classes: str | None = None) -> None:
        super().__init__(markdown, classes=classes)
        self._ready = asyncio.Event()
        self._stream: MarkdownStream | None = None
        self._shown = markdown

    async def sync(self, markdown: str) -> None:
        """Show ``markdown``: stream the new part if it extends what is shown, else replace.

        Rendering lags behind the session state (several chunks may have been applied
        before the view exists), so views sync to the full text instead of appending
        each chunk.
        """
        if markdown == self._shown:
            return
        if markdown.startswith(self._shown):
            delta = markdown[len(self._shown) :]
            self._shown = markdown
            await self.append_text(delta)
        else:
            self._shown = markdown
            await self.replace_text(markdown)

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
        """Show the entry's current content (streaming what was appended)."""
        await self.sync(_content_markdown(self.entry.blocks, self.cwd))


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

    async def refresh_entry(self) -> None:
        """Show the entry's current content (streaming what was appended)."""
        await self.markdown.sync(_content_markdown(self.entry.blocks, self.cwd))

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
        return AgentMarkdown(text + note, classes="tool-markdown")
    return Static(Content(text.rstrip("\n") + note), classes="tool-text")


class ToolCallHeader(Static, can_focus=True):
    """The one-line summary of a tool call; enter or click toggles the details."""

    BINDINGS: ClassVar = [
        Binding("enter,space", "toggle_details", "Toggle details", show=False),
        Binding("x", "cancel", "Cancel tool call", show=False),
        Binding("r", "toggle_raw", "Raw input/output", show=False),
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
            parent._user_toggled = True
            parent.expanded = not parent.expanded

    def action_cancel(self) -> None:
        parent = self.parent
        if (
            isinstance(parent, ToolCallView)
            and parent.entry.can_cancel
            and not parent.entry.finished
        ):
            parent.post_message(ToolCallView.CancelRequested(parent.entry.tool_call_id))

    async def action_toggle_raw(self) -> None:
        parent = self.parent
        if isinstance(parent, ToolCallView):
            parent.show_raw = not parent.show_raw
            parent.expanded = True
            await parent.rebuild()

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
        self.show_raw = False
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
        has_body = bool(entry.content or entry.locations or entry.raw_input is not None)
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
        if self.show_raw:
            if entry.raw_input is not None:
                widgets.append(
                    Static(Content("input: " + raw_json(entry.raw_input)), classes="tool-text")
                )
            if entry.raw_output is not None:
                widgets.append(
                    Static(Content("output: " + raw_json(entry.raw_output)), classes="tool-text")
                )
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
                child = self.state.subagents.get(session_id)
                label = child.display_title if child is not None else session_id
                return Static(Content(f"↳ session: {label}"), classes="tool-locations")
            case _:
                return Static(Content(raw_json(item.model_dump())), classes="tool-text")

    def _block_widget(self, block: ContentBlock) -> Widget:
        match block:
            case TextContent(text=text):
                return _text_widget(text)
            case EmbeddedResource(resource=TextResourceContents(text=text)):
                return _text_widget(text)
            case _:
                return AgentMarkdown(
                    content_markdown([block], self.state.cwd), classes="tool-markdown"
                )

    async def refresh_entry(self) -> None:
        """Update after the tool call changed (re-renders content only if it changed)."""
        self.query_one(ToolCallHeader).update(self._header())
        key = (*(id(item) for item in self.entry.content), len(self.entry.locations))
        if key != self._content_key:
            self._content_key = key
            await self.rebuild()
            if not self._user_toggled:
                self.expanded = self._should_expand()

    async def rebuild(self) -> None:
        """Re-create the content widgets."""
        body = self.query_one("#tool-body", Vertical)
        await body.remove_children()
        await body.mount_all(self._body())

    def set_diff_mode(self, mode: DiffViewMode) -> None:
        """Switch how diffs are shown."""
        self.diff_mode = mode
        for view in self.query(DiffView):
            view.mode = mode


class PlanView(Vertical):
    """The agent's plan (adapted from toad's plan widget: status icons, priority pills).

    Plan entries and Markdown plans are Markdown (ACP v2): entries are rendered inline
    (one line each), a Markdown plan as a document.
    """

    DEFAULT_CSS = """
    PlanView {
        height: auto;
        border: round $secondary 60%;
        border-title-color: $secondary;
        padding: 0 1;
        margin: 0 0 1 0;
        #plan-items { height: auto; }
        #plan-markdown { height: auto; margin: 0; padding: 0; background: transparent; }
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
        self.items = Static(id="plan-items")
        self.markdown = AgentMarkdown(id="plan-markdown")

    def compose(self) -> ComposeResult:
        yield self.items
        yield self.markdown

    def on_mount(self) -> None:
        self.refresh_entry()

    def refresh_entry(self) -> None:
        """Re-render the plan."""
        from aclientp.schema.v2 import PlanItems

        self.set_class(self.entry.removed, "-removed")
        content = self.entry.content
        is_items = isinstance(content, PlanItems)
        self.items.display = is_items
        self.markdown.display = not is_items
        if not isinstance(content, PlanItems):
            path = content.path if is_set(content.path) and content.path else None
            if path:
                self.border_subtitle = path
            if self.markdown.source != content.content:
                self.markdown.update(content.content)
            return
        lines: list[Content] = []
        for item in content.entries:
            label, bg, fg = self.PRIORITIES.get(
                item.priority, (item.priority[:1].upper(), "$panel", "$text")
            )
            text = inline_markdown(item.content, single_line=True)
            if item.status in {"completed", "cancelled"}:
                text = text.stylize("$text-muted strike")
            lines.append(
                Content.assemble(status_icon(item.status), " ", pill(label, bg, fg), " ", text)
            )
        done = sum(1 for item in content.entries if item.status == "completed")
        self.border_subtitle = f"{done}/{len(content.entries)}"
        self.items.update(Content("\n").join(lines) if lines else Content("(empty plan)"))


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


class SessionMessageView(Static):
    """A message sent to or received from another session."""

    DEFAULT_CSS = """
    SessionMessageView {
        height: auto;
        border-left: wide $secondary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, entry: MessageEntry, state: SessionState) -> None:
        super().__init__()
        self.entry = entry
        self.state = state
        self.refresh_entry()

    def _session_label(self, session_id: str | None) -> str:
        if session_id is None:
            return "?"
        if session_id == self.state.session_id:
            return "this session"
        child = self.state.subagents.get(session_id)
        return child.display_title if child is not None else session_id

    def refresh_entry(self) -> None:
        """Re-render."""
        entry = self.entry
        sender = self._session_label(entry.sender_session_id)
        recipient = self._session_label(entry.recipient_session_id)
        header = f"✉ {sender} → {recipient}"
        body = content_markdown(entry.blocks, self.state.cwd)
        self.update(Content.assemble((header, "bold $secondary"), "\n", body))


class SubagentView(Vertical, can_focus=True):
    """A subagent session; enter opens its conversation, x cancels its work.

    The description comes from the model that launched the subagent: Markdown (ACP v2).
    """

    BINDINGS: ClassVar = [
        Binding("enter", "open", "Open subagent", show=False),
        Binding("x", "cancel", "Cancel subagent", show=False),
    ]

    DEFAULT_CSS = """
    SubagentView {
        height: auto;
        border: round $accent 50%;
        padding: 0 1;
        margin: 0 0 1 0;
        &:focus { border: round $accent; }
        #subagent-header { height: auto; }
        #subagent-description {
            height: auto; margin: 0; padding: 0; background: transparent; color: $text-muted;
        }
    }
    """

    class Open(Message):
        """The user wants to see the subagent's conversation."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class CancelRequested(Message):
        """The user asked to cancel the subagent's work."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, entry: SubagentEntry) -> None:
        super().__init__()
        self.entry = entry
        self.header = Static(id="subagent-header")
        self.description = AgentMarkdown(id="subagent-description")

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.description

    def on_mount(self) -> None:
        self.refresh_entry()

    def refresh_entry(self) -> None:
        """Re-render."""
        entry = self.entry
        running = entry.activity in {"running", "requires_action"}
        icon = status_icon(
            "in_progress" if running else "completed" if entry.activity == "idle" else "pending"
        )
        hints = "enter: open" + ("  x: cancel" if entry.can_cancel and running else "")
        parts: list[Content | str | tuple[str, str]] = [
            icon,
            " ",
            ("subagent ", "$text-muted"),
            (entry.display_title, "bold"),
            (f"  {entry.activity}", "$text-muted"),
            (f"  ({hints})", "$text-muted italic"),
        ]
        self.header.update(Content.assemble(*parts))
        description = entry.description or ""
        self.description.display = bool(description)
        if self.description.source != description:
            self.description.update(description)

    def action_open(self) -> None:
        self.post_message(self.Open(self.entry.session_id))

    def action_cancel(self) -> None:
        if self.entry.can_cancel:
            self.post_message(self.CancelRequested(self.entry.session_id))

    def on_click(self, event: Click) -> None:
        event.stop()
        self.action_open()


def make_view(  # noqa: PLR0911
    entry: Entry, state: SessionState, diff_mode: DiffViewMode, *, expand_thoughts: bool
) -> Widget:
    """The widget for a timeline entry."""
    match entry:
        case MessageEntry(role="user"):
            return UserMessageView(entry, state.cwd)
        case MessageEntry(role="session"):
            return SessionMessageView(entry, state)
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
        case SubagentEntry():
            return SubagentView(entry)
        case ShellEntry():
            return ShellView(entry, state.cwd)


def raw_json(value: object) -> str:
    """Pretty JSON for raw tool input/output."""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
