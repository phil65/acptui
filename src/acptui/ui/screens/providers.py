"""LLM provider configuration (``providers/list``, ``providers/set``, ``providers/disable``)."""

from typing import TYPE_CHECKING, ClassVar

from aclientp.exceptions import RequestError
from aclientp.schema.v2.base import is_set
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from acptui.ui.screens.dialogs import DIALOG_CSS


if TYPE_CHECKING:
    from collections.abc import Awaitable

    from aclientp.schema.v2 import ProviderInfo
    from textual.app import ComposeResult

    from acptui.protocol.connection import AgentConnection


def parse_headers(text: str) -> dict[str, str]:
    """``Name: value`` lines as a dict (blank and malformed lines are skipped)."""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


class ProvidersScreen(ModalScreen[None]):
    """Lists the agent's providers; enter edits one."""

    DEFAULT_CSS = "ProvidersScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [Binding("escape", "close", "Close")]

    def __init__(self, connection: AgentConnection) -> None:
        super().__init__()
        self.connection = connection
        self.providers: dict[str, ProviderInfo] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("LLM providers", id="dialog-title")
            yield OptionList(id="providers")
            yield Static("Loading…", id="dialog-hint")

    def on_mount(self) -> None:
        self.load()

    @work(exclusive=True)
    async def load(self) -> None:
        """Load the providers."""
        hint = self.query_one("#dialog-hint", Static)
        try:
            response = await self.connection.list_providers()
        except (RequestError, ConnectionError) as exc:
            hint.update(Content(f"The agent cannot list providers: {exc}"))
            return
        options = self.query_one("#providers", OptionList)
        options.clear_options()
        self.providers = {p.provider_id: p for p in response.providers}
        for provider in response.providers:
            current = provider.current if is_set(provider.current) else None
            detail = (
                f"{current.api_type} · {current.base_url}" if current is not None else "disabled"
            )
            required = " (required)" if provider.required else ""
            label = Content.assemble(
                (provider.provider_id + required, "bold"), ("\n  " + detail, "$text-muted")
            )
            options.add_option(Option(label, id=provider.provider_id))
        hint.update(
            "enter: configure · escape: close" if self.providers else "No configurable providers."
        )
        if options.option_count:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        provider = self.providers.get(event.option_id or "")
        if provider is not None:

            def reload(_changed: bool | None) -> None:
                self.load()

            self.app.push_screen(ProviderEditScreen(self.connection, provider), callback=reload)

    def action_close(self) -> None:
        """Close."""
        self.dismiss(None)


class ProviderEditScreen(ModalScreen[bool]):
    """Edit (or disable) one provider."""

    DEFAULT_CSS = (
        "ProviderEditScreen {"
        + DIALOG_CSS
        + " TextArea { height: 5; } #buttons { height: auto; } }"
    )
    BINDINGS: ClassVar = [Binding("escape", "close", "Cancel")]

    def __init__(self, connection: AgentConnection, provider: ProviderInfo) -> None:
        super().__init__()
        self.connection = connection
        self.provider = provider

    def compose(self) -> ComposeResult:
        provider = self.provider
        current = provider.current if is_set(provider.current) else None
        with Vertical(id="dialog"):
            yield Static(Content(f"Provider {provider.provider_id}"), id="dialog-title")
            yield Static("API type")
            yield Select(
                [(api, api) for api in provider.supported],
                value=current.api_type if current is not None else Select.NULL,
                id="api-type",
            )
            yield Static("Base URL")
            yield Input(current.base_url if current is not None else "", id="base-url")
            yield Static("Headers (one 'Name: value' per line, replaces all headers)")
            yield TextArea("", id="headers")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="success", id="save", compact=True)
                if not provider.required:
                    yield Button("Disable", variant="error", id="disable", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "save":
                api = self.query_one("#api-type", Select).value
                url = self.query_one("#base-url", Input).value.strip()
                if api is Select.NULL or not url:
                    self.notify("API type and base URL are required", severity="error")
                    return
                headers = parse_headers(self.query_one("#headers", TextArea).text)
                await self._run(
                    self.connection.set_provider(self.provider.provider_id, str(api), url, headers)
                )
            case "disable":
                await self._run(self.connection.disable_provider(self.provider.provider_id))
            case _:
                self.dismiss(False)

    async def _run(self, call: Awaitable[None]) -> None:
        try:
            await call
        except (RequestError, ConnectionError) as exc:
            self.notify(f"The agent rejected the change: {exc}", severity="error")
            return
        self.dismiss(True)

    def action_close(self) -> None:
        """Cancel."""
        self.dismiss(False)
