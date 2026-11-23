"""The files panel's filesystem: the working directory (browsable into files) plus mounts.

Built from upathtools parts: an ``AutoExpandFS`` over the working directory, so
archives, Markdown files (by section) and source files (by class / function, with
``upathtools[tree-sitter]``) open like folders, and a ``UnionFileSystem`` that
puts it under ``workspace/`` next to the configured mounts (any fsspec / upathtools
URL: ``github://owner:repo@main/docs``, ``s3://bucket/key``, ``sftp://host/path``, a local
directory, ...).

A tree node is mentioned in the prompt as ``@path`` (inside the workspace, as for
any file) or ``@mount:path``. Nodes that aren't plain local files (a section, a
function, a file on a mount) are read here and embedded into the prompt.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any

from aclientp.schema.v2 import EmbeddedResource, ResourceLink, TextResourceContents
from fsspec import filesystem
from fsspec.implementations.dirfs import DirFileSystem
import upathtools
from upathtools.filesystems.auto_expand_fs import AutoExpandFS, main_protocol
from upathtools.filesystems.combining_filesystems.union_fs import UnionFileSystem

from acptui.protocol.prompt import MAX_EMBED_BYTES, guess_mime


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from aclientp.schema.v2 import ContentBlock
    from fsspec.spec import AbstractFileSystem
    from upath import UPath

    from acptui.protocol.prompt import PromptOptions


WORKSPACE = "workspace"
"""The working directory's folder in the tree."""

CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".rs", ".go", ".c", ".h", ".cpp", ".hpp")
"""Source files opened by class / function (tree-sitter grammars upathtools ships)."""


def _markdown(fs: AbstractFileSystem, path: str) -> dict[str, Any]:
    return {"fo": path, "target_protocol": main_protocol(fs)}


def _code(fs: AbstractFileSystem, path: str) -> dict[str, Any]:
    return {"source_file": path, "target_protocol": main_protocol(fs)}


def expanders() -> dict[str, tuple[str, Any]]:
    """File types the tree opens like folders, beyond archives (upathtools' defaults)."""
    result: dict[str, tuple[str, Any]] = {".md": ("md", _markdown)}
    if importlib.util.find_spec("tree_sitter") is not None:
        result.update(dict.fromkeys(CODE_SUFFIXES, ("ts", _code)))
    return result


class Files:
    """The working directory and the mounts, as one browsable filesystem."""

    def __init__(self, cwd: Path, mounts: Mapping[str, str] | None = None) -> None:
        upathtools.register_all_filesystems()
        self.cwd = cwd.resolve()
        self.mounts = dict(mounts or {})
        local = AutoExpandFS(fs=filesystem("file"), expanders=expanders(), asynchronous=False)
        # fsspec paths use forward slashes, also on Windows (DirFileSystem compares them)
        workspace = DirFileSystem(path=self.cwd.as_posix(), fs=local)
        self.fs = UnionFileSystem({WORKSPACE: workspace, **self.mounts})

    def root(self) -> UPath:
        """The tree's root: ``workspace/`` and one folder per mount."""
        return self.fs.get_upath()

    def mention(self, path: UPath) -> str:
        """How a tree node is written after ``@`` in the prompt."""
        top, _, rest = path.path.lstrip("/").partition("/")
        if top == WORKSPACE:
            return rest.replace(" ", "\\ ")
        return f"{top}:{rest}".replace(" ", "\\ ")

    def mention_block(self, mention: str, options: PromptOptions) -> ContentBlock | None:
        """The content block for a mention that isn't a plain local file, if it resolves.

        A part of a file (section, symbol) or a file on a mount is embedded when the
        agent accepts embedded context, else linked (the agent may not be able to open
        it); directories are linked.
        """
        name, sep, rest = mention.partition(":")
        inner = f"{name}/{rest}" if sep and name in self.mounts else f"{WORKSPACE}/{mention}"
        try:
            if not self.fs.exists(inner):
                return None
            data = self._read(inner)
        except (OSError, ValueError):
            return None
        uri = f"{self.fs.protocol if isinstance(self.fs.protocol, str) else self.fs.protocol[0]}://{inner}"
        label = mention.rsplit("/", 1)[-1] or mention
        if data is None:
            return ResourceLink(name=label, uri=uri, mime_type="inode/directory")
        mime = guess_mime_name(label)
        if options.embedded_context and len(data) <= MAX_EMBED_BYTES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                return EmbeddedResource(
                    resource=TextResourceContents(text=text, uri=uri, mime_type=mime)
                )
        return ResourceLink(name=label, uri=uri, mime_type=mime, size=len(data))

    def _read(self, inner: str) -> bytes | None:
        """A node's text; ``None`` for a plain directory.

        A Markdown section with subsections reads as only its heading line, so it's
        put together with its subsections; a class reads as its whole source already.
        """
        if (
            inner.startswith(f"{WORKSPACE}/")
            and (local := self.cwd / inner.removeprefix(f"{WORKSPACE}/")).is_file()
        ):
            return local.read_bytes()  # a real file, even one the tree opens like a folder
        if not self.fs.isdir(inner):
            return self.fs.cat_file(inner)
        if not _inside_expanded(inner):
            return None
        own = self.fs.cat_file(inner)
        if ".md/" not in inner:
            return own
        children = (self._read(child) for child in self.fs.ls(inner, detail=False))
        parts = [own.rstrip(b"\n")]
        parts.extend(text.rstrip(b"\n") for text in children if text is not None)
        return b"\n\n".join(parts) + b"\n"


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz")


def opens_like_folder(name: str) -> bool:
    """Whether the tree opens a file of this name like a folder (archive, Markdown, code)."""
    return name.lower().endswith((*expanders(), *ARCHIVE_SUFFIXES))


def _inside_expanded(inner: str) -> bool:
    """Whether a path points inside a file the tree opened (section, symbol, member)."""
    return any(opens_like_folder(part) for part in inner.split("/"))


def guess_mime_name(name: str) -> str:
    """The MIME type for a node name (a section or symbol has none: plain text)."""
    from pathlib import Path

    return guess_mime(Path(name)) if "." in name else "text/plain"
