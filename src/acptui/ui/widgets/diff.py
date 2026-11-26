"""Diff rendering for ``diff`` tool call content: unified or side by side, highlighted."""

from typing import TYPE_CHECKING, ClassVar

from aclientp.schema.v2 import (
    AddDiffChange,
    CopyDiffChange,
    DeleteDiffChange,
    ModifyDiffChange,
    MoveDiffChange,
)
from aclientp.schema.v2.base import is_set
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Resize  # noqa: TC002
from textual.highlight import highlight
from textual.reactive import reactive
from textual.widgets import Static

from acptui.config import DiffViewMode  # noqa: TC001
from acptui.protocol.content import relative_display
from acptui.protocol.diff import parse_patch, side_by_side


if TYPE_CHECKING:
    from aclientp.schema.v2 import Diff, DiffChange
    from textual.app import ComposeResult

    from acptui.protocol.diff import DiffLine, FileDiff, Hunk


SPLIT_MIN_WIDTH = 120
"""Width from which ``auto`` shows diffs side by side."""

PAD = 512
"""Lines are padded so their background fills the (clipped) width."""

LINE_STYLE = {"add": "on $success 15%", "remove": "on $error 15%", "context": ""}
SIGN = {"add": "+", "remove": "-", "context": " "}
SIGN_STYLE = {"add": "$success", "remove": "$error", "context": "$text-muted"}


def _highlighted_lines(lines: list[DiffLine], path: str) -> list[Content]:
    """Highlight a run of lines as one block of code (keeps multi-line context)."""
    if not lines:
        return []
    code = "\n".join(line.text for line in lines)
    try:
        rendered = highlight(code, path=path)
    except Exception:  # noqa: BLE001 - a lexer bug must not break the view
        rendered = Content(code)
    parts = rendered.split("\n")
    return [*parts, *[Content("")] * (len(lines) - len(parts))][: len(lines)]


def _hunk_content(hunk: Hunk, path: str) -> dict[int, Content]:
    """Highlighted code of each line of a hunk, by ``id`` of the line."""
    old_side = [line for line in hunk.lines if line.kind != "add"]
    new_side = [line for line in hunk.lines if line.kind != "remove"]
    result: dict[int, Content] = {}
    for line, content in zip(old_side, _highlighted_lines(old_side, path)):
        result[id(line)] = content
    for line, content in zip(new_side, _highlighted_lines(new_side, path)):
        result.setdefault(id(line), content)
    return result


def _number(value: int | None, width: int) -> Content:
    text = str(value) if value is not None else ""
    return Content(text.rjust(width) + " ").stylize("$text-muted")


def _row(line: DiffLine | None, code: dict[int, Content], width: int, *, both: bool) -> Content:
    """One rendered line: line number(s), sign, code; background by kind."""
    if line is None:
        return Content(" " * PAD).stylize("on $panel")
    numbers = (
        Content.assemble(_number(line.old_no, width), _number(line.new_no, width))
        if both
        else _number(line.new_no if line.kind == "add" else line.old_no, width)
    )
    sign = Content(SIGN[line.kind] + " ").stylize(SIGN_STYLE[line.kind])
    body = Content.assemble(numbers, sign, code.get(id(line), Content(line.text)))
    padded = body.pad_right(max(0, PAD - body.cell_length))
    style = LINE_STYLE[line.kind]
    return padded.stylize(style) if style else padded


def file_header(diff: FileDiff, operation: str, cwd: str) -> Content:
    """Header line of one file of a diff."""
    path = relative_display(diff.path, cwd)
    stats = Content.from_markup(
        f" [$success]+{diff.added}[/] [$error]-{diff.removed}[/]" if diff.hunks else ""
    )
    return Content.assemble((f"{operation} ", "bold $accent"), (path, "bold"), stats)


def unified_lines(diff: FileDiff) -> list[Content]:
    """The lines of a unified view of one file."""
    width = max(
        (
            len(str(n))
            for h in diff.hunks
            for line in h.lines
            for n in (line.old_no, line.new_no)
            if n
        ),
        default=1,
    )
    out: list[Content] = []
    for hunk in diff.hunks:
        code = _hunk_content(hunk, diff.path)
        out.append(
            Content(f"@@ -{hunk.old_start} +{hunk.new_start} @@ {hunk.header}").stylize(
                "$text-muted"
            )
        )
        out.extend(_row(line, code, width, both=True) for line in hunk.lines)
    return out


def split_lines(diff: FileDiff) -> tuple[list[Content], list[Content]]:
    """The left (old) and right (new) lines of a side-by-side view of one file."""
    width = max(
        (
            len(str(n))
            for h in diff.hunks
            for line in h.lines
            for n in (line.old_no, line.new_no)
            if n
        ),
        default=1,
    )
    left: list[Content] = []
    right: list[Content] = []
    for hunk in diff.hunks:
        code = _hunk_content(hunk, diff.path)
        marker = Content(f"@@ -{hunk.old_start} +{hunk.new_start} @@").stylize("$text-muted")
        left.append(marker)
        right.append(marker)
        for old, new in side_by_side(hunk):
            left.append(_row(old, code, width, both=False))
            right.append(_row(new, code, width, both=False))
    return left, right


def change_paths(change: DiffChange) -> tuple[str | None, str | None]:
    """``(old_path, path)`` of a file change; ``old_path`` only for moves and copies."""
    match change:
        case MoveDiffChange() | CopyDiffChange():
            return change.old_path, change.path
        case AddDiffChange() | DeleteDiffChange() | ModifyDiffChange():
            return None, change.path
        case _:
            path = getattr(change, "path", None)
            return None, path if isinstance(path, str) else None


class DiffView(Vertical):
    """A ``diff`` tool call content item."""

    DEFAULT_CSS = """
    DiffView {
        height: auto;
        margin: 0 0 1 0;
        .diff-header { height: auto; }
        .diff-lines {
            height: auto;
            text-wrap: nowrap;
            text-overflow: clip;
            overflow-x: hidden;
        }
        Horizontal { height: auto; }
        Horizontal > .diff-lines { width: 1fr; }
        Horizontal > .diff-lines.left { border-right: vkey $panel; }
    }
    """

    mode: reactive[DiffViewMode] = reactive("auto")
    split: reactive[bool] = reactive(False, recompose=True)

    OPERATIONS: ClassVar[dict[str, str]] = {
        "add": "Added",
        "delete": "Deleted",
        "modify": "Modified",
        "move": "Moved",
        "copy": "Copied",
    }

    def __init__(self, diff: Diff, cwd: str, mode: DiffViewMode = "auto") -> None:
        super().__init__()
        self.diff = diff
        self.cwd = cwd
        self.files: list[FileDiff] = []
        self.raw_patch: str | None = None
        if is_set(diff.patch) and diff.patch is not None:
            if diff.patch.format == "git_patch":
                self.files = parse_patch(diff.patch.text)
            else:  # a format acptui does not know: show the text as is
                self.raw_patch = diff.patch.text
        self.set_reactive(DiffView.mode, mode)
        self.set_reactive(DiffView.split, mode == "split")

    def compose(self) -> ComposeResult:
        if not self.files:
            for change in self.diff.changes:
                operation = self.OPERATIONS.get(change.operation, change.operation.title())
                old, new = change_paths(change)
                path = relative_display(new or "?", self.cwd)
                source = f"{relative_display(old, self.cwd)} → " if old else ""
                file_type = (
                    change.file_type if is_set(change.file_type) and change.file_type else None
                )
                kind = f" ({file_type})" if file_type and file_type != "text" else ""
                yield Static(
                    Content.assemble(
                        (f"{operation} ", "bold $accent"),
                        source,
                        (path, "bold"),
                        (kind, "$text-muted"),
                    ),
                    classes="diff-header",
                )
            if self.raw_patch is not None:
                yield Static(Content(self.raw_patch), classes="diff-lines unified")
            return
        operations = {change_paths(c)[1]: c.operation for c in self.diff.changes}
        for file in self.files:
            path = file.new_path or file.old_path or ""
            op = operations.get(path) or (
                "add" if file.old_path is None else "delete" if file.new_path is None else "modify"
            )
            yield Static(
                file_header(file, self.OPERATIONS.get(op, op.title()), self.cwd),
                classes="diff-header",
            )
            if file.binary:
                yield Static("(binary file)", classes="diff-lines")
            elif self.split:
                left, right = split_lines(file)
                with Horizontal():
                    yield Static(Content("\n").join(left), classes="diff-lines left")
                    yield Static(Content("\n").join(right), classes="diff-lines right")
            else:
                lines = Content("\n").join(unified_lines(file))
                yield Static(lines, classes="diff-lines unified")

    def watch_mode(self, mode: DiffViewMode) -> None:
        self._update_split()

    def on_resize(self, event: Resize) -> None:
        self._update_split()

    def _update_split(self) -> None:
        if self.mode == "auto":
            self.split = self.size.width >= SPLIT_MIN_WIDTH
        else:
            self.split = self.mode == "split"
