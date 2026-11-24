"""Recent sessions across agents, resumed from the picker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from acptui.config import Settings
from acptui.recent import RecentSession, RecentSessions
from acptui.ui.app import AcpTui
from acptui.ui.screens.dialogs import RecentSessionsScreen, time_ago
from acptui.ui.screens.picker import AgentPicker
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    from acptui.config import AgentSpec


def test_time_ago() -> None:
    assert time_ago(100, now=130) == "just now"
    assert time_ago(0, now=300) == "5 min ago"
    assert time_ago(0, now=7300) == "2 h ago"
    assert time_ago(0, now=3 * 86400) == "3 days ago"


def test_the_latest_entry_of_a_session_wins(tmp_path: Path) -> None:
    recent = RecentSessions(tmp_path / "sessions.jsonl")
    base = RecentSession("demo", "Demo", (), None, "s1", "/work")
    recent.record(base)
    recent.record(RecentSession("codex", "Codex", ("codex-acp",), None, "s2", "/other"))
    recent.record(RecentSession("demo", "Demo", (), None, "s1", "/work", title="Named"))
    recent.record(base)  # without a title: the known title stays
    sessions = recent.load()
    assert [s.session_id for s in sessions] == ["s1", "s2"]
    assert sessions[0].title == "Named"
    assert sessions[1].command == ("codex-acp",)


async def test_resume_a_recent_session_from_the_picker(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/acptui:rename Remembered")
        await until(pilot, lambda: screen.state is not None and screen.state.title == "Remembered")
        assert screen.state is not None
        session_id = screen.state.session_id
        await send(pilot, "/echo hi")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await app.action_quit()

    again = AcpTui(Settings(), workdir)
    async with again.run_test(size=SIZE) as pilot:
        assert isinstance(again.screen, AgentPicker)
        await pilot.press("r")
        assert isinstance(again.screen, RecentSessionsScreen)
        assert "Remembered" in again.screen_text()
        assert "Demo agent" in again.screen_text()
        await pilot.press("enter")
        await until(pilot, lambda: len(again.tabs) == 1)
        tab = next(iter(again.tabs.values()))
        assert tab.resume_id == session_id
        assert tab.cwd == workdir
        await again.action_quit()

    off = AcpTui(Settings(history=False), workdir, demo_spec)
    async with off.run_test(size=SIZE) as pilot:
        await connected(pilot, off)
        assert next(iter(off.tabs.values())).recent is None
