"""Markdown from the agent: a safe Markdown widget and inline Markdown as ``Content``.

ACP v2 says which display strings are Markdown (text content blocks, plan entries,
and the ``description`` of auth methods, commands and skills, config options and
their values, permission requests and subagents); everything else is plain text.
Agent content is untrusted: raw HTML is not interpreted (it shows as text, so nothing
is hidden), and links are opened only when the user clicks them (see
:func:`link_is_web`).
"""

from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from textual.content import Content
from textual.style import Style
from textual.widgets import Markdown


if TYPE_CHECKING:
    from markdown_it.token import Token


def agent_parser() -> MarkdownIt:
    """The parser for agent Markdown: GitHub-like, raw HTML shown as text."""
    return MarkdownIt("gfm-like", {"html": False})


class AgentMarkdown(Markdown):
    """Markdown written by the agent.

    Raw HTML is shown literally; links are not opened here: the click bubbles up as
    :class:`~textual.widgets.Markdown.LinkClicked` to the app, which shows the target
    and asks before handing anything but ``http(s)`` to the system.
    """

    def __init__(
        self,
        markdown: str = "",
        *,
        id: str | None = None,  # noqa: A002 - Textual's name
        classes: str | None = None,
    ) -> None:
        super().__init__(
            markdown, id=id, classes=classes, parser_factory=agent_parser, open_links=False
        )


def link_is_web(href: str) -> bool:
    """Whether a link target is an ``http(s)`` URL (others need a confirmation)."""
    return href.lower().startswith(("http://", "https://"))


_INLINE_STYLES = {
    "strong": "bold",
    "em": "italic",
    "s": "strike",
}

_inline_parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")


def inline_markdown(text: str, *, single_line: bool = False) -> Content:
    """Markdown as ``Content``, for places that show text, not widgets (lists, one-liners).

    Inline markup is rendered: code spans, emphasis, strike-through and links (a link
    whose target differs from its text shows the target too). Block structure is
    flattened: paragraphs become lines (or, with ``single_line``, one line).
    """
    tokens = _inline_parser.parse(text)
    separator = " " if single_line else "\n"
    paragraphs: list[Content] = []
    ordered: list[int] = []
    """Counters of open ordered lists (0 for bullet lists)."""
    prefix = ""
    for token in tokens:
        if token.type == "ordered_list_open":
            ordered.append(int(str(token.attrs.get("start", 1))))
        elif token.type == "bullet_list_open":
            ordered.append(0)
        elif token.type in {"ordered_list_close", "bullet_list_close"} and ordered:
            ordered.pop()
        elif token.type == "list_item_open" and ordered:
            number = ordered[-1]
            prefix = "  " * (len(ordered) - 1) + (f"{number}. " if number else "• ")
            if number:
                ordered[-1] += 1
        elif token.type == "inline" and token.children is not None:
            line = _render_inline(token.children, separator)
            paragraphs.append(Content.assemble(prefix, line) if prefix else line)
            prefix = ""
        elif token.type in {"fence", "code_block"}:
            code = token.content.rstrip("\n")
            paragraphs.append(Content(code.replace("\n", separator)).stylize("$text-accent"))
    return Content(separator).join(paragraphs)


def _render_inline(children: list[Token], separator: str) -> Content:
    parts: list[Content] = []
    styles: list[str | Style] = []
    links: list[tuple[str, int]] = []
    """Open links: target and the number of parts before the link text."""

    def add(text: str, *extra: str | Style) -> None:
        content = Content(text)
        for style in (*styles, *extra):
            content = content.stylize(style)
        parts.append(content)

    for child in children:
        kind = child.type
        if kind == "text":
            add(child.content)
        elif kind == "code_inline":
            add(child.content, "$text-accent")
        elif kind in {"softbreak", "hardbreak"}:
            add(separator)
        elif kind.endswith("_open") and kind[:-5] in _INLINE_STYLES:
            styles.append(_INLINE_STYLES[kind[:-5]])
        elif kind.endswith("_close") and kind[:-6] in _INLINE_STYLES and styles:
            styles.pop()
        elif kind == "link_open":
            href = str(child.attrs.get("href", ""))
            links.append((href, len(parts)))
            styles.append(Style(underline=True, link=href or None))
        elif kind == "link_close" and links:
            styles.pop()
            href, start = links.pop()
            label = "".join(part.plain for part in parts[start:])
            if href and href not in {label, f"mailto:{label}"}:
                add(f" ({href})", "$text-muted")
        elif kind == "image":
            add(f"[image: {child.content or child.attrs.get('src', '')}]", "$text-muted")
        elif child.content:
            add(child.content)
    return Content.assemble(*parts)


def summary_markdown(text: str) -> Content:
    """The first paragraph of a description as one line of ``Content``."""
    first = text.strip().split("\n\n", 1)[0]
    return inline_markdown(first, single_line=True)
