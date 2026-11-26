"""Session tabs: one line naming every open session, with what it is doing.

Each tab is a session screen in its own Textual mode (its own agent process and
dialogs); the app keeps them (see :class:`~acptui.ui.app.AcpTui`). The bar is shown
in every session screen (``tabs`` setting: always, when there are several, never).
"""

from dataclasses import dataclass
from typing import Literal

from textual.content import Content
from textual.events import Click  # noqa: TC002
from textual.message import Message
from textual.widgets import Static


TabState = Literal["starting", "idle", "working", "waiting", "disconnected"]

ICONS: dict[TabState, tuple[str, str]] = {
    "starting": ("◌", "$text-warning"),
    "idle": ("●", "$text-success"),
    "working": ("◐", "$text-accent"),
    "waiting": ("?", "bold $text-warning"),
    "disconnected": ("✘", "$text-error"),
}

MAX_LABEL = 28


@dataclass(frozen=True)
class TabInfo:
    """What a tab shows."""

    tab_id: str
    label: str
    state: TabState
    active: bool


def tab_content(index: int, tab: TabInfo) -> Content:
    """One tab: its number, state icon and label."""
    icon, style = ICONS[tab.state]
    label = tab.label if len(tab.label) <= MAX_LABEL else tab.label[: MAX_LABEL - 1] + "…"
    content = Content.assemble(f" {index} ", (icon, style), " ", label, " ")
    return content.stylize("reverse bold" if tab.active else "$text-muted")


class SessionTabs(Static):
    """The tab bar; a click on a tab switches to it."""

    DEFAULT_CSS = """
    SessionTabs {
        height: 1;
        background: $panel;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        display: none;
        &.-visible { display: block; }
    }
    """

    class Switch(Message):
        """The user clicked a tab."""

        def __init__(self, tab_id: str) -> None:
            super().__init__()
            self.tab_id = tab_id

    def __init__(self) -> None:
        super().__init__(id="session-tabs")
        self.tabs: list[TabInfo] = []
        self._spans: list[tuple[int, int, str]] = []
        """``(start, end, tab_id)`` cell ranges of the tabs, for clicks."""

    def show_tabs(self, tabs: list[TabInfo], *, visible: bool) -> None:
        """Show these tabs (or hide the bar)."""
        self.set_class(visible, "-visible")
        if tabs == self.tabs:
            return
        self.tabs = tabs
        parts: list[Content] = []
        self._spans = []
        offset = 0
        for index, tab in enumerate(tabs, start=1):
            part = tab_content(index, tab)
            self._spans.append((offset, offset + part.cell_length, tab.tab_id))
            offset += part.cell_length + 1
            parts.append(part)
        self.update(Content(" ").join(parts))

    def on_click(self, event: Click) -> None:
        for start, end, tab_id in self._spans:
            if start <= event.x < end:
                event.stop()
                self.post_message(self.Switch(tab_id))
                return
