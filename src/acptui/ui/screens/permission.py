"""The permission dialog: the agent's question, what it wants to do, and its options."""

from typing import TYPE_CHECKING, ClassVar

from aclientp.schema.v2 import (
    CancelledPermissionOutcome,
    CommandPermissionSubject,
    Diff,
    RequestPermissionResponse,
    SelectedPermissionOutcome,
    ToolCallPermissionSubject,
)
from aclientp.schema.v2.base import is_set
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from acptui.protocol.content import relative_display
from acptui.ui.markdown import AgentMarkdown
from acptui.ui.render import tool_icon
from acptui.ui.widgets.diff import DiffView


if TYPE_CHECKING:
    from aclientp.schema.v2 import PermissionOption, RequestPermissionRequest
    from textual.app import ComposeResult
    from textual.widgets.button import ButtonVariant

    from acptui.config import DiffViewMode


VARIANTS: dict[str, ButtonVariant] = {
    "allow_once": "success",
    "allow_always": "primary",
    "reject_once": "error",
    "reject_always": "error",
}


def cancelled() -> RequestPermissionResponse:
    """The response for a request nobody answered (e.g. the turn was cancelled)."""
    return RequestPermissionResponse(outcome=CancelledPermissionOutcome())


def selected(option_id: str) -> RequestPermissionResponse:
    """The response selecting an option."""
    return RequestPermissionResponse(outcome=SelectedPermissionOutcome(option_id=option_id))


class OptionButton(Button):
    """A button for one permission option."""

    def __init__(self, label: str, option_id: str, variant: ButtonVariant) -> None:
        super().__init__(label, variant=variant, compact=True)
        self.option_id = option_id


class PermissionScreen(ModalScreen[RequestPermissionResponse]):
    """Asks the user to pick one of the agent's permission options.

    Number keys pick an option; ``y`` picks the first "allow", ``n`` the first
    "reject" option; ``escape`` rejects once (or cancels if there is no such option).
    """

    DEFAULT_CSS = """
    PermissionScreen {
        align: center middle;
        #dialog {
            width: 90%;
            max-width: 140;
            height: auto;
            max-height: 90%;
            border: thick $warning;
            background: $surface;
            padding: 0 1;
        }
        #title { text-style: bold; color: $text-warning; margin: 0 0 1 0; }
        #description {
            height: auto; max-height: 12; color: $text-muted; margin: 0 0 1 0;
            padding: 0; background: transparent;
        }
        #details { height: auto; max-height: 24; margin: 0 0 1 0; }
        #details > Static { height: auto; }
        #options { height: auto; }
        #options Button { margin: 0 1 0 0; min-width: 12; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("escape", "reject", "Reject", show=True),
        Binding("y", "pick_kind('allow')", "Allow", show=True),
        Binding("n", "pick_kind('reject')", "Reject", show=False),
        *(Binding(str(n), f"pick_index({n - 1})", show=False) for n in range(1, 10)),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, request: RequestPermissionRequest, cwd: str, diff_mode: DiffViewMode
    ) -> None:
        super().__init__()
        self.request = request
        self.cwd = cwd
        self.diff_mode: DiffViewMode = diff_mode

    def compose(self) -> ComposeResult:
        request = self.request
        with Vertical(id="dialog"):
            yield Static(Content(f"⚠ {request.title}"), id="title")
            if is_set(request.description) and request.description:
                yield AgentMarkdown(request.description, id="description")  # Markdown (ACP v2)
            with VerticalScroll(id="details"):
                yield from self._details()
            with Horizontal(id="options"):
                for index, option in enumerate(request.options, start=1):
                    yield self._button(index, option)

    def _button(self, index: int, option: PermissionOption) -> Button:
        variant = VARIANTS.get(option.kind, "default")
        return OptionButton(f"{index} {option.name}", option.option_id, variant=variant)

    def _details(self) -> ComposeResult:
        subject = self.request.subject
        match subject:
            case ToolCallPermissionSubject(tool_call=tool_call):
                title = (
                    tool_call.title
                    if is_set(tool_call.title) and tool_call.title
                    else tool_call.tool_call_id
                )
                kind = tool_call.kind if is_set(tool_call.kind) else None
                yield Static(
                    Content.assemble((tool_icon(kind) + " ", "bold $secondary"), (title, "bold"))
                )
                if is_set(tool_call.locations) and tool_call.locations:
                    paths = "\n".join(
                        f"↳ {relative_display(loc.path, self.cwd)}" for loc in tool_call.locations
                    )
                    yield Static(Content(paths).stylize("$text-muted"))
                if is_set(tool_call.content) and tool_call.content:
                    for item in tool_call.content:
                        if isinstance(item, Diff):
                            yield DiffView(item, self.cwd, self.diff_mode)
            case CommandPermissionSubject(command=command, cwd=cwd):
                yield Static(Content.assemble(("$ ", "bold $secondary"), (command, "bold")))
                yield Static(Content(f"in {cwd}").stylize("$text-muted"))
            case _:
                return

    def on_mount(self) -> None:
        buttons = self.query(Button)
        if buttons:
            buttons.first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, OptionButton):
            self.dismiss(selected(event.button.option_id))

    def action_pick_index(self, index: int) -> None:
        """Pick the option with this index."""
        if 0 <= index < len(self.request.options):
            self.dismiss(selected(self.request.options[index].option_id))

    def action_pick_kind(self, prefix: str) -> None:
        """Pick the first option whose kind starts with ``prefix`` (``allow`` / ``reject``)."""
        option = next((o for o in self.request.options if o.kind.startswith(prefix)), None)
        if option is not None:
            self.dismiss(selected(option.option_id))

    def action_reject(self) -> None:
        """Reject once if possible, else answer as cancelled."""
        option = next((o for o in self.request.options if o.kind == "reject_once"), None)
        self.dismiss(selected(option.option_id) if option is not None else cancelled())

    def cancel(self) -> None:
        """Close the dialog without a choice (the turn was cancelled)."""
        if self.is_attached:
            self.dismiss(cancelled())
