"""SessionState: applying v2 session updates with patch semantics."""

from aclientp.schema.v2 import (
    AgentMessage,
    AgentMessageChunk,
    AgentMessageCursorAnchor,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    Command,
    CompactionSummaryChunk,
    CompactionUpdate,
    Content,
    Cost,
    CursorCapabilities,
    CursorForkCapabilities,
    CursorToolCallAnchor,
    CursorUpdate,
    Error,
    IdleStateSessionUpdate,
    Notice,
    PlanEntry as SchemaPlanEntry,
    PlanItems,
    PlanRemoved,
    PlanUpdate,
    ReplayComplete,
    RunningStateSessionUpdate,
    SessionInfoUpdate,
    Terminal,
    TerminalExitStatus,
    TerminalOutput,
    TerminalOutputChunk,
    TerminalUpdate,
    TextContent,
    ToolCallCancelCapabilities,
    ToolCallCapabilities,
    ToolCallContentChunk,
    ToolCallSessionUpdate,
    UnknownSessionUpdate,
    UsageUpdate,
    UserMessage,
    UserMessageChunk,
)

from acptui.protocol.state import (
    CompactionEntry,
    EntryAdded,
    EntryChanged,
    MessageEntry,
    NoticeEntry,
    PlanEntry,
    SessionChanged,
    SessionState,
    TerminalChanged,
    ToolCallEntry,
)


def _state() -> SessionState:
    return SessionState("s1", "/work")


def test_agent_chunks_stream_into_one_message() -> None:
    state = _state()
    first = state.apply(AgentMessageChunk(message_id="m1", content=TextContent(text="Hello ")))
    second = state.apply(AgentMessageChunk(message_id="m1", content=TextContent(text="world")))

    assert isinstance(first[0], EntryAdded)
    assert second == [EntryChanged(state.entries[0], appended="world")]
    entry = state.entries[0]
    assert isinstance(entry, MessageEntry)
    assert entry.role == "agent"
    assert entry.text == "Hello world"


def test_thoughts_and_messages_with_the_same_id_are_separate() -> None:
    state = _state()
    state.apply(AgentThoughtChunk(message_id="x", content=TextContent(text="hmm")))
    state.apply(AgentMessageChunk(message_id="x", content=TextContent(text="answer")))

    roles = [e.role for e in state.entries if isinstance(e, MessageEntry)]
    assert roles == ["thought", "agent"]


def test_message_upsert_replaces_and_null_clears() -> None:
    state = _state()
    state.apply(AgentMessageChunk(message_id="m", content=TextContent(text="draft")))
    changes = state.apply(AgentMessage(message_id="m", content=[TextContent(text="final")]))
    entry = state.entries[0]
    assert isinstance(entry, MessageEntry)
    assert entry.text == "final"
    assert changes == [EntryChanged(entry)]

    state.apply(AgentMessage(message_id="m"))  # omitted content: unchanged
    assert entry.text == "final"
    state.apply(AgentMessage(message_id="m", content=None))  # null clears
    assert entry.blocks == []


def test_agent_reported_user_message_adopts_the_local_one() -> None:
    state = _state()
    (added,) = state.add_local_user_message([TextContent(text="hi")])
    assert isinstance(added, EntryAdded)
    local = added.entry

    # The agent echoes the message before session/prompt returned.
    changes = state.apply(UserMessage(message_id="u1", content=[TextContent(text="hi")]))

    assert changes == [EntryChanged(local)]
    assert len(state.entries) == 1
    assert isinstance(local, MessageEntry)
    assert local.message_id == "u1"
    state.acknowledge_user_message(local, "u1")
    assert local.message_id == "u1"


def test_user_chunk_replaces_the_local_content_once() -> None:
    state = _state()
    state.add_local_user_message([TextContent(text="hi")])
    state.apply(UserMessageChunk(message_id="u1", content=TextContent(text="h")))
    state.apply(UserMessageChunk(message_id="u1", content=TextContent(text="i")))

    entry = state.entries[0]
    assert isinstance(entry, MessageEntry)
    assert entry.text == "hi"
    assert len(state.entries) == 1


def test_replayed_user_messages_are_new_entries() -> None:
    state = SessionState("s1", "/work", replaying=True)
    state.apply(UserMessage(message_id="old", content=[TextContent(text="earlier")]))
    state.apply(ReplayComplete())

    assert not state.replaying
    assert len(state.entries) == 1


def test_tool_call_patch_semantics() -> None:
    state = _state()
    state.apply(
        ToolCallSessionUpdate(
            tool_call_id="t1",
            title="Run tests",
            kind="execute",
            status="in_progress",
            content=[Content(content=TextContent(text="out"))],
            capabilities=ToolCallCapabilities(cancel=ToolCallCancelCapabilities()),
        )
    )
    entry = state.tool_calls["t1"]
    assert entry.can_cancel
    assert entry.display_title == "Run tests"

    state.apply(ToolCallSessionUpdate(tool_call_id="t1", status="completed"))
    assert entry.title == "Run tests"  # omitted: unchanged
    assert entry.status == "completed"
    assert entry.finished
    assert len(entry.content) == 1

    state.apply(ToolCallSessionUpdate(tool_call_id="t1", title=None, status=None, content=None))
    assert entry.title is None  # null: cleared
    assert entry.status == "pending"  # null status resets to pending
    assert entry.content == []


def test_tool_call_content_chunks_append() -> None:
    state = _state()
    state.apply(ToolCallContentChunk(tool_call_id="t", content=Terminal(terminal_id="term")))
    changes = state.apply(
        ToolCallContentChunk(tool_call_id="t", content=Content(content=TextContent(text="x")))
    )

    entry = state.tool_calls["t"]
    assert isinstance(changes[0], EntryChanged)
    assert len(entry.content) == 2
    assert entry.display_title == "t"


def test_terminal_snapshot_chunks_and_exit() -> None:
    state = _state()
    state.apply(
        TerminalUpdate(terminal_id="term", command="ls", output=TerminalOutput.from_bytes(b"a\n"))
    )
    changes = state.apply(TerminalOutputChunk.from_bytes("term", b"b\n"))
    terminal = state.terminals["term"]

    assert changes == [TerminalChanged(terminal, b"b\n")]
    assert bytes(terminal.output) == b"a\nb\n"
    assert not terminal.exited

    state.apply(TerminalUpdate(terminal_id="term", output=TerminalOutput.from_bytes(b"new")))
    assert bytes(terminal.output) == b"new"
    state.apply(TerminalUpdate(terminal_id="term", exit_status=TerminalExitStatus(exit_code=1)))
    assert terminal.exited
    assert terminal.command == "ls"


def test_plans_update_in_place_and_can_be_removed() -> None:
    state = _state()
    items = [SchemaPlanEntry(content="a", priority="high", status="pending")]
    state.apply(PlanUpdate(plan=PlanItems(plan_id="p", entries=items)))
    done = [SchemaPlanEntry(content="a", priority="high", status="completed")]
    changes = state.apply(PlanUpdate(plan=PlanItems(plan_id="p", entries=done)))

    plan = state.entries[0]
    assert isinstance(plan, PlanEntry)
    assert changes == [EntryChanged(plan)]
    assert isinstance(plan.content, PlanItems)
    assert plan.content.entries[0].status == "completed"

    state.apply(PlanRemoved(plan_id="p"))
    assert plan.removed
    assert state.apply(PlanRemoved(plan_id="p")) == []


def test_turn_states_and_stop_reasons() -> None:
    state = _state()
    state.apply(RunningStateSessionUpdate())
    assert state.running

    changes = state.apply(
        IdleStateSessionUpdate(stop_reason="error", error=Error(code=-32603, message="boom"))
    )

    assert not state.running
    assert changes[0] == SessionChanged("activity")
    notice = state.entries[-1]
    assert isinstance(notice, NoticeEntry)
    assert notice.severity == "error"
    assert notice.description == "boom"

    state.apply(IdleStateSessionUpdate(stop_reason="cancelled"))
    last = state.entries[-1]
    assert isinstance(last, NoticeEntry)
    assert last.title == "Cancelled"

    count = len(state.entries)
    state.apply(IdleStateSessionUpdate(stop_reason="end_turn"))
    assert len(state.entries) == count


def test_session_level_updates() -> None:
    state = _state()
    state.apply(
        AvailableCommandsUpdate(available_commands=[Command(name="plan", description="Plan")])
    )
    state.apply(SessionInfoUpdate(title="My session"))
    state.apply(UsageUpdate(used=50, size=100, cost=Cost(amount=0.5, currency="USD")))
    state.apply(UsageUpdate(used=60, size=100))  # omitted cost keeps the last one
    state.apply(Notice(severity="info", title="Hi"))

    assert [c.name for c in state.commands] == ["plan"]
    assert state.title == "My session"
    assert state.usage is not None
    assert state.usage.fraction == 0.6
    assert state.usage.cost is not None
    assert state.usage.cost.amount == 0.5
    assert isinstance(state.entries[-1], NoticeEntry)


def test_compactions() -> None:
    state = _state()
    state.apply(CompactionSummaryChunk(compaction_id="c", content=TextContent(text="sum")))
    state.apply(CompactionUpdate(compaction_id="c", status="completed"))

    entry = state.entries[0]
    assert isinstance(entry, CompactionEntry)
    assert entry.status == "completed"
    assert len(entry.summary) == 1


def test_unknown_updates_are_ignored() -> None:
    state = _state()
    update = UnknownSessionUpdate.model_validate({"sessionUpdate": "_custom", "x": 1})

    assert state.apply(update) == []
    assert state.entries == []
    assert isinstance(state.apply(ToolCallSessionUpdate(tool_call_id="z"))[0], EntryAdded)
    assert isinstance(state.entries[0], ToolCallEntry)


def _forkable() -> SessionState:
    """A session of two turns with a cursor after the first agent message."""
    state = _state()
    state.apply(UserMessageChunk(message_id="u1", content=TextContent(text="first")))
    state.apply(AgentMessageChunk(message_id="a1", content=TextContent(text="one")))
    state.apply(ToolCallSessionUpdate(tool_call_id="t1", title="Run", status="completed"))
    state.apply(UserMessageChunk(message_id="u2", content=TextContent(text="second")))
    state.apply(AgentMessageChunk(message_id="a2", content=TextContent(text="two")))
    state.title = "Work"
    return state


def _cursor(
    cursor_id: str, anchor: AgentMessageCursorAnchor | CursorToolCallAnchor
) -> CursorUpdate:
    caps = CursorCapabilities(fork=CursorForkCapabilities())
    return CursorUpdate(cursor_id=cursor_id, anchor=anchor, capabilities=caps)


def test_a_whole_fork_copies_the_timeline() -> None:
    state = _forkable()
    fork = state.forked("s2")

    assert fork.session_id == "s2"
    assert [type(e) for e in fork.entries] == [type(e) for e in state.entries]
    assert fork.title is None  # The agent titles the fork
    # Copies: the source goes on without changing the fork
    state.apply(AgentMessageChunk(message_id="a2", content=TextContent(text=" more")))
    last = fork.entries[-1]
    assert isinstance(last, MessageEntry)
    assert last.text == "two"
    assert fork.tool_calls["t1"] is fork.entries[2]


def test_a_fork_at_a_cursor_copies_what_comes_before_it() -> None:
    state = _forkable()
    state.apply(_cursor("c1", AgentMessageCursorAnchor(message_id="a1", position="after")))
    after = state.forked("s2", state.cursors["c1"])
    assert [e.text for e in after.entries if isinstance(e, MessageEntry)] == ["first", "one"]
    assert "t1" not in after.tool_calls

    state.apply(_cursor("c2", CursorToolCallAnchor(tool_call_id="t1", position="after")))
    with_tool = state.forked("s3", state.cursors["c2"])
    assert len(with_tool.entries) == 3
    assert with_tool.tool_calls["t1"] is with_tool.entries[2]

    state.apply(_cursor("c3", AgentMessageCursorAnchor(message_id="a1", position="before")))
    before = state.forked("s4", state.cursors["c3"])
    assert [e.text for e in before.entries if isinstance(e, MessageEntry)] == ["first"]
    assert not before.cursors  # The source's cursors are not the fork's
