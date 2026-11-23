"""Every v2 client-facing method, notification and update kind, against the demo agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AudioContent,
    BackgroundTerminal,
    BlobResourceContents,
    CommandPermissionSubject,
    Content,
    Diff,
    EmbeddedResource,
    FormElicitationRequest,
    ImageContent,
    McpServerHttp,
    McpServerStdio,
    PlanItems,
    PlanMarkdown,
    ResourceLink,
    SelectSessionConfigOption,
    SessionReference,
    SkillReference,
    TextContent,
    UnknownContent,
    UnknownToolCallContent,
    UrlElicitationRequest,
)
from aclientp.schema.v2.base import is_set
import pytest

from acptui.protocol.connection import AgentConnection, SessionSetup
from acptui.protocol.content import content_markdown, describe_block
from acptui.protocol.state import (
    CompactionEntry,
    MessageEntry,
    NoticeEntry,
    PlanEntry,
    SessionState,
    SubagentEntry,
)
from acptui.setup import initial_options
from tests.helpers import RecordingEvents, wait_for


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from acptui.config import AgentSpec


@pytest.fixture
async def demo(
    demo_spec: AgentSpec, workdir: Path
) -> AsyncIterator[tuple[AgentConnection, RecordingEvents]]:
    """A connected demo agent."""
    events = RecordingEvents()
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        yield connection, events


async def run(connection: AgentConnection, events: RecordingEvents, text: str) -> SessionState:
    """Start a session, send one prompt and wait for the turn to end."""
    session = await connection.new_session()
    state = events.track(session.session_id, str(connection.cwd))
    await connection.prompt(session.session_id, [TextContent(text=text)])
    await wait_for(lambda: state.stop_reason is not None)
    return state


def last_text(state: SessionState) -> str:
    """Text of the last agent message."""
    return next(
        e.text for e in reversed(state.entries) if isinstance(e, MessageEntry) and e.role == "agent"
    )


async def test_capabilities_are_all_detected(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, _ = demo
    assert connection.supports_images
    assert connection.supports_embedded_context
    assert connection.supports_skills
    assert connection.supports_additional_directories
    assert connection.supports_system_prompt
    assert connection.supports_system_prompt_override
    assert connection.supports_delete
    assert connection.supports_fork
    assert connection.supports_archive
    assert connection.supports_status
    assert connection.supports_options
    assert connection.supports_title
    assert connection.supports_auth_status
    assert connection.supports_providers
    assert connection.supports_login
    assert connection.mcp_transports() == {"stdio", "http"}
    assert [m.method_id for m in connection.auth_methods] == ["demo-login", "demo-terminal"]


async def test_every_content_block_kind(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, events = demo
    state = await run(connection, events, "/media")

    message = next(e for e in state.entries if isinstance(e, MessageEntry) and e.role == "agent")
    kinds = {type(block) for block in message.blocks}
    assert {
        TextContent,
        ImageContent,
        AudioContent,
        ResourceLink,
        EmbeddedResource,
        UnknownContent,
    } <= kinds
    markdown = content_markdown(message.blocks, connection.cwd)
    assert "**Project readme** `README.md`" in markdown
    assert "```python\nprint('hello')\n```" in markdown
    assert "A note only for the model" not in markdown  # audience: assistant only
    assert "image: image/png" in markdown
    assert "audio: audio/wav" in markdown
    assert "[_demo_widget]" in markdown
    blob = next(
        b
        for b in message.blocks
        if isinstance(b, EmbeddedResource) and isinstance(b.resource, BlobResourceContents)
    )
    assert "data.bin" in content_markdown([blob], connection.cwd)
    assert describe_block(SkillReference(name="review")) == "skill: /review"

    tool = state.tool_calls["media-tool"]
    assert tool.raw_input == {"prompt": "a dot"}
    assert tool.raw_output == {"files": 2}
    assert [type(c) for c in tool.content] == [Content, Content, UnknownToolCallContent, Diff, Diff]


async def test_subagent_sessions_and_session_messages(
    demo: tuple[AgentConnection, RecordingEvents],
) -> None:
    connection, events = demo
    state = await run(connection, events, "/subagent")

    (child,) = state.subagents.values()
    assert isinstance(state.entries[1], SubagentEntry)
    assert child.title == "Researcher"
    assert child.description == "Looks things up for the main agent"
    assert child.activity == "idle"
    assert not child.can_cancel  # cleared when it finished
    child_state = events.states[child.session_id]
    assert last_text(child_state) == "I found the README in the project root."
    assert "child-read" in child_state.tool_calls
    messages = [e for e in state.entries if isinstance(e, MessageEntry) and e.role == "session"]
    assert [m.text for m in messages] == ["Please find the README.", "Found it: README.md"]
    assert messages[1].sender_session_id == child.session_id
    assert isinstance(state.tool_calls["delegate-1"].content[0], SessionReference)


async def test_form_and_url_elicitation(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, events = demo
    form_state = await run(connection, events, "/elicit")
    url_state = await run(connection, events, "/url")

    form, url = events.elicitations
    assert isinstance(form, FormElicitationRequest)
    assert set(form.requested_schema.properties) == {
        "name",
        "license",
        "level",
        "ratio",
        "count",
        "public",
        "tags",
    }
    assert '"name":"acptui"' in last_text(form_state)
    assert isinstance(url, UrlElicitationRequest)
    assert events.completed == [url.elicitation_id]
    assert last_text(url_state) == "URL elicitation: accept"


async def test_compaction_suggestions_plans_notices(
    demo: tuple[AgentConnection, RecordingEvents],
) -> None:
    connection, events = demo
    compacted = await run(connection, events, "/compact")
    suggested = await run(connection, events, "/suggest")
    planned = await run(connection, events, "/plans")
    noticed = await run(connection, events, "/notice")

    compaction = next(e for e in compacted.entries if isinstance(e, CompactionEntry))
    assert compaction.status == "completed"
    assert compaction.summary
    assert len(suggested.suggestions) == 2
    plans = [e for e in planned.entries if isinstance(e, PlanEntry)]
    assert isinstance(plans[0].content, PlanMarkdown)
    assert isinstance(plans[1].content, PlanItems)
    assert plans[1].removed
    severities = [e.severity for e in noticed.entries if isinstance(e, NoticeEntry)]
    assert severities == ["info", "warning", "error"]


async def test_background_work_usage_and_cursors(
    demo: tuple[AgentConnection, RecordingEvents],
) -> None:
    connection, events = demo
    state = await run(connection, events, "/background")

    assert state.background == [BackgroundTerminal(terminal_id="bg-1")]
    terminal = state.terminals["bg-1"]
    assert terminal.size is not None
    assert terminal.size.cols == 80
    assert state.last_turn_usage is not None
    assert state.last_turn_usage.total_tokens == 1500
    assert state.usage is not None
    assert state.usage.cost is not None
    (cursor,) = state.cursors.values()
    assert cursor.can_fork
    assert cursor.anchor is not None


async def test_extensions_and_command_permission(
    demo: tuple[AgentConnection, RecordingEvents],
) -> None:
    connection, events = demo
    await run(connection, events, "/ext")
    events.answer = "yes"
    state = await run(connection, events, "/command")

    assert events.extensions == [("_demo/hello", '{"greeting": "hi"}')]
    subject = events.permissions[-1].subject
    assert isinstance(subject, CommandPermissionSubject)
    assert subject.command == "rm -rf build"
    assert last_text(state) == "Permission outcome: yes"


async def test_cancel_a_tool_call(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, events = demo
    session = await connection.new_session()
    state = events.track(session.session_id, str(connection.cwd))
    await connection.prompt(session.session_id, [TextContent(text="/cancel-tool")])
    await wait_for(lambda: "long-1" in state.tool_calls and state.tool_calls["long-1"].can_cancel)
    await connection.cancel_tool_call(session.session_id, "long-1")
    await wait_for(lambda: state.stop_reason is not None)

    assert state.tool_calls["long-1"].status == "cancelled"
    assert not state.tool_calls["long-1"].can_cancel


async def test_session_setup_is_sent(
    demo: tuple[AgentConnection, RecordingEvents], workdir: Path
) -> None:
    connection, events = demo
    setup = SessionSetup(
        additional_directories=(str(workdir / "src"),),
        mcp_servers=(
            McpServerStdio(name="files", command="/usr/bin/true"),
            McpServerHttp(name="web", url="https://example.com/mcp"),
        ),
        system_prompt="Be brief.",
        system_prompt_mode="override",
        config_options=initial_options({"mode": "auto", "model": "demo-smart", "nope": "x"}),
    )
    session = await connection.new_session(setup)
    state = events.track(session.session_id, str(workdir))
    await connection.prompt(session.session_id, [TextContent(text="/setup")])
    await wait_for(lambda: state.stop_reason is not None)

    text = last_text(state)
    assert f"additional directories: {workdir / 'src'}" in text
    assert "MCP servers: files, web" in text
    assert "system prompt: override: Be brief." in text
    assert "mode: auto, model: demo-smart" in text
    assert is_set(session.config_options)
    values = {
        o.config_id: o.current_value
        for o in session.config_options
        if isinstance(o, SelectSessionConfigOption)
    }
    assert values == {"mode": "auto", "model": "demo-smart"}


async def test_session_management(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, events = demo
    state = await run(connection, events, "/echo one")
    session_id = state.session_id

    assert await connection.set_title(session_id, "Renamed") == "Renamed"
    status = await connection.session_status(session_id)
    assert status.status == "open"
    options = await connection.session_options()
    assert options.config_options

    (cursor_id,) = state.cursors
    fork = await connection.fork_session(session_id, cursor_id)
    whole = await connection.fork_session(session_id)
    assert fork.session_id != whole.session_id != session_id

    await connection.archive_session(session_id)
    listed = await connection.list_sessions()
    archived = await connection.list_sessions(archived="only")
    assert session_id not in {s.session_id for s in listed.sessions}
    assert [s.session_id for s in archived.sessions] == [session_id]
    await connection.archive_session(session_id, archived=False)
    await connection.delete_session(session_id)
    assert session_id not in {s.session_id for s in (await connection.list_sessions()).sessions}
    with pytest.raises(RequestError):
        await connection.session_status(session_id)


async def test_skill_prompt(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, events = demo
    session = await connection.new_session()
    assert is_skill_listed(session.available_commands)
    state = events.track(session.session_id, str(connection.cwd))
    await connection.prompt(
        session.session_id, [SkillReference(name="review"), TextContent(text="the code")]
    )
    await wait_for(lambda: state.stop_reason is not None)
    assert last_text(state) == "Running skill **review**."


def is_skill_listed(commands: object) -> bool:
    """Whether a ``skill`` command is advertised."""
    return isinstance(commands, list) and any(getattr(c, "type", None) == "skill" for c in commands)


async def test_authentication(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPTUI_DEMO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("ACPTUI_DEMO_AUTH_FILE", str(workdir / "no-such-marker"))
    events = RecordingEvents()
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        with pytest.raises(RequestError) as exc_info:
            await connection.new_session()
        assert exc_info.value.code == -32000
        assert (await connection.auth_status()).state == "unauthenticated"
        await connection.login("demo-login")
        await wait_for(lambda: bool(events.auth_updates))
        assert events.auth_updates[-1].state == "authenticated"
        status = await connection.auth_status()
        assert status.state == "authenticated"
        await connection.new_session()
        await connection.logout()
        await wait_for(lambda: events.auth_updates[-1].state == "unauthenticated")


async def test_providers(demo: tuple[AgentConnection, RecordingEvents]) -> None:
    connection, _ = demo
    providers = {p.provider_id: p for p in (await connection.list_providers()).providers}
    assert providers["main"].required

    await connection.set_provider(
        "backup", "openai", "https://openai.example.com", {"Authorization": "x"}
    )
    await connection.disable_provider("backup")
    with pytest.raises(RequestError):
        await connection.disable_provider("main")
    backup = next(
        p for p in (await connection.list_providers()).providers if p.provider_id == "backup"
    )
    assert backup.current is None
