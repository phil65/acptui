"""The sidebar of a session (``ctrl+b``): the plan and the files, in two sections.

After toad's sidebar (``src/toad/widgets/side_bar.py``): collapsible sections, the plan
on top (as tall as it needs), the project tree below (the rest of the height), docked at
the left. The plan section follows the session's plans live (``plan_update`` and plan
removals); the files section is the tree of ``acptui.ui.widgets.files``.
"""

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Static

from acptui.ui.widgets.blocks import PlanView
from acptui.ui.widgets.files import FileTree


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from acptui.files import Files
    from acptui.protocol.state import SessionState


AUTO_OPEN_WIDTH = 120
"""Terminal width from which the sidebar opens by itself for a plan (``sidebar = "auto"``)."""


class PlanPanel(VerticalScroll):
    """Every plan of the session that is not removed, kept in sync with the state."""

    DEFAULT_CSS = """
    PlanPanel {
        height: auto;
        max-height: 50vh;
        scrollbar-size-vertical: 1;
        & > PlanView { border: round $secondary 40%; margin: 0; }
        & > #no-plan { color: $text-muted; text-style: italic; padding: 0 1; }
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("No plan yet", id="no-plan")

    @property
    def views(self) -> list[PlanView]:
        """The plans shown, in order."""
        return list(self.query_children(PlanView))

    def show(self, state: SessionState | None) -> None:
        """Show the plans of ``state`` (removed ones go, new ones are added at the end)."""
        plans = list(state.plans.values()) if state is not None else []
        views = {view.entry: view for view in self.views}
        for entry, view in views.items():
            if entry not in plans:
                view.remove()
        for entry in plans:
            if (view := views.get(entry)) is None:
                self.mount(PlanView(entry, title=""))
            else:
                view.refresh_entry()
        self.query_one("#no-plan", Static).display = not plans


class SideBar(Vertical):
    """Plan and files, docked at the left of the session screen (hidden until opened)."""

    DEFAULT_CSS = """
    SideBar {
        dock: left;
        width: 40;
        max-width: 45%;
        height: 1fr;
        border-right: tall $panel;
        display: none;
        &.-open { display: block; }
        Collapsible {
            padding: 0;
            border-top: none;
            background: transparent;
            &.-collapsed { height: auto; }
            & > Contents { padding: 0 0 0 1; }
        }
        #sidebar-plan { height: auto; }
        #sidebar-files { height: 1fr; min-height: 3; }
        #sidebar-files.-collapsed { height: auto; min-height: 1; }
        #sidebar-files > Contents { height: 1fr; }
        FileTree { height: 1fr; background: transparent; }
        #sidebar-hint { color: $text-muted; padding: 0 1; height: 1; }
    }
    """

    BINDINGS: ClassVar = [Binding("escape", "leave", "Prompt")]

    class Leave(Message):
        """Focus the prompt again (the sidebar stays open)."""

    def __init__(self, files: Files) -> None:
        super().__init__(id="sidebar")
        self.files = files

    def compose(self) -> ComposeResult:
        with Collapsible(title="Plan", collapsed=False, id="sidebar-plan"):
            yield PlanPanel()
        with Collapsible(title="Files", collapsed=False, id="sidebar-files"):
            yield FileTree(self.files)
        yield Static("a: mention · esc: prompt · ctrl+b: hide", id="sidebar-hint")

    @property
    def file_tree(self) -> FileTree:
        """The files section's tree."""
        return self.query_one(FileTree)

    @property
    def plans(self) -> PlanPanel:
        """The plan section."""
        return self.query_one(PlanPanel)

    @property
    def open(self) -> bool:
        """Whether the sidebar is shown."""
        return self.has_class("-open")

    def show(self, *, focus: bool = False) -> None:
        """Open the sidebar; ``focus``: move the focus into it (the files, else the plan)."""
        self.add_class("-open")
        if focus:
            files = self.query_one("#sidebar-files", Collapsible)
            if files.collapsed:
                self.query_one("#sidebar-plan CollapsibleTitle").focus()
            else:
                self.file_tree.focus()

    def hide(self) -> None:
        """Close the sidebar."""
        self.remove_class("-open")

    def action_leave(self) -> None:
        """Back to the prompt."""
        self.post_message(self.Leave())
