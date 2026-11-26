"""Prompt completion of commands and paths."""

from typing import TYPE_CHECKING

from aclientp.schema.v2 import Command, Skill, TextCommandInput

from acptui.completion import complete_command, complete_path, completion_context, first_line


if TYPE_CHECKING:
    from pathlib import Path


def test_completion_context() -> None:
    command = completion_context("/pl", 0)
    path = completion_context("look at @src/ma", 0)

    assert command is not None
    assert (command.kind, command.token, command.start) == ("command", "pl", 1)
    assert path is not None
    assert (path.kind, path.token, path.start) == ("path", "src/ma", 9)
    assert completion_context("/pl", 1) is None  # commands only at the start
    assert completion_context("/plan now", 0) is None
    assert completion_context("mail me@example", 0) is None


def test_complete_command() -> None:
    commands = [
        Command(name="plan", description="Make a plan"),
        Command(name="explain", description="Explain", input=TextCommandInput(hint="what")),
        Skill(name="deploy-plan", description="Deploy"),
    ]

    result = complete_command("pl", commands)

    assert [c.label for c in result] == ["/plan", "/explain <what>", "/deploy-plan"]
    assert result[0].insert == "plan "
    assert result[2].detail == "Deploy (skill)"
    assert complete_command("ex", commands)[0].label == "/explain <what>"


def test_command_descriptions_show_their_first_line() -> None:
    docstring = "\nClear the history.\n\nKeeps tools and settings.\n"
    [completion] = complete_command("cl", [Command(name="clear", description=docstring)])
    assert completion.detail == "Clear the history."


def test_complete_path(workdir: Path) -> None:
    top = complete_path("", workdir)
    nested = complete_path("src/m", workdir)
    spaced = complete_path("no", workdir)

    assert [c.label for c in top] == ["src/", "image.png", "notes file.txt", "README.md"]
    assert [c.insert for c in nested] == ["src/main.py"]
    assert spaced[0].insert == "notes\\ file.txt"
    assert complete_path("../", workdir) == []


def test_first_line() -> None:
    assert first_line("\n  Summary.\n\nMore.") == "Summary."
    assert first_line("") == ""


def _tree(root: Path, paths: list[str]) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("", encoding="utf-8")


def test_file_index_follows_gitignore_and_skips_hidden(tmp_path: Path) -> None:
    from acptui.completion import file_index

    _tree(
        tmp_path,
        [
            "src/deep/conversation_view.py",
            ".git/config",
            ".hidden/secret.txt",
            "node_modules/pkg/index.js",
            "build/out.bin",
            "keep.txt",
        ],
    )
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    paths = file_index(tmp_path, refresh=True).paths
    assert "src/deep/conversation_view.py" in paths
    assert "keep.txt" in paths
    assert not [p for p in paths if p.startswith((".git", ".hidden", "node_modules", "build"))]
    assert all("\\" not in p for p in paths)


def _ranked(query: str, root: Path) -> list[str]:
    from acptui.completion import fuzzy_paths

    return [match.path for match in fuzzy_paths(query, root)]


def test_fuzzy_ranking_puts_the_file_name_first(tmp_path: Path) -> None:
    from acptui.completion import file_index

    _tree(
        tmp_path,
        [
            "tui/app.py",
            "tui/app_event.py",
            "tui_app_server/snapshots/tui_app_server__app__tests__agent_picker.snap",
            "tui_app_server/snapshots/tui_app_server__diff__tests__apply.snap",
            "core/connectors.py",
            "core/connection.py",
            "core/tests/connectors_tests.py",
            "ui/app.py",
            "web/ui_helpers/apply.py",
            "src/main.py",
            "main_stuff/other/thing.py",
        ],
    )
    file_index(tmp_path, refresh=True)
    assert _ranked("tuiapp", tmp_path)[0] == "tui/app.py"
    assert _ranked("conectors", tmp_path)[0] == "core/connectors.py"  # a typo
    assert _ranked("ui/app", tmp_path)[:2] == ["ui/app.py", "tui/app.py"]
    assert _ranked("main", tmp_path)[0] == "src/main.py"
    assert _ranked("xyzq", tmp_path) == []
    assert _ranked("^core", tmp_path)[0].startswith("core/")  # frizbee's query syntax


def test_fuzzy_completion_highlights_the_match(workdir: Path) -> None:
    from acptui.completion import file_index
    from acptui.ui.widgets.prompt import completion_prompt

    _tree(workdir, ["src/deep/conversation_view.py"])
    file_index(workdir, refresh=True)
    found = complete_path("convview", workdir)
    assert found[0].insert == "src/deep/conversation_view.py"
    assert found[0].detail == "fuzzy match"
    label = found[0].label
    assert "".join(label[i] for i in found[0].highlights) == "convview"
    styled = {
        span.start for span in completion_prompt(found[0]).spans if "accent" in str(span.style)
    }
    assert set(found[0].highlights) <= styled
    browsed = complete_path("src/", workdir)
    assert browsed[0].label == "src/deep/"
    assert all(c.detail != "fuzzy match" for c in browsed)  # browsing a directory: no fuzzy
    qualified = complete_path("deep/conv", workdir)
    assert qualified[0].insert == "src/deep/conversation_view.py"
