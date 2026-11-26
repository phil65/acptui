"""Agent registry and the config file."""

from typing import TYPE_CHECKING

import pytest

from acptui.config import (
    BUILTIN_AGENTS,
    AgentSpec,
    ConfigError,
    command_spec,
    expand_env,
    load_settings,
    url_spec,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_builtin_registry() -> None:
    ids = {spec.id for spec in BUILTIN_AGENTS}
    assert {"claude", "codex", "gemini", "opencode", "agentpool", "demo"} <= ids
    demo = next(spec for spec in BUILTIN_AGENTS if spec.id == "demo")
    assert demo.available


def test_spec_needs_command_or_url() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        AgentSpec(id="x", name="x")
    with pytest.raises(ValueError, match="exactly one"):
        AgentSpec(id="x", name="x", command=("a",), url="http://x")


def test_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPTUI_TEST_KEY", "secret")
    spec = url_spec("https://x/acp", {"Authorization": "Bearer ${ACPTUI_TEST_KEY}"})

    assert spec.is_remote
    assert spec.available
    assert spec.resolved_headers() == {"Authorization": "Bearer secret"}
    assert expand_env("${ACPTUI_UNSET_VARIABLE}x") == "x"


def test_load_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
default_agent = "mine"
theme = "nord"
diff_view = "split"

[agents.mine]
name = "Mine"
command = "my-agent --acp"
env = { A = "1" }

[agents.codex]
command = ["custom-codex"]
""",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.diff_view == "split"
    mine = settings.find_agent("mine")
    assert mine is not None
    assert mine.command == ("my-agent", "--acp")
    assert mine.resolved_env() == {"A": "1"}
    codex = settings.find_agent("codex")
    assert codex is not None
    assert codex.command == ("custom-codex",)
    assert len(settings.all_agents()) == len(BUILTIN_AGENTS) + 1


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "nope.toml")
    assert settings.default_agent is None
    assert settings.diff_view == "auto"


@pytest.mark.parametrize(
    "content", ["not toml [", "diff_view = 'sideways'", "[agents.x]\nname = 'x'"]
)
def test_invalid_config(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_command_spec() -> None:
    spec = command_spec(["/usr/bin/agent", "--acp"])
    assert spec.name == "agent"
    assert spec.target == "/usr/bin/agent --acp"
