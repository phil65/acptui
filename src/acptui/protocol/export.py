"""A session's timeline as one Markdown document (``/acptui:export``, the palette).

Agent text is Markdown already and goes in as it is; plain-text fields (titles, names,
notices) are escaped; tool output and shell output go into code fences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2 import Content as ToolContent, Diff, PlanItems, PlanMarkdown, TextContent
from aclientp.schema.v2.base import is_set

from acptui.protocol.content import content_markdown, escape_markdown
from acptui.protocol.state import (
    CompactionEntry,
    MessageEntry,
    NoticeEntry,
    PlanEntry,
    ShellEntry,
    SubagentEntry,
    ToolCallEntry,
    blocks_text,
)


if TYPE_CHECKING:
    from acptui.protocol.state import Entry, SessionState


def fence(text: str, language: str = "") -> str:
    """``text`` in a code fence long enough for the backticks it contains."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def quote(text: str) -> str:
    """``text`` as a Markdown block quote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def entry_markdown(entry: Entry, cwd: str) -> str:  # noqa: PLR0911
    """One timeline entry as Markdown."""
    match entry:
        case MessageEntry(role="user"):
            return "## You\n\n" + content_markdown(entry.blocks, cwd).strip()
        case MessageEntry(role="thought"):
            return "**Thinking**\n\n" + quote(content_markdown(entry.blocks, cwd))
        case MessageEntry(role="session"):
            return "### Session message\n\n" + quote(content_markdown(entry.blocks, cwd))
        case MessageEntry():
            return "## Agent\n\n" + content_markdown(entry.blocks, cwd).strip()
        case ToolCallEntry():
            parts = [f"**Tool call:** {escape_markdown(entry.display_title)} ({entry.status})"]
            for item in entry.content:
                match item:
                    case ToolContent(content=TextContent(text=text)):
                        parts.append(fence(text))
                    case ToolContent(content=block):
                        parts.append(content_markdown([block], cwd).strip())
                    case Diff(patch=patch) if is_set(patch) and patch is not None:
                        parts.append(fence(patch.text, "diff"))
                    case _:
                        pass
            return "\n\n".join(part for part in parts if part)
        case PlanEntry(removed=True):
            return ""
        case PlanEntry(content=PlanItems() as items):
            lines = [
                f"- [{'x' if item.status == 'completed' else ' '}] {item.content}"
                for item in items.entries
            ]
            return "**Plan**\n\n" + "\n".join(lines)
        case PlanEntry(content=PlanMarkdown() as plan):
            return "**Plan**\n\n" + plan.content
        case PlanEntry():
            return ""
        case NoticeEntry():
            severity = escape_markdown(entry.severity.capitalize())
            text = f"**{severity}:** {escape_markdown(entry.title)}"
            if entry.description:
                text += "\n\n" + quote(escape_markdown(entry.description))
            return text
        case CompactionEntry():
            summary = blocks_text(entry.summary)
            return f"*Context compaction ({entry.status})*" + (
                "\n\n" + quote(summary) if summary else ""
            )
        case SubagentEntry():
            text = f"**Subagent:** {escape_markdown(entry.display_title)}"
            return text + (f"\n\n{entry.description}" if entry.description else "")
        case ShellEntry(command=command):
            output = command.output.decode("utf-8", errors="replace")
            return f"**Shell:** `$ {command.command}`\n\n" + fence(output)


def session_markdown(state: SessionState, agent: str) -> str:
    """The whole timeline of a session as a Markdown document."""
    title = state.title or "Conversation"
    head = (
        f"# {escape_markdown(title)}\n\n*{escape_markdown(agent)} · {escape_markdown(state.cwd)}*"
    )
    parts = [head, *(entry_markdown(entry, state.cwd) for entry in state.entries)]
    return "\n\n".join(part for part in parts if part.strip()) + "\n"
