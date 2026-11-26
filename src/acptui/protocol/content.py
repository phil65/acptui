"""Rendering v2 content blocks as Markdown, for every block kind."""

import base64
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote

from aclientp.schema.v2 import (
    AudioContent,
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    SkillReference,
    TextContent,
    TextResourceContents,
)
from aclientp.schema.v2.base import is_set


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from aclientp.schema.v2 import ContentBlock


LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".html": "html",
    ".css": "css",
    ".c": "c",
    ".cpp": "cpp",
    ".java": "java",
}


SHELL_URI_PREFIX = "shell:"
"""URI prefix of a shell command's output attached to a prompt (``shell:<quoted command>``)."""


_MARKDOWN_SPECIAL = frozenset("\\`*_{}[]()<>#+-.!|~")


def escape_markdown(text: str) -> str:
    """Plain text for a Markdown document: punctuation Markdown would interpret is escaped.

    ACP v2 display strings are plain text unless the spec says Markdown (names, titles,
    resource link descriptions, ...): they are escaped where acptui puts them into
    Markdown it renders.
    """
    return "".join(f"\\{char}" if char in _MARKDOWN_SPECIAL else char for char in text)


def code_span(text: str) -> str:
    """``text`` as an inline code span (literal, whatever backticks it contains)."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def human_size(size: int) -> str:
    """``12345`` -> ``12.1 KB``."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:  # noqa: PLR2004
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _b64_size(data: str) -> int:
    return len(data) * 3 // 4 - data[-2:].count("=") if data else 0


def relative_display(path: str, cwd: str | Path) -> str:
    """A path (or ``file://`` URI) relative to ``cwd`` if it is inside it, else unchanged."""
    raw = path.removeprefix("file://")
    root = str(cwd).rstrip("/") + "/"
    return raw[len(root) :] if raw.startswith(root) else raw


def for_user(block: ContentBlock) -> bool:
    """Whether a block is meant for the user (its ``audience`` annotation allows it)."""
    annotations = getattr(block, "annotations", None)
    if annotations is None or not is_set(annotations):
        return True
    audience = annotations.audience
    return not is_set(audience) or audience is None or "user" in audience


def describe_block(block: ContentBlock, cwd: str | Path = "/") -> str | None:  # noqa: PLR0911
    """A one-line description of a non-text block (``None`` for text)."""
    match block:
        case TextContent():
            return None
        case ResourceLink():
            name = block.title if is_set(block.title) and block.title else block.name
            size = (
                f", {human_size(block.size)}"
                if is_set(block.size) and block.size is not None
                else ""
            )
            return f"{name} ({relative_display(block.uri, cwd)}{size})"
        case EmbeddedResource() if block.resource.uri.startswith(SHELL_URI_PREFIX):
            return f"shell: $ {unquote(block.resource.uri.removeprefix(SHELL_URI_PREFIX))}"
        case EmbeddedResource():
            return f"file: {relative_display(block.resource.uri, cwd)}"
        case ImageContent():
            return f"image: {block.mime_type}, {human_size(_b64_size(block.data))}"
        case AudioContent():
            return f"audio: {block.mime_type}, {human_size(_b64_size(block.data))}"
        case SkillReference():
            return f"skill: /{block.name}"
        case _:
            return block.type


def block_markdown(block: ContentBlock, cwd: str | Path) -> str:
    """Markdown for one content block."""
    match block:
        case TextContent():
            return block.text
        case ResourceLink():
            name = block.title if is_set(block.title) and block.title else block.name
            if block.uri.startswith(("http://", "https://")):
                line = f"\n\n🔗 [{escape_markdown(name)}](<{block.uri.replace('>', '%3E')}>)"
            else:  # Markdown renderers refuse file: and other local links
                path = code_span(relative_display(block.uri, cwd))
                line = f"\n\n🔗 **{escape_markdown(name)}** {path}"
            if is_set(block.description) and block.description:  # plain text in v2
                line += f" — {escape_markdown(block.description)}"
            if is_set(block.size) and block.size is not None:
                line += f" ({human_size(block.size)})"
            return line + "\n\n"
        case EmbeddedResource(resource=TextResourceContents() as resource):
            suffix = PurePosixPath(resource.uri).suffix.lower()
            language = LANGUAGES.get(suffix, "")
            fence = "````" if "```" in resource.text else "```"
            label = escape_markdown(relative_display(resource.uri, cwd))
            return f"\n\n**{label}**\n\n{fence}{language}\n{resource.text.rstrip()}\n{fence}\n\n"
        case EmbeddedResource(resource=BlobResourceContents() as blob):
            mime = blob.mime_type if is_set(blob.mime_type) and blob.mime_type else "binary"
            label = relative_display(blob.uri, cwd)
            return f"\n\n{code_span(f'[{label}: {mime}, {human_size(_b64_size(blob.blob))}]')}\n\n"
        case _:
            return f"\n\n{code_span(f'[{describe_block(block, cwd)}]')}\n\n"


def content_markdown(blocks: Sequence[ContentBlock], cwd: str | Path) -> str:
    """Markdown for message content, leaving out blocks not meant for the user."""
    return "".join(block_markdown(block, cwd) for block in blocks if for_user(block))


def decode_image(block: ImageContent) -> bytes:
    """The raw bytes of an image block (empty if malformed)."""
    try:
        return base64.b64decode(block.data)
    except ValueError:
        return b""
