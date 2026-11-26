"""The sidebar (plan and files), the compact plan in the conversation, layout toggles."""

from typing import TYPE_CHECKING

from aclientp.schema.v2 import PlanEntry as SchemaPlanEntry, PlanItems, PlanMarkdown
from textual.widgets import OptionList

from acptui.config import Settings, load_settings
from acptui.protocol.state import PlanEntry
from acptui.ui.app import AcpTui
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.screens.settings import FIELDS, SettingsScreen
from acptui.ui.widgets.blocks import PlanView, plan_summary
from acptui.ui.widgets.files import FileTree
from acptui.ui.widgets.sidebar import SideBar
from tests.test_app import connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot

    from acptui.config import AgentSpec
    from acptui.ui.screens.session import SessionScreen


WIDE = (140, 40)
NARROW = (80, 24)


def items(*statuses: str) -> PlanEntry:
    entries = [
        SchemaPlanEntry(content=f"step **{i}**", priority="medium", status=status)
        for i, status in enumerate(statuses, 1)
    ]
    return PlanEntry("p", PlanItems(plan_id="p", entries=entries))


def test_plan_summary_names_the_current_entry() -> None:
    summary = plan_summary(items("completed", "in_progress", "pending")).plain
    assert summary == "plan 1/3  ◐ step 2"  # Markdown rendered: no asterisks
    assert plan_summary(items("completed", "pending")).plain == "plan 1/2  ○ step 2"
    assert plan_summary(items("completed", "completed")).plain == "plan 2/2  done"
    markdown = PlanEntry("m", PlanMarkdown(plan_id="m", content="# Plan\n\n1. Think"))
    assert plan_summary(markdown).plain == "plan Plan"
    with_path = PlanMarkdown(plan_id="m", content="1. Think", path="/work/PLAN.md")
    assert plan_summary(PlanEntry("m", with_path)).plain == "plan PLAN.md"


async def tour_until_plan(pilot: Pilot[None], screen: SessionScreen) -> None:
    await send(pilot, "show me the tour")
    await until(pilot, lambda: bool(screen.state and screen.state.plans))


async def finish_tour(pilot: Pilot[None], screen: SessionScreen) -> None:
    app = pilot.app
    await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
    await pilot.press("1")
    await until(pilot, lambda: stop_reason(screen) == "end_turn")
    await pilot.pause(0.3)


async def test_sidebar_opens_for_a_plan_on_wide_terminals(
    demo_spec: AgentSpec, workdir: Path
) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=WIDE) as pilot:
        screen = await connected(pilot, app)
        sidebar = screen.query_one(SideBar)
        assert not sidebar.open
        await tour_until_plan(pilot, screen)
        await until(pilot, lambda: sidebar.open)
        assert screen.has_class("-sidebar-open")
        await finish_tour(pilot, screen)
        [view] = sidebar.plans.views
        assert view.border_subtitle == "3/3"  # live: every update reached the sidebar
        assert "Propose an edit to README.md" in str(view.items.render())
        assert view.items.display

        inline = screen.conversation.query_one(PlanView)  # one summary line meanwhile
        assert inline.summary.display
        assert not inline.items.display
        assert inline.outer_size.height == 1
        assert "plan 3/3" in str(inline.summary.render())

        await pilot.press("ctrl+b")  # hide: the full plan is back in the conversation
        assert not sidebar.open
        await pilot.pause(0.1)
        assert inline.items.display
        assert not inline.summary.display
        assert screen.prompt.has_focus


async def test_narrow_terminal_keeps_the_layout(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=NARROW) as pilot:
        screen = await connected(pilot, app)
        sidebar = screen.query_one(SideBar)
        await tour_until_plan(pilot, screen)
        await finish_tour(pilot, screen)
        assert not sidebar.open  # too narrow to open by itself

        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        assert sidebar.open
        assert screen.query_one(FileTree).has_focus
        assert sidebar.region.width <= 36  # at most 45% of the width
        conversation = screen.conversation.region
        prompt = screen.prompt.region
        assert conversation.x >= sidebar.region.right
        assert conversation.width >= 40
        assert prompt.x >= sidebar.region.right
        assert prompt.height >= 1
        assert prompt.bottom <= NARROW[1]
        assert screen.status.region.bottom <= NARROW[1]
        assert len(sidebar.plans.views) == 1

        await pilot.press("escape")  # back to the prompt, the sidebar stays
        assert screen.prompt.has_focus
        assert sidebar.open
        await pilot.press("ctrl+b")
        assert not sidebar.open


async def test_closed_sidebar_does_not_reopen(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=WIDE) as pilot:
        screen = await connected(pilot, app)
        sidebar = screen.query_one(SideBar)
        await pilot.press("ctrl+b", "ctrl+b")  # opened and closed by hand: stays closed
        screen.prompt.focus()
        await tour_until_plan(pilot, screen)
        await finish_tour(pilot, screen)
        assert not sidebar.open
        assert len(sidebar.plans.views) == 1  # kept up to date while hidden


async def test_sidebar_setting(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(sidebar="closed"), workdir, demo_spec)
    async with app.run_test(size=WIDE) as pilot:
        screen = await connected(pilot, app)
        await tour_until_plan(pilot, screen)
        await finish_tour(pilot, screen)
        assert not screen.query_one(SideBar).open

    app = AcpTui(Settings(sidebar="open"), workdir, demo_spec)
    async with app.run_test(size=NARROW) as pilot:
        screen = await connected(pilot, app)
        sidebar = screen.query_one(SideBar)
        assert sidebar.open
        assert screen.prompt.has_focus  # opened at start, the focus stays in the prompt
        assert "No plan yet" in app.screen_text()


async def test_markdown_plans_and_removed_plans(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(sidebar="open"), workdir, demo_spec)
    async with app.run_test(size=WIDE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/plans")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await pilot.pause(0.3)
        sidebar = screen.query_one(SideBar)
        [view] = sidebar.plans.views  # the item plan was removed again
        assert str(view.border_subtitle).endswith("PLAN.md")
        assert view.markdown.display
        assert view.markdown.source == "1. Think\n2. Act"
        assert "No plan yet" not in app.screen_text()

        await send(pilot, "/acptui:new")
        await until(pilot, lambda: screen.state is not None and not screen.state.entries)
        await pilot.pause(0.1)
        assert not sidebar.plans.views  # a new session has no plan


async def test_compact_prompt_and_no_status_line(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=NARROW) as pilot:
        screen = await connected(pilot, app)
        assert screen.prompt.outer_size.height == 3
        assert screen.status.display
        conversation_height = screen.conversation.outer_size.height

        app.settings = app.settings.model_copy(update={"compact_input": True, "status_line": False})
        app.apply_layout()
        await pilot.pause(0.1)
        assert screen.has_class("-compact-input")
        assert screen.prompt.outer_size.height == 1
        assert not screen.status.display
        assert screen.conversation.outer_size.height == conversation_height + 3


async def test_layout_settings_from_the_settings_screen(
    demo_spec: AgentSpec, workdir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    app = AcpTui(Settings(), workdir, demo_spec, config_file=path)
    async with app.run_test(size=WIDE) as pilot:
        screen = await connected(pilot, app)
        await pilot.press("f3")
        settings_screen = app.screen
        assert isinstance(settings_screen, SettingsScreen)
        options = settings_screen.query_one("#settings", OptionList)
        for key in ("compact_input", "status_line"):
            options.highlighted = next(i for i, f in enumerate(FIELDS) if f.key == key)
            await pilot.press("enter")
        options.highlighted = next(i for i, f in enumerate(FIELDS) if f.key == "sidebar")
        await pilot.press("enter", "enter")  # auto -> open -> closed
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert screen.has_class("-compact-input")
        assert not screen.status.display
        assert screen.sidebar_mode == "closed"
        saved = load_settings(path)
        assert saved.compact_input
        assert not saved.status_line
        assert saved.sidebar == "closed"
        await tour_until_plan(pilot, screen)
        await finish_tour(pilot, screen)
        assert not screen.query_one(SideBar).open
