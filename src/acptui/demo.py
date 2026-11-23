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
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    BooleanSessionConfigOption,
    CancelSessionNotification,
    CancelTerminalRequest,
    CancelTerminalResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    Command,
    ConfigOptionUpdate,
    Content,
    Diff,
    DiffPatch,
    Error,
    IdleStateSessionUpdate,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    ListSessionsRequest,
    ListSessionsResponse,
    ModifyDiffChange,
    NewSessionRequest,
    NewSessionResponse,
    Notice,
    PermissionOption,
    PlanEntry,
    PlanItems,
    PlanUpdate,
    PromptCapabilities,
    PromptEmbeddedContextCapabilities,
    PromptImageCapabilities,
    PromptRequest,
    PromptResponse,
    ReplayComplete,
    RequestPermissionRequest,
    ResumeSessionRequest,
    ResumeSessionResponse,
    RunningStateSessionUpdate,
    SelectedPermissionOutcome,
    SelectSessionConfigOption,
    SessionCapabilities,
    SessionConfigSelectOption,
    SessionInfo,
    SessionInfoUpdate,
    SetConfigOptionBooleanRequest,
    SetConfigOptionIdRequest,
    SetSessionConfigOptionResponse,
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
    UpdateSessionNotification,
    UsageUpdate,
    UserMessage,
)
from aclientp.schema.v2.base import is_set
from aclientp.v2.agent import Agent, AgentSideConnection
import anyio

import acptui


if TYPE_CHECKING:
    from collections.abc import Sequence

    from aclientp.schema.v2 import (
        AvailableCommand,
        ContentBlock,
        SessionConfigOption,
        SessionUpdate,
        SetSessionConfigOptionRequest,
    )
    from anyio.abc import ByteReceiveStream, ByteSendStream


AGENT_INFO = Implementation(name="acptui-demo", title="Demo agent", version=acptui.__version__)

COMMANDS: list[AvailableCommand] = [
    Command(name="tour", description="Show what acptui renders (the default)"),
    Command(name="echo", description="Echo the input", input=TextCommandInput(hint="text to echo")),
    Command(name="slow", description="Stream slowly (try cancelling with esc)"),
    Command(name="fail", description="End the turn with an error"),
    Command(name="crash", description="Make the agent process exit"),
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

    def config_options(self) -> list[SessionConfigOption]:
        """The session's config options and their values."""
        return [
            SelectSessionConfigOption(
                config_id="mode",
                name="Mode",
                category="mode",
                current_value=self.mode,
                options=[
                    SessionConfigSelectOption(
                        value="ask", name="Ask", description="Ask before editing files"
                    ),
                    SessionConfigSelectOption(
                        value="auto", name="Auto", description="Edit without asking"
                    ),
                ],
            ),
            SelectSessionConfigOption(
                config_id="model",
                name="Model",
                category="model",
                current_value=self.model,
                options=[
                    SessionConfigSelectOption(value="demo-fast", name="Demo Fast"),
                    SessionConfigSelectOption(value="demo-smart", name="Demo Smart"),
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

    # --- Lifecycle ------------------------------------------------------------------

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        prompt = PromptCapabilities(
            image=PromptImageCapabilities(), embedded_context=PromptEmbeddedContextCapabilities()
        )
        return InitializeResponse(
            protocol_version=2,
            info=AGENT_INFO,
            capabilities=AgentCapabilities(session=SessionCapabilities(prompt=prompt)),
        )

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        if not Path(params.cwd).is_dir():
            raise RequestError.invalid_params({"details": f"not a directory: {params.cwd}"})
        session = DemoSession(str(uuid.uuid4()), params.cwd)
        self.sessions[session.session_id] = session
        return NewSessionResponse(
            session_id=session.session_id,
            config_options=session.config_options(),
            available_commands=COMMANDS,
        )

    async def list_sessions(self, params: ListSessionsRequest) -> ListSessionsResponse:
        cwd = params.cwd if is_set(params.cwd) else None
        infos = [
            SessionInfo(session_id=s.session_id, cwd=s.cwd, title=s.title, updated_at=s.updated_at)
            for s in self.sessions.values()
            if s.history and (cwd is None or s.cwd == cwd)
        ]
        return ListSessionsResponse(sessions=infos)

    async def resume_session(self, params: ResumeSessionRequest) -> ResumeSessionResponse:
        session = self.sessions.get(params.session_id)
        if session is None:
            raise RequestError.session_not_found(params.session_id)
        if is_set(params.replay_from) and params.replay_from is not None:
            for update in session.history:
                await self._send(session, update, record=False)
            await self._send(session, ReplayComplete(), record=False)
        await self._send(session, IdleStateSessionUpdate(), record=False)
        return ResumeSessionResponse(
            config_options=session.config_options(), available_commands=COMMANDS
        )

    async def close_session(self, params: CloseSessionRequest) -> CloseSessionResponse:
        session = self.sessions.get(params.session_id)
        if session is not None and session.turn is not None:
            session.turn.cancel()
        return CloseSessionResponse()

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
            case SetConfigOptionIdRequest(config_id="model", value=value) if value in {
                "demo-fast",
                "demo-smart",
            }:
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

    # --- Prompts --------------------------------------------------------------------

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        session = self._session(params.session_id)
        if session.turn is not None and not session.turn.done():
            raise RequestError.invalid_request({"details": "a turn is already running"})
        message_id = f"user-{uuid.uuid4()}"
        await self._send(session, UserMessage(message_id=message_id, content=list(params.prompt)))
        text = "".join(b.text for b in params.prompt if isinstance(b, TextContent)).strip()
        if session.title is None:
            session.title = text[:60] or "Demo session"
            await self._send(session, SessionInfoUpdate(title=session.title))
        await self._send(session, RunningStateSessionUpdate())
        session.turn = asyncio.create_task(self._turn(session, text, params.prompt))
        return PromptResponse(message_id=message_id)

    async def _turn(self, session: DemoSession, text: str, prompt: Sequence[ContentBlock]) -> None:
        try:
            command, _, rest = text.partition(" ")
            match command:
                case "/echo":
                    await self._stream(session, rest or "(nothing to echo)")
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
                case _:
                    await self._tour(session, len(prompt) - 1)
            await self._send(session, UsageUpdate(used=12_345, size=200_000))
            await self._send(session, IdleStateSessionUpdate(stop_reason="end_turn"))
        except asyncio.CancelledError:
            await self._send(session, IdleStateSessionUpdate(stop_reason="cancelled"))

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
        plan = ["Read the project", "Inspect the directory", "Propose an edit"]
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
        except (OSError, UnicodeDecodeError):
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
                description="Nothing is written: the demo agent only pretends.",
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
    """Run the demo agent on stdio."""
    from aclientp.stdio import stdio_streams

    async def run() -> None:
        stdin, stdout = await stdio_streams()
        await serve(stdout, stdin)

    anyio.run(run)


if __name__ == "__main__":
    main()
