"""The session screen: conversation, prompt and status; drives the agent connection.

It implements :class:`~acptui.protocol.connection.AgentEvents`: every message the
agent sends arrives here and is routed to the session it belongs to (the main
session, or one of its subagent sessions).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, ClassVar

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AuthMethodAgent,
    AuthMethodTerminal,
    DeclineElicitationResponse,
    FormElicitationRequest,
    SelectSessionConfigOption,
    Skill,
    ToolCallPermissionSubject,
    UrlElicitationRequest,
)
from aclientp.schema.v2.base import is_set
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Footer, Static

from acptui.protocol.connection import (
    AgentConnection,
    AgentError,
    SessionSetup,
    UnsupportedProtocolError,
)
from acptui.protocol.content import content_markdown
from acptui.protocol.prompt import PromptOptions, build_prompt
from acptui.protocol.state import EntryAdded, MessageEntry, SessionState
from acptui.ui.screens.dialogs import (
    ConfigScreen,
    ForkScreen,
    HelpScreen,
    SessionListScreen,
    TextInputScreen,
    next_select_value,
)
from acptui.ui.screens.elicitation import FormElicitationScreen, UrlElicitationScreen
from acptui.ui.screens.log import LogScreen
from acptui.ui.screens.permission import PermissionScreen, cancelled
from acptui.ui.screens.providers import ProvidersScreen
from acptui.ui.widgets.conversation import ConversationView
from acptui.ui.widgets.prompt import CompletionList, PromptInput
from acptui.ui.widgets.status import StatusBar


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from aclientp.schema.v2 import (
        AuthStatusUpdateNotification,
        AvailableCommand,
        CompleteElicitationNotification,
        CreateElicitationRequest,
        CreateElicitationResponse,
        RequestPermissionRequest,
        RequestPermissionResponse,
        SessionConfigOption,
        UpdateSessionNotification,
    )
    from aclientp.schema.v2.auth import AuthAccount
    from aclientp.schema.v2.base import JsonValue
    from textual.app import ComposeResult
    from textual.screen import ModalScreen
    from textual.widget import Widget

    from acptui.config import AgentSpec, DiffViewMode
    from acptui.protocol.state import SessionActivity
    from acptui.ui.agent_log import AgentLog
    from acptui.ui.screens.dialogs import ConfigChoice
    from acptui.ui.widgets.blocks import SubagentView, ToolCallView
    from acptui.ui.widgets.terminal import TerminalView


DIFF_MODES: tuple[DiffViewMode, ...] = ("unified", "split", "auto")
ACTIVITIES: dict[str, SessionActivity] = {
    "idle": "idle",
    "running": "running",
    "requires_action": "requires_action",
}
STDERR_TAIL = 12


class ErrorPanel(Static):
    """A problem with the agent (it failed to start, crashed, is unsupported, ...)."""

    DEFAULT_CSS = """
    ErrorPanel {
        height: auto;
        border: round $error;
        border-title-color: $error;
        padding: 0 1;
        margin: 1 0;
    }
    """


class SubagentScreen(Screen[None]):
    """The conversation of a subagent session (read-only)."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Back"),
        Binding("x", "cancel_subagent", "Cancel subagent"),
    ]

    DEFAULT_CSS = """
    SubagentScreen { #subagent-title { height: 1; background: $panel; padding: 0 1; } }
    """

    def __init__(self, parent: SessionScreen, state: SessionState, title: str) -> None:
        super().__init__()
        self.parent_screen = parent
        self.state = state
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Static(
            Content.assemble(("subagent: ", "$text-muted"), (self.title_text, "bold")),
            id="subagent-title",
        )
        yield ConversationView(
            self.parent_screen.agent_log,
            diff_mode=self.parent_screen.diff_mode,
            expand_thoughts=self.parent_screen.expand_thoughts,
        )
        yield Footer(compact=True)

    @property
    def conversation(self) -> ConversationView:
        """The child's conversation."""
        return self.query_one(ConversationView)

    def on_mount(self) -> None:
        self.conversation.show(self.state)

    def action_close(self) -> None:
        """Back to the parent session."""
        self.dismiss(None)

    def action_cancel_subagent(self) -> None:
        """Cancel the subagent's work (if the parent allows it)."""
        self.parent_screen.cancel_subagent(self.state.session_id)


class SessionScreen(Screen[None]):
    """A conversation with one agent."""

    DEFAULT_CSS = """
    SessionScreen {
        layout: vertical;
        #prompt-area { height: auto; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("escape", "cancel_turn", "Cancel"),
        Binding("ctrl+n", "new_session", "New"),
        Binding("ctrl+r", "resume_session", "Resume"),
        Binding("ctrl+o", "options", "Options"),
        Binding("shift+tab", "next_mode", "Mode", show=False),
        Binding("ctrl+d", "toggle_diff", "Diff view", show=False),
        Binding("ctrl+f", "fork_session", "Fork", show=False),
        Binding("ctrl+l", "show_log", "Log"),
        Binding("f1", "help", "Help"),
        Binding("f2", "rename_session", "Rename", show=False),
        Binding("f5", "restart", "Restart", show=False),
        Binding("pageup", "scroll_conversation(-1)", show=False, priority=True),
        Binding("pagedown", "scroll_conversation(1)", show=False, priority=True),
    ]

    def __init__(
        self,
        spec: AgentSpec,
        cwd: Path,
        agent_log: AgentLog,
        *,
        diff_mode: DiffViewMode = "auto",
        expand_thoughts: bool = False,
        resume_id: str | None = None,
        setup: SessionSetup | None = None,
    ) -> None:
        super().__init__()
        self.spec = spec
        self.cwd = cwd
        self.agent_log = agent_log
        self.diff_mode: DiffViewMode = diff_mode
        self.expand_thoughts = expand_thoughts
        self.resume_id = resume_id
        self.setup = setup or SessionSetup()
        self.connection: AgentConnection | None = None
        self.state: SessionState | None = None
        """The session shown in this screen."""
        self.sessions: dict[str, SessionState] = {}
        """Every session the agent told us about: the main one and subagent sessions."""
        self.connected = False
        self.closing = False
        self._dialogs: set[Widget] = set()
        """Open permission and elicitation dialogs (closed when the agent goes away)."""
        self._url_elicitations: dict[str, UrlElicitationScreen] = {}
        self._subagent_screens: dict[str, SubagentScreen] = {}

    # --- Layout ---------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield ConversationView(
            self.agent_log,
            diff_mode=self.diff_mode,
            expand_thoughts=self.expand_thoughts,
            id="conversation",
        )
        completion_list = CompletionList()
        prompt = PromptInput(self.cwd)
        prompt.completion_list = completion_list
        with Vertical(id="prompt-area"):
            yield completion_list
            yield prompt
            yield StatusBar()
        yield Footer(compact=True)

    @property
    def conversation(self) -> ConversationView:
        """The main conversation."""
        return self.query_one("#conversation", ConversationView)

    @property
    def prompt(self) -> PromptInput:
        """The prompt editor."""
        return self.query_one(PromptInput)

    @property
    def status(self) -> StatusBar:
        """The status line."""
        return self.query_one(StatusBar)

    def on_mount(self) -> None:
        self.status.agent = self.spec.name
        self.status.refresh_status()
        self.prompt.focus()
        self.start_agent()

    def call[*Ts](self, func: Callable[[*Ts], Awaitable[object]], *args: *Ts) -> None:
        """Call ``func(*args)`` in the background (see :meth:`spawn`)."""
        self.spawn(func(*args))

    def spawn(self, call: Awaitable[object], group: str = "rpc") -> None:
        """Run a call in the background; failures are shown, never raised into the UI."""

        async def run() -> None:
            try:
                await call
            except (RequestError, ConnectionError, AgentError) as exc:
                self.notify(str(exc), severity="error")

        self.run_worker(run(), group=group, exit_on_error=False)

    # --- Agent lifecycle ------------------------------------------------------------

    def start_agent(self) -> None:
        """(Re)start the connection to the agent."""
        self.run_worker(
            self._run_agent(), name="agent", group="agent", exclusive=True, exit_on_error=False
        )

    async def _run_agent(self) -> None:
        connection = AgentConnection(self.spec, self, self.cwd)
        self.connection = connection
        self._set_connection_state("starting")
        self.agent_log.add("info", f"starting {self.spec.name}: {self.spec.target}")
        try:
            async with connection.open():
                self.agent_log.add(
                    "info", f"connected to {connection.agent_name} {connection.agent_version}"
                )
                self.status.agent = f"{connection.agent_name} {connection.agent_version}".strip()
                self._set_connection_state("connected")
                if connection.supports_auth_status:
                    self.spawn(self._refresh_auth_status())
                await self._start_session(self.resume_id)
                await connection.wait_closed()
            if not self.closing:
                code = connection.exit_code
                suffix = f" (exit code {code})" if code is not None else ""
                self._fatal(f"The agent disconnected{suffix}.", title="Agent exited")
        except UnsupportedProtocolError as exc:
            self._fatal(str(exc), title="Unsupported agent", show_stderr=False)
        except AgentError as exc:
            self._fatal(str(exc), title="Agent error")
        except (ConnectionError, RequestError) as exc:
            if not self.closing:
                self._fatal(f"Connection to the agent failed: {exc}", title="Agent error")
        finally:
            self._close_dialogs()
            if not self.closing:
                self._set_connection_state("disconnected")

    async def _start_session(self, resume_id: str | None) -> None:
        connection = self._connection()
        try:
            if resume_id is not None:
                await self._resume(resume_id)
                return
            response = await connection.new_session(self.setup)
        except RequestError as exc:
            self._session_error(exc)
            return
        self._show_session(
            response.session_id, response.config_options, response.available_commands
        )

    def _show_session(
        self,
        session_id: str,
        options: list[SessionConfigOption] | object,
        commands: list[AvailableCommand] | object,
        *,
        replaying: bool = False,
    ) -> SessionState:
        state = SessionState(session_id, str(self.cwd), replaying=replaying)
        self.sessions[session_id] = state
        self.state = state
        self.status.state = state
        self.conversation.show(state)
        if isinstance(options, list) and options:
            self.conversation.push(state.set_config_options(options))
        if isinstance(commands, list):
            self.conversation.push(state.set_commands(commands))
            self.prompt.commands = list(state.commands)
        self.status.refresh_status()
        return state

    def _session_error(self, exc: RequestError) -> None:
        connection = self._connection()
        if exc.code == -32000:  # noqa: PLR2004 - authentication required
            methods = "\n".join(
                f"  • {m.name}"
                + (
                    " (ctrl+p → Log in)"
                    if isinstance(m, AuthMethodAgent | AuthMethodTerminal)
                    else ""
                )
                for m in connection.auth_methods
            )
            methods = methods or "  (it offers no login methods)"
            text = f"{connection.agent_name} needs you to log in.\n{methods}"
            self._fatal(text, title="Authentication required", show_stderr=False, fatal=False)
        else:
            self._fatal(
                f"Could not create a session: {exc} ({exc.code})",
                title="Session error",
                fatal=False,
            )

    async def _resume(self, session_id: str) -> None:
        connection = self._connection()
        state = self._show_session(session_id, None, None, replaying=True)
        try:
            response = await connection.resume_session(session_id, setup=self.setup)
        except RequestError as exc:
            self._fatal(f"Could not resume the session: {exc}", title="Session error", fatal=False)
            return
        state.replaying = False
        if is_set(response.config_options):
            self.conversation.push(state.set_config_options(response.config_options))
        if is_set(response.available_commands):
            self.conversation.push(state.set_commands(response.available_commands))
        if connection.supports_status:
            self.spawn(self.refresh_session_status())

    def _connection(self) -> AgentConnection:
        if self.connection is None:
            msg = "no agent connection"
            raise AgentError(msg)
        return self.connection

    def _set_connection_state(self, value: str) -> None:
        self.connected = value == "connected"
        self.status.connection_state = value
        self.status.refresh_status()
        self.prompt.set_class(not self.connected, "-disabled")

    def _fatal(
        self, message: str, *, title: str, show_stderr: bool = True, fatal: bool = True
    ) -> None:
        text = Content(message)
        if show_stderr and (tail := self.agent_log.tail("stderr", STDERR_TAIL)):
            text = Content.assemble(
                text, ("\n\nLast stderr lines:\n", "bold"), ("\n".join(tail), "$text-muted")
            )
        hint = "\n\nf5: restart · ctrl+l: log · ctrl+q: quit" if fatal else "\n\nctrl+l: log"
        panel = ErrorPanel(Content.assemble(text, (hint, "$text-muted italic")))
        panel.border_title = title
        self.agent_log.add("error", f"{title}: {message}")
        self.conversation.mount(panel)

    # --- AgentEvents ----------------------------------------------------------------

    def _state_for(self, session_id: str) -> SessionState:
        """The state of a session; sessions we did not create are subagent sessions."""
        state = self.sessions.get(session_id)
        if state is None:
            parent = next((s for s in self.sessions.values() if session_id in s.subagents), None)
            state = SessionState(
                session_id, str(self.cwd), parent_id=parent.session_id if parent else None
            )
            self.sessions[session_id] = state
        return state

    def session_update(self, notification: UpdateSessionNotification) -> None:
        state = self._state_for(notification.session_id)
        changes = state.apply(notification.update)
        if state is self.state:
            self.conversation.push(changes)
        elif (
            screen := self._subagent_screens.get(state.session_id)
        ) is not None and screen.is_attached:
            screen.conversation.push(changes)

    async def request_permission(
        self, request: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        state = self._state_for(request.session_id)
        if is_set(request.subject) and isinstance(request.subject, ToolCallPermissionSubject):
            changes = state.apply_tool_call(request.subject.tool_call)
            if state is self.state:
                self.conversation.push(changes)
        screen = PermissionScreen(request, state.cwd, self.diff_mode)
        self.app.bell()
        return await self._ask(screen, cancelled())

    async def _ask[T](self, screen: ModalScreen[T], default: T) -> T:
        """Show a dialog and wait for its answer.

        If the agent cancels the request (``$/cancel_request``) or the connection ends,
        the dialog is closed.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._dialogs.add(screen)

        def done(result: T | None) -> None:
            if not future.done():
                future.set_result(result if result is not None else default)

        self.app.push_screen(screen, callback=done)
        try:
            return await future
        except asyncio.CancelledError:
            if screen.is_attached:
                screen.dismiss(default)
            raise
        finally:
            self._dialogs.discard(screen)

    async def create_elicitation(
        self, request: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        from acptui.ui.screens.elicitation import cancelled_elicitation

        match request:
            case FormElicitationRequest():
                return await self._ask(FormElicitationScreen(request), cancelled_elicitation())
            case UrlElicitationRequest():
                screen = UrlElicitationScreen(request)
                self._url_elicitations[request.elicitation_id] = screen
                try:
                    return await self._ask(screen, cancelled_elicitation())
                finally:
                    self._url_elicitations.pop(request.elicitation_id, None)
            case _:
                self.notify(f"Unsupported elicitation mode {request.mode!r}", severity="warning")
                return DeclineElicitationResponse()

    def complete_elicitation(self, notification: CompleteElicitationNotification) -> None:
        if (screen := self._url_elicitations.get(notification.elicitation_id)) is not None:
            screen.complete()

    def auth_status_update(self, notification: AuthStatusUpdateNotification) -> None:
        accounts = notification.accounts if is_set(notification.accounts) else []
        self._show_auth(notification.state, accounts)

    def ext_notification(self, method: str, params: JsonValue) -> None:
        text = json.dumps(params, ensure_ascii=False)
        self.agent_log.add("info", f"extension notification {method}: {text}")

    def stderr_line(self, line: str) -> None:
        self.agent_log.add("stderr", line)

    def traffic(self, direction: str, text: str) -> None:
        self.agent_log.add("incoming" if direction == "incoming" else "outgoing", text)

    def _close_dialogs(self) -> None:
        for screen in list(self._dialogs):
            if isinstance(screen, PermissionScreen | FormElicitationScreen | UrlElicitationScreen):
                screen.cancel()
        self._dialogs.clear()

    def _show_auth(self, state: str, accounts: list[AuthAccount] | object) -> None:
        label = "not logged in" if state == "unauthenticated" else ""
        if isinstance(accounts, list):
            active = next(
                (a for a in accounts if is_set(a.active) and a.active),
                accounts[0] if accounts else None,
            )
            if active is not None:
                label = active.label
        self.status.account = label
        self.status.refresh_status()

    async def _refresh_auth_status(self) -> None:
        response = await self._connection().auth_status()
        accounts = response.accounts if is_set(response.accounts) else []
        self._show_auth(response.state, list(accounts))

    async def refresh_session_status(self) -> None:
        state = self.state
        if state is None:
            return
        response = await self._connection().session_status(state.session_id)
        snapshot = response.state if is_set(response.state) else None
        if snapshot is not None:
            state.activity = ACTIVITIES.get(snapshot.state, "unknown")
        self.notify(f"Session is {response.status}", timeout=2)
        self.status.refresh_status()

    # --- Conversation events --------------------------------------------------------

    def on_conversation_view_session_field_changed(
        self, message: ConversationView.SessionFieldChanged
    ) -> None:
        state = self.state
        if state is None:
            return
        if message.field == "commands":
            self.prompt.commands = list(state.commands)
        if message.field == "suggestions":
            self._show_suggestion()
        self.status.refresh_status()

    def _show_suggestion(self) -> None:
        state = self.state
        suggestion = state.suggestions[0] if state is not None and state.suggestions else None
        text = (
            content_markdown(suggestion.prompt, self.cwd).strip() if suggestion is not None else ""
        )
        self.prompt.suggestion = text

    # --- Prompt ---------------------------------------------------------------------

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        state = self.state
        connection = self.connection
        if state is None or connection is None or not self.connected:
            self.notify("Not connected to an agent", severity="warning")
            return
        skills = frozenset(c.name for c in state.commands if isinstance(c, Skill))
        options = PromptOptions(
            embedded_context=connection.supports_embedded_context,
            images=connection.supports_images,
            audio=connection.supports_audio,
            skills=skills if connection.supports_skills else frozenset(),
        )
        blocks = build_prompt(message.text, self.cwd, options)
        changes = state.add_local_user_message(blocks)
        entry = changes[0].entry if changes and isinstance(changes[0], EntryAdded) else None
        self.conversation.push(changes)
        self.prompt.load_text("")
        self.prompt.suggestion = ""
        self.conversation.anchor()
        if isinstance(entry, MessageEntry):
            self.run_worker(self._send_prompt(state, entry), group="rpc", exit_on_error=False)

    async def _send_prompt(self, state: SessionState, entry: MessageEntry) -> None:
        connection = self._connection()
        try:
            response = await connection.prompt(state.session_id, entry.blocks)
        except (RequestError, ConnectionError) as exc:
            self.conversation.push(
                state.add_notice("error", "The agent rejected the prompt", str(exc))
            )
            return
        state.acknowledge_user_message(entry, response.message_id)

    # --- Actions --------------------------------------------------------------------

    def action_cancel_turn(self) -> None:
        """Cancel the running turn."""
        state = self.state
        if state is None or self.connection is None or not self.connected:
            return
        if not state.running:
            self.notify("Nothing to cancel", timeout=2)
            return
        self.spawn(self.connection.cancel(state.session_id))
        self.notify("Cancelling…", timeout=2)

    def action_new_session(self) -> None:
        """Start a new session."""
        if self.connection is None or not self.connected:
            return
        self.run_worker(self._new_session(), group="session", exclusive=True, exit_on_error=False)

    async def _new_session(self) -> None:
        connection = self._connection()
        if self.state is not None:
            await connection.close_session(self.state.session_id)
        await self._start_session(None)
        self.prompt.focus()

    def action_resume_session(self) -> None:
        """Pick a session to resume."""
        if self.connection is None or not self.connected:
            return

        def chosen(session_id: str | None) -> None:
            if session_id is not None:
                self.run_worker(
                    self._resume(session_id), group="session", exclusive=True, exit_on_error=False
                )

        self.app.push_screen(SessionListScreen(self.connection), callback=chosen)

    def action_fork_session(self) -> None:
        """Fork the session (whole, or at a cursor the agent offers)."""
        connection = self.connection
        state = self.state
        if connection is None or state is None:
            return
        if not connection.supports_fork:
            self.notify("The agent cannot fork sessions", timeout=2)
            return

        def chosen(choice: tuple[str | None] | None) -> None:
            if choice is not None:
                self.run_worker(
                    self._fork(state.session_id, choice[0]),
                    group="session",
                    exclusive=True,
                    exit_on_error=False,
                )

        self.app.push_screen(ForkScreen(state), callback=chosen)

    async def _fork(self, session_id: str, cursor_id: str | None) -> None:
        try:
            response = await self._connection().fork_session(session_id, cursor_id, self.setup)
        except (RequestError, ConnectionError) as exc:
            self.notify(f"Could not fork: {exc}", severity="error")
            return
        self._show_session(
            response.session_id, response.config_options, response.available_commands
        )
        self.notify("Forked: now in the new session", timeout=3)

    def action_rename_session(self) -> None:
        """Rename the session."""
        state = self.state
        connection = self.connection
        if state is None or connection is None:
            return
        if not connection.supports_title:
            self.notify("The agent cannot rename sessions", timeout=2)
            return

        def chosen(title: str | None) -> None:
            if title:
                self.spawn(self._rename(state, title))

        self.app.push_screen(TextInputScreen("Session title", state.title or ""), callback=chosen)

    async def _rename(self, state: SessionState, title: str) -> None:
        state.title = await self._connection().set_title(state.session_id, title)
        self.status.refresh_status()

    def action_options(self) -> None:
        """Show the session options."""
        if self.state is None:
            return
        self.app.push_screen(
            ConfigScreen(list(self.state.config_options)), callback=self._config_chosen
        )

    def _config_chosen(self, choice: ConfigChoice | None) -> None:
        if choice is not None:
            config_id, value = choice
            self.spawn(self.set_config(config_id, value))

    async def set_config(self, config_id: str, value: str | bool) -> None:
        """Set a config option of the session."""
        state = self.state
        connection = self.connection
        if state is None or connection is None:
            return
        try:
            response = await connection.set_config_option(state.session_id, config_id, value)
        except (RequestError, ConnectionError) as exc:
            self.notify(f"Could not change the option: {exc}", severity="error")
            return
        self.conversation.push(state.set_config_options(response.config_options))

    def action_next_mode(self) -> None:
        """Switch to the next value of the mode option."""
        state = self.state
        if state is None:
            return
        option = next(
            (
                o
                for o in state.config_options
                if isinstance(o, SelectSessionConfigOption)
                and is_set(o.category)
                and o.category == "mode"
            ),
            None,
        )
        if option is None:
            self.notify("The agent has no mode option", timeout=2)
            return
        self.spawn(self.set_config(option.config_id, next_select_value(option)))

    def action_toggle_diff(self) -> None:
        """Cycle the diff view: unified, split, auto."""
        self.diff_mode = DIFF_MODES[(DIFF_MODES.index(self.diff_mode) + 1) % len(DIFF_MODES)]
        self.conversation.set_diff_mode(self.diff_mode)
        self.notify(f"Diff view: {self.diff_mode}", timeout=2)

    def action_show_log(self) -> None:
        """Show the agent log."""
        self.app.push_screen(LogScreen(self.agent_log))

    def action_help(self) -> None:
        """Show the help."""
        self.app.push_screen(HelpScreen())

    def action_providers(self) -> None:
        """Configure the agent's LLM providers."""
        if self.connection is not None and self.connection.supports_providers:
            self.app.push_screen(ProvidersScreen(self.connection))
        else:
            self.notify("The agent has no configurable providers", timeout=2)

    def action_restart(self) -> None:
        """Restart the agent (and start a new session)."""
        self.run_worker(self._restart(), group="session", exclusive=True, exit_on_error=False)

    async def _restart(self) -> None:
        self.closing = True
        self.workers.cancel_group(self, "agent")
        await asyncio.sleep(0)
        self.closing = False
        self.sessions.clear()
        self.state = None
        self.status.state = None
        self.conversation.show(None)
        self.start_agent()

    def action_scroll_conversation(self, direction: int) -> None:
        """Scroll the conversation by a page."""
        if direction < 0:
            self.conversation.scroll_page_up()
        else:
            self.conversation.scroll_page_down()

    def on_tool_call_view_cancel_requested(self, message: ToolCallView.CancelRequested) -> None:
        state = self.state
        if state is not None and self.connection is not None:
            self.spawn(self.connection.cancel_tool_call(state.session_id, message.tool_call_id))

    def on_terminal_view_cancel_requested(self, message: TerminalView.CancelRequested) -> None:
        state = self.state
        if state is not None and self.connection is not None:
            self.spawn(self.connection.cancel_terminal(state.session_id, message.terminal_id))

    def on_subagent_view_open(self, message: SubagentView.Open) -> None:
        state = self._state_for(message.session_id)
        parent = self.state
        child = parent.subagents.get(message.session_id) if parent is not None else None
        screen = SubagentScreen(
            self, state, child.display_title if child is not None else message.session_id
        )
        self._subagent_screens[message.session_id] = screen

        def closed(_: None) -> None:
            self._subagent_screens.pop(message.session_id, None)

        self.app.push_screen(screen, callback=closed)

    def on_subagent_view_cancel_requested(self, message: SubagentView.CancelRequested) -> None:
        self.cancel_subagent(message.session_id)

    def cancel_subagent(self, session_id: str) -> None:
        """Cancel a subagent's work, if its parent allows it."""
        parent = self.state
        child = parent.subagents.get(session_id) if parent is not None else None
        if child is None or not child.can_cancel or self.connection is None:
            self.notify("This subagent cannot be cancelled", timeout=2)
            return
        self.spawn(self.connection.cancel(session_id))

    # --- Authentication -------------------------------------------------------------

    async def login(self, method_id: str) -> None:
        """Log in with an auth method, then retry creating a session."""
        connection = self._connection()
        method = next((m for m in connection.auth_methods if m.method_id == method_id), None)
        if isinstance(method, AuthMethodTerminal):
            await self._terminal_login(method)
        else:
            try:
                await connection.login(method_id)
            except (RequestError, ConnectionError) as exc:
                self.notify(f"Login failed: {exc}", severity="error")
                return
        self.notify("Logged in", timeout=2)
        if connection.supports_auth_status:
            await self._refresh_auth_status()
        if self.state is None:
            await self._start_session(None)

    async def logout(self) -> None:
        """Log out of the agent."""
        connection = self._connection()
        try:
            await connection.logout()
        except (RequestError, ConnectionError) as exc:
            self.notify(f"Logout failed: {exc}", severity="error")
            return
        self.notify("Logged out", timeout=2)
        if connection.supports_auth_status:
            await self._refresh_auth_status()

    async def _terminal_login(self, method: AuthMethodTerminal) -> None:
        if not self.spec.command:
            self.notify("Terminal login needs a local agent", severity="error")
            return
        args = list(method.args) if is_set(method.args) else []
        env = {**os.environ, **self.spec.resolved_env()}
        if is_set(method.env):
            env.update({variable.name: variable.value for variable in method.env})
        with self.app.suspend():
            process = await asyncio.create_subprocess_exec(
                *self.spec.command, *args, env=env, cwd=self.cwd
            )
            await process.wait()

    async def shutdown(self) -> None:
        """Stop the agent (on quit)."""
        self.closing = True
        self._close_dialogs()
        self.conversation.stop()
        self.workers.cancel_group(self, "agent")
