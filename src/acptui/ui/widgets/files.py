"""The files tree of the sidebar: the working directory and the mounts (see ``acptui.files``).

Files open like folders where upathtools can look inside (archives, Markdown by
section, source files by class / function). ``a`` mentions the highlighted node in
the prompt; files the agent touched in this session are marked.
"""

from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DirectoryTree
from upath import UPath

from acptui.files import WORKSPACE, opens_like_folder


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from rich.style import Style
    from textual.widgets._directory_tree import DirEntry
    from textual.widgets.tree import TreeNode

    from acptui.files import Files


TOUCHED = " ●"
OPENED_FILE = "📑 "
OPENED_FILE_EXPANDED = "📖 "


def _as_upath(path: str | Path | UPath) -> UPath:
    return path if isinstance(path, UPath) else UPath(path)


class FileTree(DirectoryTree):
    """A directory tree over any UPath (here: the union of workspace and mounts)."""

    # DirectoryTree wraps every path in PATH(...). UPath(upath) would rebuild the
    # filesystem from its protocol and lose this one (workspace + mounts), so existing
    # UPaths pass through. (DirectoryTree is typed for pathlib.Path; UPath isn't one.)
    PATH = cast("Callable[[str | Path], Path]", staticmethod(_as_upath))
    BINDINGS: ClassVar = [Binding("a", "mention", "Mention in prompt")]

    class Mention(Message):
        """Mention ``text`` (``@`` not included) in the prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, files: Files, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(cast("Path", files.root()), id=id)
        self.show_root = False  # the union's root has no name: workspace and mounts on top
        self.files = files
        self.touched: frozenset[str] = frozenset()
        """Workspace-relative paths the agent read or changed."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        hidden = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
        return [path for path in paths if path.name not in hidden]

    def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
        label = super().render_label(node, base_style, style)
        if node.data is not None and node.allow_expand and opens_like_folder(node.data.path.name):
            # A file the tree looks into (archive, sections, symbols): not a folder icon
            for folder_icon, file_icon in (
                (self.ICON_NODE_EXPANDED, OPENED_FILE_EXPANDED),
                (self.ICON_NODE, OPENED_FILE),
            ):
                if label.plain.startswith(folder_icon):
                    label = Text.assemble((file_icon, base_style), label[len(folder_icon) :])
                    break
        if node.data is not None and self._is_touched(cast("UPath", node.data.path)):
            label.append(TOUCHED, style="bold yellow")
        return label

    def _is_touched(self, path: UPath) -> bool:
        top, _, rest = path.path.lstrip("/").partition("/")
        if top != WORKSPACE or not rest:
            return False
        return any(touched == rest or touched.startswith(f"{rest}/") for touched in self.touched)

    def set_touched(self, paths: Iterable[str]) -> None:
        """Mark these workspace-relative paths (and their folders)."""
        touched = frozenset(paths)
        if touched != self.touched:
            self.touched = touched
            self.refresh()

    def action_mention(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None or node is self.root:
            return
        path = cast("UPath", node.data.path)
        if path.path.strip("/") == WORKSPACE:
            return  # the folder itself: nothing to mention
        self.post_message(self.Mention(self.files.mention(path)))
