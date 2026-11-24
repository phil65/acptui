"""What the block cursor does with a conversation block: its text, its picture.

The block cursor (``alt+up`` / ``alt+down``, after toad's) selects one block of the
conversation; ``c`` copies it, ``p`` puts it into the prompt, ``v`` saves it as SVG and
``m`` maximizes it. Messages give their Markdown, tool calls their title and output,
anything else the text it shows.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from aclientp.schema.v2 import Content as ToolContent, Diff, TextContent
from aclientp.schema.v2.base import is_set
from rich.console import Console

from acptui.protocol.content import content_markdown
from acptui.protocol.state import MessageEntry, blocks_text


if TYPE_CHECKING:
    from textual.widget import Widget


def render_widget(widget: Widget, *, color: bool) -> Console:
    """Render a widget alone (at its current size) into a recording console."""
    # Textual's compositor is internal; this is how Textual renders a screen, for one widget.
    from textual._compositor import Compositor

    width, height = widget.outer_size
    compositor = Compositor()
    compositor.reflow(widget, widget.outer_size)
    console = Console(
        width=max(width, 1),
        height=max(height, 1),
        file=io.StringIO(),
        record=True,
        legacy_windows=False,
        color_system="truecolor" if color else None,
        force_terminal=True,
        safe_box=False,
    )
    console.print(compositor.render_full_update())
    return console


def block_svg(widget: Widget, title: str = "acptui") -> str:
    """The block as an SVG image (like a screenshot of only that block)."""
    return render_widget(widget, color=True).export_svg(title=title)


def block_text(widget: Widget) -> str:
    """The text of a block: Markdown for messages, title and output for tool calls."""
    from acptui.ui.widgets.blocks import (
        AgentMessageView,
        ThoughtView,
        ToolCallView,
        UserMessageView,
    )

    match widget:
        case AgentMessageView():
            return widget.source.strip()
        case ThoughtView():
            return widget.markdown.source.strip()
        case UserMessageView():
            return blocks_text(widget.entry.blocks)
        case ToolCallView():
            entry = widget.entry
            parts = [entry.display_title]
            for item in entry.content:
                match item:
                    case ToolContent(content=TextContent(text=text)):
                        parts.append(text.rstrip())
                    case ToolContent(content=block):
                        parts.append(content_markdown([block], widget.state.cwd).strip())
                    case Diff(patch=patch) if is_set(patch) and patch is not None:
                        parts.append(patch.text.rstrip())
                    case _:
                        pass
            return "\n\n".join(part for part in parts if part)
        case _:
            entry = getattr(widget, "entry", None)
            if isinstance(entry, MessageEntry):
                return content_markdown(entry.blocks, "/").strip()
            text = render_widget(widget, color=False).export_text(styles=False)
            return "\n".join(line.rstrip() for line in text.splitlines()).strip()
