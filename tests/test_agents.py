"""The agent registry, the picker's agent details, install and login actions."""

from typing import TYPE_CHECKING

from textual.widgets import OptionList

from acptui.cli import main
from acptui.config import BUILTIN_AGENTS, AgentSpec, Settings
from acptui.ui.app import AcpTui
from acptui.ui.screens import agent as agent_module
from acptui.ui.screens.agent import AgentDetailsScreen
from acptui.ui.screens.picker import AgentPicker
from tests.test_app import SIZE, until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from textual.app import App


def test_registry_has_toads_agents() -> None:
    ids = [spec.id for spec in BUILTIN_AGENTS]
    assert len(ids) == len(set(ids))
    assert {"goose", "kimi", "copilot", "openhands", "vibe"} <= set(ids)
    known = [spec.id for spec in BUILTIN_AGENTS if spec.acp_v2]
    assert known == ["claude", "codex", "agentpool", "demo"]
    assert ids.index("demo") < ids.index("goose")  # v2 agents first


def test_agents_command_shows_v2_support(capsys: pytest.CaptureFixture[str]) -> None:
    main(["agents"])
    lines = capsys.readouterr().out.splitlines()
    claude = next(line for line in lines if line.startswith("claude "))
    goose = next(line for line in lines if line.startswith("goose "))
    assert " v2 " in claude
    assert " v2? " in goose


def _highlight(picker: AgentPicker, agent_id: str) -> None:
    options = picker.query_one("#agents", OptionList)
    options.highlighted = next(
        i for i in range(options.option_count) if options.get_option_at_index(i).id == agent_id
    )


async def test_details_install_and_start(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def fake_run(app: App[object], command: str) -> int:
        ran.append(command)
        return 0

    monkeypatch.setattr(agent_module, "run_in_terminal", fake_run)
    app = AcpTui(Settings(), workdir)
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        _highlight(picker, "kimi")
        await pilot.press("i")
        assert isinstance(app.screen, AgentDetailsScreen)
        text = app.screen_text()
        assert "uv tool install kimi-cli" in text
        assert "ACP v2 support unknown" in text
        await pilot.press("i")
        await until(pilot, lambda: app.screen is picker)
        assert ran == ["uv tool install kimi-cli"]

        _highlight(picker, "openhands")
        await pilot.press("i")
        await pilot.press("l")
        await until(pilot, lambda: ran[-1] == "openhands login")
        await pilot.press("escape")

        _highlight(picker, "demo")
        await pilot.press("i")
        assert "speaks ACP v2" in app.screen_text()
        await pilot.press("s")
        await until(pilot, lambda: len(app.tabs) == 1)
        await app.action_quit()


async def test_install_without_a_terminal_says_so(workdir: Path) -> None:
    app = AcpTui(Settings(), workdir)
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        _highlight(picker, "goose")
        await pilot.press("i", "i")  # headless: the terminal cannot be handed over
        await pilot.pause(0.2)
        assert isinstance(app.screen, AgentDetailsScreen)
        assert any("Run it in a terminal" in str(n.message) for n in app._notifications)


async def test_probe_v2_and_v1(demo_spec: AgentSpec, v1_spec: AgentSpec, workdir: Path) -> None:
    from acptui.probe import probe

    ok = await probe(demo_spec, workdir)
    assert ok.v2
    assert ok.version == "2"
    assert "Demo" in ok.agent
    old = await probe(v1_spec, workdir)
    assert not old.v2
    assert old.version == "1"
    assert old.summary == "speaks ACP v1, not v2"
    gone = await probe(AgentSpec(id="x", name="X", command=("acptui-no-such-agent",)), workdir)
    assert not gone.v2
    assert gone.version == ""
    assert gone.summary.startswith("test failed:")


async def test_test_v2_in_the_details_is_remembered(v1_spec: AgentSpec, workdir: Path) -> None:
    from acptui.config import UserAgent
    from acptui.probe import ProbeResults

    command = list(v1_spec.command)
    settings = Settings(agents={"old": UserAgent(name="Old agent", command=command)})
    app = AcpTui(settings, workdir)
    async with app.run_test(size=SIZE) as pilot:
        picker = app.screen
        assert isinstance(picker, AgentPicker)
        _highlight(picker, "old")
        await pilot.press("i")
        assert "tested: not yet" in app.screen_text()
        await pilot.press("t")
        await until(pilot, lambda: "speaks ACP v1, not v2" in app.screen_text())
        assert not ProbeResults().load()["old"].v2
        await pilot.press("escape")
        assert app.screen is picker
        old = next(spec for spec in picker.agents if spec.id == "old")
        assert old.acp_v2 is False  # remembered: the picker shows "no ACP v2 yet"
        assert "no ACP v2 yet" in app.screen_text()

    again = AcpTui(settings, workdir)
    async with again.run_test(size=SIZE):
        picker = again.screen
        assert isinstance(picker, AgentPicker)
        assert next(spec for spec in picker.agents if spec.id == "old").acp_v2 is False
