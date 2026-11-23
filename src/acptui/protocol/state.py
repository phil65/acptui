"""Client-side model of a session, built from ``session/update`` notifications.

ACP v2 updates are upserts keyed by IDs (messages, tool calls, terminals, plans,
compactions) with patch semantics: an omitted field leaves the stored value
unchanged, ``None`` clears it. :class:`SessionState` applies them and reports what
changed, so a UI only has to render :class:`Change` events.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from aclientp.schema.v2 import (
    AgentMessage,
    AgentMessageChunk,
    AgentThought,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    CompactionSummaryChunk,
    CompactionUpdate,
    ConfigOptionUpdate,
    IdleStateSessionUpdate,
    Notice,
    OtherStateSessionUpdate,
    PlanItems,
    PlanMarkdown,
    PlanRemoved,
    PlanUpdate,
    ReplayComplete,
    RequiresActionStateSessionUpdate,
    RunningStateSessionUpdate,
    SessionInfoUpdate,
    TerminalOutputChunk,
    TerminalUpdate,
    TextContent,
    ToolCallContentChunk,
    ToolCallSessionUpdate,
    ToolCallUpdate,
    UnknownStateSessionUpdate,
    UsageUpdate,
    UserMessage,
    UserMessageChunk,
)
from aclientp.schema.v2.base import is_set


if TYPE_CHECKING:
    from collections.abc import Sequence

    from aclientp.schema.v2 import (
        AvailableCommand,
        ContentBlock,
        Cost,
        Error,
        SessionConfigOption,
        SessionUpdate,
        TerminalExitStatus,
        ToolCallContent,
        ToolCallLocation,
    )

MessageRole = Literal["user", "agent", "thought"]
SessionActivity = Literal["idle", "running", "requires_action", "unknown"]

MAX_TERMINAL_BYTES = 512 * 1024
"""Terminal output kept per terminal; older bytes are dropped."""


def blocks_text(blocks: Sequence[ContentBlock]) -> str:
    """The text of content blocks; non-text blocks are left out."""
    return "".join(block.text for block in blocks if isinstance(block, TextContent))


# --- Timeline entries ---------------------------------------------------------------


@dataclass(eq=False)
class MessageEntry:
    """A user message, an agent message or an agent thought."""

    role: MessageRole
    message_id: str | None
    """``None`` for a user message not yet acknowledged by the agent."""

    blocks: list[ContentBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The message text."""
        return blocks_text(self.blocks)


@dataclass(eq=False)
class ToolCallEntry:
    """A tool call and its latest state."""

    tool_call_id: str
    name: str | None = None
    title: str | None = None
    kind: str | None = None
    status: str = "pending"
    content: list[ToolCallContent] = field(default_factory=list)
    locations: list[ToolCallLocation] = field(default_factory=list)
    raw_input: object = None
    raw_output: object = None
    can_cancel: bool = False

    @property
    def display_title(self) -> str:
        """Title for display, falling back to the tool name."""
        return self.title or self.name or self.tool_call_id

    @property
    def finished(self) -> bool:
        """Whether the tool call is done (successfully or not)."""
        return self.status in {"completed", "failed", "cancelled"}

    def apply(self, update: ToolCallUpdate) -> None:
        """Apply an upsert (patch semantics)."""
        if is_set(update.name):
            self.name = update.name
        if is_set(update.title):
            self.title = update.title
        if is_set(update.kind):
            self.kind = update.kind
        if is_set(update.status):
            self.status = update.status if update.status is not None else "pending"
        if is_set(update.content):
            self.content = list(update.content or ())
        if is_set(update.locations):
            self.locations = list(update.locations or ())
        if is_set(update.raw_input):
            self.raw_input = update.raw_input
        if is_set(update.raw_output):
            self.raw_output = update.raw_output
        if is_set(update.capabilities):
            caps = update.capabilities
            self.can_cancel = caps is not None and is_set(caps.cancel) and caps.cancel is not None


@dataclass(eq=False)
class PlanEntry:
    """A plan (structured items or markdown); updates replace its content."""

    plan_id: str
    content: PlanItems | PlanMarkdown
    removed: bool = False


@dataclass(eq=False)
class NoticeEntry:
    """Information for the user: an agent notice, or a turn that ended unusually."""

    severity: str
    title: str
    description: str | None = None


@dataclass(eq=False)
class CompactionEntry:
    """A context compaction."""

    compaction_id: str
    status: str
    summary: list[ContentBlock] = field(default_factory=list)
    error: str | None = None


Entry = MessageEntry | ToolCallEntry | PlanEntry | NoticeEntry | CompactionEntry
"""An item of the conversation timeline."""


@dataclass(eq=False)
class TerminalState:
    """An agent-owned terminal: display only, referenced by tool calls."""

    terminal_id: str
    command: str | None = None
    cwd: str | None = None
    output: bytearray = field(default_factory=bytearray)
    exit_status: TerminalExitStatus | None = None
    can_cancel: bool = False

    @property
    def exited(self) -> bool:
        """Whether the process has exited."""
        return self.exit_status is not None

    def append(self, data: bytes) -> None:
        """Append output, keeping at most :data:`MAX_TERMINAL_BYTES`."""
        self.output.extend(data)
        if len(self.output) > MAX_TERMINAL_BYTES:
            del self.output[: len(self.output) - MAX_TERMINAL_BYTES]


@dataclass(frozen=True)
class Usage:
    """Context window use and cost."""

    used: int
    size: int
    cost: Cost | None = None

    @property
    def fraction(self) -> float:
        """Used part of the context window (0..1)."""
        return self.used / self.size if self.size else 0.0


# --- Changes ------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryAdded:
    """A new timeline entry."""

    entry: Entry


@dataclass(frozen=True)
class EntryChanged:
    """A timeline entry changed.

    For messages, ``appended`` holds the text added by a chunk (so a UI can stream
    it); ``None`` means the content was replaced and must be rendered anew.
    """

    entry: Entry
    appended: str | None = None


@dataclass(frozen=True)
class TerminalChanged:
    """A terminal changed. ``appended`` is the new output, ``None`` for a replacement."""

    terminal: TerminalState
    appended: bytes | None = None


SessionField = Literal["activity", "commands", "config", "usage", "title", "replay"]


@dataclass(frozen=True)
class SessionChanged:
    """A session-level property changed."""

    field: SessionField


Change = EntryAdded | EntryChanged | TerminalChanged | SessionChanged
"""What an update changed."""


# --- The session ----------------------------------------------------------------------


def _decode(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return b""


@dataclass(eq=False)
class SessionState:
    """Everything the client knows about one session."""

    session_id: str
    cwd: str
    title: str | None = None
    entries: list[Entry] = field(default_factory=list)
    tool_calls: dict[str, ToolCallEntry] = field(default_factory=dict)
    terminals: dict[str, TerminalState] = field(default_factory=dict)
    plans: dict[str, PlanEntry] = field(default_factory=dict)
    commands: list[AvailableCommand] = field(default_factory=list)
    config_options: list[SessionConfigOption] = field(default_factory=list)
    usage: Usage | None = None
    activity: SessionActivity = "idle"
    stop_reason: str | None = None
    last_error: Error | None = None
    replaying: bool = False
    _messages: dict[tuple[MessageRole, str], MessageEntry] = field(default_factory=dict)
    _compactions: dict[str, CompactionEntry] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        """Whether the agent is working on a turn."""
        return self.activity in {"running", "requires_action"}

    # --- Local events -------------------------------------------------------------

    def add_local_user_message(self, blocks: Sequence[ContentBlock]) -> list[Change]:
        """Show a prompt the user just sent, before the agent acknowledges it."""
        entry = MessageEntry("user", None, list(blocks))
        self.entries.append(entry)
        return [EntryAdded(entry)]

    def acknowledge_user_message(self, entry: MessageEntry, message_id: str) -> None:
        """Record the ID the agent gave a local user message (``session/prompt`` result)."""
        if entry.message_id is None and ("user", message_id) not in self._messages:
            entry.message_id = message_id
            self._messages["user", message_id] = entry

    def add_notice(self, severity: str, title: str, description: str | None = None) -> list[Change]:
        """Add a client-side notice to the timeline."""
        entry = NoticeEntry(severity, title, description)
        self.entries.append(entry)
        return [EntryAdded(entry)]

    # --- Updates ------------------------------------------------------------------

    def apply(self, update: SessionUpdate) -> list[Change]:  # noqa: PLR0911
        """Apply one ``session/update``; returns what changed."""
        match update:
            case UserMessageChunk() | AgentMessageChunk() | AgentThoughtChunk():
                return self._message_chunk(update)
            case UserMessage() | AgentMessage() | AgentThought():
                return self._message_upsert(update)
            case ToolCallSessionUpdate():
                return self.apply_tool_call(update)
            case ToolCallContentChunk():
                return self._tool_call_chunk(update)
            case TerminalUpdate():
                return self._terminal_update(update)
            case TerminalOutputChunk():
                terminal = self._terminal(update.terminal_id)
                data = _decode(update.data)
                terminal.append(data)
                return [TerminalChanged(terminal, data)]
            case PlanUpdate():
                return self._plan_update(update.plan)
            case PlanRemoved():
                if (plan := self.plans.pop(update.plan_id, None)) is None:
                    return []
                plan.removed = True
                return [EntryChanged(plan)]
            case (
                RunningStateSessionUpdate()
                | IdleStateSessionUpdate()
                | RequiresActionStateSessionUpdate()
                | UnknownStateSessionUpdate()
                | OtherStateSessionUpdate()
            ):
                return self._state_update(update)
            case AvailableCommandsUpdate():
                self.commands = list(update.available_commands)
                return [SessionChanged("commands")]
            case ConfigOptionUpdate():
                self.config_options = list(update.config_options)
                return [SessionChanged("config")]
            case SessionInfoUpdate():
                if not is_set(update.title):
                    return []
                self.title = update.title
                return [SessionChanged("title")]
            case UsageUpdate():
                cost = self.usage.cost if self.usage is not None else None
                if is_set(update.cost):
                    cost = update.cost
                self.usage = Usage(update.used, update.size, cost)
                return [SessionChanged("usage")]
            case Notice():
                description = update.description if is_set(update.description) else None
                return self.add_notice(update.severity, update.title, description)
            case CompactionUpdate():
                return self._compaction_update(update)
            case CompactionSummaryChunk():
                compaction, changes = self._compaction(update.compaction_id)
                compaction.summary.append(update.content)
                return [*changes, EntryChanged(compaction)]
            case ReplayComplete():
                self.replaying = False
                return [SessionChanged("replay")]
            case _:
                # Subagents, cursors, session messages, prompt suggestions and
                # custom updates are not shown yet.
                return []

    def set_config_options(self, options: Sequence[SessionConfigOption]) -> list[Change]:
        """Replace the config options (e.g. from a ``session/set_config_option`` result)."""
        self.config_options = list(options)
        return [SessionChanged("config")]

    def set_commands(self, commands: Sequence[AvailableCommand]) -> list[Change]:
        """Replace the available commands (e.g. from a ``session/new`` result)."""
        self.commands = list(commands)
        return [SessionChanged("commands")]

    def _message_entry(self, role: MessageRole, message_id: str) -> tuple[MessageEntry, bool]:
        """The message with this ID, creating it if needed; also whether it is new.

        A user message the agent reports first (before ``session/prompt`` returned)
        adopts the oldest local user message still waiting for its ID.
        """
        if (entry := self._messages.get((role, message_id))) is not None:
            return entry, False
        if role == "user" and not self.replaying:
            pending = next(
                (
                    e
                    for e in self.entries
                    if isinstance(e, MessageEntry) and e.role == "user" and e.message_id is None
                ),
                None,
            )
            if pending is not None:
                pending.message_id = message_id
                pending.blocks.clear()
                self._messages[role, message_id] = pending
                return pending, False
        entry = MessageEntry(role, message_id)
        self._messages[role, message_id] = entry
        self.entries.append(entry)
        return entry, True

    def _message_chunk(
        self, update: UserMessageChunk | AgentMessageChunk | AgentThoughtChunk
    ) -> list[Change]:
        role: MessageRole = (
            "user"
            if isinstance(update, UserMessageChunk)
            else "agent"
            if isinstance(update, AgentMessageChunk)
            else "thought"
        )
        entry, created = self._message_entry(role, update.message_id)
        replaced = not created and role == "user" and not entry.blocks
        entry.blocks.append(update.content)
        if created:
            return [EntryAdded(entry)]
        if replaced or not isinstance(update.content, TextContent):
            return [EntryChanged(entry)]
        return [EntryChanged(entry, appended=update.content.text)]

    def _message_upsert(self, update: UserMessage | AgentMessage | AgentThought) -> list[Change]:
        role: MessageRole = (
            "user"
            if isinstance(update, UserMessage)
            else "agent"
            if isinstance(update, AgentMessage)
            else "thought"
        )
        entry, created = self._message_entry(role, update.message_id)
        if is_set(update.content):
            entry.blocks = list(update.content or ())
        return [EntryAdded(entry) if created else EntryChanged(entry)]

    def apply_tool_call(self, update: ToolCallUpdate) -> list[Change]:
        """Apply a tool call upsert (also used for tool calls in permission requests)."""
        entry = self.tool_calls.get(update.tool_call_id)
        if entry is None:
            entry = ToolCallEntry(update.tool_call_id)
            entry.apply(update)
            self.tool_calls[update.tool_call_id] = entry
            self.entries.append(entry)
            return [EntryAdded(entry)]
        entry.apply(update)
        return [EntryChanged(entry)]

    def _tool_call_chunk(self, update: ToolCallContentChunk) -> list[Change]:
        entry = self.tool_calls.get(update.tool_call_id)
        if entry is None:
            entry = ToolCallEntry(update.tool_call_id, content=[update.content])
            self.tool_calls[update.tool_call_id] = entry
            self.entries.append(entry)
            return [EntryAdded(entry)]
        entry.content.append(update.content)
        return [EntryChanged(entry)]

    def _terminal(self, terminal_id: str) -> TerminalState:
        if (terminal := self.terminals.get(terminal_id)) is None:
            terminal = self.terminals[terminal_id] = TerminalState(terminal_id)
        return terminal

    def _terminal_update(self, update: TerminalUpdate) -> list[Change]:
        terminal = self._terminal(update.terminal_id)
        if is_set(update.command):
            terminal.command = update.command
        if is_set(update.cwd):
            terminal.cwd = update.cwd
        if is_set(update.output):
            terminal.output = bytearray()
            if update.output is not None:
                terminal.append(_decode(update.output.data))
        if is_set(update.exit_status):
            terminal.exit_status = update.exit_status
        if is_set(update.capabilities):
            caps = update.capabilities
            terminal.can_cancel = (
                caps is not None and is_set(caps.cancel) and caps.cancel is not None
            )
        return [TerminalChanged(terminal)]

    def _plan_update(self, content: PlanItems | PlanMarkdown | object) -> list[Change]:
        if not isinstance(content, PlanItems | PlanMarkdown):
            return []
        plan = self.plans.get(content.plan_id)
        if plan is None:
            plan = self.plans[content.plan_id] = PlanEntry(content.plan_id, content)
            self.entries.append(plan)
            return [EntryAdded(plan)]
        plan.content = content
        return [EntryChanged(plan)]

    def _state_update(
        self,
        update: RunningStateSessionUpdate
        | IdleStateSessionUpdate
        | RequiresActionStateSessionUpdate
        | UnknownStateSessionUpdate
        | OtherStateSessionUpdate,
    ) -> list[Change]:
        changes: list[Change] = []
        match update:
            case RunningStateSessionUpdate():
                self.activity = "running"
                self.stop_reason = None
                self.last_error = None
            case RequiresActionStateSessionUpdate():
                self.activity = "requires_action"
            case UnknownStateSessionUpdate() | OtherStateSessionUpdate():
                self.activity = "unknown"
            case IdleStateSessionUpdate():
                self.activity = "idle"
                stop = update.stop_reason if is_set(update.stop_reason) else None
                self.stop_reason = stop
                error = update.error if is_set(update.error) else None
                self.last_error = error
                if stop == "error" or error is not None:
                    detail = error.message if error is not None else None
                    changes += self.add_notice("error", "The turn failed", detail)
                elif stop == "cancelled":
                    changes += self.add_notice("warning", "Cancelled")
                elif stop == "refusal":
                    changes += self.add_notice("warning", "The agent refused to continue")
                elif stop in {"max_tokens", "max_turn_requests"}:
                    changes += self.add_notice("warning", f"Turn stopped: {stop.replace('_', ' ')}")
        return [SessionChanged("activity"), *changes]

    def _compaction(self, compaction_id: str) -> tuple[CompactionEntry, list[Change]]:
        if (entry := self._compactions.get(compaction_id)) is not None:
            return entry, []
        entry = self._compactions[compaction_id] = CompactionEntry(compaction_id, "in_progress")
        self.entries.append(entry)
        return entry, [EntryAdded(entry)]

    def _compaction_update(self, update: CompactionUpdate) -> list[Change]:
        entry, changes = self._compaction(update.compaction_id)
        entry.status = update.status
        if is_set(update.summary):
            entry.summary = list(update.summary or ())
        if is_set(update.error):
            entry.error = update.error
        return changes or [EntryChanged(entry)]
