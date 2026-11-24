"""The agent picker shown when acptui starts without an agent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from acptui.config import command_spec, url_spec


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from acptui.config import AgentSpec
    from acptui.recent import RecentSession


def agent_option(spec: AgentSpec) -> Option:
    """The picker entry of an agent."""
    available = spec.available
    status = (
        Content("installed").stylize("$text-success")
        if available
        else Content("not found").stylize("$text-error")
    )
    v2 = {
        True: Content(" ACP v2").stylize("$text-success"),
        False: Content(" no ACP v2 yet").stylize("$text-warning"),
        None: Content(" ACP v2?").stylize("$text-muted"),
    }[spec.acp_v2]
    first = Content.assemble((spec.name, "bold"), "  ", status, " ·", v2)
    detail = spec.description or spec.target
    second = Content.assemble(
        ("  " + spec.target, "$text-muted"),
        ("  ·  " + detail, "$text-muted") if spec.description else "",
    )
    if not available and spec.install:
        second = Content.assemble(second, ("\n  install: " + spec.install, "$text-muted italic"))
    return Option(Content.assemble(first, "\n", second), id=spec.id)


class AgentPicker(Screen[None]):
    """Choose an agent from the registry, or type a command / URL."""

    DEFAULT_CSS = """
    AgentPicker {
        align: center top;
        #picker {
            width: 100%;
            max-width: 110;
            height: 1fr;
            padding: 1 2 0 2;
        }
        #heading { text-style: bold; color: $accent; margin: 0 0 1 0; }
        #agents { height: 1fr; border: round $primary 50%; }
        #agents:focus { border: round $primary; }
        #custom { margin: 1 0 0 0; }
        #hint { color: $text-muted; margin: 1 0 0 0; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("c", "custom", "Command / URL"),
        Binding("d", "directory", "Directory"),
        Binding("i", "details", "Details"),
        Binding("r", "recent", "Recent sessions"),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
    ]

    class Picked(Message):
        """An agent was chosen."""

        def __init__(self, spec: AgentSpec, cwd: str) -> None:
            super().__init__()
            self.spec = spec
            self.cwd = cwd
            """The project directory for the session."""

    def __init__(self, agents: list[AgentSpec], cwd: str) -> None:
        super().__init__()
        self.agents = agents
        self.cwd = cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self._heading(), id="heading")
            yield OptionList(*(agent_option(spec) for spec in self.agents), id="agents")
            yield Input(
                placeholder="…or an agent command (e.g. my-agent --acp) or URL, then enter",
                id="custom",
            )
            yield Static(
                "acptui speaks ACP v2 only: agents that negotiate another version get an error.",
                id="hint",
            )
        yield Footer()

    def _heading(self) -> Content:
        home = str(Path.home())
        cwd = "~" + self.cwd[len(home) :] if self.cwd.startswith(home) else self.cwd
        return Content.assemble(
            "acptui · pick an agent for ", (cwd, "italic"), ("  (d: change)", "$text-muted")
        )

    def on_mount(self) -> None:
        options = self.query_one("#agents", OptionList)
        options.focus()
        first_available = next((i for i, spec in enumerate(self.agents) if spec.available), 0)
        options.highlighted = first_available

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        spec = next((s for s in self.agents if s.id == event.option_id), None)
        if spec is not None:
            self.post_message(self.Picked(spec, self.cwd))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        spec = url_spec(value) if "://" in value else command_spec(value.split())
        self.post_message(self.Picked(spec, self.cwd))

    def action_custom(self) -> None:
        """Focus the command input."""
        self.query_one("#custom", Input).focus()

    def action_focus_list(self) -> None:
        """Focus the agent list."""
        self.query_one("#agents", OptionList).focus()

    def action_back(self) -> None:
        """Back to the agent list, or (from a new tab) back to the last tab."""
        options = self.query_one("#agents", OptionList)
        if not options.has_focus:
            options.focus()
            return
        self.post_message(self.Back())  # the app shows the last tab, if there is one

    class Resume(Message):
        """Resume a recent session."""

        def __init__(self, session: RecentSession) -> None:
            super().__init__()
            self.session = session

    def action_recent(self) -> None:
        """Pick a recent session (any agent, any directory) to resume."""
        from acptui.recent import RecentSessions
        from acptui.ui.screens.dialogs import RecentSessionsScreen

        def chosen(session: RecentSession | None) -> None:
            if session is not None:
                self.post_message(self.Resume(session))

        self.app.push_screen(RecentSessionsScreen(RecentSessions().load()), callback=chosen)

    class Back(Message):
        """Leave the picker for the last tab."""

    def action_details(self) -> None:
        """Details of the highlighted agent: what it is, how to install and log in."""
        from acptui.ui.screens.agent import AgentDetailsScreen

        options = self.query_one("#agents", OptionList)
        index = options.highlighted
        if index is None:
            return
        option_id = options.get_option_at_index(index).id
        spec = next((s for s in self.agents if s.id == option_id), None)
        if spec is None:
            return

        def chosen(action: str | None) -> None:
            if action == "start":
                self.post_message(self.Picked(spec, self.cwd))
            elif action == "installed":
                self.refresh_agents()

        self.app.push_screen(AgentDetailsScreen(spec), callback=chosen)

    def refresh_agents(self) -> None:
        """Re-check which agents are installed."""
        options = self.query_one("#agents", OptionList)
        highlighted = options.highlighted
        options.clear_options()
        options.add_options(agent_option(spec) for spec in self.agents)
        options.highlighted = highlighted

    def action_directory(self) -> None:
        """Change the project directory of the next session."""
        from acptui.ui.screens.dialogs import TextInputScreen

        def chosen(value: str | None) -> None:
            if not value:
                return
            path = Path(value).expanduser()
            path = path if path.is_absolute() else Path(self.cwd) / path
            if not path.is_dir():
                self.notify(f"Not a directory: {path}", severity="error")
                return
            self.cwd = str(path.resolve())
            self.query_one("#heading", Static).update(self._heading())

        self.app.push_screen(TextInputScreen("Project directory", self.cwd), callback=chosen)
