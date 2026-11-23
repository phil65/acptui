"""Connection to an ACP v2 agent: launching it, negotiating the version, typed calls."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager, suppress
import json
import os
from typing import TYPE_CHECKING, Protocol, Self

from aclientp.exceptions import ProtocolVersionError, RequestError
from aclientp.schema.v2 import (
    AgentCapabilities,
    AuthMethod,
    CancelSessionNotification,
    CancelTerminalRequest,
    CancelToolCallRequest,
    ClientAuthCapabilities,
    ClientCapabilities,
    ClientSessionCapabilities,
    CloseSessionRequest,
    CommandsCapabilities,
    CommandsSkillCapabilities,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    ListSessionsRequest,
    ListSessionsResponse,
    LoginAuthRequest,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    ReplayFromStart,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResumeSessionRequest,
    ResumeSessionResponse,
    SetConfigOptionBooleanRequest,
    SetConfigOptionIdRequest,
    SetSessionConfigOptionResponse,
    TerminalAuthCapabilities,
    UpdateSessionNotification,
)
from aclientp.schema.v2.base import is_set
from aclientp.transports import spawn_stdio_transport
from aclientp.v2.client import Client, ClientSideConnection
from aclientp.ws_transport import connect_remote_transport
import anyio
from pydantic import ValidationError

import acptui


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path

    from aclientp.connection import StreamEvent
    from aclientp.schema.v2 import ContentBlock, PromptCapabilities, SessionCapabilities
    from anyio.abc import ByteReceiveStream, ByteSendStream, Process

    from acptui.config import AgentSpec


INITIALIZE_TIMEOUT = 60.0
"""Seconds to wait for the agent's ``initialize`` response (npx may download first)."""

CLIENT_INFO = Implementation(name="acptui", title="acptui", version=acptui.__version__)


class AgentEvents(Protocol):
    """What the UI provides to a connection: handlers for messages from the agent."""

    def session_update(self, notification: UpdateSessionNotification) -> None:
        """Handle a ``session/update`` (called in the order the agent sent them)."""
        ...

    async def request_permission(
        self, request: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        """Ask the user for permission."""
        ...

    def stderr_line(self, line: str) -> None:
        """A line the agent process wrote to stderr."""
        ...

    def traffic(self, direction: str, text: str) -> None:
        """A JSON-RPC message sent (``outgoing``) or received (``incoming``), as JSON text."""
        ...


class AgentError(Exception):
    """The agent could not be started or used; the message is meant for the user."""


class UnsupportedProtocolError(AgentError):
    """The agent does not speak ACP v2."""

    def __init__(self, agent_name: str, received: object) -> None:
        self.agent_name = agent_name
        self.received = received
        version = f"v{received}" if isinstance(received, int) else repr(received)
        super().__init__(
            f"{agent_name} speaks ACP {version}, but acptui only supports ACP v2.\n"
            "Update the agent (or its ACP adapter) to a version with ACP v2 support."
        )


class _ClientHandler(Client):
    """Forwards the agent's requests and notifications to the UI."""

    def __init__(self, events: AgentEvents) -> None:
        self._events = events

    async def session_update(self, params: UpdateSessionNotification) -> None:
        # Synchronous on purpose: updates are handled in their own tasks, which start
        # in arrival order; not awaiting keeps that order.
        self._events.session_update(params)

    async def request_permission(
        self, params: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        return await self._events.request_permission(params)


def _agent_name(result: object, fallback: str) -> str:
    """The agent name from a raw (possibly v1) ``initialize`` result."""
    if isinstance(result, dict):
        for key in ("info", "agentInfo"):
            info = result.get(key)
            if isinstance(info, dict):
                name = info.get("title") or info.get("name")
                if isinstance(name, str) and name:
                    return name
    return fallback


class AgentConnection:
    """A connection to one agent (local subprocess or remote) speaking ACP v2."""

    def __init__(self, spec: AgentSpec, events: AgentEvents, cwd: Path) -> None:
        self.spec = spec
        self.cwd = cwd
        self._events = events
        self._conn: ClientSideConnection | None = None
        self._process: Process | None = None
        self.init: InitializeResponse | None = None
        """The agent's ``initialize`` response, once connected."""

    # --- Lifecycle -------------------------------------------------------------------

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[Self]:
        """Start (or connect to) the agent and initialize the connection.

        Raises:
            UnsupportedProtocolError: The agent does not speak v2.
            AgentError: The agent could not be started or did not initialize.
        """
        async with AsyncExitStack() as stack:
            writer, reader = await self._open_transport(stack)
            conn = ClientSideConnection(
                _ClientHandler(self._events), writer, reader, observers=[self._observe]
            )
            stack.push_async_callback(conn.close)
            self._conn = conn
            self.init = await self._initialize(conn)
            try:
                yield self
            finally:
                self._conn = None

    async def _open_transport(
        self, stack: AsyncExitStack
    ) -> tuple[ByteSendStream, ByteReceiveStream]:
        spec = self.spec
        if spec.url is not None:
            try:
                reader, writer = await stack.enter_async_context(
                    connect_remote_transport(spec.url, headers=spec.resolved_headers())
                )
            except (OSError, ValueError) as exc:
                msg = f"Could not connect to {spec.url}: {exc}"
                raise AgentError(msg) from exc
            return writer, reader
        program, *args = spec.command
        env = {**os.environ, **spec.resolved_env()}
        try:
            reader, writer, process = await stack.enter_async_context(
                spawn_stdio_transport(
                    program, *args, env=env, cwd=self.cwd, on_stderr=self._events.stderr_line
                )
            )
        except OSError as exc:
            hint = f"\nInstall it with: {spec.install}" if spec.install else ""
            msg = f"Could not start {spec.name} ({spec.target}): {exc}{hint}"
            raise AgentError(msg) from exc
        self._process = process
        return writer, reader

    async def _initialize(self, conn: ClientSideConnection) -> InitializeResponse:
        capabilities = ClientCapabilities(
            auth=ClientAuthCapabilities(terminal=TerminalAuthCapabilities()),
            session=ClientSessionCapabilities(
                commands=CommandsCapabilities(skill=CommandsSkillCapabilities())
            ),
        )
        request = InitializeRequest(protocol_version=2, info=CLIENT_INFO, capabilities=capabilities)
        try:
            with anyio.fail_after(INITIALIZE_TIMEOUT):
                return await conn.initialize(request)
        except ProtocolVersionError as exc:
            name = _agent_name(exc.result, self.spec.name)
            raise UnsupportedProtocolError(name, exc.received) from exc
        except TimeoutError as exc:
            msg = (
                f"{self.spec.name} did not answer `initialize` within {INITIALIZE_TIMEOUT:.0f} s.\n"
                "It may not speak ACP on stdio, or it waits for a login (see the log: ctrl+l)."
            )
            raise AgentError(msg) from exc
        except RequestError as exc:
            if exc.code == -32022:  # noqa: PLR2004
                raise UnsupportedProtocolError(self.spec.name, exc.data) from exc
            msg = (
                f"{self.spec.name} rejected `initialize`: {exc} ({exc.code}).\n"
                "It may only speak ACP v1."
            )
            raise AgentError(msg) from exc
        except ConnectionError as exc:
            msg = f"{self.spec.name} exited during startup{self._exit_suffix()}."
            raise AgentError(msg) from exc
        except ValidationError as exc:
            # Claims v2, but the response does not have the v2 shape (e.g. an agent
            # echoing the requested version with a v1 response).
            msg = (
                f"{self.spec.name} answered `initialize` with an invalid ACP v2 response "
                f"({exc.error_count()} errors). It probably only speaks ACP v1."
            )
            raise AgentError(msg) from exc

    def _exit_suffix(self) -> str:
        code = self.exit_code
        return f" with exit code {code}" if code is not None else ""

    def _observe(self, event: StreamEvent) -> None:
        self._events.traffic(event.direction, json.dumps(event.message, ensure_ascii=False))

    @property
    def connection(self) -> ClientSideConnection:
        """The live JSON-RPC connection.

        Raises:
            AgentError: Not connected.
        """
        if self._conn is None:
            msg = "not connected to an agent"
            raise AgentError(msg)
        return self._conn

    @property
    def connected(self) -> bool:
        """Whether the connection is open."""
        return self._conn is not None and not self._conn.connection.closed

    @property
    def exit_code(self) -> int | None:
        """Exit code of a local agent that has exited."""
        return self._process.returncode if self._process is not None else None

    async def wait_closed(self) -> None:
        """Wait until the agent disconnects (or exits)."""
        await self.connection.wait_closed()
        if self._process is not None:
            # Give the exit code a moment to arrive after stdout closed.
            with anyio.move_on_after(2):
                await self._process.wait()

    # --- Capabilities ----------------------------------------------------------------

    @property
    def agent_name(self) -> str:
        """Display name of the agent."""
        if self.init is None:
            return self.spec.name
        info = self.init.info
        return info.title if is_set(info.title) and info.title else info.name

    @property
    def agent_version(self) -> str:
        """Version the agent reports."""
        return self.init.info.version if self.init is not None else ""

    @property
    def capabilities(self) -> AgentCapabilities:
        """The agent's capabilities (empty before initialization)."""
        if self.init is not None and is_set(self.init.capabilities):
            return self.init.capabilities
        return AgentCapabilities()

    @property
    def session_capabilities(self) -> SessionCapabilities | None:
        """The agent's session capabilities, if it has any."""
        session = self.capabilities.session
        return session if is_set(session) else None

    @property
    def prompt_capabilities(self) -> PromptCapabilities | None:
        """What the agent accepts in prompts beyond text and resource links."""
        session = self.session_capabilities
        if session is None or not is_set(session.prompt) or session.prompt is None:
            return None
        return session.prompt

    @property
    def supports_images(self) -> bool:
        """Whether prompts may contain images."""
        prompt = self.prompt_capabilities
        return prompt is not None and is_set(prompt.image) and prompt.image is not None

    @property
    def supports_embedded_context(self) -> bool:
        """Whether prompts may embed resource contents."""
        prompt = self.prompt_capabilities
        return (
            prompt is not None
            and is_set(prompt.embedded_context)
            and prompt.embedded_context is not None
        )

    @property
    def supports_skills(self) -> bool:
        """Whether prompts may contain ``skill`` blocks."""
        prompt = self.prompt_capabilities
        return prompt is not None and is_set(prompt.skill) and prompt.skill is not None

    @property
    def auth_methods(self) -> Sequence[AuthMethod]:
        """Authentication methods the agent offers."""
        if self.init is not None and is_set(self.init.auth_methods):
            return self.init.auth_methods
        return ()

    # --- Calls -----------------------------------------------------------------------

    async def new_session(self) -> NewSessionResponse:
        """Create a session in the working directory."""
        return await self.connection.new_session(NewSessionRequest(cwd=str(self.cwd)))

    async def resume_session(
        self, session_id: str, *, replay: bool = True
    ) -> ResumeSessionResponse:
        """Resume a session, replaying its history."""
        request = ResumeSessionRequest(
            session_id=session_id,
            cwd=str(self.cwd),
            replay_from=ReplayFromStart() if replay else None,
        )
        return await self.connection.resume_session(request)

    async def list_sessions(self, cursor: str | None = None) -> ListSessionsResponse:
        """List sessions of the working directory."""
        request = ListSessionsRequest(cwd=str(self.cwd), cursor=cursor)
        return await self.connection.list_sessions(request)

    async def close_session(self, session_id: str) -> None:
        """Close a session (ignoring agents that do not support it)."""
        with suppress(RequestError):
            await self.connection.close_session(CloseSessionRequest(session_id=session_id))

    async def prompt(self, session_id: str, blocks: Sequence[ContentBlock]) -> PromptResponse:
        """Send a user message; returns once the agent inserted it."""
        return await self.connection.prompt(PromptRequest(session_id=session_id, prompt=blocks))

    async def cancel(self, session_id: str) -> None:
        """Cancel the session's foreground work."""
        await self.connection.cancel(CancelSessionNotification(session_id=session_id))

    async def cancel_tool_call(self, session_id: str, tool_call_id: str) -> None:
        """Cancel one tool call."""
        request = CancelToolCallRequest(session_id=session_id, tool_call_id=tool_call_id)
        await self.connection.cancel_tool_call(request)

    async def cancel_terminal(self, session_id: str, terminal_id: str) -> None:
        """Cancel one terminal."""
        request = CancelTerminalRequest(session_id=session_id, terminal_id=terminal_id)
        await self.connection.cancel_terminal(request)

    async def set_config_option(
        self, session_id: str, config_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        """Set a select (``str`` value) or boolean config option."""
        if isinstance(value, bool):
            request: SetConfigOptionIdRequest | SetConfigOptionBooleanRequest = (
                SetConfigOptionBooleanRequest(
                    session_id=session_id, config_id=config_id, value=value
                )
            )
        else:
            request = SetConfigOptionIdRequest(
                session_id=session_id, config_id=config_id, value=value
            )
        return await self.connection.set_session_config_option(request)

    async def login(self, method_id: str) -> None:
        """Log in with an ``agent`` auth method."""
        await self.connection.login(LoginAuthRequest(method_id=method_id))
