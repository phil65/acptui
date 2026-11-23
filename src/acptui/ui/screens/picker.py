"""The agent picker shown when acptui starts without an agent."""

from __future__ import annotations

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


def agent_option(spec: AgentSpec) -> Option:
    """The picker entry of an agent."""
    available = spec.available
    status = (
        Content("installed").stylize("$text-success")
        if available
        else Content("not found").stylize("$text-error")
    )
    first = Content.assemble((spec.name, "bold"), "  ", status)
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
        Binding("escape", "focus_list", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
    ]

    class Picked(Message):
        """An agent was chosen."""

        def __init__(self, spec: AgentSpec) -> None:
            super().__init__()
            self.spec = spec

    def __init__(self, agents: list[AgentSpec], cwd: str) -> None:
        super().__init__()
        self.agents = agents
        self.cwd = cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(
                Content.assemble("acptui · pick an agent for ", (self.cwd, "italic")), id="heading"
            )
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

    def on_mount(self) -> None:
        options = self.query_one("#agents", OptionList)
        options.focus()
        first_available = next((i for i, spec in enumerate(self.agents) if spec.available), 0)
        options.highlighted = first_available

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        spec = next((s for s in self.agents if s.id == event.option_id), None)
        if spec is not None:
            self.post_message(self.Picked(spec))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        spec = url_spec(value) if "://" in value else command_spec(value.split())
        self.post_message(self.Picked(spec))

    def action_custom(self) -> None:
        """Focus the command input."""
        self.query_one("#custom", Input).focus()

    def action_focus_list(self) -> None:
        """Focus the agent list."""
        self.query_one("#agents", OptionList).focus()
