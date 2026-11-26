"""Session tabs: several agents at once, each in its own Textual mode."""

from typing import TYPE_CHECKING

from textual.widgets import Input

from acptui.config import Settings
from acptui.ui.app import AcpTui
from acptui.ui.screens.dialogs import TabsScreen
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.widgets.tabs import SessionTabs, TabInfo, tab_content
from tests.test_app import SIZE, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from textual.pilot import Pilot

    from acptui.config import AgentSpec
    from acptui.ui.screens.session import SessionScreen


def test_tab_content_marks_the_active_tab() -> None:
    tab = TabInfo("session-1", "Demo agent: a very long session title indeed", "waiting", True)
    content = tab_content(1, tab)
    assert content.plain.startswith(" 1 ? Demo agent: a very long ses")
    assert content.plain.endswith("… ")


async def ready(pilot: Pilot[None], screen: SessionScreen) -> None:
    await until(pilot, lambda: screen.state is not None and screen.connected)


async def test_tabs_run_sessions_side_by_side(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        first = app.tabs["session-1"]
        await ready(pilot, first)
        assert not first.query_one(SessionTabs).has_class("-visible")  # one tab: no bar

        await pilot.press("ctrl+t")
        assert isinstance(app.screen, AgentPicker)
        await pilot.press("escape")  # back to the tab
        assert app.screen is first
        await pilot.press("ctrl+t")
        app.screen.post_message(AgentPicker.Picked(demo_spec, str(workdir / "src")))
        await until(pilot, lambda: len(app.tabs) == 2)
        second = app.tabs["session-2"]
        await ready(pilot, second)
        assert app.screen is second
        assert second.cwd == workdir / "src"
        assert first.connection is not second.connection  # an agent per tab
        bar = second.query_one(SessionTabs)
        await until(pilot, lambda: bar.has_class("-visible") and len(bar.tabs) == 2)
        assert [t.active for t in bar.tabs] == [False, True]

        await pilot.press("alt+left")
        assert app.screen is first
        await pilot.press("alt+right")
        assert app.screen is second

        await pilot.press("ctrl+s")
        assert isinstance(app.screen, TabsScreen)
        await pilot.press("up", "enter")
        assert app.screen is first

        await pilot.press("alt+w")
        await until(pilot, lambda: list(app.tabs) == ["session-2"])
        assert app.screen is second
        assert first.closing
        await pilot.press("alt+w")
        await until(pilot, lambda: not app.tabs)
        assert isinstance(app.screen, AgentPicker)


async def test_a_background_tab_waits_with_its_question(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPTUI_DEMO_DELAY", "0.05")  # time to look away before it asks
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        first = app.tabs["session-1"]
        await ready(pilot, first)
        second = app.open_tab(demo_spec)
        await ready(pilot, second)
        app.switch_tab("session-1")
        first.prompt.load_text("edit the readme")
        first.prompt.focus()
        await pilot.press("end", "enter")
        app.switch_tab("session-2")  # look away before the agent asks
        await until(pilot, lambda: first.waiting)
        await pilot.pause(0.2)
        assert app.screen is second  # the dialog waits for its tab
        assert first.tab_state == "waiting"
        assert app.alerts[-1].kind == "question"
        assert app.alerts[-1].outside  # not shown: notify outside the app
        bar = second.query_one(SessionTabs)
        await until(pilot, lambda: bar.tabs[0].state == "waiting")
        assert " 1 ? Demo agent" in str(bar.render())

        app.switch_tab("session-1")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.press("1")
        await until(
            pilot, lambda: first.state is not None and first.state.stop_reason == "end_turn"
        )


async def test_the_picker_changes_the_directory(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir)
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        await pilot.press("d")
        field = app.screen.query_one(Input)
        field.value = "src"
        await pilot.press("enter")
        assert picker.cwd == str((workdir / "src").resolve())
        await pilot.press("d")
        app.screen.query_one(Input).value = "nope"
        await pilot.press("enter")
        assert picker.cwd == str((workdir / "src").resolve())  # not a directory: unchanged
