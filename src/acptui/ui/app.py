"""The Textual application."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar

from aclientp.schema.v2 import (
    AuthMethodAgent,
    AuthMethodTerminal,
    BooleanSessionConfigOption,
    SelectSessionConfigOption,
)
from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider

from acptui.ui.agent_log import AgentLog
from acptui.ui.screens.dialogs import _select_values
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.screens.session import SessionScreen


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from acptui.config import AgentSpec, Settings


def session_commands(screen: SessionScreen) -> Iterator[tuple[str, str, Callable[[], object]]]:
    """Commands for the palette while a session is open."""
    yield "New session", "Start a new session with the agent", screen.action_new_session
    yield "Resume session", "Pick a session to resume", screen.action_resume_session
    yield "Session options", "Mode, model and other options", screen.action_options
    yield "Cancel turn", "Cancel the running turn", screen.action_cancel_turn
    yield "Toggle diff view", "Unified / split / auto", screen.action_toggle_diff
    yield "Agent log", "stderr and protocol traffic", screen.action_show_log
    yield "Restart agent", "Restart the agent process", screen.action_restart
    yield "Help", "Keys and tips", screen.action_help
    state = screen.state
    if state is not None:
        for option in state.config_options:
            if isinstance(option, SelectSessionConfigOption):
                for value, name, _ in _select_values(option):
                    callback = partial(
                        screen.run_worker, screen.set_config(option.config_id, value)
                    )
                    yield f"{option.name}: {name}", f"Set {option.name.lower()} to {name}", callback
            elif isinstance(option, BooleanSessionConfigOption):
                callback = partial(
                    screen.run_worker, screen.set_config(option.config_id, not option.current_value)
                )
                yield (
                    f"Toggle {option.name}",
                    f"Turn {option.name.lower()} {'off' if option.current_value else 'on'}",
                    callback,
                )
    connection = screen.connection
    if connection is not None:
        for method in connection.auth_methods:
            if isinstance(method, AuthMethodAgent | AuthMethodTerminal):
                callback = partial(screen.run_worker, screen.login(method.method_id))
                yield f"Log in: {method.name}", "Authenticate with the agent", callback


class SessionCommands(Provider):
    """Command palette entries of the session screen."""

    def _commands(self) -> list[tuple[str, str, Callable[[], object]]]:
        screen = self.screen
        return list(session_commands(screen)) if isinstance(screen, SessionScreen) else []

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class AcpTui(App[None]):
    """acptui: a terminal UI for ACP v2 agents."""

    TITLE = "acptui"
    COMMANDS: ClassVar = App.COMMANDS | {SessionCommands}
    BINDINGS: ClassVar = [Binding("ctrl+q", "quit", "Quit", priority=True)]
    CSS = """
    Screen { background: $background; }
    """

    def __init__(
        self,
        settings: Settings,
        cwd: Path,
        spec: AgentSpec | None = None,
        *,
        resume_id: str | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.cwd = cwd
        self.spec = spec
        self.resume_id = resume_id
        self.agent_log = AgentLog()

    def on_mount(self) -> None:
        if self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        if self.spec is not None:
            self.push_screen(self._session_screen(self.spec))
        else:
            self.push_screen(AgentPicker(self.settings.all_agents(), str(self.cwd)))

    def _session_screen(self, spec: AgentSpec) -> SessionScreen:
        return SessionScreen(
            spec,
            self.cwd,
            self.agent_log,
            diff_mode=self.settings.diff_view,
            expand_thoughts=self.settings.expand_thoughts,
            resume_id=self.resume_id,
        )

    def on_agent_picker_picked(self, message: AgentPicker.Picked) -> None:
        self.spec = message.spec
        self.switch_screen(self._session_screen(message.spec))

    async def action_quit(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, SessionScreen):
                await screen.shutdown()
        self.exit()
