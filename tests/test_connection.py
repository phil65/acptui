"""The protocol layer against real agent subprocesses: the demo agent and a v1 agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2 import BooleanSessionConfigOption, SelectSessionConfigOption, TextContent
import pytest

from acptui.protocol.connection import AgentConnection, AgentError, UnsupportedProtocolError
from acptui.protocol.state import MessageEntry, NoticeEntry, PlanEntry, ToolCallEntry
from tests.helpers import RecordingEvents, wait_for


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from aclientp.schema.v2 import SessionConfigOption

    from acptui.config import AgentSpec


def option_values(options: Sequence[SessionConfigOption]) -> dict[str, str | bool]:
    """Current values of select and boolean options."""
    return {
        o.config_id: o.current_value
        for o in options
        if isinstance(o, SelectSessionConfigOption | BooleanSessionConfigOption)
    }


async def test_tour_with_permission(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents(answer="allow")
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        assert connection.agent_name == "Demo agent"
        assert connection.supports_images
        assert connection.supports_embedded_context
        assert connection.supports_skills
        session = await connection.new_session()
        state = events.track(session.session_id, str(workdir))
        response = await connection.prompt(session.session_id, [TextContent(text="/tour")])
        await wait_for(lambda: state.stop_reason == "end_turn")

    assert response.message_id.startswith("user-")
    kinds = [type(e).__name__ for e in state.entries]
    assert kinds[:3] == ["MessageEntry", "MessageEntry", "PlanEntry"]  # user message, thought, plan
    tools = [e for e in state.entries if isinstance(e, ToolCallEntry)]
    assert [t.kind for t in tools] == ["read", "execute", "edit"]
    assert all(t.status == "completed" for t in tools)
    terminal = state.terminals["term-1"]
    assert terminal.exited
    assert b"README.md" in terminal.output
    plan = next(e for e in state.entries if isinstance(e, PlanEntry))
    assert plan.content.type == "items"
    assert len(events.permissions) == 1
    answer = state.entries[-1]
    assert isinstance(answer, MessageEntry)
    assert "## Done" in answer.text
    assert state.usage is not None
    assert events.traffic_lines[0][0] == "outgoing"


async def test_rejected_permission_fails_the_tool_call(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents(answer="reject")
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        session = await connection.new_session()
        state = events.track(session.session_id, str(workdir))
        await connection.prompt(session.session_id, [TextContent(text="go")])
        await wait_for(lambda: state.stop_reason == "end_turn")

    assert state.tool_calls["edit-1"].status == "failed"


async def test_config_options_and_mode(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents(answer=None)
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        session = await connection.new_session()
        assert session.config_options
        response = await connection.set_config_option(session.session_id, "mode", "auto")
        values = option_values(response.config_options)
        assert values["mode"] == "auto"
        state = events.track(session.session_id, str(workdir))
        await connection.prompt(session.session_id, [TextContent(text="go")])
        await wait_for(lambda: state.stop_reason == "end_turn")
        toggled = await connection.set_config_option(session.session_id, "thinking", False)

    assert events.permissions == []  # auto mode does not ask
    assert option_values(toggled.config_options)["thinking"] is False


async def test_cancel_and_failures(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents()
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        session = await connection.new_session()
        state = events.track(session.session_id, str(workdir))
        await connection.prompt(session.session_id, [TextContent(text="/slow")])
        await wait_for(lambda: state.running)
        await connection.cancel(session.session_id)
        await wait_for(lambda: state.stop_reason == "cancelled")
        await connection.prompt(session.session_id, [TextContent(text="/fail")])
        await wait_for(lambda: state.stop_reason == "error")

    notices = [e for e in state.entries if isinstance(e, NoticeEntry)]
    assert [n.title for n in notices] == ["Cancelled", "The turn failed"]
    assert notices[1].description == "Demo failure: the model is unavailable"


async def test_list_and_resume_with_replay(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents()
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        session = await connection.new_session()
        state = events.track(session.session_id, str(workdir))
        await connection.prompt(session.session_id, [TextContent(text="/echo hi there")])
        await wait_for(lambda: state.stop_reason == "end_turn")
        listed = await connection.list_sessions()
        replay = events.track(session.session_id, str(workdir))
        replay.replaying = True
        await connection.resume_session(session.session_id)

    assert [s.session_id for s in listed.sessions] == [session.session_id]
    assert not replay.replaying
    texts = [e.text for e in replay.entries if isinstance(e, MessageEntry)]
    assert texts == ["/echo hi there", "hi there"]


async def test_crash_reports_exit_code_and_stderr(demo_spec: AgentSpec, workdir: Path) -> None:
    events = RecordingEvents()
    connection = AgentConnection(demo_spec, events, workdir)
    async with connection.open():
        session = await connection.new_session()
        await connection.prompt(session.session_id, [TextContent(text="/crash")])
        await connection.wait_closed()
        assert not connection.connected
        assert connection.exit_code == 3
    await wait_for(lambda: any("simulated crash" in line for line in events.stderr))


async def test_v1_agent_is_rejected_clearly(v1_spec: AgentSpec, workdir: Path) -> None:
    connection = AgentConnection(v1_spec, RecordingEvents(), workdir)
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        async with connection.open():
            pass

    assert exc_info.value.received == 1
    assert exc_info.value.agent_name == "old-agent"
    assert "speaks ACP v1" in str(exc_info.value)
    assert "only supports ACP v2" in str(exc_info.value)


async def test_v1_shaped_response_claiming_v2(fake_v2_spec: AgentSpec, workdir: Path) -> None:
    connection = AgentConnection(fake_v2_spec, RecordingEvents(), workdir)
    with pytest.raises(AgentError, match="invalid ACP v2 response"):
        async with connection.open():
            pass


async def test_missing_program(missing_spec: AgentSpec, workdir: Path) -> None:
    connection = AgentConnection(missing_spec, RecordingEvents(), workdir)
    with pytest.raises(AgentError, match=r"(?s)Could not start Missing.*pip install it"):
        async with connection.open():
            pass


async def test_unreachable_url(workdir: Path) -> None:
    from acptui.config import url_spec

    connection = AgentConnection(url_spec("ftp://example.invalid/acp"), RecordingEvents(), workdir)
    with pytest.raises(AgentError, match="Could not connect"):
        async with connection.open():
            pass


async def test_connect_sends_a_bearer_token_to_a_websocket_agent(workdir: Path) -> None:
    """Aclientp 0.6 WebSocket servers authenticate the upgrade; ``-H`` carries the token."""
    import asyncio
    import socket

    from aclientp import WebSocketTransport
    from aclientp.server_auth import bearer_token_auth
    from aclientp.transports import serve

    from acptui.config import url_spec
    from acptui.demo import DemoAgent
    from acptui.probe import probe

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    transport = WebSocketTransport(
        host="127.0.0.1", port=port, authenticate=bearer_token_auth("s3cret")
    )
    shutdown = asyncio.Event()
    server = asyncio.create_task(
        serve(v2_agent=DemoAgent, transport=transport, shutdown_event=shutdown)
    )
    url = f"ws://127.0.0.1:{port}"
    try:
        async with asyncio.timeout(20):
            while True:  # until the server listens
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        ok = await probe(url_spec(url, {"Authorization": "Bearer s3cret"}), workdir)
        assert ok.v2, ok.error
        missing = await probe(url_spec(url), workdir)
        assert not missing.v2
        assert "401" in missing.error
        wrong = await probe(url_spec(url, {"Authorization": "Bearer nope"}), workdir)
        assert not wrong.v2
        assert "401" in wrong.error
    finally:
        shutdown.set()
        await asyncio.wait_for(server, 10)
