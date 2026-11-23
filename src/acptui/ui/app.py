"""The Textual application."""

from __future__ import annotations

import datetime as dt
from functools import partial
import io
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from aclientp.schema.v2 import (
    AuthMethodAgent,
    AuthMethodTerminal,
    BooleanSessionConfigOption,
    SelectSessionConfigOption,
)
import platformdirs
from rich.console import Console
from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider

from acptui.setup import build_setup
from acptui.ui.agent_log import AgentLog
from acptui.ui.screens.dialogs import _select_values
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.screens.session import SessionScreen


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from acptui.config import AgentSpec, Settings
    from acptui.protocol.connection import SessionSetup


def session_commands(screen: SessionScreen) -> Iterator[tuple[str, str, Callable[[], object]]]:
    """Commands for the palette while a session is open."""
    yield "New session", "Start a new session with the agent", screen.action_new_session
    yield "Resume session", "Pick a session to resume", screen.action_resume_session
    yield (
        "Fork session",
        "Fork the session, whole or at a point the agent offers",
        screen.action_fork_session,
    )
    yield "Rename session", "Set the session title", screen.action_rename_session
    yield "Session options", "Mode, model and other options", screen.action_options
    yield "Cancel turn", "Cancel the running turn", screen.action_cancel_turn
    yield "LLM providers", "Configure the agent's LLM providers", screen.action_providers
    yield "Toggle diff view", "Unified / split / auto", screen.action_toggle_diff
    yield "Agent log", "stderr and protocol traffic", screen.action_show_log
    yield "Restart agent", "Restart the agent process", screen.action_restart
    yield "Help", "Keys and tips", screen.action_help
    state = screen.state
    if state is not None:
        for option in state.config_options:
            if isinstance(option, SelectSessionConfigOption):
                for value, name, _ in _select_values(option):
                    callback = partial(screen.call, screen.set_config, option.config_id, value)
                    yield f"{option.name}: {name}", f"Set {option.name.lower()} to {name}", callback
            elif isinstance(option, BooleanSessionConfigOption):
                new = not option.current_value
                callback = partial(screen.call, screen.set_config, option.config_id, new)
                yield (
                    f"Toggle {option.name}",
                    f"Turn {option.name.lower()} {'on' if new else 'off'}",
                    callback,
                )
    connection = screen.connection
    if connection is None:
        return
    for method in connection.auth_methods:
        if isinstance(method, AuthMethodAgent | AuthMethodTerminal):
            kind = "in a terminal" if isinstance(method, AuthMethodTerminal) else "via the agent"
            yield (
                f"Log in: {method.name}",
                f"Authenticate {kind}",
                partial(screen.call, screen.login, method.method_id),
            )
    if connection.supports_login:
        yield "Log out", "Log out of the agent", partial(screen.call, screen.logout)
    if connection.supports_status:
        yield (
            "Session status",
            "Is the session open or saved?",
            partial(screen.call, screen.refresh_session_status),
        )


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
    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f12", "screenshot", "Screenshot", show=False),
    ]
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
        setup: SessionSetup | None = None,
    ) -> None:
        super().__init__()
        self.setup_override = setup
        self.settings = settings
        self.cwd = cwd
        self.spec = spec
        self.resume_id = resume_id
        self.agent_log = AgentLog()

    def screen_text(self) -> str:
        """The current screen as plain text (what an SVG screenshot shows, without styling)."""
        width, height = self.size
        console = Console(
            width=width,
            height=height,
            file=io.StringIO(),
            record=True,
            legacy_windows=False,
            color_system=None,
            force_terminal=True,
        )
        console.print(self.screen._compositor.render_update(full=True))
        return console.export_text(styles=False).rstrip() + "\n"

    def action_screenshot(self, filename: str | None = None, path: str | None = None) -> None:
        """Save the screen as SVG and as text (Downloads by default); copy the text.

        Extends Textual's screenshot (palette: "Screenshot", key: F12): the text version
        can go straight into an issue or to an agent.
        """
        folder = Path(path) if path else platformdirs.user_downloads_path()
        stem = Path(filename).stem if filename else f"acptui_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}"
        text = self.screen_text()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{stem}.svg").write_text(self.export_screenshot(), encoding="utf-8")
            (folder / f"{stem}.txt").write_text(text, encoding="utf-8")
        except OSError as exc:
            self.notify(
                f"Couldn't save the screenshot: {exc}", title="Screenshot", severity="error"
            )
            return
        self.copy_to_clipboard(text)
        self.notify(f"{folder / stem}.txt / .svg (text copied)", title="Screenshot")

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
            setup=self.setup_override or build_setup(self.settings, spec),
        )

    def on_agent_picker_picked(self, message: AgentPicker.Picked) -> None:
        self.spec = message.spec
        self.switch_screen(self._session_screen(message.spec))

    async def action_quit(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, SessionScreen):
                await screen.shutdown()
        self.exit()
