"""Elicitation: structured input the agent asks for, as a form (promptantic) or a URL to visit."""

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar
import webbrowser

from aclientp.schema.v2 import (
    AcceptElicitationResponse,
    CancelElicitationResponse,
    DeclineElicitationResponse,
)
from aclientp.schema.v2.base import is_set
from promptantic.form import target_for
from promptantic.textual import FormView
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Static


if TYPE_CHECKING:
    from aclientp.schema.v2 import (
        CreateElicitationResponse,
        FormElicitationRequest,
        UrlElicitationRequest,
    )
    from pydantic import MISSING
    from textual.app import ComposeResult


FieldValue = str | int | float | bool | list[str]

DIALOG_CSS = """
    align: center middle;
    #dialog {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }
    #message { text-style: bold; margin: 0 0 1 0; }
    #fields { height: auto; max-height: 30; }
    .label { margin: 1 0 0 0; }
    .help { color: $text-muted; }
    #errors { color: $text-error; height: auto; }
    #buttons { height: auto; margin: 1 0 0 0; }
    #buttons Button { margin: 0 1 0 0; }
    SelectionList { height: auto; max-height: 8; }
"""


def _optional[T](value: T | MISSING | None) -> T | None:
    """A MISSING / None field value as ``None``."""
    return value if is_set(value) else None


def cancelled_elicitation() -> CreateElicitationResponse:
    """The response when the user (or the agent) cancelled."""
    return CancelElicitationResponse()


class FormElicitationScreen(ModalScreen["CreateElicitationResponse"]):
    """A form for the requested schema, built and validated by promptantic."""

    DEFAULT_CSS = "FormElicitationScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit", priority=True),
    ]

    def __init__(self, request: FormElicitationRequest) -> None:
        super().__init__()
        self.request = request
        self.schema: dict[str, Any] = request.requested_schema.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        self.form = FormView(target_for(self.schema))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Content(self.schema.get("title") or "The agent needs input"), id="message")
            yield Static(Content(self.request.message))
            if description := self.schema.get("description"):
                yield Static(Content(description), classes="help")
            yield self.form
            with Horizontal(id="buttons"):
                yield Button("Submit", variant="success", id="submit", compact=True)
                yield Button("Decline", variant="error", id="decline", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def action_submit(self) -> None:
        self.form.submit()

    def on_form_view_submitted(self, event: FormView.Submitted) -> None:
        event.stop()
        self.dismiss(AcceptElicitationResponse(content=event.result))

    def action_cancel(self) -> None:
        """Close without an answer."""
        self.dismiss(CancelElicitationResponse())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        match event.button.id:
            case "submit":
                self.action_submit()
            case "decline":
                self.dismiss(DeclineElicitationResponse())
            case _:
                self.action_cancel()

    def cancel(self) -> None:
        """Close because the agent cancelled the request."""
        if self.is_attached:
            self.dismiss(CancelElicitationResponse())


class UrlElicitationScreen(ModalScreen["CreateElicitationResponse"]):
    """Asks the user to visit a URL (e.g. to sign in); closes when the agent reports completion."""

    DEFAULT_CSS = "UrlElicitationScreen {" + DIALOG_CSS + " #url { color: $text-accent; } }"
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel"), Binding("o", "open", "Open URL")]

    def __init__(self, request: UrlElicitationRequest) -> None:
        super().__init__()
        self.request = request
        self.elicitation_id = request.elicitation_id

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("The agent asks you to open a URL", id="message")
            yield Static(Content(self.request.message))
            yield Static(Content(self.request.url), id="url")
            yield Static("Waiting for the agent to confirm… (o: open in browser)", classes="help")
            with Horizontal(id="buttons"):
                yield Button("Open", variant="primary", id="open", compact=True)
                yield Button("Done", variant="success", id="done", compact=True)
                yield Button("Decline", variant="error", id="decline", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    async def action_open(self) -> None:
        """Open the URL in a browser (in a thread: it may block)."""
        await asyncio.to_thread(webbrowser.open, self.request.url)

    def action_cancel(self) -> None:
        """Cancel the elicitation."""
        self.dismiss(CancelElicitationResponse())

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "open":
                await self.action_open()
            case "done":
                self.dismiss(AcceptElicitationResponse())
            case "decline":
                self.dismiss(DeclineElicitationResponse())
            case _:
                self.action_cancel()

    def complete(self) -> None:
        """The agent reported the elicitation complete."""
        if self.is_attached:
            self.dismiss(AcceptElicitationResponse())

    def cancel(self) -> None:
        """Close because the agent cancelled the request."""
        if self.is_attached:
            self.dismiss(CancelElicitationResponse())
