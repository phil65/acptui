"""Prompt completion of commands and paths."""

from __future__ import annotations

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
