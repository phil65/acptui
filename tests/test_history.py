"""The prompt history file."""

from __future__ import annotations

from typing import TYPE_CHECKING

from acptui import history as history_module
from acptui.config import Settings
from acptui.history import History
from acptui.ui.app import AcpTui
from tests.test_app import SIZE, connected, send, session, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


def test_history_file_round_trip(tmp_path: Path) -> None:
    history = History(tmp_path / "h" / "history.jsonl")
    assert history.load() == []
    assert history.append("first", cwd="/a")
    assert history.append("first")
    assert history.append("ls -la", shell=True)
    with (tmp_path / "h" / "history.jsonl").open("a", encoding="utf-8") as file:
        file.write("not json\n")
    assert history.texts(shell=False) == ["first"]  # repeats in a row collapse
    assert history.texts(shell=True) == ["ls -la"]
    assert history.load()[0].cwd == "/a"


def test_history_is_trimmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_module, "MAX_ENTRIES", 3)
    history = History(tmp_path / "history.jsonl")
    for index in range(7):
        history.append(f"p{index}")
    assert history.texts(shell=False) == ["p4", "p5", "p6"]


async def test_history_outlives_the_app(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/echo remember this")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await app.action_quit()

    again = AcpTui(Settings(), workdir, demo_spec)
    async with again.run_test(size=SIZE) as pilot:
        await connected(pilot, again)
        prompt = session(again).prompt
        prompt.focus()
        await pilot.press("up")
        assert prompt.text == "/echo remember this"
        await again.action_quit()

    off = AcpTui(Settings(history=False), workdir, demo_spec)
    async with off.run_test(size=SIZE) as pilot:
        await connected(pilot, off)
        assert session(off).prompt.prompt_history == []
