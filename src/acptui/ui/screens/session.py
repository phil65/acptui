"""The session screen: conversation, prompt and status; drives the agent connection.

It implements :class:`~acptui.protocol.connection.AgentEvents`: every message the
agent sends arrives here and is routed to the session it belongs to (the main
session, or one of its subagent sessions).
"""

import asyncio
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, ClassVar

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AuthMethodAgent,
    AuthMethodTerminal,
    Command,
    DeclineElicitationResponse,
    FormElicitationRequest,
    SelectSessionConfigOption,
    Skill,
    ToolCallPermissionSubject,
    UrlElicitationRequest,
)
from aclientp.schema.v2.base import is_set
import platformdirs
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.screen import Screen
from textual.widget import Widget  # noqa: TC002
from textual.widgets import Footer, Static

from acptui.files import Files
from acptui.protocol.connection import (
    AgentConnection,
    AgentError,
    SessionSetup,
    UnsupportedProtocolError,
)
from acptui.protocol.content import content_markdown
from acptui.protocol.prompt import PromptOptions, build_prompt, shell_block
from acptui.protocol.state import (
    EntryAdded,
    EntryChanged,
    MessageEntry,
    PlanEntry,
    SessionState,
    ShellEntry,
)
from acptui.recent import RecentSession
from acptui.shell import open_shell
from acptui.ui.attention import Attention, TitleChanged
from acptui.ui.markdown import inline_markdown
from acptui.ui.screens.dialogs import (
    ConfigChoice,
    ConfigScreen,
    ForkScreen,
    HelpScreen,
    LoginScreen,
    SessionListScreen,
    TextInputScreen,
    method_description,
    next_select_value,
)
from acptui.ui.screens.elicitation import FormElicitationScreen, UrlElicitationScreen
from acptui.ui.screens.log import LogScreen
from acptui.ui.screens.permission import PermissionScreen, cancelled
from acptui.ui.screens.providers import ProvidersScreen
from acptui.ui.widgets.blocks import SubagentView, ToolCallView  # noqa: TC001
from acptui.ui.widgets.conversation import ConversationView
from acptui.ui.widgets.files import FileTree  # noqa: TC001
from acptui.ui.widgets.prompt import CompletionList, PromptInput
from acptui.ui.widgets.sidebar import AUTO_OPEN_WIDTH, SideBar
from acptui.ui.widgets.status import StatusBar
from acptui.ui.widgets.tabs import SessionTabs
from acptui.ui.widgets.terminal import ShellView, TerminalView  # noqa: TC001


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

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
    from textual.timer import Timer

    from acptui.config import AgentSpec, DiffViewMode, SidebarMode, ToolExpand
    from acptui.history import History
    from acptui.protocol.state import Change, SessionActivity
    from acptui.recent import RecentSessions
    from acptui.shell import Shell, ShellCommand
    from acptui.ui.agent_log import AgentLog
    from acptui.ui.attention import AttentionKind
    from acptui.ui.widgets.tabs import TabInfo, TabState


DIFF_MODES: tuple[DiffViewMode, ...] = ("unified", "split", "auto")
ACTIVITIES: dict[str, SessionActivity] = {
    "idle": "idle",
    "running": "running",
    "requires_action": "requires_action",
}
STDERR_TAIL = 12
MIN_COLUMN = 40


@dataclass(frozen=True)
class LayoutSettings:
    """The layout settings of a session screen (the app updates them)."""

    column_width: int = 0
    """A centered column for the conversation and the prompt (0: full width)."""
    footer: bool = True
    status_line: bool = True
    compact_input: bool = False


CLIENT_COMMANDS: tuple[Command, ...] = tuple(
    Command(name=f"acptui:{name}", description=f"{description} *(acptui)*")
    for name, description in (
        ("export", "Save the conversation as Markdown (and copy it)"),
        ("clear", "Clear the view; the session keeps its history"),
        ("rename", "Rename the session: `/acptui:rename TITLE`"),
        ("new", "Start a new session in this tab"),
        ("tab", "Open a new tab (agent picker)"),
        ("close", "Close this tab and stop its agent"),
        ("about", "Versions, paths, terminal and agent"),
    )
)
"""acptui's own slash commands; the ``acptui:`` prefix keeps them apart from the agent's."""


def about(screen: SessionScreen) -> str:
    """Versions, paths, the terminal and the agent, for bug reports."""
    from importlib.metadata import version

    from acptui.config import config_path
    from acptui.ui.widgets.image import image_mode

    connection = screen.connection
    agent = (
        f"{connection.agent_name} {connection.agent_version}".strip()
        if connection is not None
        else screen.spec.name
    )
    terminal = os.environ.get("TERM_PROGRAM") or os.environ.get("TERM") or "unknown"
    lines = [
        f"acptui {version('acptui')}, aclientp {version('aclientp')}, textual {version('textual')}",
        f"Python {sys.version.split()[0]} ({sys.platform})",
        f"agent: {agent} ({screen.spec.target})",
        f"config: {config_path()}",
        f"working directory: {screen.cwd}",
        f"terminal: {terminal}, images: {image_mode()}",
    ]
    return "\n".join(lines)


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
            tool_expand=self.parent_screen.tool_expand,
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
        #shell-context { height: auto; padding: 0 1; color: $text-muted; display: none; }
        #shell-context.-pending { display: block; }
    }
    SessionScreen.-sidebar-open #conversation PlanView {
        border: none;
        border-left: wide $secondary 60%;
        padding: 0 1;
        #plan-items, #plan-markdown { display: none; }
        #plan-summary { display: block; }
    }
    SessionScreen.-compact-input PromptInput {
        border: none;
        border-left: wide $primary 50%;
        min-height: 1;
        &:focus { border: none; border-left: wide $primary; }
        &.-disabled { border: none; border-left: wide $panel; }
        &.-shell { border: none; border-left: wide $warning 50%; }
        &.-shell:focus { border: none; border-left: wide $warning; }
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
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+g", "drop_shell_context", "Drop shell output", show=False),
        Binding("f1", "help", "Help"),
        Binding("f2", "rename_session", "Rename", show=False),
        Binding("f5", "restart", "Restart", show=False),
        Binding("alt+up", "block_cursor(-1)", "Blocks", show=False),
        Binding("alt+down", "block_cursor(1)", show=False),
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
        tool_expand: ToolExpand = "auto",
        resume_id: str | None = None,
        setup: SessionSetup | None = None,
        mounts: Mapping[str, str] | None = None,
        history: History | None = None,
        recent: RecentSessions | None = None,
    ) -> None:
        super().__init__()
        self.recent = recent
        """The recent sessions list (for the picker), if kept."""
        self.history = history
        """The prompt history file (``None``: history only for this screen)."""
        self.spec = spec
        self.cwd = cwd
        self.files = Files(cwd, mounts)
        """The sidebar's filesystem: the workspace (browsable into files) and the mounts."""
        self._files_timer: Timer | None = None
        self.shell: Shell | None = None
        """The shell of shell mode (started with the first command)."""
        self.shell_context: list[ShellCommand] = []
        """Finished shell commands, sent to the agent with the next prompt."""
        self._shell_entries: dict[ShellCommand, tuple[SessionState, ShellEntry]] = {}
        self.agent_log = agent_log
        self.diff_mode: DiffViewMode = diff_mode
        self.expand_thoughts = expand_thoughts
        self.tool_expand: ToolExpand = tool_expand
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
        self.connection_state = "starting"
        """``starting``, ``connected`` or ``disconnected``."""
        self.tab_id = ""
        """The app's name for this tab (its Textual mode)."""
        self.tab_bar = SessionTabs()
        """The tab bar (updated by the app, also before the screen is composed)."""
        self.layout_settings = LayoutSettings()
        """Applied on mount (the app updates them later with :meth:`apply_layout`)."""
        self.max_blocks = 0
        """Blocks kept in the conversation view (0: all)."""
        self.sidebar_mode: SidebarMode = "auto"
        """When the sidebar opens by itself (the ``sidebar`` setting)."""
        self._sidebar_toggled = False
        """The user opened or closed the sidebar in this tab: it no longer opens by itself."""
        self._shown = asyncio.Event()
        """Set when the screen is shown again (dialogs of a background tab wait for it)."""
        self._subagent_screens: dict[str, SubagentScreen] = {}

    # --- Layout ---------------------------------------------------------------------

    @property
    def sidebar(self) -> SideBar:
        """The sidebar: plan and files."""
        return self.query_one(SideBar)

    def action_toggle_sidebar(self) -> None:
        """Show (and focus) or hide the sidebar."""
        self._sidebar_toggled = True
        if self.sidebar.open:
            self.close_sidebar()
            self.prompt.focus()
        else:
            self.open_sidebar(focus=True)

    def open_sidebar(self, *, focus: bool = False) -> None:
        """Show the sidebar (and mark the files the agent touched while it is open)."""
        self.sidebar.show(focus=focus)
        self.add_class("-sidebar-open")
        self._mark_touched()
        if self._files_timer is None:
            self._files_timer = self.set_interval(2.0, self._mark_touched, name="files-markers")
        else:
            self._files_timer.resume()

    def close_sidebar(self) -> None:
        """Hide the sidebar."""
        self.sidebar.hide()
        self.remove_class("-sidebar-open")
        if self._files_timer is not None:
            self._files_timer.pause()

    def on_side_bar_leave(self, message: SideBar.Leave) -> None:
        message.stop()
        self.prompt.focus()

    def _update_plans(self, state: SessionState, changes: list[Change]) -> None:
        """Show plan changes in the sidebar; open it for a new plan (``sidebar = "auto"``)."""
        if not any(
            isinstance(change, EntryAdded | EntryChanged) and isinstance(change.entry, PlanEntry)
            for change in changes
        ):
            return
        self.sidebar.plans.show(state)
        if (
            self.sidebar_mode == "auto"
            and not self._sidebar_toggled
            and not self.sidebar.open
            and state.plans
            and self.app.size.width >= AUTO_OPEN_WIDTH
        ):
            self.open_sidebar()

    def _mark_touched(self) -> None:
        sidebar = self.sidebar
        if sidebar.open and self.state is not None:
            sidebar.file_tree.set_touched(self._touched_paths())

    def _touched_paths(self) -> set[str]:
        """Workspace-relative paths of the session's tool call locations."""
        root = self.cwd.resolve()
        touched: set[str] = set()
        for tool_call in self.state.tool_calls.values() if self.state else ():
            for location in tool_call.locations:
                path = Path(location.path)
                path = path if path.is_absolute() else root / path
                if path.is_relative_to(root):
                    touched.add(path.relative_to(root).as_posix())
        return touched

    def on_file_tree_mention(self, message: FileTree.Mention) -> None:
        """Put ``@node`` into the prompt, where the cursor is."""
        message.stop()
        self.prompt.insert(f"@{message.text} ")
        self.prompt.focus()

    def compose(self) -> ComposeResult:
        yield self.tab_bar
        yield SideBar(self.files)
        yield ConversationView(
            self.agent_log,
            diff_mode=self.diff_mode,
            expand_thoughts=self.expand_thoughts,
            tool_expand=self.tool_expand,
            id="conversation",
        )
        completion_list = CompletionList()
        prompt = PromptInput(self.cwd)
        prompt.completion_list = completion_list
        with Vertical(id="prompt-area"):
            yield completion_list
            yield Static("", id="shell-context")
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

    def apply_layout(self, layout: LayoutSettings) -> None:
        """Column width, footer, status line and compact prompt."""
        self.layout_settings = layout
        column_width = layout.column_width
        width = max(column_width, MIN_COLUMN) if column_width else None
        self.styles.align_horizontal = "center" if width else "left"
        for widget in (self.conversation, self.query_one("#prompt-area")):
            widget.styles.max_width = width
        self.query_one(Footer).display = layout.footer
        self.status.display = layout.status_line
        self.set_class(layout.compact_input, "-compact-input")

    def on_mount(self) -> None:
        self.apply_layout(self.layout_settings)
        if self.sidebar_mode == "open":
            self.open_sidebar()
        self.conversation.max_blocks = self.max_blocks
        if self.history is not None:
            self.prompt.prompt_history = self.history.texts(shell=False)
            self.prompt.shell_history = self.history.texts(shell=True)
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

    def remember(self, state: SessionState) -> None:
        """Note the session in the recent sessions list (main sessions only)."""
        if self.recent is None or state.parent_id is not None:
            return
        spec = self.spec
        self.recent.record(
            RecentSession(
                agent_id=spec.id,
                agent_name=spec.name,
                command=spec.command,
                url=spec.url,
                session_id=state.session_id,
                cwd=str(self.cwd),
                title=state.title or "",
            )
        )

    def _show_session(
        self,
        session_id: str,
        options: list[SessionConfigOption] | object,
        commands: list[AvailableCommand] | object,
        *,
        replaying: bool = False,
        state: SessionState | None = None,
    ) -> SessionState:
        """Make ``session_id`` the shown session (a new state, or ``state``)."""
        if state is None:
            state = SessionState(session_id, str(self.cwd), replaying=replaying)
        self.sessions[session_id] = state
        self.state = state
        self.status.state = state
        self.conversation.show(state)
        self.sidebar.plans.show(state)
        if isinstance(options, list) and options:
            self.conversation.push(state.set_config_options(options))
        if isinstance(commands, list):
            self.conversation.push(state.set_commands(commands))
            self.prompt.commands = [*state.commands, *CLIENT_COMMANDS]
        self.status.refresh_status()
        self.remember(state)
        return state

    def _session_error(self, exc: RequestError) -> None:
        connection = self._connection()
        if exc.code == -32000:  # noqa: PLR2004 - authentication required
            lines: list[Content] = []
            for method in connection.auth_methods:
                runnable = isinstance(method, AuthMethodAgent | AuthMethodTerminal)
                line = Content.assemble("  • ", (method.name, "bold"))
                if description := method_description(method):  # Markdown (ACP v2)
                    line = Content.assemble(line, " — ", inline_markdown(description))
                lines.append(line)
                if not runnable:
                    lines[-1] = Content.assemble(lines[-1], (" (unsupported)", "$text-muted"))
            methods = (
                Content("\n").join(lines) if lines else Content("  (it offers no login methods)")
            )
            hint = "\nctrl+p → Log in" if connection.auth_methods else ""
            text = Content.assemble(
                f"{connection.agent_name} needs you to log in.\n", methods, hint
            )
            self._fatal(text, title="Authentication required", show_stderr=False, fatal=False)
        else:
            self._fatal(
                f"Could not create a session: {exc} ({exc.code})",
                title="Session error",
                fatal=False,
            )

    async def _resume(self, session_id: str, title: str | None = None) -> None:
        """Resume a session with its history replayed.

        The resume response has no title. The replayed history (handled when the response
        arrives) may name older ones: the session list's (current) title wins; without
        it, the one the session had here before, if the replay names none.
        """
        connection = self._connection()
        known = self.sessions.get(session_id)
        earlier = known.title if known is not None else None
        state = self._show_session(session_id, None, None, replaying=True)
        try:
            response = await connection.resume_session(session_id, setup=self.setup)
        except RequestError as exc:
            self._fatal(f"Could not resume the session: {exc}", title="Session error", fatal=False)
            return
        state.replaying = False
        if title is not None or state.title is None:
            state.title = title or earlier
            self.status.refresh_status()
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
        self.connection_state = value
        self.status.connection_state = value
        self.status.refresh_status()
        self.prompt.set_class(not self.connected, "-disabled")

    def _fatal(
        self, message: str | Content, *, title: str, show_stderr: bool = True, fatal: bool = True
    ) -> None:
        text = Content(message) if isinstance(message, str) else message
        if show_stderr and (tail := self.agent_log.tail("stderr", STDERR_TAIL)):
            text = Content.assemble(
                text, ("\n\nLast stderr lines:\n", "bold"), ("\n".join(tail), "$text-muted")
            )
        hint = "\n\nf5: restart · ctrl+l: log · ctrl+q: quit" if fatal else "\n\nctrl+l: log"
        panel = ErrorPanel(Content.assemble(text, (hint, "$text-muted italic")))
        panel.border_title = title
        self.agent_log.add("error", f"{title}: {text.plain}")
        if fatal:
            self.attention("error", title, text.plain.splitlines()[0])
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
        was_running = state.running
        old_title = state.title
        changes = state.apply(notification.update)
        if state is self.state:
            if was_running and not state.running and not state.replaying:
                reason = state.stop_reason or "done"
                self.attention("turn_end", "Turn ended", reason.replace("_", " "))
            if state.title != old_title:
                self.post_message(TitleChanged())
                self.remember(state)
        if state is self.state:
            self.conversation.push(changes)
            self._update_plans(state, changes)
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
        options = ", ".join(option.name for option in request.options)
        return await self._ask(screen, cancelled(), (request.title, options))

    # --- Tabs -----------------------------------------------------------------------

    @property
    def tab_label(self) -> str:
        """The tab's label: the agent, and the session title if there is one."""
        title = self.state.title if self.state is not None else None
        return f"{self.spec.name}: {title}" if title else self.spec.name

    @property
    def tab_state(self) -> TabState:
        """What the session is doing, for the tab bar."""
        if self.waiting:
            return "waiting"
        if self.connection_state == "starting":
            return "starting"
        if not self.connected:
            return "disconnected"
        return "working" if self.state is not None and self.state.running else "idle"

    def show_tabs(self, tabs: list[TabInfo], *, visible: bool) -> None:
        """Update the tab bar."""
        self.tab_bar.show_tabs(tabs, visible=visible)

    def on_screen_resume(self) -> None:
        self._shown.set()

    async def _until_shown(self) -> None:
        """Wait until this tab is the one shown (dialogs of other tabs wait)."""
        while self not in self.app.screen_stack:
            self._shown.clear()
            await self._shown.wait()

    @property
    def waiting(self) -> bool:
        """Whether the agent waits for the user (a permission or input dialog is open)."""
        return bool(self._dialogs)

    def attention(self, kind: AttentionKind, title: str, message: str = "") -> None:
        """Ask the app for the user's attention (bell, sound, notification)."""
        self.post_message(Attention(kind, f"{self.spec.name}: {title}", message, self))

    async def _ask[T](
        self, screen: ModalScreen[T], default: T, alert: tuple[str, str] | None = None
    ) -> T:
        """Show a dialog and wait for its answer (``alert``: notification title and text).

        If the agent cancels the request (``$/cancel_request``) or the connection ends,
        the dialog is closed.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._dialogs.add(screen)
        title, text = alert or ("Input needed", "")
        self.attention("question", title, text)
        try:
            await self._until_shown()
        except asyncio.CancelledError:
            self._dialogs.discard(screen)
            raise

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
            self.post_message(TitleChanged())

    async def create_elicitation(
        self, request: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        from acptui.ui.screens.elicitation import cancelled_elicitation

        match request:
            case FormElicitationRequest():
                alert = ("Input needed", request.message)
                return await self._ask(
                    FormElicitationScreen(request), cancelled_elicitation(), alert
                )
            case UrlElicitationRequest():
                screen = UrlElicitationScreen(request)
                self._url_elicitations[request.elicitation_id] = screen
                try:
                    alert = ("Please open a URL", request.message)
                    return await self._ask(screen, cancelled_elicitation(), alert)
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
            self.prompt.commands = [*state.commands, *CLIENT_COMMANDS]
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
        if self.history is not None:
            self.history.append(message.text, shell=message.shell, cwd=str(self.cwd))
        if not message.shell and message.text.startswith("/acptui:"):
            self.prompt.load_text("")
            name, _, argument = message.text[len("/acptui:") :].partition(" ")
            self.client_command(name.strip(), argument.strip())
            return
        if message.shell:
            self.run_worker(self._run_shell(message.text), group="shell", exit_on_error=False)
            return
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
        blocks = build_prompt(message.text, self.cwd, options, resolve=self.files.mention_block)
        blocks.extend(shell_block(command, options) for command in self.shell_context)
        self.shell_context.clear()
        self._update_shell_context()
        changes = state.add_local_user_message(blocks)
        entry = changes[0].entry if changes and isinstance(changes[0], EntryAdded) else None
        self.conversation.push(changes)
        self.prompt.load_text("")
        self.prompt.suggestion = ""
        self.conversation.anchor()
        if isinstance(entry, MessageEntry):
            self.run_worker(self._send_prompt(state, entry), group="rpc", exit_on_error=False)

    def client_command(self, name: str, argument: str = "") -> None:
        """Run one of acptui's own slash commands (see :data:`CLIENT_COMMANDS`)."""
        app = self.app
        match name:
            case "export":
                self.action_export()
            case "clear":
                self.conversation.clear_view()
            case "rename" if argument and self.state is not None:
                if self.connection is not None and self.connection.supports_title:
                    self.spawn(self._rename(self.state, argument))
                else:
                    self.notify("The agent cannot rename sessions", timeout=2)
            case "rename":
                self.action_rename_session()
            case "new":
                self.action_new_session()
            case "tab":
                app.call_later(app.run_action, "new_tab")
            case "close":
                app.call_later(app.run_action, "close_tab")
            case "about":
                if self.state is not None:
                    self.conversation.push(
                        self.state.add_notice("info", "About acptui", about(self))
                    )
            case _:
                self.notify(f"Unknown command /acptui:{name}", severity="warning")

    def action_export(self) -> None:
        """Save the conversation as Markdown (Downloads) and copy it."""
        from acptui.protocol.export import session_markdown

        state = self.state
        if state is None:
            return
        text = session_markdown(state, self.spec.name)
        folder = platformdirs.user_downloads_path()
        path = folder / f"acptui_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.md"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.notify(f"Couldn't save the conversation: {exc}", severity="error")
            return
        self.app.copy_to_clipboard(text)
        self.notify(f"{path} (copied)", title="Conversation exported", markup=False)

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

    # --- Shell mode -----------------------------------------------------------------

    async def _run_shell(self, text: str) -> None:
        """Run a shell-mode command (or type the line into the command that is running)."""
        state = self.state
        if state is None:
            self.notify("Shell mode needs a session (connecting…)", severity="warning")
            return
        self.prompt.load_text("")
        if self.shell is None:
            self.shell = open_shell(self.cwd, on_output=self._on_shell_output)
        shell = self.shell
        if shell.busy:
            await shell.send_input(text)
            return
        try:
            command = await shell.run(text)
        except OSError as exc:
            self.conversation.push(state.add_notice("error", "The shell did not start", str(exc)))
            self.shell = None
            return
        changes = state.add_shell_command(command)
        entry = changes[0].entry if changes and isinstance(changes[0], EntryAdded) else None
        if isinstance(entry, ShellEntry):
            self._shell_entries[command] = (state, entry)
        if self.state is state:
            self.conversation.push(changes)
            self.conversation.anchor()
        if command.done:  # failed right away
            self._on_shell_output(command)

    def _on_shell_output(self, command: ShellCommand) -> None:
        known = self._shell_entries.get(command)
        if known is not None and known[0] is self.state:
            self.conversation.push([EntryChanged(known[1])])
        if command.done:
            self._shell_entries.pop(command, None)
            self.shell_context.append(command)
            self._update_shell_context()

    def _update_shell_context(self) -> None:
        label = self.query_one("#shell-context", Static)
        commands = self.shell_context
        label.set_class(bool(commands), "-pending")
        if not commands:
            label.update("")
            return
        listed = ", ".join(f"$ {c.command.splitlines()[0]} (exit {c.exit_code})" for c in commands)
        label.update(f"With the next prompt: {listed}   ctrl+g: drop")

    def action_drop_shell_context(self) -> None:
        """Don't send the output of the shell commands run since the last prompt."""
        if not self.shell_context:
            self.notify("No shell output waiting", timeout=2)
            return
        self.shell_context.clear()
        self._update_shell_context()
        self.notify("Shell output dropped", timeout=2)

    def on_prompt_input_interrupt_shell(self, message: PromptInput.InterruptShell) -> None:
        message.stop()
        if self.shell is not None and self.shell.busy:
            self.spawn(self.shell.interrupt())

    def on_shell_view_interrupt_requested(self, message: ShellView.InterruptRequested) -> None:
        message.stop()
        if self.shell is not None and self.shell.current is message.entry.command:
            self.spawn(self.shell.interrupt())

    # --- Actions --------------------------------------------------------------------

    def action_cancel_turn(self) -> None:
        """Cancel the running turn (or the running shell command, when the agent is idle)."""
        state = self.state
        agent_busy = state is not None and state.running
        if not agent_busy and self.shell is not None and self.shell.busy:
            self.spawn(self.shell.interrupt())
            return
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

        listing = SessionListScreen(self.connection)

        def chosen(session_id: str | None) -> None:
            if session_id is not None:
                info = listing.sessions.get(session_id)
                title = info.title if info is not None and is_set(info.title) else None
                self.run_worker(
                    self._resume(session_id, title),
                    group="session",
                    exclusive=True,
                    exit_on_error=False,
                )

        self.app.push_screen(listing, callback=chosen)

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
                    self._fork(state, choice[0]),
                    group="session",
                    exclusive=True,
                    exit_on_error=False,
                )

        self.app.push_screen(ForkScreen(state), callback=chosen)

    async def _fork(self, source: SessionState, cursor_id: str | None) -> None:
        connection = self._connection()
        try:
            response = await connection.fork_session(source.session_id, cursor_id, self.setup)
        except (RequestError, ConnectionError) as exc:
            self.notify(f"Could not fork: {exc}", severity="error")
            return
        # The fork has the history up to the cursor (all of it without): show that part
        cursor = source.cursors.get(cursor_id) if cursor_id is not None else None
        forked = source.forked(response.session_id, cursor)
        self._show_session(
            response.session_id,
            response.config_options,
            response.available_commands,
            state=forked,
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
        self.remember(state)

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

    def action_block_cursor(self, step: int) -> None:
        """Select a conversation block (``c`` copy, ``p`` to prompt, ``v`` SVG, ``m`` max)."""
        self.conversation.move_block_cursor(step)

    def watch_maximized(self, maximized: Widget | None) -> None:
        """Back from a maximized block: the block cursor keeps the focus."""
        if maximized is None and self.is_mounted and self.conversation.block_cursor is not None:
            self.call_after_refresh(self.conversation.focus)

    def on_conversation_view_to_prompt(self, message: ConversationView.ToPrompt) -> None:
        self.prompt.insert(message.text)
        self.prompt.focus()

    def on_conversation_view_left_blocks(self) -> None:
        self.prompt.focus()

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

    def action_login(self) -> None:
        """Pick one of the agent's login methods."""
        connection = self.connection
        if connection is None or not connection.auth_methods:
            self.notify("The agent offers no login methods", timeout=2)
            return

        def chosen(method_id: str | None) -> None:
            if method_id is not None:
                self.call(self.login, method_id)

        self.app.push_screen(
            LoginScreen(connection.agent_name, connection.auth_methods), callback=chosen
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

    def on_unmount(self) -> None:
        if self.shell is not None:
            self.shell.close()

    async def shutdown(self) -> None:
        """Stop the agent and the shell (on quit)."""
        self.closing = True
        if self.shell is not None:
            self.shell.close()
        self._close_dialogs()
        self.conversation.stop()
        self.workers.cancel_group(self, "agent")
