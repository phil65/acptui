"""Regenerate the SVG screenshots in docs/images with the demo agent.

Run: uv run python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile

from acptui.config import BUILTIN_AGENTS, Settings
from acptui.ui.app import AcpTui
from acptui.ui.screens.dialogs import ConfigScreen
from acptui.ui.screens.elicitation import FormElicitationScreen
from acptui.ui.screens.permission import PermissionScreen
from acptui.ui.screens.picker import AgentPicker
from acptui.ui.screens.session import SessionScreen
from acptui.ui.widgets.blocks import PlanView, SubagentView
from acptui.ui.widgets.image import ImageView


OUT = Path(__file__).parent.parent / "docs" / "images"
SIZE = (100, 36)
PROJECT_README = "# my-project\n\nA small example project.\n"


async def wait(pilot: object, condition: object, timeout: float = 20) -> None:
    """Let the app run until ``condition()`` holds."""
    assert callable(condition)
    pause = pilot.pause
    async with asyncio.timeout(timeout):
        while not condition():
            await pause(0.05)


async def session_shots(project: Path) -> None:
    """Screens of a demo session."""
    spec = next(s for s in BUILTIN_AGENTS if s.id == "demo")
    app = AcpTui(Settings(theme="tokyo-night", images="halfcell", tabs="always"), project, spec)
    async with app.run_test(size=SIZE) as pilot:
        await wait(pilot, lambda: isinstance(app.screen, SessionScreen) and bool(app.screen.state))
        screen = app.screen
        assert isinstance(screen, SessionScreen)

        async def send(text: str) -> None:
            assert screen.state is not None
            screen.state.stop_reason = None
            screen.prompt.focus()
            screen.prompt.load_text(text)
            screen.prompt.move_cursor(screen.prompt.document.end)
            await pilot.press("enter")

        def done() -> bool:
            return screen.state is not None and screen.state.stop_reason is not None

        await send("Give me the tour of @README.md")
        await wait(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.pause(0.5)
        app.save_screenshot("permission.svg", str(OUT))
        await pilot.press("1")
        await wait(pilot, done)
        await pilot.pause(1)
        plan = screen.query_one(PlanView)
        screen.conversation.scroll_to_widget(plan, animate=False, top=True)
        await pilot.pause(0.3)
        app.save_screenshot("session.svg", str(OUT))

        await send("/subagent")
        await wait(pilot, done)
        await pilot.pause(0.5)
        screen.query_one(SubagentView).focus()
        await pilot.pause(0.3)
        app.save_screenshot("subagent.svg", str(OUT))

        await send("/elicit")
        await wait(pilot, lambda: isinstance(app.screen, FormElicitationScreen))
        await pilot.pause(0.5)
        app.save_screenshot("elicitation.svg", str(OUT))
        await pilot.press("escape")
        await wait(pilot, done)

        await send("/media")
        await wait(pilot, done)
        await pilot.pause(0.5)
        image = screen.query(ImageView).first()
        screen.conversation.scroll_to_widget(image, animate=False, top=True)
        await pilot.pause(0.3)
        app.save_screenshot("images.svg", str(OUT))

        await pilot.press("ctrl+o")
        await wait(pilot, lambda: isinstance(app.screen, ConfigScreen))
        await pilot.pause(0.3)
        app.save_screenshot("options.svg", str(OUT))
        await pilot.press("escape")

        await pilot.press("ctrl+t")
        app.screen.post_message(AgentPicker.Picked(spec, str(project)))
        await wait(pilot, lambda: len(app.tabs) > 1 and app.screen is not screen)
        other = app.screen
        assert isinstance(other, SessionScreen)
        await wait(pilot, lambda: other.state is not None and other.connected)
        other.prompt.load_text("/slow")
        other.prompt.move_cursor(other.prompt.document.end)
        await pilot.press("enter")
        await pilot.pause(0.6)
        app.save_screenshot("tabs.svg", str(OUT))
        await app.action_quit()


async def picker_shot(project: Path) -> None:
    """The agent picker."""
    app = AcpTui(Settings(theme="tokyo-night"), project)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        app.save_screenshot("picker.svg", str(OUT))


def main() -> None:
    """Write the screenshots."""
    os.environ["ACPTUI_DEMO_DELAY"] = "0.001"
    os.environ["ACPTUI_HISTORY"] = os.devnull  # no history written, none shown
    OUT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp())
    project = root / "my-project"
    project.mkdir()
    (project / "README.md").write_text(PROJECT_README, encoding="utf-8")
    try:
        asyncio.run(session_shots(project))
        asyncio.run(picker_shot(project))
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
