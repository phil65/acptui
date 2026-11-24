"""The settings screen, settings saved to config.toml, and the settings it adds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import OptionList

from acptui.config import Settings, load_settings
from acptui.ui.app import AcpTui
from acptui.ui.screens.settings import FIELDS, SettingsScreen, choices, save_setting
from acptui.ui.widgets.blocks import ToolCallView
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from acptui.config import AgentSpec


def test_every_field_has_choices() -> None:
    for field in FIELDS:
        if field.key != "theme":
            assert len(choices(field.key)) >= 2, field.key
    assert choices("diff_view") == ("unified", "split", "auto")
    assert choices("notifications.sounds") == (True, False)


def test_saving_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '# my config\ntheme = "nord"  # dark\n\n[agents.mine]\ncommand = ["mine"]\n',
        encoding="utf-8",
    )
    save_setting(path, "theme", "gruvbox")
    save_setting(path, "notifications.sounds", False)
    save_setting(path, "tabs", "always")
    text = path.read_text(encoding="utf-8")
    assert text.startswith('# my config\ntheme = "gruvbox"  # dark\n')
    assert 'command = ["mine"]' in text
    settings = load_settings(path)
    assert settings.theme == "gruvbox"
    assert settings.tabs == "always"
    assert not settings.notifications.sounds
    assert settings.find_agent("mine") is not None


async def test_settings_screen(demo_spec: AgentSpec, workdir: Path, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    app = AcpTui(Settings(), workdir, demo_spec, config_file=path)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await pilot.press("f3")
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("down", "enter")  # diff view: auto -> unified
        assert app.settings.diff_view == "unified"
        assert screen.diff_mode == "unified"
        assert load_settings(path).diff_view == "unified"

        await pilot.press("up", "enter")  # the theme list
        assert "nord" in app.screen_text()
        settings_screen = app.screen
        assert isinstance(settings_screen, SettingsScreen)
        options = settings_screen.query_one("#settings", OptionList)
        index = next(i for i, theme in enumerate(settings_screen.themes) if theme == "nord")
        options.highlighted = index
        await pilot.press("enter")
        assert app.theme == "nord"
        assert load_settings(path).theme == "nord"

        tool = next(i for i, field in enumerate(FIELDS) if field.key == "tool_expand")
        options.highlighted = tool
        await pilot.press("enter", "enter")  # auto -> always -> never
        assert app.settings.tool_expand == "never"
        await pilot.press("escape")
        assert app.screen is screen


async def test_tool_calls_follow_the_expand_setting(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(tool_expand="never"), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/media")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await until(pilot, lambda: bool(screen.query(ToolCallView)))
        assert not any(tool.expanded for tool in screen.query(ToolCallView))


async def test_auto_copy(
    demo_spec: AgentSpec, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []
    app = AcpTui(Settings(auto_copy=True), workdir, demo_spec)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        monkeypatch.setattr(screen, "get_selected_text", lambda: "selected words")
        app.on_text_selected()
        assert copied == ["selected words"]
