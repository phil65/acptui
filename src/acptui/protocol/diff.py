"""Parsing ``git_patch`` text (``diff --git`` sections) into files, hunks and lines."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal


LineKind = Literal["context", "add", "remove"]

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    """One line of a hunk with its line numbers (``None`` on the side it is missing from)."""

    kind: LineKind
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    """A hunk: a run of changed lines with context."""

    old_start: int
    new_start: int
    header: str = ""
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    """The changes of one file."""

    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        """The path to show (the new one, or the old one for deletions)."""
        return self.new_path or self.old_path or "?"

    @property
    def added(self) -> int:
        """Number of added lines."""
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "add")

    @property
    def removed(self) -> int:
        """Number of removed lines."""
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "remove")


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path == "/dev/null":
        return None
    if path.startswith(('"a/', '"b/')):
        path = path[1:-1]
    if path.startswith(("a/", "b/")) and not path.startswith(("a//", "b//")):
        path = path[2:]
    return path


def parse_patch(text: str) -> list[FileDiff]:
    """Parse unified / git patch text. Unknown lines are skipped, never fatal."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    old_left = new_left = 0  # lines of the current hunk still to come, per side
    for raw in text.splitlines():
        if hunk is not None and (old_left > 0 or new_left > 0):
            kind = raw[:1]
            if kind == "+":
                hunk.lines.append(DiffLine("add", raw[1:], None, new_no))
                new_no += 1
                new_left -= 1
            elif kind == "-":
                hunk.lines.append(DiffLine("remove", raw[1:], old_no, None))
                old_no += 1
                old_left -= 1
            elif kind in {" ", ""}:
                hunk.lines.append(DiffLine("context", raw[1:], old_no, new_no))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            elif kind != "\\":  # anything but "\ No newline at end of file" ends the hunk
                old_left = new_left = 0
            if old_left > 0 or new_left > 0 or kind in {"+", "-", " ", "", "\\"}:
                continue
        if raw.startswith("diff --git "):
            parts = raw[len("diff --git ") :].split(" ")
            half = len(parts) // 2
            old = " ".join(parts[:half]) if half else None
            new = " ".join(parts[half:]) if half else None
            current = FileDiff(
                _strip_prefix(old) if old else None, _strip_prefix(new) if new else None
            )
            files.append(current)
            hunk = None
            continue
        if raw.startswith("--- "):
            if current is None or current.hunks:
                current = FileDiff(None, None)
                files.append(current)
            current.old_path = _strip_prefix(raw[4:].split("\t")[0])
            hunk = None
            continue
        if raw.startswith("+++ ") and current is not None:
            current.new_path = _strip_prefix(raw[4:].split("\t")[0])
            continue
        if raw.startswith("Binary files") and current is not None:
            current.binary = True
            continue
        if match := _HUNK.match(raw):
            if current is None:
                current = FileDiff(None, None)
                files.append(current)
            old_no, new_no = int(match.group(1)), int(match.group(3))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            hunk = Hunk(old_no, new_no, match.group(5).strip())
            current.hunks.append(hunk)
    return files


def side_by_side(hunk: Hunk) -> list[tuple[DiffLine | None, DiffLine | None]]:
    """Pair a hunk's lines for a split view: removals next to the additions replacing them."""
    rows: list[tuple[DiffLine | None, DiffLine | None]] = []
    removed: list[DiffLine] = []
    added: list[DiffLine] = []

    def flush() -> None:
        rows.extend(
            (
                removed[index] if index < len(removed) else None,
                added[index] if index < len(added) else None,
            )
            for index in range(max(len(removed), len(added)))
        )
        removed.clear()
        added.clear()

    for line in hunk.lines:
        if line.kind == "remove":
            if added:
                flush()
            removed.append(line)
        elif line.kind == "add":
            added.append(line)
        else:
            flush()
            rows.append((line, line))
    flush()
    return rows
