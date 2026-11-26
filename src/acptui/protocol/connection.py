"""Connection to an ACP v2 agent: launching it, negotiating the version, typed calls.

:class:`AgentConnection` covers the client side of the whole v2 surface: every
agent method has a typed call here, and every client method / notification is
forwarded to :class:`AgentEvents` (the UI).
"""

from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
import os
from typing import TYPE_CHECKING, Protocol, Self
import uuid

from aclientp.exceptions import InvalidResponseError, ProtocolVersionError, RequestError
from aclientp.schema.v2 import (
    AgentCapabilities,
    ArchiveSessionRequest,
    AuthStatusRequest,
    AuthStatusResponse,
    CancelSessionNotification,
    CancelTerminalRequest,
    CancelToolCallRequest,
    ClientAuthCapabilities,
    ClientCapabilities,
    ClientSessionCapabilities,
    CloseSessionRequest,
    CommandsCapabilities,
    CommandsSkillCapabilities,
    DeleteSessionRequest,
    DisableProviderRequest,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    ElicitationUrlCapabilities,
    ForkSessionRequest,
    ForkSessionResponse,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    ListProvidersRequest,
    ListProvidersResponse,
    ListSessionsRequest,
    ListSessionsResponse,
    LoginAuthRequest,
    LogoutAuthRequest,
    McpServerAcp,
    McpServerHttp,
    McpServerStdio,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    PromptSuggestionsCapabilities,
    ReplayFromStart,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResumeSessionRequest,
    ResumeSessionResponse,
    SessionOptionsRequest,
    SessionOptionsResponse,
    SessionStatusRequest,
    SessionStatusResponse,
    SetConfigOptionBooleanRequest,
    SetConfigOptionIdRequest,
    SetProviderRequest,
    SetSessionConfigOptionResponse,
    SetSessionTitleRequest,
    SystemPrompt,
    TerminalAuthCapabilities,
    TextContent,
    UnarchiveSessionRequest,
    UpdateSessionNotification,
    supports,
)
from aclientp.schema.v2.base import JsonValue, is_set
from aclientp.transports import spawn_stdio_transport
from aclientp.v2.client import Client, ClientSideConnection
from aclientp.ws_transport import connect_remote_transport
import anyio

import acptui


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from aclientp.connection import StreamEvent
    from aclientp.schema.v2 import (
        AuthMethod,
        AuthStatusUpdateNotification,
        CompleteElicitationNotification,
        ContentBlock,
        CreateElicitationRequest,
        CreateElicitationResponse,
        InitialSessionConfigOption,
        McpServer,
    )
    from anyio.abc import ByteReceiveStream, ByteSendStream, Process

    from acptui.config import AgentSpec


INITIALIZE_TIMEOUT = 60.0
"""Seconds to wait for the agent's ``initialize`` response (npx may download first)."""

CLIENT_INFO = Implementation(name="acptui", title="acptui", version=acptui.__version__)

CLIENT_CAPABILITIES = ClientCapabilities(
    auth=ClientAuthCapabilities(terminal=TerminalAuthCapabilities()),
    elicitation=ElicitationCapabilities(
        form=ElicitationFormCapabilities(), url=ElicitationUrlCapabilities()
    ),
    session=ClientSessionCapabilities(
        prompt_suggestions=PromptSuggestionsCapabilities(),
        commands=CommandsCapabilities(skill=CommandsSkillCapabilities()),
    ),
)
"""Everything acptui supports: terminal auth, form and URL elicitation, prompt
suggestions (text and resource links) and skill commands."""


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

    async def create_elicitation(
        self, request: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        """Ask the user for structured input (a form or a URL to visit)."""
        ...

    def complete_elicitation(self, notification: CompleteElicitationNotification) -> None:
        """A URL elicitation was completed."""
        ...

    def auth_status_update(self, notification: AuthStatusUpdateNotification) -> None:
        """The agent's authentication status changed."""
        ...

    def ext_notification(self, method: str, params: JsonValue) -> None:
        """A custom (``_``-prefixed) notification."""
        ...

    def stderr_line(self, line: str) -> None:
        """A line the agent process wrote to stderr."""
        ...

    def traffic(self, direction: str, text: str) -> None:
        """A JSON-RPC message sent (``outgoing``) or received (``incoming``), as JSON text."""
        ...


@dataclass(frozen=True)
class SessionSetup:
    """What acptui sends on ``session/new``, ``session/resume`` and ``session/fork``.

    Fields the agent has no capability for are left out (MCP servers are filtered by
    the transports it supports).
    """

    additional_directories: tuple[str, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    system_prompt: str | None = None
    system_prompt_mode: str = "append"
    config_options: tuple[InitialSessionConfigOption, ...] = ()


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

    async def create_elicitation(
        self, params: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        return await self._events.create_elicitation(params)

    async def complete_elicitation(self, params: CompleteElicitationNotification) -> None:
        self._events.complete_elicitation(params)

    async def auth_status_update(self, params: AuthStatusUpdateNotification) -> None:
        self._events.auth_status_update(params)

    async def ext_notification(self, method: str, params: JsonValue) -> None:
        self._events.ext_notification(method, params)

    # ext_method (custom requests) and mcp/message keep the base behavior: -32601.
    # acptui hosts no ACP-transport MCP servers, so agents have no reason to send
    # mcp/message to it.


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
        capabilities = CLIENT_CAPABILITIES
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
        except InvalidResponseError as exc:
            # Claims v2, but the response does not have the v2 shape (e.g. an agent
            # echoing the requested version with a v1 response).
            msg = (
                f"{self.spec.name} answered `initialize` with an invalid ACP v2 response "
                f"({len(exc.errors)} errors). It probably only speaks ACP v1."
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
    def auth_methods(self) -> Sequence[AuthMethod]:
        """Authentication methods the agent offers."""
        if self.init is not None and is_set(self.init.auth_methods):
            return self.init.auth_methods
        return ()

    def supports(self, path: str) -> bool:
        """Whether the agent has a capability, by its dotted wire path (``session.fork``)."""
        return self.init is not None and supports(self.init.capabilities, path)

    @property
    def supports_images(self) -> bool:
        """Whether prompts may contain images."""
        return self.supports("session.prompt.image")

    @property
    def supports_audio(self) -> bool:
        """Whether prompts may contain audio."""
        return self.supports("session.prompt.audio")

    @property
    def supports_embedded_context(self) -> bool:
        """Whether prompts may embed resource contents."""
        return self.supports("session.prompt.embeddedContext")

    @property
    def supports_skills(self) -> bool:
        """Whether prompts may contain ``skill`` blocks."""
        return self.supports("session.prompt.skill")

    @property
    def supports_additional_directories(self) -> bool:
        """``additionalDirectories`` on session setup."""
        return self.supports("session.additionalDirectories")

    @property
    def supports_system_prompt(self) -> bool:
        """``systemPrompt`` on session setup."""
        return self.supports("session.systemPrompt")

    @property
    def supports_system_prompt_override(self) -> bool:
        """The ``override`` system prompt mode."""
        return self.supports("session.systemPrompt.mode.override")

    @property
    def supports_delete(self) -> bool:
        """``session/delete``."""
        return self.supports("session.delete")

    @property
    def supports_fork(self) -> bool:
        """``session/fork``."""
        return self.supports("session.fork")

    @property
    def supports_archive(self) -> bool:
        """``session/archive`` and ``session/unarchive``."""
        return self.supports("session.archive")

    @property
    def supports_status(self) -> bool:
        """``session/status``."""
        return self.supports("session.status")

    @property
    def supports_options(self) -> bool:
        """``session/options``."""
        return self.supports("session.options")

    @property
    def supports_title(self) -> bool:
        """``session/set_title``."""
        return self.supports("session.title")

    @property
    def supports_auth_status(self) -> bool:
        """``auth/status`` (and ``auth/status_update``)."""
        return self.supports("auth.status")

    @property
    def supports_providers(self) -> bool:
        """``providers/list``, ``providers/set`` and ``providers/disable``."""
        return self.supports("providers")

    @property
    def supports_login(self) -> bool:
        """``auth/login`` and ``auth/logout`` (any auth methods advertised)."""
        return bool(self.auth_methods)

    def mcp_transports(self) -> set[str]:
        """MCP transports the agent accepts on session setup."""
        return {name for name in ("stdio", "http", "acp") if self.supports(f"session.mcp.{name}")}

    # --- Calls -----------------------------------------------------------------------

    def _setup_fields(self, setup: SessionSetup) -> _SetupFields:
        directories = (
            list(setup.additional_directories)
            if setup.additional_directories and self.supports_additional_directories
            else None
        )
        transports = self.mcp_transports()
        servers = [server for server in setup.mcp_servers if _mcp_transport(server) in transports]
        system_prompt = None
        if setup.system_prompt and self.supports_system_prompt:
            mode = setup.system_prompt_mode
            if mode == "override" and not self.supports_system_prompt_override:
                mode = "append"
            system_prompt = SystemPrompt(mode=mode, content=[TextContent(text=setup.system_prompt)])
        return _SetupFields(directories, servers, system_prompt, list(setup.config_options))

    async def new_session(self, setup: SessionSetup | None = None) -> NewSessionResponse:
        """Create a session in the working directory."""
        fields = self._setup_fields(setup or SessionSetup())
        request = NewSessionRequest(cwd=str(self.cwd), mcp_servers=fields.mcp_servers)
        _apply_setup(request, fields)
        return await self.connection.new_session(request)

    async def resume_session(
        self, session_id: str, *, replay: bool = True, setup: SessionSetup | None = None
    ) -> ResumeSessionResponse:
        """Resume a session, replaying its history."""
        fields = self._setup_fields(setup or SessionSetup())
        request = ResumeSessionRequest(
            session_id=session_id,
            cwd=str(self.cwd),
            replay_from=ReplayFromStart() if replay else None,
            mcp_servers=fields.mcp_servers,
        )
        _apply_setup(request, fields)
        return await self.connection.resume_session(request)

    async def fork_session(
        self, session_id: str, cursor_id: str | None = None, setup: SessionSetup | None = None
    ) -> ForkSessionResponse:
        """Fork a session (at a cursor, or whole)."""
        fields = self._setup_fields(setup or SessionSetup())
        request = ForkSessionRequest(
            session_id=session_id,
            cwd=str(self.cwd),
            cursor_id=cursor_id,
            mcp_servers=fields.mcp_servers,
        )
        _apply_setup(request, fields)
        return await self.connection.fork_session(request)

    async def list_sessions(
        self, cursor: str | None = None, *, archived: str | None = None
    ) -> ListSessionsResponse:
        """List sessions of the working directory (``archived``: exclude, include, only)."""
        request = ListSessionsRequest(cwd=str(self.cwd), cursor=cursor)
        if archived is not None:
            request.archived = archived
        return await self.connection.list_sessions(request)

    async def close_session(self, session_id: str) -> None:
        """Close a session (errors are ignored: the session is left anyway)."""
        with suppress(RequestError):
            await self.connection.close_session(CloseSessionRequest(session_id=session_id))

    async def delete_session(self, session_id: str) -> None:
        """Delete a session from the agent's list."""
        await self.connection.delete_session(DeleteSessionRequest(session_id=session_id))

    async def archive_session(self, session_id: str, *, archived: bool = True) -> None:
        """Archive (or unarchive) a session."""
        if archived:
            await self.connection.archive_session(ArchiveSessionRequest(session_id=session_id))
        else:
            await self.connection.unarchive_session(UnarchiveSessionRequest(session_id=session_id))

    async def session_status(self, session_id: str) -> SessionStatusResponse:
        """Whether the agent has the session open or saved, and its state."""
        return await self.connection.session_status(SessionStatusRequest(session_id=session_id))

    async def session_options(self, session_id: str | None = None) -> SessionOptionsResponse:
        """Config options and commands a session would start with."""
        request = SessionOptionsRequest(cwd=str(self.cwd), session_id=session_id)
        return await self.connection.session_options(request)

    async def set_title(self, session_id: str, title: str) -> str:
        """Rename a session; returns the title the agent stored."""
        request = SetSessionTitleRequest(session_id=session_id, title=title)
        return (await self.connection.set_session_title(request)).title

    async def prompt(self, session_id: str, blocks: Sequence[ContentBlock]) -> PromptResponse:
        """Send a user message; returns once the agent inserted it.

        Each call gets a fresh idempotency key, so a retried request is not inserted twice.
        """
        request = PromptRequest(
            session_id=session_id, prompt=blocks, idempotency_key=str(uuid.uuid4())
        )
        return await self.connection.prompt(request)

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

    async def logout(self) -> None:
        """Log out."""
        await self.connection.logout(LogoutAuthRequest())

    async def auth_status(self) -> AuthStatusResponse:
        """The agent's authentication state and accounts."""
        return await self.connection.auth_status(AuthStatusRequest())

    async def list_providers(self) -> ListProvidersResponse:
        """The agent's configurable LLM providers."""
        return await self.connection.list_providers(ListProvidersRequest())

    async def set_provider(
        self,
        provider_id: str,
        api_type: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Configure an LLM provider."""
        request = SetProviderRequest(provider_id=provider_id, api_type=api_type, base_url=base_url)
        if headers:
            request.headers = dict(headers)
        await self.connection.set_provider(request)

    async def disable_provider(self, provider_id: str) -> None:
        """Disable an LLM provider."""
        await self.connection.disable_provider(DisableProviderRequest(provider_id=provider_id))


@dataclass
class _SetupFields:
    additional_directories: list[str] | None
    mcp_servers: list[McpServer] = field(default_factory=list)
    system_prompt: SystemPrompt | None = None
    config_options: list[InitialSessionConfigOption] = field(default_factory=list)


def _apply_setup(
    request: NewSessionRequest | ResumeSessionRequest | ForkSessionRequest, fields: _SetupFields
) -> None:
    """Set the optional setup fields that have values (others stay omitted)."""
    if fields.additional_directories:
        request.additional_directories = fields.additional_directories
    if fields.system_prompt is not None:
        request.system_prompt = fields.system_prompt
    if fields.config_options:
        request.config_options = fields.config_options


def _mcp_transport(server: McpServer) -> str:
    match server:
        case McpServerStdio():
            return "stdio"
        case McpServerHttp():
            return "http"
        case McpServerAcp():
            return "acp"
        case _:
            return server.type
