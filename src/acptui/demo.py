"""A scripted ACP v2 agent: no LLM, no API key, used for demos, docs and tests.

Run it with ``acptui run demo`` (or ``python -m acptui.demo`` as an agent command).
Prompts select a script: ``/tour`` (default) shows streaming, thoughts, a plan, tool
calls, a live terminal and a diff behind a permission request; ``/echo``, ``/slow``,
``/fail`` and ``/crash`` exercise other paths. Nothing is ever written to disk.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING
import uuid

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AgentAuthCapabilities,
    AgentCapabilities,
    AgentMessage,
    AgentMessageChunk,
    AgentMessageCursorAnchor,
    AgentThoughtChunk,
    ArchiveSessionRequest,
    ArchiveSessionResponse,
    AuthAccount,
    AuthMethodAgent,
    AuthMethodTerminal,
    AuthStatusCapabilities,
    AuthStatusRequest,
    AuthStatusResponse,
    AuthStatusUpdateNotification,
    BooleanSessionConfigOption,
    CancelSessionNotification,
    CancelTerminalRequest,
    CancelTerminalResponse,
    CancelToolCallRequest,
    CancelToolCallResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    Command,
    ConfigOptionUpdate,
    Content,
    Cost,
    CursorCapabilities,
    CursorForkCapabilities,
    CursorUpdate,
    DeleteSessionRequest,
    DeleteSessionResponse,
    Diff,
    DiffPatch,
    DisableProviderRequest,
    DisableProviderResponse,
    Error,
    ForkSessionRequest,
    ForkSessionResponse,
    IdleStateSessionUpdate,
    IdleStateUpdate,
    Implementation,
    InitialConfigOptionBoolean,
    InitialConfigOptionId,
    InitializeRequest,
    InitializeResponse,
    ListProvidersRequest,
    ListProvidersResponse,
    ListSessionsRequest,
    ListSessionsResponse,
    LoginAuthRequest,
    LoginAuthResponse,
    LogoutAuthRequest,
    LogoutAuthResponse,
    McpCapabilities,
    McpHttpCapabilities,
    McpStdioCapabilities,
    ModifyDiffChange,
    NewSessionRequest,
    NewSessionResponse,
    Notice,
    PermissionOption,
    PlanEntry,
    PlanItems,
    PlanUpdate,
    PromptAudioCapabilities,
    PromptCapabilities,
    PromptEmbeddedContextCapabilities,
    PromptImageCapabilities,
    PromptRequest,
    PromptResponse,
    PromptSkillCapabilities,
    ProviderCurrentConfig,
    ProviderInfo,
    ProvidersCapabilities,
    ReplayComplete,
    RequestPermissionRequest,
    ResumeSessionRequest,
    ResumeSessionResponse,
    RunningStateSessionUpdate,
    RunningStateUpdate,
    SelectedPermissionOutcome,
    SelectSessionConfigOption,
    SessionAdditionalDirectoriesCapabilities,
    SessionArchiveCapabilities,
    SessionCapabilities,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
    SessionDeleteCapabilities,
    SessionForkCapabilities,
    SessionInfo,
    SessionInfoUpdate,
    SessionOptionsCapabilities,
    SessionOptionsRequest,
    SessionOptionsResponse,
    SessionStatusCapabilities,
    SessionStatusRequest,
    SessionStatusResponse,
    SessionSystemPromptCapabilities,
    SessionSystemPromptModeCapabilities,
    SessionSystemPromptOverrideCapabilities,
    SessionTitleCapabilities,
    SetConfigOptionBooleanRequest,
    SetConfigOptionIdRequest,
    SetProviderRequest,
    SetProviderResponse,
    SetSessionConfigOptionResponse,
    SetSessionTitleRequest,
    SetSessionTitleResponse,
    Skill,
    SkillReference,
    Terminal,
    TerminalCancelCapabilities,
    TerminalCapabilities,
    TerminalExitStatus,
    TerminalOutputChunk,
    TerminalUpdate,
    TextCommandInput,
    TextContent,
    ToolCallLocation,
    ToolCallPermissionSubject,
    ToolCallSessionUpdate,
    ToolCallUpdate,
    UnarchiveSessionRequest,
    UnarchiveSessionResponse,
    UpdateSessionNotification,
    Usage,
    UsageUpdate,
    UserMessage,
)
from aclientp.schema.v2.base import is_set
from aclientp.v2.agent import Agent, AgentSideConnection
import anyio

import acptui
from acptui import demo_scripts as scripts


if TYPE_CHECKING:
    from collections.abc import Sequence

    from aclientp.schema.v2 import (
        AvailableCommand,
        BackgroundWork,
        ContentBlock,
        InitialSessionConfigOption,
        SessionConfigOption,
        SessionUpdate,
        SetSessionConfigOptionRequest,
        StateUpdate,
    )
    from anyio.abc import ByteReceiveStream, ByteSendStream


AGENT_INFO = Implementation(name="acptui-demo", title="Demo agent", version=acptui.__version__)

MODELS = frozenset({"demo-fast", "demo-smart"})

COMMANDS: list[AvailableCommand] = [
    Command(name="tour", description="Show what **acptui** renders (the default)"),
    Command(
        name="echo",
        description="Echo the `text` you give it",
        input=TextCommandInput(hint="text to echo"),
    ),
    Command(name="slow", description="Stream slowly (try cancelling with esc)"),
    Command(name="fail", description="End the turn with an error"),
    Command(name="crash", description="Make the agent process exit"),
    Command(name="media", description="Every kind of content block"),
    Command(name="subagent", description="Run a subagent session"),
    Command(name="elicit", description="Ask for input with a form"),
    Command(name="url", description="Ask the user to visit a URL"),
    Command(name="compact", description="Compact the context"),
    Command(name="suggest", description="Suggest next prompts"),
    Command(name="plans", description="Markdown and item plans"),
    Command(name="notice", description="Notices of every severity"),
    Command(name="background", description="Leave a terminal running in the background"),
    Command(name="ext", description="Send custom (extension) messages"),
    Command(name="command", description="Ask permission to run a command"),
    Command(name="setup", description="Show what the client sent on session setup"),
    Command(name="cancel-tool", description="A tool call that can be cancelled"),
]

ANSWER = """\
## Done

I looked at the project and prepared a change:

- read **{file}**
- listed the directory in a terminal
- proposed an edit (shown as a diff)

```python
def greet(name: str) -> str:
    return f"Hello, {{name}}!"
```

> This is the demo agent: nothing was written to disk.
"""

TERMINAL_LINES = [
    "\x1b[1;34mtotal 24\x1b[0m\r\n",
    "drwxr-xr-x  5 user user 4096 \x1b[32mREADME.md\x1b[0m\r\n",
    "-rw-r--r--  1 user user  812 \x1b[32mpyproject.toml\x1b[0m\r\n",
    "drwxr-xr-x  3 user user 4096 \x1b[1;34msrc\x1b[0m\r\n",
    "Progress: [#####     ] 50%\r",
    "Progress: [##########] 100%\r\n",
]


def _delay() -> float:
    return float(os.environ.get("ACPTUI_DEMO_DELAY", "0.02"))


@dataclass
class DemoSession:
    """A session of the demo agent."""

    session_id: str
    cwd: str
    title: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    history: list[SessionUpdate] = field(default_factory=list)
    mode: str = "ask"
    model: str = "demo-fast"
    thinking: bool = True
    turn: asyncio.Task[None] | None = None
    archived: bool = False
    parent_id: str | None = None
    additional_directories: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, object]] = field(default_factory=list)
    system_prompt: str | None = None
    cursors: dict[str, int] = field(default_factory=dict)
    """Fork points: cursor ID -> length of the history before it."""
    cancelled_tools: set[str] = field(default_factory=set)

    def apply_initial(self, option: InitialSessionConfigOption) -> None:
        """Apply an initial config option value; unknown ones are ignored (as the spec says)."""
        match option:
            case InitialConfigOptionId(config_id="mode", value=value) if value in {"ask", "auto"}:
                self.mode = value
            case InitialConfigOptionId(config_id="model", value=value) if value in MODELS:
                self.model = value
            case InitialConfigOptionBoolean(config_id="thinking", value=value):
                self.thinking = value
            case _:
                pass

    def describe_setup(self) -> str:
        """What the client sent on session setup, as Markdown."""
        servers = ", ".join(str(s.get("name")) for s in self.mcp_servers) or "none"
        return "\n".join([
            "## Session setup",
            "",
            f"- cwd: `{self.cwd}`",
            f"- additional directories: {', '.join(self.additional_directories) or 'none'}",
            f"- MCP servers: {servers}",
            f"- system prompt: {self.system_prompt or 'none'}",
            f"- mode: {self.mode}, model: {self.model}, thinking: {self.thinking}",
        ])

    def config_options(self) -> list[SessionConfigOption]:
        """The session's config options and their values."""
        return [
            SelectSessionConfigOption(
                config_id="mode",
                name="Mode",
                category="mode",
                description="How the agent edits: **ask** first, or `auto`.",
                current_value=self.mode,
                options=[
                    SessionConfigSelectOption(
                        value="ask", name="Ask", description="Ask before *editing* files"
                    ),
                    SessionConfigSelectOption(
                        value="auto", name="Auto", description="Edit **without** asking"
                    ),
                ],
            ),
            SelectSessionConfigOption(
                config_id="model",
                name="Model",
                category="model",
                current_value=self.model,
                options=[
                    SessionConfigSelectGroup(
                        group_id="fast",
                        name="Fast",
                        options=[SessionConfigSelectOption(value="demo-fast", name="Demo Fast")],
                    ),
                    SessionConfigSelectGroup(
                        group_id="smart",
                        name="Smart",
                        options=[SessionConfigSelectOption(value="demo-smart", name="Demo Smart")],
                    ),
                ],
            ),
            BooleanSessionConfigOption(
                config_id="thinking",
                name="Thinking",
                category="thought_level",
                current_value=self.thinking,
            ),
        ]


class DemoAgent(Agent):
    """The scripted agent."""

    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn
        self.sessions: dict[str, DemoSession] = {}
        self.client_skills = False
        self.logged_in = False
        self.providers: dict[str, ProviderInfo] = {
            "main": ProviderInfo(
                provider_id="main",
                supported=["anthropic", "openai"],
                required=True,
                current=ProviderCurrentConfig(
                    api_type="anthropic", base_url="https://api.example.com"
                ),
            ),
            "backup": ProviderInfo(provider_id="backup", supported=["openai"], required=False),
        }

    # --- Lifecycle ------------------------------------------------------------------

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        caps = params.capabilities if is_set(params.capabilities) else None
        session_caps = caps.session if caps is not None and is_set(caps.session) else None
        commands = (
            session_caps.commands
            if session_caps is not None and is_set(session_caps.commands)
            else None
        )
        self.client_skills = (
            commands is not None and is_set(commands.skill) and commands.skill is not None
        )
        prompt = PromptCapabilities(
            image=PromptImageCapabilities(),
            audio=PromptAudioCapabilities(),
            embedded_context=PromptEmbeddedContextCapabilities(),
            skill=PromptSkillCapabilities(),
        )
        session = SessionCapabilities(
            prompt=prompt,
            mcp=McpCapabilities(stdio=McpStdioCapabilities(), http=McpHttpCapabilities()),
            delete=SessionDeleteCapabilities(),
            additional_directories=SessionAdditionalDirectoriesCapabilities(),
            system_prompt=SessionSystemPromptCapabilities(
                mode=SessionSystemPromptModeCapabilities(
                    override=SessionSystemPromptOverrideCapabilities()
                )
            ),
            fork=SessionForkCapabilities(),
            archive=SessionArchiveCapabilities(),
            status=SessionStatusCapabilities(),
            options=SessionOptionsCapabilities(),
            title=SessionTitleCapabilities(),
        )
        return InitializeResponse(
            protocol_version=2,
            info=AGENT_INFO,
            capabilities=AgentCapabilities(
                session=session,
                auth=AgentAuthCapabilities(status=AuthStatusCapabilities()),
                providers=ProvidersCapabilities(),
            ),
            auth_methods=[
                AuthMethodAgent(
                    method_id="demo-login",
                    name="Demo login",
                    description="Pretend to log in (`demo login`)",
                ),
                AuthMethodTerminal(
                    method_id="demo-terminal",
                    name="Demo terminal login",
                    description="Runs the agent with `--login` in the terminal",
                    args=["--login"],
                ),
            ],
        )

    def _authenticated(self) -> bool:
        if self.logged_in or os.environ.get("ACPTUI_DEMO_REQUIRE_AUTH") != "1":
            return True
        marker = os.environ.get("ACPTUI_DEMO_AUTH_FILE")
        return marker is not None and Path(marker).is_file()

    def _commands(self) -> list[AvailableCommand]:
        skills: list[AvailableCommand] = [
            Skill(name="review", description="Review the code (a skill)")
        ]
        return COMMANDS + skills if self.client_skills else list(COMMANDS)

    def _setup(
        self,
        session: DemoSession,
        params: NewSessionRequest | ResumeSessionRequest | ForkSessionRequest,
    ) -> None:
        if not Path(params.cwd).is_dir():
            raise RequestError.invalid_params({"details": f"not a directory: {params.cwd}"})
        session.cwd = params.cwd
        if is_set(params.additional_directories):
            session.additional_directories = list(params.additional_directories)
        if is_set(params.mcp_servers):
            session.mcp_servers = [
                server.model_dump(by_alias=True) for server in params.mcp_servers
            ]
        if is_set(params.system_prompt) and params.system_prompt is not None:
            text = "".join(
                b.text for b in params.system_prompt.content if isinstance(b, TextContent)
            )
            session.system_prompt = f"{params.system_prompt.mode}: {text}"
        if is_set(params.config_options):
            for option in params.config_options:
                session.apply_initial(option)

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        if not self._authenticated():
            raise RequestError.auth_required({"details": "log in first"})
        session = DemoSession(str(uuid.uuid4()), params.cwd)
        self._setup(session, params)
        self.sessions[session.session_id] = session
        return NewSessionResponse(
            session_id=session.session_id,
            config_options=session.config_options(),
            available_commands=self._commands(),
        )

    async def list_sessions(self, params: ListSessionsRequest) -> ListSessionsResponse:
        cwd = params.cwd if is_set(params.cwd) else None
        archived = params.archived if is_set(params.archived) and params.archived else "exclude"
        infos = [
            SessionInfo(
                session_id=s.session_id,
                cwd=s.cwd,
                title=s.title,
                updated_at=s.updated_at,
                archived=s.archived,
            )
            for s in self.sessions.values()
            if s.history
            and s.parent_id is None
            and (cwd is None or s.cwd == cwd)
            and (archived == "include" or (archived == "only") == s.archived)
        ]
        return ListSessionsResponse(sessions=infos)

    async def resume_session(self, params: ResumeSessionRequest) -> ResumeSessionResponse:
        session = self._session(params.session_id)
        self._setup(session, params)
        if is_set(params.replay_from) and params.replay_from is not None:
            for update in session.history:
                await self._send(session, update, record=False)
            await self._send(session, ReplayComplete(), record=False)
        await self._send(session, IdleStateSessionUpdate(), record=False)
        return ResumeSessionResponse(
            config_options=session.config_options(), available_commands=self._commands()
        )

    async def fork_session(self, params: ForkSessionRequest) -> ForkSessionResponse:
        source = self._session(params.session_id)
        fork = DemoSession(
            str(uuid.uuid4()), params.cwd, title=f"{source.title or 'session'} (fork)"
        )
        self._setup(fork, params)
        cursor_id = params.cursor_id if is_set(params.cursor_id) else None
        if cursor_id is not None and cursor_id not in source.cursors:
            raise RequestError.invalid_params({"details": f"unknown cursor {cursor_id}"})
        cut = source.cursors[cursor_id] if cursor_id is not None else len(source.history)
        fork.history = list(source.history[:cut])
        self.sessions[fork.session_id] = fork
        return ForkSessionResponse(
            session_id=fork.session_id,
            config_options=fork.config_options(),
            available_commands=self._commands(),
        )

    async def close_session(self, params: CloseSessionRequest) -> CloseSessionResponse:
        session = self.sessions.get(params.session_id)
        if session is not None and session.turn is not None:
            session.turn.cancel()
        return CloseSessionResponse()

    async def delete_session(self, params: DeleteSessionRequest) -> DeleteSessionResponse:
        self._session(params.session_id)
        del self.sessions[params.session_id]
        return DeleteSessionResponse()

    async def archive_session(self, params: ArchiveSessionRequest) -> ArchiveSessionResponse:
        self._session(params.session_id).archived = True
        return ArchiveSessionResponse()

    async def unarchive_session(self, params: UnarchiveSessionRequest) -> UnarchiveSessionResponse:
        self._session(params.session_id).archived = False
        return UnarchiveSessionResponse()

    async def session_status(self, params: SessionStatusRequest) -> SessionStatusResponse:
        session = self._session(params.session_id)
        running = session.turn is not None and not session.turn.done()
        state: StateUpdate = RunningStateUpdate() if running else IdleStateUpdate()
        return SessionStatusResponse(status="open", state=state)

    async def session_options(self, params: SessionOptionsRequest) -> SessionOptionsResponse:
        session_id = params.session_id if is_set(params.session_id) else None
        session = self.sessions.get(session_id) if session_id else None
        options = (session or DemoSession("preview", params.cwd)).config_options()
        return SessionOptionsResponse(config_options=options, available_commands=self._commands())

    async def set_session_title(self, params: SetSessionTitleRequest) -> SetSessionTitleResponse:
        session = self._session(params.session_id)
        session.title = params.title.strip()[:80] or session.title
        return SetSessionTitleResponse(title=session.title or "")

    async def set_session_config_option(
        self, params: SetSessionConfigOptionRequest
    ) -> SetSessionConfigOptionResponse:
        session = self._session(params.session_id)
        match params:
            case SetConfigOptionIdRequest(config_id="mode", value=value) if value in {
                "ask",
                "auto",
            }:
                session.mode = value
            case SetConfigOptionIdRequest(config_id="model", value=value) if value in MODELS:
                session.model = value
            case SetConfigOptionBooleanRequest(config_id="thinking", value=value):
                session.thinking = value
            case _:
                raise RequestError.invalid_params({"details": "unknown option or value"})
        return SetSessionConfigOptionResponse(config_options=session.config_options())

    async def cancel(self, params: CancelSessionNotification) -> None:
        session = self.sessions.get(params.session_id)
        if session is not None and session.turn is not None:
            session.turn.cancel()

    async def cancel_terminal(self, params: CancelTerminalRequest) -> CancelTerminalResponse:
        await self.cancel(CancelSessionNotification(session_id=params.session_id))
        return CancelTerminalResponse()

    async def cancel_tool_call(self, params: CancelToolCallRequest) -> CancelToolCallResponse:
        session = self._session(params.session_id)
        session.cancelled_tools.add(params.tool_call_id)
        return CancelToolCallResponse()

    # --- Authentication and providers -----------------------------------------------

    async def login(self, params: LoginAuthRequest) -> LoginAuthResponse:
        if params.method_id != "demo-login":
            raise RequestError.invalid_params({"details": f"unknown method {params.method_id}"})
        self.logged_in = True
        await self._auth_changed()
        return LoginAuthResponse()

    async def logout(self, params: LogoutAuthRequest) -> LogoutAuthResponse:
        self.logged_in = False
        await self._auth_changed()
        return LogoutAuthResponse()

    def _auth_state(self) -> tuple[str, list[AuthAccount]]:
        if self.logged_in:
            return "authenticated", [
                AuthAccount(label="Demo account", active=True, email="demo@example.com")
            ]
        if self._authenticated():
            return "not_required", []
        return "unauthenticated", []

    async def auth_status(self, params: AuthStatusRequest) -> AuthStatusResponse:
        state, accounts = self._auth_state()
        return AuthStatusResponse(state=state, accounts=accounts)

    async def _auth_changed(self) -> None:
        state, accounts = self._auth_state()
        await self.conn.auth_status_update(
            AuthStatusUpdateNotification(state=state, accounts=accounts)
        )

    async def list_providers(self, params: ListProvidersRequest) -> ListProvidersResponse:
        return ListProvidersResponse(providers=list(self.providers.values()))

    async def set_provider(self, params: SetProviderRequest) -> SetProviderResponse:
        provider = self.providers.get(params.provider_id)
        if provider is None or params.api_type not in provider.supported:
            raise RequestError.invalid_params({"details": "unknown provider or api type"})
        provider.current = ProviderCurrentConfig(api_type=params.api_type, base_url=params.base_url)
        return SetProviderResponse()

    async def disable_provider(self, params: DisableProviderRequest) -> DisableProviderResponse:
        provider = self.providers.get(params.provider_id)
        if provider is None or provider.required:
            raise RequestError.invalid_params({"details": "unknown or required provider"})
        provider.current = None
        return DisableProviderResponse()

    # --- Prompts --------------------------------------------------------------------

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        session = self._session(params.session_id)
        if session.turn is not None and not session.turn.done():
            raise RequestError.invalid_request({"details": "a turn is already running"})
        message_id = f"user-{uuid.uuid4()}"
        await self._send(session, UserMessage(message_id=message_id, content=list(params.prompt)))
        text = "".join(b.text for b in params.prompt if isinstance(b, TextContent)).strip()
        skill = next((b.name for b in params.prompt if isinstance(b, SkillReference)), None)
        if session.title is None:
            session.title = text[:60] or "Demo session"
            await self._send(session, SessionInfoUpdate(title=session.title))
        await self._send(session, RunningStateSessionUpdate())
        script = f"/skill {skill}" if skill else text
        session.turn = asyncio.create_task(self._turn(session, script, params.prompt))
        return PromptResponse(message_id=message_id)

    async def _turn(self, session: DemoSession, text: str, prompt: Sequence[ContentBlock]) -> None:
        try:
            command, _, rest = text.partition(" ")
            background: list[BackgroundWork] = []
            match command:
                case "/echo":
                    await self._stream(session, rest or "(nothing to echo)")
                case "/skill":
                    await self._stream(session, f"Running skill **{rest}**.")
                case "/slow":
                    await self._stream(
                        session, "counting slowly: " + " ".join(map(str, range(1, 400))), 0.05
                    )
                case "/fail":
                    error = Error(code=-32603, message="Demo failure: the model is unavailable")
                    await self._send(
                        session, IdleStateSessionUpdate(stop_reason="error", error=error)
                    )
                    return
                case "/crash":
                    sys.stderr.write("demo agent: simulated crash (exit code 3)\n")
                    sys.stderr.flush()
                    await anyio.sleep(0.05)
                    os._exit(3)
                case "/media":
                    await scripts.media(self, session)
                case "/subagent":
                    await scripts.subagent(self, session)
                case "/elicit":
                    await scripts.elicit_form(self, session)
                case "/url":
                    await scripts.elicit_url(self, session)
                case "/compact":
                    await scripts.compact(self, session)
                case "/suggest":
                    await scripts.suggest(self, session)
                case "/plans":
                    await scripts.plans(self, session)
                case "/notice":
                    await scripts.notices(self, session)
                case "/background":
                    background = await scripts.background(self, session)
                case "/ext":
                    await scripts.extension(self, session)
                case "/command":
                    await scripts.command_permission(self, session)
                case "/setup":
                    await self._stream(session, session.describe_setup())
                case "/cancel-tool":
                    await scripts.cancellable_tool(self, session)
                case _:
                    await self._tour(session, len(prompt) - 1)
            await self._send(
                session,
                UsageUpdate(used=12_345, size=200_000, cost=Cost(amount=0.01, currency="USD")),
            )
            await self._mark_cursor(session)
            usage = Usage(total_tokens=1500, input_tokens=1200, output_tokens=300)
            await self._send(
                session,
                IdleStateSessionUpdate(stop_reason="end_turn", usage=usage, background=background),
            )
        except asyncio.CancelledError:
            await self._send(session, IdleStateSessionUpdate(stop_reason="cancelled"))

    async def _mark_cursor(self, session: DemoSession) -> None:
        """Offer a fork point after the last agent message of the turn."""
        last = next(
            (
                u
                for u in reversed(session.history)
                if isinstance(u, AgentMessageChunk | AgentMessage)
            ),
            None,
        )
        if last is None:
            return
        cursor_id = f"cursor-{len(session.cursors) + 1}"
        session.cursors[cursor_id] = len(session.history)
        await self._send(
            session,
            CursorUpdate(
                cursor_id=cursor_id,
                anchor=AgentMessageCursorAnchor(message_id=last.message_id, position="after"),
                capabilities=CursorCapabilities(fork=CursorForkCapabilities()),
            ),
        )

    async def _tour(self, session: DemoSession, attachments: int) -> None:
        cwd = Path(session.cwd)
        target = next(
            (cwd / name for name in ("README.md", "pyproject.toml") if (cwd / name).is_file()),
            cwd / "example.py",
        )
        if session.thinking:
            await self._stream(
                session,
                "The user wants a tour. I'll read a file, run a command, and propose an edit.",
                kind="thought",
            )
        if attachments:
            await self._send(
                session,
                Notice(severity="info", title=f"Received {attachments} attached resource(s)"),
            )
        plan = ["Read the project", "Inspect the directory", "Propose an edit to `README.md`"]
        await self._plan(session, plan, done=0)

        # 1. A read tool call
        read_id = "read-1"
        await self._send(
            session,
            ToolCallSessionUpdate(
                tool_call_id=read_id,
                name="read_file",
                title=f"Read {target.name}",
                kind="read",
                status="in_progress",
                locations=[ToolCallLocation(path=str(target), line=1)],
            ),
        )
        await anyio.sleep(_delay() * 5)
        try:
            head = "".join(target.read_text(encoding="utf-8").splitlines(keepends=True)[:5])
        except OSError, UnicodeDecodeError:
            head = "(file not readable)"
        await self._send(
            session,
            ToolCallSessionUpdate(
                tool_call_id=read_id,
                status="completed",
                content=[Content(content=TextContent(text=head))],
            ),
        )
        await self._plan(session, plan, done=1)

        # 2. A command in an agent-owned terminal
        term_id = "term-1"
        await self._send(
            session,
            ToolCallSessionUpdate(
                tool_call_id="exec-1",
                name="shell",
                title="ls -la",
                kind="execute",
                status="in_progress",
                content=[Terminal(terminal_id=term_id)],
            ),
        )
        await self._send(
            session,
            TerminalUpdate(
                terminal_id=term_id,
                command="ls -la",
                cwd=session.cwd,
                capabilities=TerminalCapabilities(cancel=TerminalCancelCapabilities()),
            ),
        )
        for line in TERMINAL_LINES:
            await anyio.sleep(_delay() * 3)
            await self._send(session, TerminalOutputChunk.from_bytes(term_id, line.encode()))
        await self._send(
            session,
            TerminalUpdate(
                terminal_id=term_id, exit_status=TerminalExitStatus(exit_code=0), capabilities=None
            ),
        )
        await self._send(session, ToolCallSessionUpdate(tool_call_id="exec-1", status="completed"))
        await self._plan(session, plan, done=2)

        # 3. An edit behind a permission request
        edit = _edit_call(ToolCallUpdate, target)
        await self._send(session, _edit_call(ToolCallSessionUpdate, target))
        allowed = session.mode == "auto" or await self._ask(session, edit)
        status = "completed" if allowed else "failed"
        await self._send(session, ToolCallSessionUpdate(tool_call_id="edit-1", status=status))
        await self._plan(session, plan, done=3)
        await self._stream(session, ANSWER.format(file=target.name))

    async def _ask(self, session: DemoSession, tool_call: ToolCallUpdate) -> bool:
        response = await self.conn.request_permission(
            RequestPermissionRequest(
                session_id=session.session_id,
                title="Allow the demo agent to edit this file?",
                description="Nothing is written: the demo agent only *pretends* (`dry run`).",
                subject=ToolCallPermissionSubject(tool_call=tool_call),
                options=[
                    PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                    PermissionOption(
                        option_id="allow-always", name="Always allow", kind="allow_always"
                    ),
                    PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
                ],
            )
        )
        outcome = response.outcome
        if isinstance(outcome, SelectedPermissionOutcome) and outcome.option_id == "allow-always":
            session.mode = "auto"
            await self._send(session, ConfigOptionUpdate(config_options=session.config_options()))
        return isinstance(outcome, SelectedPermissionOutcome) and outcome.option_id.startswith(
            "allow"
        )

    async def _plan(self, session: DemoSession, steps: list[str], done: int) -> None:
        entries = [
            PlanEntry(
                content=step,
                priority="high" if index == 0 else "medium",
                status="completed"
                if index < done
                else "in_progress"
                if index == done
                else "pending",
            )
            for index, step in enumerate(steps)
        ]
        await self._send(session, PlanUpdate(plan=PlanItems(plan_id="plan-1", entries=entries)))

    async def _stream(
        self, session: DemoSession, text: str, delay: float | None = None, kind: str = "message"
    ) -> None:
        message_id = f"{kind}-{uuid.uuid4()}"
        words = text.split(" ")
        for index, word in enumerate(words):
            chunk = TextContent(text=word if index == len(words) - 1 else word + " ")
            update: SessionUpdate = (
                AgentThoughtChunk(message_id=message_id, content=chunk)
                if kind == "thought"
                else AgentMessageChunk(message_id=message_id, content=chunk)
            )
            await self._send(session, update)
            await anyio.sleep(_delay() if delay is None else delay)

    async def _send(
        self, session: DemoSession, update: SessionUpdate, *, record: bool = True
    ) -> None:
        if record and not isinstance(update, RunningStateSessionUpdate | IdleStateSessionUpdate):
            session.history.append(update)
            session.updated_at = datetime.now(UTC).isoformat()
        notification = UpdateSessionNotification(session_id=session.session_id, update=update)
        await self.conn.session_update(notification)

    def _session(self, session_id: str) -> DemoSession:
        if (session := self.sessions.get(session_id)) is None:
            raise RequestError.session_not_found(session_id)
        return session


def _edit_call[T: ToolCallUpdate](cls: type[T], target: Path) -> T:
    return cls(
        tool_call_id="edit-1",
        name="edit_file",
        title=f"Edit {target.name}",
        kind="edit",
        status="pending",
        locations=[ToolCallLocation(path=str(target), line=1)],
        content=[Diff(changes=[ModifyDiffChange(path=str(target))], patch=_demo_patch(target))],
    )


def _demo_patch(target: Path) -> DiffPatch:
    name = str(target)
    text = (
        f"diff --git {name} {name}\n"
        f"--- {name}\n"
        f"+++ {name}\n"
        "@@ -1,4 +1,6 @@\n"
        " import sys\n"
        "-def greet(name):\n"
        "-    print('Hello ' + name)\n"
        "+def greet(name: str) -> str:\n"
        '+    """Return a greeting."""\n'
        '+    return f"Hello, {name}!"\n'
        " \n"
        "+print(greet(sys.argv[1]))\n"
    )
    return DiffPatch(format="git_patch", text=text)


async def serve(writer: ByteSendStream, reader: ByteReceiveStream) -> None:
    """Serve the demo agent on a pair of byte streams until the client disconnects."""
    conn = AgentSideConnection(DemoAgent, writer, reader)
    try:
        await conn.wait_closed()
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


def main() -> None:
    """Run the demo agent on stdio (``--login``: the terminal login of the demo)."""
    from aclientp.stdio import stdio_streams

    if "--login" in sys.argv[1:]:
        marker = os.environ.get("ACPTUI_DEMO_AUTH_FILE")
        if marker:
            Path(marker).write_text("logged in\n", encoding="utf-8")
        print("Demo agent: you are now logged in.")
        return

    async def run() -> None:
        stdin, stdout = await stdio_streams()
        await serve(stdout, stdin)

    anyio.run(run)


if __name__ == "__main__":
    main()
