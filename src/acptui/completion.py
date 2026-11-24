"""Completions for the prompt: ``/commands`` and ``@paths``."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Literal

from aclientp.schema.v2 import Command, Skill, TextCommandInput
from aclientp.schema.v2.base import is_set


if TYPE_CHECKING:
    from collections.abc import Sequence

    from aclientp.schema.v2 import AvailableCommand


IGNORED_DIRS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
})
MAX_PATH_COMPLETIONS = 50

CompletionKind = Literal["command", "path"]

_PATH_TOKEN = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s])*)$")


@dataclass(frozen=True)
class CompletionContext:
    """What is being completed: the token before the cursor and where it starts."""

    kind: CompletionKind
    token: str
    """The text typed so far (without the ``/`` or ``@``)."""

    start: int
    """Column of the token start (after the ``/`` or ``@``) in the cursor line."""


@dataclass(frozen=True)
class Completion:
    """A completion candidate."""

    label: str
    """Text shown in the list."""

    insert: str
    """Text that replaces the token."""

    detail: str = ""
    """Description shown next to the label."""

    markdown: bool = False
    """Whether ``detail`` is Markdown (command and skill descriptions are, in ACP v2)."""


def completion_context(line: str, row: int) -> CompletionContext | None:
    """The completion context for the text before the cursor in line ``row``."""
    if row == 0 and (match := re.fullmatch(r"/(\S*)", line)):
        return CompletionContext("command", match.group(1), 1)
    if match := _PATH_TOKEN.search(line):
        return CompletionContext("path", match.group(1), match.start(1))
    return None


def first_line(text: str) -> str:
    """The first non-empty line: a long description's (a docstring's) summary."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def complete_command(prefix: str, commands: Sequence[AvailableCommand]) -> list[Completion]:
    """Commands matching a prefix (then a substring), with their descriptions."""
    lowered = prefix.lower()
    starts: list[Completion] = []
    contains: list[Completion] = []
    for command in commands:
        name = command.name
        hint = ""
        if (
            isinstance(command, Command)
            and is_set(command.input)
            and isinstance(command.input, TextCommandInput)
        ):
            hint = f" <{command.input.hint}>"
        kind = " (skill)" if isinstance(command, Skill) else ""
        summary = first_line(command.description)
        completion = Completion(f"/{name}{hint}", f"{name} ", f"{summary}{kind}", markdown=True)
        if name.lower().startswith(lowered):
            starts.append(completion)
        elif lowered in name.lower():
            contains.append(completion)
    return starts + contains


def _escape(path: str) -> str:
    return path.replace(" ", "\\ ")


MAX_INDEXED_FILES = 20_000
"""The fuzzy file search looks at no more files than this."""

INDEX_TTL = 10.0
"""Seconds a file index is reused before the workspace is walked again."""

_indexes: dict[Path, tuple[float, list[str]]] = {}


def file_index(cwd: Path) -> list[str]:
    """Workspace-relative paths of the files under ``cwd``, cached for a while.

    Ignored and hidden folders are left out; the index lives :data:`INDEX_TTL` seconds.
    """
    root = cwd.resolve()
    cached = _indexes.get(root)
    now = time.monotonic()
    if cached is not None and now - cached[0] < INDEX_TTL:
        return cached[1]
    paths: list[str] = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith("."))
        base = Path(directory).relative_to(root)
        paths.extend((base / name).as_posix() for name in sorted(files))
        if len(paths) >= MAX_INDEXED_FILES:
            break
    _indexes[root] = (now, paths[:MAX_INDEXED_FILES])
    return _indexes[root][1]


def fuzzy_score(query: str, path: str) -> float | None:
    """How well ``query`` matches ``path`` as a subsequence (``None``: not at all).

    Matches in the file name, at word starts and in runs score higher; long paths a bit
    lower.
    """
    lowered = path.lower()
    name_start = lowered.rfind("/") + 1
    score = 0.0
    position = 0
    previous = -2
    for char in query.lower():
        index = lowered.find(char, position)
        if index < 0:
            return None
        score += 1.0
        if index == previous + 1:
            score += 2.0  # a run
        if index == 0 or lowered[index - 1] in "/_-. ":
            score += 1.5  # a word start
        if index >= name_start:
            score += 1.0  # in the file name
        previous = index
        position = index + 1
    return score - len(path) / 100


def fuzzy_paths(query: str, cwd: Path, limit: int = MAX_PATH_COMPLETIONS) -> list[str]:
    """The workspace files that match ``query`` best (fuzzy, after toad's file picker)."""
    scored = [
        (score, path) for path in file_index(cwd) if (score := fuzzy_score(query, path)) is not None
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:limit]]


def complete_path(token: str, cwd: Path, limit: int = MAX_PATH_COMPLETIONS) -> list[Completion]:
    """Files and directories inside ``cwd`` matching a partial path.

    Entries of the typed directory come first; a name of two characters or more (no
    ``/`` in it) also finds files anywhere in the workspace by fuzzy search.
    """
    direct = _complete_directory(token, cwd, limit)
    raw = token.replace("\\ ", " ")
    if len(raw) < 2 or "/" in raw:  # noqa: PLR2004
        return direct
    seen = {completion.insert for completion in direct}
    fuzzy = [
        Completion(path, _escape(path), "fuzzy match")
        for path in fuzzy_paths(raw, cwd, limit)
        if _escape(path) not in seen
    ]
    return (direct + fuzzy)[:limit]


def _complete_directory(token: str, cwd: Path, limit: int) -> list[Completion]:
    """Entries of the directory in ``token`` whose name matches its last part."""
    raw = token.replace("\\ ", " ")
    directory, _, name = raw.rpartition("/")
    base = (cwd / directory).resolve() if directory else cwd.resolve()
    if not base.is_relative_to(cwd.resolve()) or not base.is_dir():
        return []
    lowered = name.lower()
    starts: list[Completion] = []
    contains: list[Completion] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return []
    for entry in entries:
        if entry.name in IGNORED_DIRS or (entry.name.startswith(".") and not name.startswith(".")):
            continue
        is_dir = entry.is_dir()
        relative = f"{directory}/{entry.name}" if directory else entry.name
        insert = _escape(relative) + ("/" if is_dir else "")
        completion = Completion(
            relative + ("/" if is_dir else ""), insert, "directory" if is_dir else ""
        )
        if entry.name.lower().startswith(lowered):
            starts.append(completion)
        elif lowered and lowered in entry.name.lower():
            contains.append(completion)
        if len(starts) >= limit:
            break
    return (starts + contains)[:limit]
