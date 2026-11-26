"""Shared fixtures: a working directory, the demo agent, a v1-only agent."""

import sys
from typing import TYPE_CHECKING

import pytest

from acptui.config import BUILTIN_AGENTS, AgentSpec


if TYPE_CHECKING:
    from pathlib import Path


V1_AGENT = """
import json, sys
request = json.loads(sys.stdin.readline())
info = {"name": "old-agent", "version": "1"}
result = {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": info}
print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
sys.stdin.read()
"""
"""A minimal v1 agent: answers ``initialize`` with version 1 and a v1-shaped result."""

FAKE_V2_AGENT = V1_AGENT.replace('"protocolVersion": 1', '"protocolVersion": 2')
"""Claims v2 but answers with a v1-shaped result."""


@pytest.fixture(autouse=True)
def _fast_demo(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No artificial delays in the demo agent; history, recent sessions and config of its own."""
    monkeypatch.setenv("ACPTUI_DEMO_DELAY", "0")
    history = tmp_path_factory.mktemp("history") / "history.jsonl"
    monkeypatch.setenv("ACPTUI_HISTORY", str(history))
    monkeypatch.setenv("ACPTUI_SESSIONS", str(history.with_name("sessions.jsonl")))
    monkeypatch.setenv("ACPTUI_PROBES", str(history.with_name("agent_versions.json")))
    # Pins and the settings screen save to config.toml: never the user's own file
    monkeypatch.setenv("ACPTUI_CONFIG", str(history.with_name("config.toml")))


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A small project directory."""
    (tmp_path / "README.md").write_text("# Project\n\nHello.\n", encoding="utf-8")
    (tmp_path / "notes file.txt").write_text("some notes\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    return tmp_path


@pytest.fixture
def demo_spec() -> AgentSpec:
    """The bundled demo agent."""
    return next(spec for spec in BUILTIN_AGENTS if spec.id == "demo")


@pytest.fixture
def v1_spec() -> AgentSpec:
    """An agent that only speaks ACP v1 (aclientp's v1 test agent)."""
    return AgentSpec(id="v1", name="Old agent", command=(sys.executable, "-c", V1_AGENT))


@pytest.fixture
def fake_v2_spec() -> AgentSpec:
    """An agent that claims v2 with a v1-shaped ``initialize`` result."""
    return AgentSpec(id="fake", name="Fake agent", command=(sys.executable, "-c", FAKE_V2_AGENT))


@pytest.fixture
def missing_spec() -> AgentSpec:
    """An agent whose program does not exist."""
    return AgentSpec(
        id="missing", name="Missing", command=("acptui-no-such-agent",), install="pip install it"
    )
