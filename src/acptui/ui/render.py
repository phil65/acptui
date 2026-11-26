"""Small rendering helpers: terminal output, pills, icons."""

from rich.text import Text
from textual.content import Content


MAX_TERMINAL_LINES = 1000
"""Lines of terminal output rendered (older ones are scrolled out)."""


def terminal_text(data: bytes, max_lines: int = MAX_TERMINAL_LINES) -> Text:
    """Render terminal output bytes: ANSI colors, carriage-return overwrites, last ``max_lines``.

    This is not a terminal emulator: cursor movement is ignored, but colors and
    progress lines redrawn with carriage returns come out right.
    """
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        if "\r" in line:
            # A carriage return starts the line over; a trailing one does not erase it.
            segments = [segment for segment in line.split("\r") if segment]
            line = segments[-1] if segments else ""
        lines.append(line)
    if lines and lines[-1] == "":
        lines.pop()
    return Text.from_ansi("\n".join(lines[-max_lines:]), no_wrap=False)


def pill(text: str, background: str, foreground: str) -> Content:
    """Text as a pill with half-block ends.

    Adapted from toad (https://github.com/batrachianai/toad, ``src/toad/pill.py``),
    Copyright (c) Will McGugan, AGPL-3.0.
    """
    end_style = f"{background} on transparent r"
    return Content.assemble(
        ("▌", end_style),
        Content(text).stylize(f"{foreground} on {background}"),
        ("▐", end_style),
    )


TOOL_KIND_ICONS: dict[str, str] = {
    "read": "»",
    "edit": "✎",
    "delete": "✗",
    "move": "⇄",
    "search": "⌕",
    "execute": "$",
    "think": "…",
    "fetch": "↓",
    "switch_mode": "⇆",
    "other": "•",
}
"""Single-width symbols for tool kinds (emoji widths vary between terminals)."""


def tool_icon(kind: str | None) -> str:
    """Icon for a tool kind."""
    return TOOL_KIND_ICONS.get(kind or "other", "•")


STATUS_MARKUP: dict[str, str] = {
    "pending": "[$text-muted]○[/]",
    "in_progress": "[$accent]◐[/]",
    "paused": "[$warning]⏸[/]",
    "completed": "[$success]✔[/]",
    "failed": "[$error]✘[/]",
    "cancelled": "[$warning]⊘[/]",
}


def status_icon(status: str) -> Content:
    """Icon for a tool call or plan entry status."""
    return Content.from_markup(STATUS_MARKUP.get(status, "[$text-muted]?[/]"))


def human_tokens(count: int) -> str:
    """``12345`` -> ``12.3k``."""
    if count >= 1_000_000:  # noqa: PLR2004
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:  # noqa: PLR2004
        return f"{count / 1000:.1f}k"
    return str(count)
