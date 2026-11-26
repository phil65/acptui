"""The status line: agent, session, activity, mode / model and context usage."""

from typing import TYPE_CHECKING

from aclientp.schema.v2.base import is_set
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Static

from acptui.ui.render import human_tokens


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from acptui.protocol.state import SessionState


CONTEXT_WARNING = 0.7
CONTEXT_CRITICAL = 0.9
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusBar(Horizontal):
    """One line above the footer."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $panel;
        padding: 0 1;
        #status-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
        #status-right { width: auto; text-wrap: nowrap; }
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.agent = ""
        self.connection_state = "starting"
        self.state: SessionState | None = None
        self.account = ""
        """The agent's active account (from ``auth/status``), if known."""
        self._frame = 0

    def compose(self) -> ComposeResult:
        yield Static(id="status-left")
        yield Static(id="status-right")

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self.refresh_status()

    def _tick(self) -> None:
        if self.state is not None and self.state.running:
            self._frame = (self._frame + 1) % len(SPINNER)
            self.refresh_status()

    def refresh_status(self) -> None:
        """Redraw from the current state."""
        left: list[Content | tuple[str, str] | str] = []
        state = self.state
        match self.connection_state:
            case "connected" if state is not None and state.running:
                label = "waiting for you" if state.activity == "requires_action" else "working"
                left.append((f"{SPINNER[self._frame]} {label}  ", "$text-accent"))
            case "connected":
                left.append(("● ", "$text-success"))
            case "starting":
                left.append(("◌ starting  ", "$text-warning"))
            case _:
                left.append(("✘ disconnected  ", "$text-error"))
        left.append((self.agent, "bold"))
        if state is not None and state.title:
            left.append(("  " + state.title, "$text-muted"))
        self.query_one("#status-left", Static).update(Content.assemble(*left))
        self.query_one("#status-right", Static).update(self._right())

    def _right(self) -> Content:
        state = self.state
        if state is None:
            return Content("")
        from acptui.ui.screens.dialogs import option_value_label

        parts: list[Content] = []
        if self.account:
            parts.append(Content(self.account).stylize("$text-muted"))
        if state.background:
            parts.append(Content(f"⚙ {len(state.background)} bg").stylize("$text-accent"))
        for option in state.config_options:
            category = option.category if is_set(option.category) else None
            if category in {"mode", "model"}:
                parts.append(
                    Content.assemble(
                        (f"{option.name}: ", "$text-muted"), option_value_label(option)
                    )
                )
        if (usage := state.usage) is not None and usage.size:
            used = f"{human_tokens(usage.used)}/{human_tokens(usage.size)}"
            style = (
                "$text-error"
                if usage.fraction > CONTEXT_CRITICAL
                else "$text-warning"
                if usage.fraction > CONTEXT_WARNING
                else ""
            )
            parts.append(Content.assemble(("ctx ", "$text-muted"), (used, style)))
            if usage.cost is not None:
                parts.append(Content(f"{usage.cost.amount:.2f} {usage.cost.currency}"))
        if (turn := state.last_turn_usage) is not None:
            parts.append(
                Content(f"turn {human_tokens(turn.total_tokens)} tok").stylize("$text-muted")
            )
        return Content("  ").join(parts)
