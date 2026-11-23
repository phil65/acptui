"""Completions for the prompt: ``/commands`` and ``@paths``."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import TYPE_CHECKING, Literal

from aclientp.schema.v2 import Command, Skill, TextCommandInput
from aclientp.schema.v2.base import is_set


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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


def completion_context(line: str, row: int) -> CompletionContext | None:
    """The completion context for the text before the cursor in line ``row``."""
    if row == 0 and (match := re.fullmatch(r"/(\S*)", line)):
        return CompletionContext("command", match.group(1), 1)
    if match := _PATH_TOKEN.search(line):
        return CompletionContext("path", match.group(1), match.start(1))
    return None


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
        completion = Completion(f"/{name}{hint}", f"{name} ", f"{command.description}{kind}")
        if name.lower().startswith(lowered):
            starts.append(completion)
        elif lowered in name.lower():
            contains.append(completion)
    return starts + contains


def _escape(path: str) -> str:
    return path.replace(" ", "\\ ")


def complete_path(token: str, cwd: Path, limit: int = MAX_PATH_COMPLETIONS) -> list[Completion]:
    """Files and directories inside ``cwd`` matching a partial path."""
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
