"""The session screen: conversation, prompt and status; drives the agent connection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AuthMethodAgent,
    AuthMethodTerminal,
    SelectSessionConfigOption,
    Skill,
    ToolCallPermissionSubject,
)
from aclientp.schema.v2.base import is_set
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Footer, Static

from acptui.protocol.connection import AgentConnection, AgentError, UnsupportedProtocolError
from acptui.protocol.prompt import PromptOptions, build_prompt
from acptui.protocol.state import (
    EntryAdded,
    EntryChanged,
    MessageEntry,
    SessionChanged,
    SessionState,
    TerminalChanged,
)
from acptui.ui.screens.dialogs import ConfigScreen, HelpScreen, SessionListScreen, next_select_value
from acptui.ui.screens.log import LogScreen
from acptui.ui.screens.permission import PermissionScreen, cancelled
from acptui.ui.widgets.blocks import AgentMessageView, ThoughtView, ToolCallView, make_view
from acptui.ui.widgets.prompt import CompletionList, PromptInput
from acptui.ui.widgets.status import StatusBar
from acptui.ui.widgets.terminal import TerminalView


if TYPE_CHECKING:
    from pathlib import Path

    from aclientp.schema.v2 import (
        RequestPermissionRequest,
        RequestPermissionResponse,
        UpdateSessionNotification,
    )
    from textual.app import ComposeResult
    from textual.widget import Widget

    from acptui.config import AgentSpec, DiffViewMode
    from acptui.protocol.state import Change, Entry
    from acptui.ui.agent_log import AgentLog
    from acptui.ui.screens.dialogs import ConfigChoice


DIFF_MODES: tuple[DiffViewMode, ...] = ("unified", "split", "auto")
STDERR_TAIL = 12


class ErrorPanel(Static):
    """A fatal problem with the agent (it failed to start, crashed or is unsupported)."""

    DEFAULT_CSS = """
    ErrorPanel {
        height: auto;
        border: round $error;
        border-title-color: $error;
        padding: 0 1;
        margin: 1 0;
    }
    """


class Conversation(VerticalScroll):
    """The scrolling timeline."""

    DEFAULT_CSS = """
    Conversation {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    """


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
        Binding("ctrl+l", "show_log", "Log"),
        Binding("f1", "help", "Help"),
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
    ) -> None:
        super().__init__()
        self.spec = spec
        self.cwd = cwd
        self.agent_log = agent_log
        self.diff_mode: DiffViewMode = diff_mode
        self.expand_thoughts = expand_thoughts
        self.resume_id = resume_id
        self.connection: AgentConnection | None = None
        self.state: SessionState | None = None
        self.connected = False
        self._views: dict[Entry, Widget] = {}
        self._queue: asyncio.Queue[list[Change] | None] = asyncio.Queue()
        self._permissions: set[PermissionScreen] = set()
        self.closing = False

    # --- Layout ---------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Conversation(id="conversation")
        completion_list = CompletionList()
        prompt = PromptInput(self.cwd)
        prompt.completion_list = completion_list
        with Vertical(id="prompt-area"):
            yield completion_list
            yield prompt
            yield StatusBar()
        yield Footer(compact=True)

    @property
    def conversation(self) -> Conversation:
        """The timeline container."""
        return self.query_one(Conversation)

    @property
    def prompt(self) -> PromptInput:
        """The prompt editor."""
        return self.query_one(PromptInput)

    @property
    def status(self) -> StatusBar:
        """The status line."""
        return self.query_one(StatusBar)

    def on_mount(self) -> None:
        self.conversation.anchor()
        self.status.agent = self.spec.name
        self.status.refresh_status()
        self.prompt.focus()
        self.run_worker(self._render_loop(), name="render", group="render")
        self.start_agent()

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
            self._cancel_permissions()
            if not self.closing:
                self._set_connection_state("disconnected")

    async def _start_session(self, resume_id: str | None) -> None:
        connection = self._connection()
        try:
            if resume_id is not None:
                await self._resume(resume_id)
                return
            response = await connection.new_session()
        except RequestError as exc:
            self._session_error(exc)
            return
        state = self._new_state(response.session_id)
        changes = []
        if is_set(response.config_options):
            changes += state.set_config_options(response.config_options)
        if is_set(response.available_commands):
            changes += state.set_commands(response.available_commands)
        self._queue.put_nowait(changes)

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

    def _new_state(self, session_id: str, *, replaying: bool = False) -> SessionState:
        state = SessionState(session_id, str(self.cwd), replaying=replaying)
        self.state = state
        self.status.state = state
        self.status.refresh_status()
        return state

    async def _resume(self, session_id: str) -> None:
        connection = self._connection()
        await self._clear_conversation()
        state = self._new_state(session_id, replaying=True)
        try:
            response = await connection.resume_session(session_id)
        except RequestError as exc:
            self._fatal(f"Could not resume the session: {exc}", title="Session error", fatal=False)
            return
        state.replaying = False
        changes = []
        if is_set(response.config_options):
            changes += state.set_config_options(response.config_options)
        if is_set(response.available_commands):
            changes += state.set_commands(response.available_commands)
        self._queue.put_nowait(changes)

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

    def session_update(self, notification: UpdateSessionNotification) -> None:
        state = self.state
        if state is None or notification.session_id != state.session_id:
            return
        self._queue.put_nowait(state.apply(notification.update))

    async def request_permission(
        self, request: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        state = self.state
        if state is None or request.session_id != state.session_id:
            return cancelled()
        if is_set(request.subject) and isinstance(request.subject, ToolCallPermissionSubject):
            self._queue.put_nowait(state.apply_tool_call(request.subject.tool_call))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RequestPermissionResponse] = loop.create_future()
        screen = PermissionScreen(request, state.cwd, self.diff_mode)
        self._permissions.add(screen)

        def done(result: RequestPermissionResponse | None) -> None:
            self._permissions.discard(screen)
            if not future.done():
                future.set_result(result if result is not None else cancelled())

        self.app.push_screen(screen, callback=done)
        self.app.bell()
        return await future

    def stderr_line(self, line: str) -> None:
        self.agent_log.add("stderr", line)

    def traffic(self, direction: str, text: str) -> None:
        self.agent_log.add("incoming" if direction == "incoming" else "outgoing", text)

    def _cancel_permissions(self) -> None:
        for screen in list(self._permissions):
            screen.cancel()
        self._permissions.clear()

    # --- Rendering ------------------------------------------------------------------

    async def _render_loop(self) -> None:
        while True:
            changes = await self._queue.get()
            if changes is None:
                return
            for change in changes:
                try:
                    await self._render_change(change)
                except Exception as exc:  # noqa: BLE001 - one bad update must not kill the view
                    self.agent_log.add(
                        "error", f"render failed for {type(change).__name__}: {exc!r}"
                    )

    async def _render_change(self, change: Change) -> None:
        state = self.state
        if state is None:
            return
        match change:
            case EntryAdded(entry=entry):
                view = make_view(entry, state, self.diff_mode, expand_thoughts=self.expand_thoughts)
                self._views[entry] = view
                await self.conversation.mount(view)
            case EntryChanged(entry=entry, appended=appended):
                view = self._views.get(entry)
                if isinstance(view, AgentMessageView | ThoughtView):
                    if appended is not None:
                        await view.append_text(appended)
                    else:
                        await view.refresh_entry()
                elif isinstance(view, ToolCallView):
                    await view.refresh_entry()
                elif view is not None and hasattr(view, "refresh_entry"):
                    refresh = view.refresh_entry
                    if callable(refresh):
                        refresh()
            case TerminalChanged(terminal=terminal):
                for terminal_view in self.query(TerminalView):
                    if terminal_view.terminal is terminal:
                        terminal_view.refresh_terminal()
            case SessionChanged(field=field):
                await self._session_changed(field)

    async def _session_changed(self, field: str) -> None:
        state = self.state
        if state is None:
            return
        if field == "commands":
            self.prompt.commands = list(state.commands)
        if field == "activity" and not state.running:
            for view in self.query(AgentMessageView):
                await view.finish()
            for thought in self.query(ThoughtView):
                await thought.finish()
        self.status.refresh_status()

    async def _clear_conversation(self) -> None:
        self._views.clear()
        await self.conversation.remove_children()

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
            skills=skills if connection.supports_skills else frozenset(),
        )
        blocks = build_prompt(message.text, self.cwd, options)
        changes = state.add_local_user_message(blocks)
        entry = changes[0].entry if changes and isinstance(changes[0], EntryAdded) else None
        self._queue.put_nowait(changes)
        self.prompt.load_text("")
        self.conversation.anchor()
        if isinstance(entry, MessageEntry):
            self.run_worker(self._send_prompt(state, entry), group="rpc", exit_on_error=False)

    async def _send_prompt(self, state: SessionState, entry: MessageEntry) -> None:
        connection = self._connection()
        try:
            response = await connection.prompt(state.session_id, entry.blocks)
        except (RequestError, ConnectionError) as exc:
            self._queue.put_nowait(
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
        self._cancel_permissions()
        self.run_worker(self.connection.cancel(state.session_id), group="rpc", exit_on_error=False)
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
        await self._clear_conversation()
        self.state = None
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
            self.run_worker(self.set_config(config_id, value), group="rpc", exit_on_error=False)

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
        self._queue.put_nowait(state.set_config_options(response.config_options))

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
        self.run_worker(
            self.set_config(option.config_id, next_select_value(option)),
            group="rpc",
            exit_on_error=False,
        )

    def action_toggle_diff(self) -> None:
        """Cycle the diff view: unified, split, auto."""
        self.diff_mode = DIFF_MODES[(DIFF_MODES.index(self.diff_mode) + 1) % len(DIFF_MODES)]
        for view in self.query(ToolCallView):
            view.set_diff_mode(self.diff_mode)
        self.notify(f"Diff view: {self.diff_mode}", timeout=2)

    def action_show_log(self) -> None:
        """Show the agent log."""
        self.app.push_screen(LogScreen(self.agent_log))

    def action_help(self) -> None:
        """Show the help."""
        self.app.push_screen(HelpScreen())

    def action_restart(self) -> None:
        """Restart the agent (and start a new session)."""
        self.run_worker(self._restart(), group="session", exclusive=True, exit_on_error=False)

    async def _restart(self) -> None:
        self.closing = True
        self.workers.cancel_group(self, "agent")
        await asyncio.sleep(0)
        self.closing = False
        await self._clear_conversation()
        self.state = None
        self.status.state = None
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
            self.run_worker(
                self.connection.cancel_tool_call(state.session_id, message.tool_call_id),
                group="rpc",
                exit_on_error=False,
            )

    def on_terminal_view_cancel_requested(self, message: TerminalView.CancelRequested) -> None:
        state = self.state
        if state is not None and self.connection is not None:
            self.run_worker(
                self.connection.cancel_terminal(state.session_id, message.terminal_id),
                group="rpc",
                exit_on_error=False,
            )

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
        await self._clear_conversation()
        await self._start_session(None)

    async def _terminal_login(self, method: AuthMethodTerminal) -> None:
        import os

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
        self._cancel_permissions()
        self._queue.put_nowait(None)
        self.workers.cancel_group(self, "agent")
