"""The prompt: a multi-line editor with ``/command`` and ``@path`` completion and history."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from acptui.completion import Completion, complete_command, complete_path, completion_context


if TYPE_CHECKING:
    from pathlib import Path

    from aclientp.schema.v2 import AvailableCommand
    from textual.events import Key

    from acptui.completion import CompletionContext


class CompletionList(OptionList):
    """Completion candidates, shown above the prompt."""

    DEFAULT_CSS = """
    CompletionList {
        height: auto;
        max-height: 8;
        border: none;
        padding: 0;
        background: $panel;
        display: none;
        &.-visible { display: block; }
    }
    """

    def __init__(self) -> None:
        super().__init__(id="completions")
        self.completions: list[Completion] = []
        self.can_focus = False

    def show(self, completions: list[Completion]) -> None:
        """Show candidates (or hide when there are none)."""
        self.completions = completions
        self.clear_options()
        self.add_options(
            Option(
                Content.assemble(
                    (c.label, "bold"), ("  " + c.detail if c.detail else "", "$text-muted")
                )
            )
            for c in completions
        )
        self.set_class(bool(completions), "-visible")
        if completions:
            self.highlighted = 0

    def hide(self) -> None:
        """Hide the list."""
        self.completions = []
        self.set_class(False, "-visible")

    @property
    def is_shown(self) -> bool:
        """Whether candidates are shown."""
        return self.has_class("-visible")

    @property
    def selected(self) -> Completion | None:
        """The highlighted candidate."""
        index = self.highlighted
        if index is None or not (0 <= index < len(self.completions)):
            return None
        return self.completions[index]


class PromptInput(TextArea):
    """Where the user types. ``enter`` sends, ``shift+enter`` / ``ctrl+j`` add a line."""

    DEFAULT_CSS = """
    PromptInput {
        height: auto;
        min-height: 3;
        max-height: 10;
        border: tall $primary 50%;
        &:focus { border: tall $primary; }
        &.-disabled { border: tall $panel; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("ctrl+j,shift+enter,alt+enter", "newline", "New line", show=False),
    ]

    class Submitted(Message):
        """The user sent the prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, cwd: Path) -> None:
        super().__init__(
            "",
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            highlight_cursor_line=False,
            placeholder="Message the agent  (/ commands, @ files, shift+enter: new line)",
            id="prompt",
        )
        self.cwd = cwd
        self.commands: list[AvailableCommand] = []
        self.prompt_history: list[str] = []
        self._history_index: int | None = None
        self._completion_context: CompletionContext | None = None
        self.completion_list: CompletionList | None = None
        self._default_placeholder = str(self.placeholder)
        self._suggestion = ""

    @property
    def suggestion(self) -> str:
        """The agent's suggested next prompt (``tab`` on an empty prompt takes it)."""
        return self._suggestion

    @suggestion.setter
    def suggestion(self, text: str) -> None:
        self._suggestion = text
        first_line = text.splitlines()[0] if text else ""
        self.placeholder = (
            f"{first_line}  (tab: use suggestion)" if text else self._default_placeholder
        )

    # --- Completion -----------------------------------------------------------------

    def _update_completions(self) -> None:
        if self.completion_list is None:
            return
        row, column = self.cursor_location
        line = self.document.get_line(row)[:column]
        context = completion_context(line, row)
        self._completion_context = context
        if context is None:
            self.completion_list.hide()
            return
        if context.kind == "command":
            completions = complete_command(context.token, self.commands)
        else:
            completions = complete_path(context.token, self.cwd)
        self.completion_list.show(completions)

    def _accept_completion(self) -> bool:
        completion_list = self.completion_list
        if completion_list is None or not completion_list.is_shown:
            return False
        selected = completion_list.selected
        context = self._completion_context
        if selected is None or context is None:
            return False
        row, column = self.cursor_location
        is_dir = selected.insert.endswith("/")
        # A finished completion ends with a space, which ends the completion context.
        insert = (
            selected.insert if is_dir or selected.insert.endswith(" ") else selected.insert + " "
        )
        self.replace(insert, (row, context.start), (row, column))
        return True

    def _completion_is_typed(self) -> bool:
        """Whether the highlighted completion is exactly what was typed (enter then sends)."""
        selected = self.completion_list.selected if self.completion_list is not None else None
        context = self._completion_context
        return (
            selected is not None
            and context is not None
            and selected.insert.strip() == context.token
        )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._update_completions()

    def action_newline(self) -> None:
        """Insert a line break."""
        self.insert("\n")

    # --- Keys -----------------------------------------------------------------------

    async def _on_key(self, event: Key) -> None:  # noqa: PLR0911
        completion_list = self.completion_list
        popup = completion_list is not None and completion_list.is_shown
        match event.key:
            case "enter":
                event.stop()
                event.prevent_default()
                if popup and not self._completion_is_typed() and self._accept_completion():
                    return
                self._submit()
                return
            case "tab" if not popup and not self.text and self._suggestion:
                event.stop()
                event.prevent_default()
                self.load_text(self._suggestion)
                self.move_cursor(self.document.end)
                return
            case "tab" if popup:
                event.stop()
                event.prevent_default()
                self._accept_completion()
                return
            case "up" if popup and completion_list is not None:
                event.stop()
                event.prevent_default()
                completion_list.action_cursor_up()
                return
            case "down" if popup and completion_list is not None:
                event.stop()
                event.prevent_default()
                completion_list.action_cursor_down()
                return
            case "escape" if popup and completion_list is not None:
                event.stop()
                event.prevent_default()
                completion_list.hide()
                return
            case "up" if self.cursor_location[0] == 0 and self.prompt_history:
                event.stop()
                event.prevent_default()
                self._history_step(-1)
                return
            case "down" if (
                self._history_index is not None
                and self.cursor_location[0] == self.document.line_count - 1
            ):
                event.stop()
                event.prevent_default()
                self._history_step(1)
                return
        await super()._on_key(event)

    def _history_step(self, step: int) -> None:
        index = len(self.prompt_history) if self._history_index is None else self._history_index
        index += step
        if index >= len(self.prompt_history):
            self._history_index = None
            self.load_text("")
            return
        self._history_index = max(0, index)
        self.load_text(self.prompt_history[self._history_index])
        self.move_cursor(self.document.end)

    def _submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        if not self.prompt_history or self.prompt_history[-1] != text:
            self.prompt_history.append(text)
        self._history_index = None
        if self.completion_list is not None:
            self.completion_list.hide()
        self.post_message(self.Submitted(text))
