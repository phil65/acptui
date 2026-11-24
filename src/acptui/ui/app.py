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
from textual.markup import escape

from acptui.config import command_spec, config_path, url_spec
from acptui.history import History
from acptui.notify import (
    desktop_notify,
    play_sound,
    terminal_notification,
    terminal_title,
    use_desktop,
)
from acptui.recent import RecentSessions
from acptui.setup import build_setup
from acptui.ui.agent_log import AgentLog
from acptui.ui.attention import SOUNDS, WAITING_MARK, Alert, Attention, TitleChanged
from acptui.ui.markdown import AgentMarkdown, link_is_web, summary_markdown
from acptui.ui.screens.dialogs import (
    ConfirmScreen,
    TabsScreen,
    _select_values,
    method_description,
)
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.screens.session import SessionScreen
from acptui.ui.screens.settings import SettingsChanged, SettingsScreen
from acptui.ui.widgets.image import prepare as prepare_images
from acptui.ui.widgets.tabs import SessionTabs, TabInfo


PICKER_MODE = "picker"
"""The Textual mode of the agent picker (each tab has a mode of its own)."""

SAVE_TITLE = "\x1b[22;0t"
"""XTWINOPS: push the terminal title (restored on quit)."""
RESTORE_TITLE = "\x1b[23;0t"


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from textual.screen import Screen
    from textual.widgets import Markdown

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
    app = screen.app
    if isinstance(app, AcpTui):
        yield "New tab", "Start another agent session in a new tab (ctrl+t)", app.action_new_tab
        yield (
            "Close tab",
            "Stop this tab's agent and close it (alt+w)",
            partial(app.call_later, app.action_close_tab),
        )
        yield "Sessions", "All tabs and what they are doing (ctrl+s)", app.action_sessions
    yield "Export conversation", "Save it as Markdown and copy it", screen.action_export
    yield "Clear the view", "The session keeps its history", screen.conversation.clear_view
    yield (
        "About acptui",
        "Versions, paths, terminal and agent",
        partial(screen.client_command, "about"),
    )
    yield "Session options", "Mode, model and other options", screen.action_options
    yield "Cancel turn", "Cancel the running turn", screen.action_cancel_turn
    yield "LLM providers", "Configure the agent's LLM providers", screen.action_providers
    yield "Toggle diff view", "Unified / split / auto", screen.action_toggle_diff
    yield "Agent log", "stderr and protocol traffic", screen.action_show_log
    yield "Restart agent", "Restart the agent process", screen.action_restart
    yield "Help", "Keys and tips", screen.action_help
    yield (
        "Settings",
        "Theme, diffs, tabs, notifications, ... (f3)",
        partial(screen.app.run_action, "settings"),
    )
    state = screen.state
    if state is not None:
        for option in state.config_options:
            if isinstance(option, SelectSessionConfigOption):
                for value, name, _ in _select_values(option):
                    callback = partial(screen.call, screen.set_config, option.config_id, value)
                    help_text = escape(f"Set {option.name.lower()} to {name}")
                    yield f"{option.name}: {name}", help_text, callback
            elif isinstance(option, BooleanSessionConfigOption):
                new = not option.current_value
                callback = partial(screen.call, screen.set_config, option.config_id, new)
                yield (
                    f"Toggle {option.name}",
                    escape(f"Turn {option.name.lower()} {'on' if new else 'off'}"),
                    callback,
                )
    connection = screen.connection
    if connection is None:
        return
    if connection.auth_methods:
        yield "Log in…", "Pick one of the agent's login methods", screen.action_login
    for method in connection.auth_methods:
        if isinstance(method, AuthMethodAgent | AuthMethodTerminal):
            kind = "in a terminal" if isinstance(method, AuthMethodTerminal) else "via the agent"
            description = method_description(method)  # Markdown (ACP v2)
            yield (
                f"Log in: {method.name}",
                summary_markdown(description).markup if description else f"Authenticate {kind}",
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
        Binding("ctrl+t", "new_tab", "New tab", show=False),
        Binding("alt+w", "close_tab", "Close tab", show=False),
        Binding("alt+right", "next_tab(1)", "Next tab", show=False),
        Binding("alt+left", "next_tab(-1)", "Previous tab", show=False),
        Binding("ctrl+s", "sessions", "Tabs", show=False),
        Binding("f3", "settings", "Settings", show=False),
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
        config_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.config_file = config_file or config_path()
        """Where the settings screen saves changes."""
        self.setup_override = setup
        self.settings = settings
        self.cwd = cwd
        self.spec = spec
        self.resume_id = resume_id
        self._fallback_log = AgentLog()
        self.tabs: dict[str, SessionScreen] = {}
        """Open tabs: mode name -> session screen, in tab order."""
        self._tab_counter = 0
        prepare_images(settings.images)
        self.alerts: list[Alert] = []
        """Alerts raised so far (see :meth:`on_attention`)."""
        self._terminal_title = ""

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

    # --- Attention: bell, sounds, notifications, terminal title -----------------------

    def on_attention(self, message: Attention) -> None:
        """A session wants attention: bell for questions, and outside the app if away."""
        message.stop()
        settings = self.settings.notifications
        if message.kind == "question":
            self.bell()
        away = not self.app_focus or not self.is_shown(message.source)
        outside = settings.when == "always" or (settings.when == "unfocused" and away)
        if message.kind == "turn_end" and not settings.turn_end:
            outside = False
        self.alerts.append(Alert(message.kind, message.title, message.message, outside))
        self.refresh_title()
        if not outside or self.is_headless:
            return
        sound = SOUNDS[message.kind] if settings.sounds else None
        desktop = use_desktop(settings.method)
        if not desktop:
            self._write_terminal(terminal_notification(message.title, message.message))
        title, text = message.title, message.message

        def notify_outside() -> None:
            if desktop:
                desktop_notify(title, text)
            if sound is not None:
                play_sound(sound)

        self.run_worker(notify_outside, thread=True, group="alerts", exit_on_error=False)

    def is_shown(self, screen: Screen[object]) -> bool:
        """Whether ``screen`` is what the user looks at (on top, or under a dialog)."""
        return screen in self.screen_stack

    def on_title_changed(self, message: TitleChanged) -> None:
        message.stop()
        self.refresh_title()

    def refresh_title(self) -> None:
        """Show the current session in the terminal title; mark it while an agent waits."""
        if not self.settings.notifications.title:
            return
        screen = next(
            (s for s in reversed(self.screen_stack) if isinstance(s, SessionScreen)), None
        )
        parts = ["acptui"]
        if screen is not None:
            parts.append(screen.spec.name)
            if screen.state is not None and screen.state.title:
                parts.append(screen.state.title)
        waiting = any(s.waiting for s in self.session_screens())
        title = (WAITING_MARK if waiting else "") + " · ".join(parts)
        if title != self._terminal_title:
            self._terminal_title = title
            self._write_terminal(terminal_title(title))

    def session_screens(self) -> list[SessionScreen]:
        """The open session screens (one per tab)."""
        return list(self.tabs.values())

    def _write_terminal(self, text: str) -> None:
        # Like App.bell: escape sequences go straight to the terminal.
        if not self.is_headless and self._driver is not None:
            self._driver.write(text)

    def on_mount(self) -> None:
        if self.settings.notifications.title:
            self._write_terminal(SAVE_TITLE)
        if self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        # The picker has a mode of its own: "new tab" and closing the last tab go there.
        picker = AgentPicker(self.settings.all_agents(), str(self.cwd))
        self.add_mode(PICKER_MODE, lambda: picker)
        if self.spec is None:
            self.switch_mode(PICKER_MODE)
        if self.spec is not None:
            self.open_tab(
                self.spec, resume_id=self.resume_id, setup=self.setup_override, focus=True
            )
        self.set_interval(0.5, self.refresh_tabs, name="tabs")

    # --- Tabs: one session screen (and agent) per Textual mode -----------------------

    @property
    def agent_log(self) -> AgentLog:
        """The agent log of the session shown (or of the last one)."""
        screen = self.current_tab or next(reversed(self.tabs.values()), None)
        return screen.agent_log if screen is not None else self._fallback_log

    @property
    def current_tab(self) -> SessionScreen | None:
        """The session screen of the tab shown (``None`` on the picker)."""
        return self.tabs.get(self.current_mode)

    def open_tab(
        self,
        spec: AgentSpec,
        cwd: Path | None = None,
        *,
        resume_id: str | None = None,
        setup: SessionSetup | None = None,
        focus: bool = True,
    ) -> SessionScreen:
        """Start a session with ``spec`` in a new tab (and show it)."""
        self._tab_counter += 1
        tab_id = f"session-{self._tab_counter}"
        screen = SessionScreen(
            spec,
            cwd or self.cwd,
            AgentLog(),
            diff_mode=self.settings.diff_view,
            expand_thoughts=self.settings.expand_thoughts,
            tool_expand=self.settings.tool_expand,
            resume_id=resume_id,
            setup=setup or build_setup(self.settings, spec),
            mounts=self.settings.mounts,
            history=History() if self.settings.history else None,
            recent=RecentSessions() if self.settings.history else None,
        )
        screen.tab_id = tab_id
        self.tabs[tab_id] = screen
        self.add_mode(tab_id, lambda: screen)
        if focus:
            self.switch_mode(tab_id)
        self.refresh_tabs()
        return screen

    async def close_tab(self, tab_id: str | None = None) -> None:
        """Close a tab (the shown one by default): stop its agent, show a neighbor."""
        tab_id = tab_id or self.current_mode
        screen = self.tabs.get(tab_id)
        if screen is None:
            return
        await screen.shutdown()
        ids = list(self.tabs)
        index = ids.index(tab_id)
        del self.tabs[tab_id]
        if self.current_mode == tab_id:
            rest = list(self.tabs)
            target = rest[min(index, len(rest) - 1)] if rest else PICKER_MODE
            await self.switch_mode(target)
        await self.remove_mode(tab_id)
        self.refresh_tabs()
        self.refresh_title()

    def switch_tab(self, tab_id: str) -> None:
        """Show a tab."""
        if tab_id in self.tabs and tab_id != self.current_mode:
            self.switch_mode(tab_id)
            self.refresh_tabs()
            self.refresh_title()

    def action_new_tab(self) -> None:
        """Pick an agent for a new tab (the agent picker; escape goes back)."""
        self.switch_mode(PICKER_MODE)
        self.refresh_title()

    async def action_close_tab(self) -> None:
        """Close the shown tab."""
        await self.close_tab()

    def action_next_tab(self, step: int) -> None:
        """Show the next (``1``) or previous (``-1``) tab."""
        ids = list(self.tabs)
        if not ids:
            return
        index = ids.index(self.current_mode) if self.current_mode in ids else -1
        self.switch_tab(ids[(index + step) % len(ids)])

    def action_sessions(self) -> None:
        """All tabs and what they are doing; enter shows one."""

        def chosen(tab_id: str | None) -> None:
            if tab_id is not None:
                self.switch_tab(tab_id)

        self.push_screen(TabsScreen(self.tab_infos(), self._tab_details()), callback=chosen)

    def tab_infos(self) -> list[TabInfo]:
        """What the tab bars show."""
        return [
            TabInfo(tab_id, screen.tab_label, screen.tab_state, tab_id == self.current_mode)
            for tab_id, screen in self.tabs.items()
        ]

    def _tab_details(self) -> dict[str, str]:
        return {tab_id: str(screen.cwd) for tab_id, screen in self.tabs.items()}

    def refresh_tabs(self) -> None:
        """Update the tab bars (labels, states, which one is shown)."""
        infos = self.tab_infos()
        mode = self.settings.tabs
        visible = mode == "always" or (mode == "multiple" and len(infos) > 1)
        for screen in self.tabs.values():
            if screen.is_mounted:
                screen.show_tabs(infos, visible=visible)

    def on_session_tabs_switch(self, message: SessionTabs.Switch) -> None:
        self.switch_tab(message.tab_id)

    def on_agent_picker_resume(self, message: AgentPicker.Resume) -> None:
        session = message.session
        spec = self.settings.find_agent(session.agent_id)
        if spec is None and session.url is not None:
            spec = url_spec(session.url)
        elif spec is None and session.command:
            spec = command_spec(session.command)
        if spec is None:
            self.notify(f"Unknown agent {session.agent_id!r}", severity="error")
            return
        cwd = Path(session.cwd)
        if not cwd.is_dir():
            self.notify(f"The directory is gone: {cwd}", severity="error")
            return
        self.open_tab(spec, cwd, resume_id=session.session_id)

    def on_agent_picker_back(self) -> None:
        if self.tabs:
            self.switch_tab(list(self.tabs)[-1])

    def on_agent_picker_picked(self, message: AgentPicker.Picked) -> None:
        self.spec = message.spec
        self.open_tab(message.spec, Path(message.cwd) if message.cwd else None)

    # --- Settings ---------------------------------------------------------------------

    def action_settings(self) -> None:
        """Change settings (saved to config.toml)."""
        self.push_screen(
            SettingsScreen(self.settings, self.config_file, sorted(self.available_themes))
        )

    def on_settings_changed(self, message: SettingsChanged) -> None:
        """Apply a changed setting where it can be applied right away."""
        self.settings = message.settings
        match message.key:
            case "theme" if self.settings.theme in self.available_themes:
                self.theme = self.settings.theme
            case "diff_view":
                for screen in self.tabs.values():
                    screen.diff_mode = self.settings.diff_view
                    if screen.is_mounted:
                        screen.conversation.set_diff_mode(self.settings.diff_view)
            case "expand_thoughts":
                for screen in self.tabs.values():
                    screen.expand_thoughts = self.settings.expand_thoughts
            case "tabs":
                self.refresh_tabs()
            case "notifications.title":
                self._terminal_title = ""
                self.refresh_title()
            case _:
                pass

    def on_text_selected(self) -> None:
        """Copy selected text right away (``auto_copy``)."""
        if self.settings.auto_copy and (text := self.screen.get_selected_text()):
            self.copy_to_clipboard(text)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Open a link the user clicked in agent Markdown.

        ``http(s)`` links open in the browser (the toast shows the full target); anything
        else is shown and needs a confirmation first (ACP v2 security guidance).
        """
        if not isinstance(event.markdown, AgentMarkdown):
            return
        href = event.href
        if link_is_web(href):
            self.open_url(href)
            self.notify(href, title="Opening link", markup=False)
            return

        def answered(yes: bool | None) -> None:
            if yes:
                self.open_url(href)

        self.push_screen(ConfirmScreen(f"Open this link?\n\n{href}"), callback=answered)

    async def action_quit(self) -> None:
        for screen in list(self.tabs.values()):
            await screen.shutdown()
        if self.settings.notifications.title:
            self._write_terminal(RESTORE_TITLE)
        self.exit()
