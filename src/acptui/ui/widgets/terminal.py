"""Live view of an agent-owned terminal (v2 terminals are display-only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2.base import is_set
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, Static

from acptui.ui.render import pill, terminal_text


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from acptui.protocol.state import ShellEntry, TerminalState
    from acptui.shell import ShellCommand


REFRESH_INTERVAL = 0.1
"""Seconds between redraws while output streams in."""


class TerminalView(Vertical):
    """Command, live output and exit status of a terminal."""

    DEFAULT_CSS = """
    TerminalView {
        height: auto;
        border-left: outer $secondary;
        padding: 0 0 0 1;
        margin: 0 0 1 0;
        .terminal-header { height: 1; width: 1fr; }
        .terminal-output {
            height: auto;
            max-height: 16;
            background: $background-darken-1;
            scrollbar-size-vertical: 1;
        }
        .terminal-output > Static { height: auto; }
        Button { min-width: 10; height: 1; border: none; margin: 0 1; }
        #terminal-top { height: 1; }
    }
    """

    class CancelRequested(Message):
        """The user asked to cancel the terminal."""

        def __init__(self, terminal_id: str) -> None:
            super().__init__()
            self.terminal_id = terminal_id

    def __init__(self, terminal: TerminalState) -> None:
        super().__init__(classes=f"terminal-{terminal.terminal_id}")
        self.terminal = terminal
        self._dirty = False

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Horizontal(id="terminal-top"):
            yield Static(self._header(), classes="terminal-header")
            yield Button("cancel", variant="warning", compact=True, id="terminal-cancel")
        with VerticalScroll(classes="terminal-output"):
            yield Static(terminal_text(self._output()), id="terminal-text")

    def on_mount(self) -> None:
        self.query_one(".terminal-output", VerticalScroll).anchor()
        self._sync_cancel()
        self.set_interval(REFRESH_INTERVAL, self._flush)

    def _output(self) -> bytes:
        return bytes(self.terminal.output)

    def _header(self) -> Content:
        terminal = self.terminal
        command = Content.assemble(("$ ", "bold $secondary"), (terminal.command or "…", "bold"))
        status = terminal.exit_status
        if status is None:
            badge = pill("running", "$accent-muted", "$text-accent")
        else:
            code = status.exit_code if is_set(status.exit_code) else None
            signal = status.signal if is_set(status.signal) else None
            if signal:
                badge = pill(f"signal {signal}", "$error-muted", "$text-error")
            elif code in {0, None}:
                badge = pill("exit 0" if code == 0 else "exited", "$success-muted", "$text-success")
            else:
                badge = pill(f"exit {code}", "$error-muted", "$text-error")
        return Content.assemble(command, "  ", badge)

    def _sync_cancel(self) -> None:
        button = self.query_one("#terminal-cancel", Button)
        button.display = self.terminal.can_cancel and not self.terminal.exited

    def refresh_terminal(self) -> None:
        """Schedule a redraw (output is redrawn at most every :data:`REFRESH_INTERVAL`)."""
        self._dirty = True

    def _flush(self) -> None:
        if not self._dirty or not self.is_mounted:
            return
        self._dirty = False
        self.query_one(".terminal-header", Static).update(self._header())
        self.query_one("#terminal-text", Static).update(terminal_text(self._output()))
        self._sync_cancel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.CancelRequested(self.terminal.terminal_id))


class ShellView(TerminalView):
    """A command the user ran in shell mode: like an agent terminal, plus where it ran."""

    DEFAULT_CSS = """
    ShellView { border-left: outer $warning; }
    """

    class InterruptRequested(Message):
        """The user asked to interrupt the command."""

        def __init__(self, entry: ShellEntry) -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, entry: ShellEntry, session_cwd: str) -> None:
        self.entry = entry
        self.session_cwd = session_cwd
        Vertical.__init__(self, classes="shell-command")
        self._dirty = False

    @property
    def command(self) -> ShellCommand:
        """The command shown."""
        return self.entry.command

    def _output(self) -> bytes:
        return self.command.display_output

    def _header(self) -> Content:
        command = self.command
        prompt = Content.assemble(("! ", "bold $warning"), (command.command, "bold"))
        if command.cwd != self.session_cwd:
            prompt = Content.assemble(
                prompt, ("  in ", "$text-muted"), (command.cwd, "$text-muted")
            )
        code = command.exit_code
        if code is None:
            badge = pill("running", "$accent-muted", "$text-accent")
        elif code == 0:
            badge = pill("exit 0", "$success-muted", "$text-success")
        else:
            badge = pill(f"exit {code}", "$error-muted", "$text-error")
        return Content.assemble(prompt, "  ", badge)

    def _sync_cancel(self) -> None:
        button = self.query_one("#terminal-cancel", Button)
        button.label = "interrupt"
        button.display = not self.command.done

    def refresh_entry(self) -> None:
        """Redraw after new output (at most every :data:`REFRESH_INTERVAL`)."""
        self.refresh_terminal()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.InterruptRequested(self.entry))
