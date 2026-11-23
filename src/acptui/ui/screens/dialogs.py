"""Small modal dialogs: session list, config options, choices, help."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from aclientp.exceptions import RequestError
from aclientp.schema.v2 import (
    AgentMessageCursorAnchor,
    AgentThoughtCursorAnchor,
    AuthMethodAgent,
    AuthMethodTerminal,
    BooleanSessionConfigOption,
    CursorCompactionAnchor,
    CursorToolCallAnchor,
    SelectSessionConfigOption,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
    SessionMessageCursorAnchor,
    UserMessageCursorAnchor,
)
from aclientp.schema.v2.base import is_set
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from acptui.protocol.state import MessageEntry
from acptui.ui.markdown import AgentMarkdown, summary_markdown


if TYPE_CHECKING:
    from collections.abc import Sequence

    from aclientp.schema.v2 import AuthMethod, SessionConfigOption, SessionInfo
    from textual.app import ComposeResult

    from acptui.protocol.connection import AgentConnection
    from acptui.protocol.state import CursorState, SessionState


DIALOG_CSS = """
    align: center middle;
    #dialog {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 85%;
        border: thick $primary;
        background: $surface;
        padding: 0 1;
    }
    #dialog-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    #dialog-hint { color: $text-muted; }
    OptionList { height: auto; max-height: 20; border: none; }
"""


class SessionListScreen(ModalScreen[str | None]):
    """Lists the agent's sessions for the working directory; returns the one to resume.

    ``d`` deletes, ``a`` archives / unarchives (when the agent supports it), ``h`` shows
    archived sessions too.
    """

    DEFAULT_CSS = "SessionListScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("d", "delete", "Delete"),
        Binding("a", "archive", "Archive / unarchive"),
        Binding("h", "toggle_archived", "Show archived"),
    ]

    def __init__(self, connection: AgentConnection) -> None:
        super().__init__()
        self.connection = connection
        self._cursor: str | None = None
        self.sessions: dict[str, SessionInfo] = {}
        self.show_archived = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Resume a session", id="dialog-title")
            yield OptionList(id="sessions")
            yield Static("Loading…", id="dialog-hint")

    def on_mount(self) -> None:
        self.load()

    def _hint(self) -> str:
        keys = ["enter: resume"]
        if self.connection.supports_delete:
            keys.append("d: delete")
        if self.connection.supports_archive:
            keys += [
                "a: archive/unarchive",
                "h: " + ("hide" if self.show_archived else "show") + " archived",
            ]
        keys.append("escape: close")
        return " · ".join(keys)

    @work(exclusive=True)
    async def load(self, *, reset: bool = False) -> None:
        """Load (the next page of) sessions."""
        hint = self.query_one("#dialog-hint", Static)
        options = self.query_one("#sessions", OptionList)
        if reset:
            self._cursor = None
            self.sessions.clear()
            options.clear_options()
        archived = (
            ("include" if self.show_archived else None)
            if self.connection.supports_archive
            else None
        )
        try:
            response = await self.connection.list_sessions(self._cursor, archived=archived)
        except (RequestError, ConnectionError) as exc:
            hint.update(Content(f"The agent cannot list sessions: {exc}"))
            return
        if (
            options.option_count
            and options.get_option_at_index(options.option_count - 1).id == "more"
        ):
            options.remove_option("more")
        for info in response.sessions:
            self.sessions[info.session_id] = info
            title = info.title if is_set(info.title) and info.title else info.session_id
            updated = info.updated_at if is_set(info.updated_at) and info.updated_at else ""
            flag = "  [archived]" if is_set(info.archived) and info.archived else ""
            label = Content.assemble(
                (title, "bold"),
                (flag, "$text-warning"),
                ("\n  " + updated[:19].replace("T", " "), "$text-muted"),
            )
            options.add_option(Option(label, id=f"s:{info.session_id}"))
        cursor = response.next_cursor if is_set(response.next_cursor) else None
        self._cursor = cursor
        if cursor is not None and response.sessions:
            options.add_option(Option(Content("… load more").stylize("$text-muted"), id="more"))
        hint.update(self._hint() if self.sessions else "No sessions for this directory.")
        if options.highlighted is None and options.option_count:
            options.highlighted = 0
        options.focus()

    def _highlighted(self) -> SessionInfo | None:
        options = self.query_one("#sessions", OptionList)
        if options.highlighted is None:
            return None
        option_id = options.get_option_at_index(options.highlighted).id or ""
        return self.sessions.get(option_id[2:]) if option_id.startswith("s:") else None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        if option_id == "more":
            self.load()
        elif option_id.startswith("s:"):
            self.dismiss(option_id[2:])

    def action_dismiss_none(self) -> None:
        """Close without choosing."""
        self.dismiss(None)

    def action_delete(self) -> None:
        """Delete the highlighted session (after confirming)."""
        info = self._highlighted()
        if info is None or not self.connection.supports_delete:
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._mutate(info.session_id, "delete")

        title = info.title if is_set(info.title) and info.title else info.session_id
        self.app.push_screen(ConfirmScreen(f"Delete session {title!r}?"), callback=confirmed)

    def action_archive(self) -> None:
        """Archive or unarchive the highlighted session."""
        info = self._highlighted()
        if info is not None and self.connection.supports_archive:
            archived = is_set(info.archived) and bool(info.archived)
            self._mutate(info.session_id, "unarchive" if archived else "archive")

    def action_toggle_archived(self) -> None:
        """Show or hide archived sessions."""
        if self.connection.supports_archive:
            self.show_archived = not self.show_archived
            self.load(reset=True)

    @work(group="mutate")
    async def _mutate(self, session_id: str, action: str) -> None:
        try:
            if action == "delete":
                await self.connection.delete_session(session_id)
            else:
                await self.connection.archive_session(session_id, archived=action == "archive")
        except (RequestError, ConnectionError) as exc:
            self.notify(f"Could not {action} the session: {exc}", severity="error")
            return
        self.notify(f"Session {action}d", timeout=2)
        self.load(reset=True)


class ConfirmScreen(ModalScreen[bool]):
    """A yes / no question."""

    DEFAULT_CSS = "ConfirmScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Content(self.question), id="dialog-title")
            yield Static("y: yes · n: no", id="dialog-hint")

    def action_answer(self, answer: bool) -> None:
        """Answer."""
        self.dismiss(answer)


class TextInputScreen(ModalScreen[str | None]):
    """Asks for one line of text."""

    DEFAULT_CSS = "TextInputScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Content(self.title_text), id="dialog-title")
            yield Input(self.value, id="text")
            yield Static("enter: save · escape: cancel", id="dialog-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_dismiss_none(self) -> None:
        """Cancel."""
        self.dismiss(None)


SNIPPET = 50


def describe_anchor(cursor: CursorState, state: SessionState) -> str:
    """Where a cursor is, in words."""
    anchor = cursor.anchor
    match anchor:
        case CursorToolCallAnchor():
            entry = state.tool_calls.get(anchor.tool_call_id)
            what = f"tool call {entry.display_title if entry else anchor.tool_call_id}"
            return f"{anchor.position} {what}"
        case CursorCompactionAnchor():
            return f"{anchor.position} compaction {anchor.compaction_id}"
        case (
            UserMessageCursorAnchor()
            | AgentMessageCursorAnchor()
            | AgentThoughtCursorAnchor()
            | SessionMessageCursorAnchor()
        ):
            role = {
                "user_message": "user",
                "agent_message": "agent",
                "agent_thought": "thought",
                "session_message": "session",
            }[anchor.type]
            message = next(
                (
                    m
                    for m in state.entries
                    if isinstance(m, MessageEntry)
                    and m.role == role
                    and m.message_id == anchor.message_id
                ),
                None,
            )
            text = message.text if message is not None else anchor.message_id
            snippet = text if len(text) <= SNIPPET else text[:SNIPPET] + "…"
            return f"{anchor.position} {anchor.type.replace('_', ' ')}: {snippet}"
        case _:
            return cursor.cursor_id


class ForkScreen(ModalScreen[tuple[str | None] | None]):
    """Choose where to fork: the whole session, or at one of the agent's cursors."""

    DEFAULT_CSS = "ForkScreen {" + DIALOG_CSS + "}"
    BINDINGS: ClassVar = [Binding("escape", "dismiss_none", "Close")]

    def __init__(self, state: SessionState) -> None:
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Fork the session", id="dialog-title")
            options = [Option("The whole session", id="whole")]
            options += [
                Option(Content(describe_anchor(cursor, self.state)), id=f"c:{cursor.cursor_id}")
                for cursor in self.state.cursors.values()
                if cursor.can_fork and cursor.anchor is not None
            ]
            yield OptionList(*options, id="cursors")
            yield Static("enter: fork · escape: close", id="dialog-hint")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        self.dismiss((option_id[2:] if option_id.startswith("c:") else None,))

    def action_dismiss_none(self) -> None:
        """Close."""
        self.dismiss(None)


def _select_values(option: SelectSessionConfigOption) -> list[tuple[str, str, str]]:
    """``(value, name, group)`` of a select option's values."""
    values: list[tuple[str, str, str]] = []
    for item in option.options:
        if isinstance(item, SessionConfigSelectGroup):
            values.extend((v.value, v.name, item.name) for v in item.options)
        elif isinstance(item, SessionConfigSelectOption):
            values.append((item.value, item.name, ""))
    return values


def _value_descriptions(option: SelectSessionConfigOption) -> list[str]:
    """The (Markdown) descriptions of a select option's values, in ``_select_values`` order."""
    items: list[SessionConfigSelectOption] = []
    for item in option.options:
        if isinstance(item, SessionConfigSelectGroup):
            items.extend(item.options)
        elif isinstance(item, SessionConfigSelectOption):
            items.append(item)
    return [v.description if is_set(v.description) and v.description else "" for v in items]


def _described(label: Content, description: str) -> Content:
    """An option list entry: the label, then the description's first paragraph (Markdown)."""
    if not description:
        return label
    summary = summary_markdown(description).stylize_before("$text-muted")
    return Content.assemble(label, "\n  ", summary)


def option_value_label(option: SessionConfigOption) -> str:
    """The display name of an option's current value."""
    match option:
        case SelectSessionConfigOption():
            return next(
                (
                    name
                    for value, name, _ in _select_values(option)
                    if value == option.current_value
                ),
                option.current_value,
            )
        case BooleanSessionConfigOption():
            return "on" if option.current_value else "off"
        case _:
            return "?"


def next_select_value(option: SelectSessionConfigOption) -> str:
    """The value after the current one (wrapping around)."""
    values = [value for value, _, _ in _select_values(option)]
    if option.current_value not in values:
        return values[0]
    return values[(values.index(option.current_value) + 1) % len(values)]


ConfigChoice = tuple[str, str | bool]
"""``(config_id, value)`` chosen in the config dialog."""


class ConfigScreen(ModalScreen[ConfigChoice | None]):
    """Session options (mode, model, ...) as the agent exposes them; returns a new value.

    Descriptions of options and values are Markdown (ACP v2): the list shows their first
    paragraph, the panel below the whole description of the highlighted entry.
    """

    DEFAULT_CSS = (
        "ConfigScreen {"
        + DIALOG_CSS
        + """ #config-description {
            height: auto; max-height: 10; margin: 1 0 0 0; padding: 0;
            background: transparent; color: $text-muted;
        } }"""
    )
    BINDINGS: ClassVar = [Binding("escape", "back", "Back")]

    def __init__(self, options: list[SessionConfigOption]) -> None:
        super().__init__()
        self.options = options
        self.current: SelectSessionConfigOption | None = None
        self.descriptions: dict[str, str] = {}
        """Option list id -> Markdown description."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Session options", id="dialog-title")
            yield OptionList(id="config")
            yield AgentMarkdown(id="config-description")
            yield Static("enter: change · escape: close", id="dialog-hint")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        description = self.descriptions.get(event.option_id or "", "")
        panel = self.query_one("#config-description", AgentMarkdown)
        panel.display = bool(description)
        panel.update(description)

    def on_mount(self) -> None:
        self._show_options()

    def _show_options(self) -> None:
        self.current = None
        self.query_one("#dialog-title", Static).update("Session options")
        options = self.query_one("#config", OptionList)
        options.clear_options()
        self.descriptions.clear()
        if not self.options:
            self.query_one("#dialog-hint", Static).update(
                "The agent offers no options for this session."
            )
        for index, option in enumerate(self.options):
            category = option.category if is_set(option.category) and option.category else ""
            label = Content.assemble(
                (option.name, "bold"),
                ": ",
                (option_value_label(option), "$text-accent"),
                (f"  ({category})" if category else "", "$text-muted"),
            )
            description = option.description if is_set(option.description) else None
            self.descriptions[f"o:{index}"] = description or ""
            options.add_option(Option(_described(label, description or ""), id=f"o:{index}"))
        if self.options:
            options.highlighted = 0
        options.focus()

    def _show_values(self, option: SelectSessionConfigOption) -> None:
        self.current = option
        self.query_one("#dialog-title", Static).update(Content(option.name))
        options = self.query_one("#config", OptionList)
        options.clear_options()
        self.descriptions.clear()
        descriptions = _value_descriptions(option)
        group = None
        highlight = 0
        for index, (value, name, group_name) in enumerate(_select_values(option)):
            if group_name and group_name != group:
                group = group_name
                options.add_option(
                    Option(Content(group_name).stylize("bold $text-muted"), disabled=True)
                )
            marker = "● " if value == option.current_value else "  "
            self.descriptions[f"v:{index}"] = descriptions[index]
            options.add_option(
                Option(_described(Content(marker + name), descriptions[index]), id=f"v:{index}")
            )
            if value == option.current_value:
                highlight = options.option_count - 1
        options.highlighted = highlight
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        if option_id.startswith("o:"):
            option = self.options[int(option_id[2:])]
            if isinstance(option, SelectSessionConfigOption):
                self._show_values(option)
            elif isinstance(option, BooleanSessionConfigOption):
                self.dismiss((option.config_id, not option.current_value))
        elif option_id.startswith("v:") and self.current is not None:
            value = _select_values(self.current)[int(option_id[2:])][0]
            self.dismiss((self.current.config_id, value))

    def action_back(self) -> None:
        """Back to the option list, or close."""
        if self.current is not None:
            self._show_options()
        else:
            self.dismiss(None)


def method_description(method: AuthMethod) -> str:
    """An auth method's description (Markdown in ACP v2), or ``""``."""
    if isinstance(method, AuthMethodAgent | AuthMethodTerminal) and is_set(method.description):
        return method.description or ""
    return ""


class LoginScreen(ModalScreen[str | None]):
    """The agent's login methods with their (Markdown) descriptions; returns a method id.

    Methods acptui cannot run (unknown types) are listed but disabled.
    """

    DEFAULT_CSS = (
        "LoginScreen {"
        + DIALOG_CSS
        + """ #login-description {
            height: auto; max-height: 12; margin: 1 0 0 0; padding: 0; background: transparent;
        } }"""
    )
    BINDINGS: ClassVar = [Binding("escape", "dismiss_none", "Close")]

    def __init__(self, agent_name: str, methods: Sequence[AuthMethod]) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.methods = list(methods)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Content(f"Log in to {self.agent_name}"), id="dialog-title")
            options: list[Option] = []
            for index, method in enumerate(self.methods):
                runnable = isinstance(method, AuthMethodAgent | AuthMethodTerminal)
                kind = (
                    "in a terminal"
                    if isinstance(method, AuthMethodTerminal)
                    else "via the agent"
                    if runnable
                    else f"unsupported: {method.type}"
                )
                label = Content.assemble((method.name, "bold"), (f"  ({kind})", "$text-muted"))
                options.append(
                    Option(
                        _described(label, method_description(method)),
                        id=f"m:{index}",
                        disabled=not runnable,
                    )
                )
            yield OptionList(*options, id="methods")
            yield AgentMarkdown(id="login-description")
            yield Static("enter: log in · escape: close", id="dialog-hint")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.focus()
        if self.methods:
            options.highlighted = 0

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        index = int((event.option_id or "m:0")[2:])
        description = method_description(self.methods[index]) if self.methods else ""
        panel = self.query_one("#login-description", AgentMarkdown)
        panel.display = bool(description)
        panel.update(description)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int((event.option_id or "m:0")[2:])
        self.dismiss(self.methods[index].method_id)

    def action_dismiss_none(self) -> None:
        """Close."""
        self.dismiss(None)


HELP = """\
# acptui

A terminal UI for AI coding agents speaking **ACP v2**.

## Keys

| Key | Action |
| --- | --- |
| `enter` | Send the prompt (or accept a completion) |
| `shift+enter`, `ctrl+j` | New line in the prompt |
| `/` at the start | Complete the agent's commands |
| `@` | Complete a file path (attached to the prompt) |
| `up` / `down` | Prompt history |
| `escape` | Cancel the running turn |
| `shift+tab` | Next mode (if the agent has a mode option) |
| `ctrl+o` | Session options: mode, model, ... |
| `ctrl+n` | New session |
| `ctrl+r` | Resume a session |
| `ctrl+d` | Diff view: unified / split / auto |
| `ctrl+l` | Agent log (stderr and protocol traffic) |
| `tab` | Focus the conversation; `enter` on a tool call toggles it, `x` cancels it |
| `pageup` / `pagedown` | Scroll the conversation |
| `ctrl+p` | Command palette |
| `f5` | Restart the agent |
| `ctrl+b` | Files panel; `a` mentions the highlighted node |
| `!` / `$` | Shell mode (on an empty prompt); `esc` back, `ctrl+c` interrupts |
| `ctrl+g` | Drop the shell output waiting for the next prompt |
| `f12` | Screenshot: SVG + text to Downloads, text copied |
| `ctrl+q` | Quit |

## Permission dialog

Number keys pick an option, `y` allows, `n` or `escape` rejects.

## Files

`@path` mentions are attached to the prompt: small text files are embedded if the
agent accepts embedded context, images are attached if it accepts images, anything
else is sent as a link.
"""


class HelpScreen(ModalScreen[None]):
    """Keys and tips."""

    DEFAULT_CSS = (
        "HelpScreen {"
        + DIALOG_CSS
        + " #dialog { max-width: 90; } Markdown { height: auto; max-height: 40; margin: 0; } }"
    )
    BINDINGS: ClassVar = [Binding("escape,f1,q", "dismiss_help", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Markdown(HELP)

    def action_dismiss_help(self) -> None:
        """Close."""
        self.dismiss(None)
