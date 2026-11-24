"""The agent picker shown when acptui starts without an agent.

Pinned agents (``pinned_agents`` in the config; ``p`` here or in the details) come first,
numbered: ``1``-``9`` start one at once (after toad's launcher, "show in launcher").
"""

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
from acptui.ui.render import pill


if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.app import ComposeResult

    from acptui.config import AgentSpec
    from acptui.recent import RecentSession


def with_probes(agents: list[AgentSpec]) -> list[AgentSpec]:
    """The agents, with ``acp_v2`` from the user's "Test v2" results where there is one."""
    from acptui.probe import ProbeResults

    results = ProbeResults().load()
    tested: list[AgentSpec] = []
    for spec in agents:
        result = results.get(spec.id)
        if result is not None and result.version and result.v2 != spec.acp_v2:
            spec = spec.model_copy(update={"acp_v2": result.v2})
        tested.append(spec)
    return tested


QUICK_KEYS = "123456789"
"""Keys that start the pinned agents, in order."""


class PinToggled(Message):
    """Pin an agent to the top of the picker, or unpin it (the app saves it)."""

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id


def agent_option(spec: AgentSpec, key: str | None = None) -> Option:
    """The picker entry of an agent; ``key``: it is pinned (``""``: beyond the keys)."""
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
    if key is not None:
        mark = pill(key, "$accent-muted", "$text-accent") if key else Content("📌")
        first = Content.assemble(mark, " ", first)
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
        Binding("p", "pin", "Pin / unpin"),
        Binding("1", "quick_launch(0)", "Start pinned (1-9)"),
        *(
            Binding(key, f"quick_launch({index})", show=False)
            for index, key in enumerate(QUICK_KEYS)
            if index
        ),
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

    def __init__(self, agents: list[AgentSpec], cwd: str, *, pinned: Sequence[str] = ()) -> None:
        super().__init__()
        self.agents = with_probes(agents)
        self.cwd = cwd
        self.pinned: list[str] = []
        """Ids of the pinned agents that exist, in order."""
        self._set_pinned_ids(pinned)

    def _set_pinned_ids(self, pinned: Sequence[str]) -> None:
        known = {spec.id for spec in self.agents}
        self.pinned = [agent_id for agent_id in dict.fromkeys(pinned) if agent_id in known]

    def _options(self) -> list[Option | None]:
        """Pinned agents first (numbered), a separator, then the others."""
        by_id = {spec.id: spec for spec in self.agents}
        options: list[Option | None] = [
            agent_option(by_id[agent_id], QUICK_KEYS[i] if i < len(QUICK_KEYS) else "")
            for i, agent_id in enumerate(self.pinned)
        ]
        if options:
            options.append(None)
        pinned = set(self.pinned)
        options += [agent_option(spec) for spec in self.agents if spec.id not in pinned]
        return options

    def set_pinned(self, pinned: Sequence[str]) -> None:
        """Show a new list of pinned agents (the highlighted agent stays highlighted)."""
        self._set_pinned_ids(pinned)
        if self.is_mounted:
            self._refill()

    def _highlighted_id(self) -> str | None:
        options = self.query_one("#agents", OptionList)
        index = options.highlighted
        return options.get_option_at_index(index).id if index is not None else None

    def _refill(self) -> None:
        options = self.query_one("#agents", OptionList)
        current = self._highlighted_id()
        options.clear_options()
        options.add_options(self._options())
        self._highlight(current)

    def _highlight(self, agent_id: str | None) -> None:
        options = self.query_one("#agents", OptionList)
        for index in range(options.option_count):
            if options.get_option_at_index(index).id == agent_id:
                options.highlighted = index
                return

    def action_pin(self) -> None:
        """Pin the highlighted agent to the top (or unpin it)."""
        if (agent_id := self._highlighted_id()) is not None:
            self.post_message(PinToggled(agent_id))

    def action_quick_launch(self, index: int) -> None:
        """Start the pinned agent with this number (``1`` is 0)."""
        if index >= len(self.pinned):
            return
        spec = next(spec for spec in self.agents if spec.id == self.pinned[index])
        self.post_message(self.Picked(spec, self.cwd))

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self._heading(), id="heading")
            yield OptionList(*self._options(), id="agents")
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
        first = self.pinned[0] if self.pinned else None
        first = first or next((spec.id for spec in self.agents if spec.available), None)
        self._highlight(first or (self.agents[0].id if self.agents else None))

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

        option_id = self._highlighted_id()
        spec = next((s for s in self.agents if s.id == option_id), None)
        if spec is None:
            return

        def chosen(action: str | None) -> None:
            if action == "start":
                self.post_message(self.Picked(spec, self.cwd))
            elif action in {"installed", "tested"}:
                self.refresh_agents()

        from acptui.probe import ProbeResults

        known = ProbeResults().load().get(spec.id)
        screen = AgentDetailsScreen(spec, Path(self.cwd), known, pinned=spec.id in self.pinned)
        self.app.push_screen(screen, callback=chosen)

    def refresh_agents(self) -> None:
        """Re-check which agents are installed and what tests found."""
        self.agents = with_probes(self.agents)
        self._refill()

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
