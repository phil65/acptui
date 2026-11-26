"""Completions for the prompt: ``/commands`` and ``@paths``."""

from dataclasses import dataclass
import heapq
import os
import re
import time
from typing import TYPE_CHECKING, Literal

from aclientp.schema.v2 import Command, Skill, TextCommandInput
from aclientp.schema.v2.base import is_set
import frizbee
import ripgrep_rs


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from aclientp.schema.v2 import AvailableCommand
    from frizbee import MatchIndices


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

    highlights: tuple[int, ...] = ()
    """Indices of ``label`` characters to highlight (what a fuzzy search matched)."""


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


MAX_INDEXED_FILES = 100_000
"""Upper bound of the fuzzy file index (a huge tree must not stall the prompt)."""

INDEX_TTL = 10.0
"""Seconds a file index is reused before the workspace is listed again."""

NAME_BONUS = 24
"""How much matched characters in the file name count (see :func:`fuzzy_paths`)."""


@dataclass
class FileIndex:
    """The workspace files for the fuzzy search, kept in Rust memory for frizbee."""

    root: Path
    paths: list[str]
    """Workspace-relative, ``/``-separated."""
    name_starts: list[int]
    """Where each path's file name starts."""
    stem_lengths: list[int]
    """Length of each file name without its extension (at least 1)."""
    haystacks: frizbee.Haystacks
    built: float


_indexes: dict[Path, FileIndex] = {}
_matcher = frizbee.Matcher("")
"""One matcher for every query: frizbee compiles a pattern once and reuses its memory."""


def list_files(root: Path, limit: int = MAX_INDEXED_FILES) -> list[str]:
    """The files under ``root`` the way ripgrep sees them.

    ``.gitignore`` (and ``.ignore``) rules apply, hidden files are left out, and so are
    :data:`IGNORED_DIRS` (also outside a git repository).
    """
    paths = ripgrep_rs.files(
        paths=[str(root)],
        relative_to=str(root),
        glob_not=sorted(IGNORED_DIRS),
        max_count=limit,
    )
    return [path.replace("\\", "/") for path in paths]


def _stem_length(name: str) -> int:
    dot = name.rfind(".")
    return max(dot if dot > 0 else len(name), 1)


def file_index(cwd: Path, *, refresh: bool = False) -> FileIndex:
    """The fuzzy file index of ``cwd``, cached for :data:`INDEX_TTL` seconds."""
    root = cwd.resolve()
    cached = _indexes.get(root)
    now = time.monotonic()
    if cached is not None and not refresh and now - cached.built < INDEX_TTL:
        return cached
    paths = list_files(root)
    starts = [path.rfind("/") + 1 for path in paths]
    index = FileIndex(
        root,
        paths,
        starts,
        [_stem_length(path[start:]) for path, start in zip(paths, starts, strict=True)],
        frizbee.Haystacks(paths),
        now,
    )
    _indexes[root] = index
    return index


@dataclass(frozen=True)
class FuzzyMatch:
    """A file the fuzzy search found."""

    path: str
    score: float
    positions: tuple[int, ...]
    """Indices of the matched characters in ``path``, ascending (for highlighting)."""


def max_typos(query: str) -> int:
    """Typos the search forgives: none in short queries, then one, then two."""
    return 0 if len(query) < 7 else 1 if len(query) < 12 else 2  # noqa: PLR2004


def fuzzy_paths(query: str, cwd: Path, limit: int = MAX_PATH_COMPLETIONS) -> list[FuzzyMatch]:
    """The workspace files that match ``query`` best (after toad's file picker).

    frizbee (Smith-Waterman, SIMD) scores the whole path, with a few typos forgiven and
    its query syntax (``^prefix``, ``suffix$``, ``'substring``, ``!not``). The file name
    counts most: matched characters that fall into the file name add a bonus that grows
    with how much of the name they cover, so ``tuiapp`` finds ``tui/app.py`` before
    ``tui_app/snapshots/long_name.snap``.
    """
    index = file_index(cwd)
    if not query or not index.paths:
        return []
    _matcher.set_config(max_typos=max_typos(query))
    _matcher.set_patterns(frizbee.Pattern.from_query(query))
    starts, stems, weight = index.name_starts, index.stem_lengths, NAME_BONUS / len(query)
    scored: list[tuple[float, int, MatchIndices]] = []
    for match in _matcher.match_list_indices(index.haystacks):
        i = match.index
        start = starts[i]
        in_name = 0
        for position in match.indices:  # descending: the file name's matches come first
            if position < start:
                break
            in_name += 1
        cover = in_name / stems[i] if in_name < stems[i] else 1.0
        scored.append((
            match.score + weight * in_name * in_name * cover,
            -len(index.paths[i]),
            match,
        ))
    best = heapq.nlargest(limit, scored, key=lambda item: (item[0], item[1]))
    return [
        FuzzyMatch(index.paths[m.index], score, tuple(reversed(m.indices))) for score, _, m in best
    ]


def complete_path(token: str, cwd: Path, limit: int = MAX_PATH_COMPLETIONS) -> list[Completion]:
    """Files and directories inside ``cwd`` matching a partial path.

    Entries of the typed directory come first; a token of two characters or more (unless
    it ends with ``/``: then a directory is browsed) also finds files anywhere in the
    workspace by fuzzy search, ``ui/app`` too (paths under ``ui/`` rank high).
    """
    direct = _complete_directory(token, cwd, limit)
    raw = token.replace("\\ ", " ")
    if len(raw) < 2 or raw.endswith("/"):  # noqa: PLR2004 - a directory is browsed
        return direct
    seen = {completion.insert for completion in direct}
    fuzzy = [
        Completion(match.path, _escape(match.path), "fuzzy match", highlights=match.positions)
        for match in fuzzy_paths(raw, cwd, limit)
        if _escape(match.path) not in seen
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
