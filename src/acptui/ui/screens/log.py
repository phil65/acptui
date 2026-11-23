"""The agent log: stderr of the agent process and (optionally) the JSON-RPC traffic."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from acptui.ui.agent_log import AgentLog, LogLine


STYLES = {
    "stderr": "yellow",
    "incoming": "cyan",
    "outgoing": "green",
    "info": "dim",
    "error": "bold red",
}
PREFIX = {"stderr": "err", "incoming": "<--", "outgoing": "-->", "info": "   ", "error": "!!!"}


class LogScreen(Screen[None]):
    """Live view of the :class:`AgentLog`."""

    DEFAULT_CSS = """
    LogScreen {
        #log-title { height: 1; background: $panel; padding: 0 1; }
        RichLog { height: 1fr; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("escape,ctrl+l", "close", "Back"),
        Binding("t", "toggle_traffic", "Protocol traffic"),
    ]

    def __init__(self, log: AgentLog) -> None:
        super().__init__()
        self.agent_log = log
        self.show_traffic = False

    def compose(self) -> ComposeResult:
        yield Static("", id="log-title")
        yield RichLog(wrap=True, highlight=False, markup=False, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        self._reload()
        self.agent_log.subscribe(self._append)

    def on_unmount(self) -> None:
        self.agent_log.unsubscribe(self._append)

    def _reload(self) -> None:
        traffic = "shown" if self.show_traffic else "hidden (t)"
        self.query_one("#log-title", Static).update(
            f"Agent log · stderr · protocol traffic {traffic}"
        )
        view = self.query_one(RichLog)
        view.clear()
        for line in self.agent_log.lines:
            self._append(line)

    def _append(self, line: LogLine) -> None:
        if line.kind in {"incoming", "outgoing"} and not self.show_traffic:
            return
        text = Text.assemble(
            (line.time.strftime("%H:%M:%S "), "dim"),
            (PREFIX[line.kind] + " ", STYLES[line.kind]),
            (line.text, STYLES[line.kind] if line.kind != "stderr" else ""),
        )
        self.query_one(RichLog).write(text)

    def action_toggle_traffic(self) -> None:
        """Show or hide the JSON-RPC messages."""
        self.show_traffic = not self.show_traffic
        self._reload()

    def action_close(self) -> None:
        """Back to the session."""
        self.dismiss(None)
