"""Elicitation: structured input the agent asks for, as a form or a URL to visit."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar
import webbrowser

from aclientp.schema.v2 import (
    AcceptElicitationResponse,
    BooleanPropertySchema,
    CancelElicitationResponse,
    DeclineElicitationResponse,
    IntegerPropertySchema,
    MultiSelectPropertySchema,
    NumberPropertySchema,
    StringMultiSelectItems,
    StringPropertySchema,
    TitledMultiSelectItems,
)
from aclientp.schema.v2.base import is_set
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, SelectionList, Static, Switch


if TYPE_CHECKING:
    from aclientp.schema.v2 import (
        CreateElicitationResponse,
        ElicitationPropertySchema,
        FormElicitationRequest,
        UrlElicitationRequest,
    )
    from pydantic import MISSING
    from textual.app import ComposeResult
    from textual.widget import Widget


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
    """A form generated from the requested schema."""

    DEFAULT_CSS = "FormElicitationScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit"),
    ]

    def __init__(self, request: FormElicitationRequest) -> None:
        super().__init__()
        self.request = request
        self.schema = request.requested_schema
        required = self.schema.required
        self.required: set[str] = set(required) if is_set(required) and required else set()
        self.fields: dict[str, Widget] = {}

    def compose(self) -> ComposeResult:
        schema = self.schema
        with Vertical(id="dialog"):
            title = schema.title if is_set(schema.title) and schema.title else None
            yield Static(Content(title or "The agent needs input"), id="message")
            yield Static(Content(self.request.message))
            if is_set(schema.description) and schema.description:
                yield Static(Content(schema.description), classes="help")
            with VerticalScroll(id="fields"):
                for name, prop in schema.properties.items():
                    yield from self._field(name, prop)
            yield Static("", id="errors")
            with Horizontal(id="buttons"):
                yield Button("Submit", variant="success", id="submit", compact=True)
                yield Button("Decline", variant="error", id="decline", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def _field(self, name: str, prop: ElicitationPropertySchema) -> ComposeResult:
        title = getattr(prop, "title", None)
        label = title if isinstance(title, str) and title else name
        star = " *" if name in self.required else ""
        yield Static(Content.assemble((label, "bold"), star), classes="label")
        description = getattr(prop, "description", None)
        if isinstance(description, str) and description:
            yield Static(Content(description), classes="help")
        widget = self._widget(prop)
        self.fields[name] = widget
        yield widget

    def _widget(self, prop: ElicitationPropertySchema) -> Widget:  # noqa: PLR0911
        match prop:
            case StringPropertySchema():
                default = _optional(prop.default)
                one_of = _optional(prop.one_of)
                enum = _optional(prop.enum)
                if one_of:
                    options = [(option.title, option.const) for option in one_of]
                    return Select(
                        options, value=default if default is not None else Select.NULL, compact=True
                    )
                if enum:
                    return Select(
                        [(value, value) for value in enum],
                        value=default if default is not None else Select.NULL,
                        compact=True,
                    )
                fmt = _optional(prop.format)
                max_length = _optional(prop.max_length)
                return Input(
                    value=default or "",
                    placeholder=fmt or "",
                    max_length=max_length or 0,
                    compact=True,
                )
            case IntegerPropertySchema():
                default_int = _optional(prop.default)
                return Input(
                    value="" if default_int is None else str(default_int),
                    type="integer",
                    compact=True,
                )
            case NumberPropertySchema():
                default_num = _optional(prop.default)
                return Input(
                    value="" if default_num is None else str(default_num),
                    type="number",
                    compact=True,
                )
            case BooleanPropertySchema():
                return Switch(value=bool(_optional(prop.default)))
            case MultiSelectPropertySchema():
                chosen = set(_optional(prop.default) or ())
                items = prop.items
                if isinstance(items, TitledMultiSelectItems):
                    pairs = [(option.title, option.const) for option in items.any_of]
                elif isinstance(items, StringMultiSelectItems):
                    pairs = [(value, value) for value in items.enum]
                else:
                    pairs = []
                return SelectionList[str](
                    *((label, value, value in chosen) for label, value in pairs)
                )
            case _:
                return Input(placeholder=f"({prop.type})", compact=True)

    def on_mount(self) -> None:
        first = next(iter(self.fields.values()), None)
        if first is not None:
            first.focus()

    def _value(self, prop: ElicitationPropertySchema, widget: Widget) -> FieldValue | None:  # noqa: PLR0911
        match widget:
            case Switch():
                return widget.value
            case SelectionList():
                return [str(value) for value in widget.selected]
            case Select():
                value = widget.value
                return None if value is Select.NULL else str(value)
            case Input():
                text = widget.value.strip()
                if not text:
                    return None
                if isinstance(prop, IntegerPropertySchema):
                    return int(text)
                if isinstance(prop, NumberPropertySchema):
                    return float(text)
                return text
            case _:
                return None

    def _validate(  # noqa: PLR0911
        self, name: str, prop: ElicitationPropertySchema, value: FieldValue | None
    ) -> str | None:
        if value is None or value == []:
            return f"{name} is required" if name in self.required else None
        match prop:
            case StringPropertySchema() if isinstance(value, str):
                low, high = _optional(prop.min_length), _optional(prop.max_length)
                if low is not None and len(value) < low:
                    return f"{name}: at least {low} characters"
                if high is not None and len(value) > high:
                    return f"{name}: at most {high} characters"
            case IntegerPropertySchema() | NumberPropertySchema() if isinstance(value, int | float):
                low_n, high_n = _optional(prop.minimum), _optional(prop.maximum)
                if low_n is not None and value < low_n:
                    return f"{name}: at least {low_n}"
                if high_n is not None and value > high_n:
                    return f"{name}: at most {high_n}"
            case MultiSelectPropertySchema() if isinstance(value, list):
                low_i, high_i = _optional(prop.min_items), _optional(prop.max_items)
                if low_i is not None and len(value) < low_i:
                    return f"{name}: choose at least {low_i}"
                if high_i is not None and len(value) > high_i:
                    return f"{name}: choose at most {high_i}"
        return None

    def action_submit(self) -> None:
        """Validate and accept."""
        content: dict[str, FieldValue] = {}
        errors: list[str] = []
        for name, prop in self.schema.properties.items():
            try:
                value = self._value(prop, self.fields[name])
            except ValueError:
                errors.append(f"{name}: not a valid number")
                continue
            if (error := self._validate(name, prop, value)) is not None:
                errors.append(error)
            elif value is not None and value != []:
                content[name] = value
        if errors:
            self.query_one("#errors", Static).update(Content("\n".join(errors)))
            return
        self.dismiss(AcceptElicitationResponse(content=content))

    def action_cancel(self) -> None:
        """Cancel the elicitation."""
        self.dismiss(CancelElicitationResponse())

    def on_button_pressed(self, event: Button.Pressed) -> None:
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
