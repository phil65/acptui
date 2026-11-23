from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2 import EmbeddedResource, ResourceLink, TextResourceContents
import pytest

from acptui.config import Settings
from acptui.files import Files, opens_like_folder
from acptui.protocol.prompt import PromptOptions, build_prompt
from acptui.ui.app import AcpTui
from acptui.ui.widgets.files import FilesPanel, FileTree
from tests.test_app import SIZE, connected, until


if TYPE_CHECKING:
    from pathlib import Path

    from acptui.config import AgentSpec


EMBED = PromptOptions(embedded_context=True)
GUIDE = "# Guide\n\nIntro.\n\n## Setup\n\nInstall it.\n\n## Use\n\nRun it.\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (root / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return root


@pytest.fixture
def mount(tmp_path: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "todo.txt").write_text("buy milk\n", encoding="utf-8")
    return notes


def embedded_text(block: object) -> str:
    assert isinstance(block, EmbeddedResource)
    assert isinstance(block.resource, TextResourceContents)
    return block.resource.text


def test_tree_has_the_workspace_and_the_mounts(project: Path, mount: Path) -> None:
    files = Files(project, {"notes": str(mount)})
    assert sorted(path.name for path in files.root().iterdir()) == ["notes", "workspace"]
    workspace = files.root() / "workspace"
    assert sorted(path.name for path in workspace.iterdir()) == ["docs", "main.py"]
    guide = workspace / "docs" / "guide.md"
    assert guide.is_dir()  # a Markdown file opens by section
    assert opens_like_folder("guide.md")
    assert not opens_like_folder("notes.txt")


def test_mentions(project: Path, mount: Path) -> None:
    files = Files(project, {"notes": str(mount)})
    root = files.root()
    assert files.mention(root / "workspace" / "docs" / "guide.md") == "docs/guide.md"
    assert files.mention(root / "notes" / "todo.txt") == "notes:todo.txt"


def test_sections_symbols_and_mounts_are_embedded(project: Path, mount: Path) -> None:
    files = Files(project, {"notes": str(mount)})
    section = files.mention_block("docs/guide.md/Guide/Setup", EMBED)
    assert embedded_text(section).startswith("## Setup")
    whole = embedded_text(files.mention_block("docs/guide.md/Guide", EMBED))
    assert "## Setup" in whole  # a section with subsections reads with them
    assert "## Use" in whole
    assert embedded_text(files.mention_block("notes:todo.txt", EMBED)) == "buy milk\n"
    assert files.mention_block("docs/missing.md", EMBED) is None
    folder = files.mention_block("docs", EMBED)
    assert isinstance(folder, ResourceLink)
    assert folder.mime_type == "inode/directory"


def test_without_embedded_context_parts_are_linked(project: Path) -> None:
    block = Files(project).mention_block("docs/guide.md/Guide/Setup", PromptOptions())
    assert isinstance(block, ResourceLink)


def test_build_prompt_resolves_mentions_that_arent_files(project: Path) -> None:
    files = Files(project)
    blocks = build_prompt(
        "explain @docs/guide.md/Guide/Use and @main.py", project, EMBED, files.mention_block
    )
    assert len(blocks) == 3
    assert embedded_text(blocks[1]).startswith("## Use")
    assert embedded_text(blocks[2]).startswith("def main")


async def test_panel_mentions_into_the_prompt(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await pilot.press("ctrl+b")
        panel = screen.query_one(FilesPanel)
        assert panel.open
        tree = screen.query_one(FileTree)
        await until(pilot, lambda: bool(tree.root.children))
        workspace = tree.root.children[0]
        workspace.expand()
        await until(pilot, lambda: bool(workspace.children))
        readme = next(node for node in workspace.children if str(node.label) == "README.md")
        tree.move_cursor(readme)
        await pilot.press("a")
        assert screen.prompt.text == "@README.md "
        assert screen.prompt.has_focus
        await pilot.press("ctrl+b")
        assert not panel.open


async def test_touched_files_are_marked(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await pilot.press("ctrl+b")
        tree = screen.query_one(FileTree)
        tree.set_touched({"src/main.py"})
        await until(pilot, lambda: bool(tree.root.children))
        workspace = tree.root.children[0]
        workspace.expand()
        await until(pilot, lambda: bool(workspace.children))
        src = next(node for node in workspace.children if str(node.label) == "src")
        assert tree.render_label(src, tree.rich_style, tree.rich_style).plain.endswith("●")
