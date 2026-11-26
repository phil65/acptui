"""Turning prompt text into v2 content blocks: ``@file`` mentions and skills."""

import base64
from dataclasses import dataclass
import mimetypes
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from aclientp.schema.v2 import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    SkillReference,
    TextContent,
    TextResourceContents,
)

from acptui.protocol.content import SHELL_URI_PREFIX


if TYPE_CHECKING:
    from collections.abc import Callable

    from aclientp.schema.v2 import ContentBlock

    from acptui.shell import ShellCommand


MENTION = re.compile(r"(?<![\w@])@((?:\\ |[^\s])+)")
"""An ``@path`` mention; spaces in paths are escaped as ``\\ ``."""

MAX_EMBED_BYTES = 256 * 1024
"""Text files up to this size are embedded when the agent accepts embedded context."""

MAX_IMAGE_BYTES = 5 * 1024 * 1024
"""Images up to this size are attached when the agent accepts images."""

TEXT_MIME_EXTRAS = frozenset({
    "application/json",
    "application/toml",
    "application/xml",
    "application/x-sh",
    "application/javascript",
    "application/x-yaml",
})


@dataclass(frozen=True)
class PromptOptions:
    """What the agent accepts in prompts."""

    embedded_context: bool = False
    images: bool = False
    audio: bool = False
    skills: frozenset[str] = frozenset()
    """Names of the session's ``skill`` commands (only used if the agent accepts skills)."""


def find_mentions(text: str) -> list[str]:
    """The paths mentioned with ``@`` in the text (unescaped, in order, unique)."""
    seen: dict[str, None] = {}
    for match in MENTION.finditer(text):
        path = match.group(1).replace("\\ ", " ").rstrip(".,;:!?)")
        if path:
            seen.setdefault(path, None)
    return list(seen)


def resolve_mention(cwd: Path, mention: str) -> Path | None:
    """The file a mention refers to, if it exists inside ``cwd``."""
    root = cwd.resolve()
    path = (root / Path(mention).expanduser()).resolve()
    if not path.is_relative_to(root) or not path.exists():
        return None
    return path


def guess_mime(path: Path) -> str:
    """The MIME type of a file, ``text/plain`` if unknown."""
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "text/plain"


def _is_text(path: Path, mime: str) -> bool:
    if mime.startswith("text/") or mime in TEXT_MIME_EXTRAS:
        return True
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\0" not in head


def file_block(path: Path, options: PromptOptions) -> ContentBlock:
    """The content block for a mentioned file.

    Small text files are embedded if the agent accepts embedded context, images and
    audio are attached if it accepts them; everything else (and directories) is linked.
    """
    uri = path.as_uri()
    mime = guess_mime(path)
    if path.is_file():
        size = path.stat().st_size
        if options.images and mime.startswith("image/") and size <= MAX_IMAGE_BYTES:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return ImageContent(data=data, mime_type=mime, uri=uri)
        if options.audio and mime.startswith("audio/") and size <= MAX_IMAGE_BYTES:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return AudioContent(data=data, mime_type=mime)
        if options.embedded_context and size <= MAX_EMBED_BYTES and _is_text(path, mime):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                pass
            else:
                return EmbeddedResource(
                    resource=TextResourceContents(text=text, uri=uri, mime_type=mime)
                )
        return ResourceLink(name=path.name, uri=uri, mime_type=mime, size=size)
    return ResourceLink(name=path.name, uri=uri, mime_type="inode/directory")


def shell_block(command: ShellCommand, options: PromptOptions) -> ContentBlock:
    """A shell command the user ran, with its output, as context for the agent.

    Embedded as a resource where the agent accepts that (shown as one line in the
    message), else as text.
    """
    text = command.context_text()
    if options.embedded_context:
        uri = SHELL_URI_PREFIX + quote(command.command, safe="")
        return EmbeddedResource(
            resource=TextResourceContents(text=text, uri=uri, mime_type="text/plain")
        )
    return TextContent(text=f"\n\nI ran a shell command:\n\n```\n{text}```\n")


def build_prompt(
    text: str,
    cwd: Path,
    options: PromptOptions,
    resolve: Callable[[str, PromptOptions], ContentBlock | None] | None = None,
) -> list[ContentBlock]:
    """The content blocks for a prompt.

    The text is sent as typed (slash commands are plain text in v2); a leading
    ``/skill`` naming one of the session's skills becomes a ``skill`` block. Each
    ``@file`` that exists inside ``cwd`` adds a resource block after the text; other
    mentions (a section of a file, a file on a mount) go to ``resolve``.
    """
    blocks: list[ContentBlock] = []
    body = text
    if options.skills and (match := re.match(r"/(\S+)\s*", text)):
        name = match.group(1)
        if name in options.skills:
            blocks.append(SkillReference(name=name))
            body = text[match.end() :]
    if body.strip():
        blocks.append(TextContent(text=body))
    for mention in find_mentions(text):
        if (path := resolve_mention(cwd, mention)) is not None:
            blocks.append(file_block(path, options))
        elif resolve is not None and (block := resolve(mention, options)) is not None:
            blocks.append(block)
    if not blocks:
        blocks.append(TextContent(text=text))
    return blocks
