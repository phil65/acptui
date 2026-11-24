"""Pinned agents: the picker's quick-launch list (``pinned_agents``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Button, OptionList

from acptui.config import Settings, load_settings
from acptui.ui.app import AcpTui
from acptui.ui.screens.agent import AgentDetailsScreen
from acptui.ui.screens.picker import AgentPicker
from tests.test_agents import _highlight
from tests.test_app import SIZE, until


if TYPE_CHECKING:
    from pathlib import Path


CONFIG = """\
# my settings
theme = "nord"   # a theme I like

[agents.mine]
name = "Mine"
command = ["mine", "--acp"]
"""


def option_ids(picker: AgentPicker) -> list[str | None]:
    options = picker.query_one("#agents", OptionList)
    return [options.get_option_at_index(i).id for i in range(options.option_count)]


async def test_pinned_agents_come_first_and_start_with_digits(
    workdir: Path, tmp_path: Path
) -> None:
    settings = Settings(pinned_agents=["kimi", "gone", "demo", "kimi"])
    app = AcpTui(settings, workdir, config_file=tmp_path / "config.toml")
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        assert picker.pinned == ["kimi", "demo"]  # unknown ids and doubles are left out
        assert option_ids(picker)[:2] == ["kimi", "demo"]
        options = picker.query_one("#agents", OptionList)
        assert options.get_option_at_index(options.highlighted or 0).id == "kimi"
        text = app.screen_text()
        assert "1 Kimi" in text.replace("▌", "").replace("▐", "")
        await pilot.press("9")  # no ninth pinned agent: nothing happens
        assert app.screen is picker
        await pilot.press("2")
        await until(pilot, lambda: len(app.tabs) == 1)
        assert app.tabs["session-1"].spec.id == "demo"
        await app.action_quit()


async def test_pin_and_unpin_in_the_picker_and_the_details(workdir: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(CONFIG, encoding="utf-8")
    app = AcpTui(load_settings(config), workdir, config_file=config)
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        assert picker.pinned == []
        _highlight(picker, "goose")
        await pilot.press("p")
        await pilot.pause(0.1)
        assert app.settings.pinned_agents == ["goose"]
        assert option_ids(picker)[0] == "goose"
        assert picker.query_one("#agents", OptionList).highlighted == 0  # still on goose
        saved = config.read_text(encoding="utf-8")
        assert 'pinned_agents = ["goose"]' in saved
        assert "# a theme I like" in saved  # comments and tables kept
        assert load_settings(config).agents["mine"].name == "Mine"

        _highlight(picker, "demo")
        await pilot.press("i")
        details = app.screen
        assert isinstance(details, AgentDetailsScreen)
        assert str(details.query_one("#pin", Button).label) == "Pin to top (p)"
        await pilot.press("p")
        await pilot.pause(0.1)
        assert str(details.query_one("#pin", Button).label) == "Unpin (p)"
        assert app.settings.pinned_agents == ["goose", "demo"]
        await pilot.press("escape")
        assert option_ids(picker)[:2] == ["goose", "demo"]

        _highlight(picker, "goose")
        await pilot.press("p")  # unpin
        await pilot.pause(0.1)
        assert app.settings.pinned_agents == ["demo"]
        assert option_ids(picker)[0] == "demo"
        assert load_settings(config).pinned_agents == ["demo"]

        _highlight(picker, "demo")
        await pilot.press("i")
        assert str(app.screen.query_one("#pin", Button).label) == "Unpin (p)"
        await pilot.press("p", "escape")
        await pilot.pause(0.1)
        assert load_settings(config).pinned_agents == []
        assert None not in option_ids(picker)  # no separator without pinned agents
