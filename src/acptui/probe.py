"""Testing whether an agent speaks ACP v2 (the "Test v2" action of the agent details).

acptui never starts agents by itself to find out: the user picks the action, acptui
runs ``initialize`` once against the configured command (no session), shows what the
agent negotiated and remembers it (``agent_versions.json`` in the state directory).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING

import anyio
import platformdirs

from acptui.protocol.connection import AgentConnection, AgentError, UnsupportedProtocolError


if TYPE_CHECKING:
    from acptui.config import AgentSpec


PROBE_TIMEOUT = 30.0
"""Seconds an agent gets to answer ``initialize`` (npx may download first)."""


def probes_path() -> Path:
    """Where test results are kept (``$ACPTUI_PROBES`` overrides it)."""
    if override := os.environ.get("ACPTUI_PROBES"):
        return Path(override)
    return platformdirs.user_state_path("acptui") / "agent_versions.json"


@dataclass(frozen=True)
class ProbeResult:
    """What an agent answered."""

    agent_id: str
    v2: bool
    """Whether it negotiated ACP v2."""
    version: str
    """The protocol version it answered (``"2"``, ``"1"``, ...) or ``""`` if it failed."""
    agent: str = ""
    """Its name and version, as it reports them."""
    error: str = ""
    """Why the test failed (it did not start, timed out, ...)."""
    time: float = 0.0

    @property
    def summary(self) -> str:
        """One line for the UI."""
        if self.v2:
            return f"speaks ACP v2 ({self.agent})" if self.agent else "speaks ACP v2"
        if self.version:
            return f"speaks ACP v{self.version}, not v2"
        return f"test failed: {self.error}"


async def probe(spec: AgentSpec, cwd: Path, timeout: float = PROBE_TIMEOUT) -> ProbeResult:
    """Run ``initialize`` once against the agent and close it again."""
    from acptui.headless import QuietEvents

    connection = AgentConnection(spec, QuietEvents(), cwd)
    try:
        with anyio.fail_after(timeout):
            async with connection.open():
                agent = f"{connection.agent_name} {connection.agent_version}".strip()
                return ProbeResult(spec.id, v2=True, version="2", agent=agent, time=time.time())
    except UnsupportedProtocolError as exc:
        received = str(exc.received)
        return ProbeResult(
            spec.id, v2=False, version=received, agent=exc.agent_name, time=time.time()
        )
    except TimeoutError:
        return ProbeResult(
            spec.id, v2=False, version="", error=f"no answer in {timeout:.0f} s", time=time.time()
        )
    except (AgentError, ConnectionError, OSError) as exc:
        return ProbeResult(
            spec.id, v2=False, version="", error=str(exc).splitlines()[0], time=time.time()
        )


class ProbeResults:
    """Remembered test results, per agent id."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or probes_path()

    def load(self) -> dict[str, ProbeResult]:
        """All results (unreadable entries skipped)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        results: dict[str, ProbeResult] = {}
        if isinstance(data, dict):
            for agent_id, item in data.items():
                try:
                    results[agent_id] = ProbeResult(**item)
                except TypeError:
                    continue
        return results

    def save(self, result: ProbeResult) -> None:
        """Remember a result (the latest per agent)."""
        results = self.load()
        results[result.agent_id] = result
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {agent_id: asdict(item) for agent_id, item in results.items()}
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
