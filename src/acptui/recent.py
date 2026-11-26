"""Recent sessions of every agent, to resume them from the picker (``r``).

v2 agents list their own sessions (``session/list``, ``ctrl+r`` in a session); this list
is acptui's own and spans agents and directories, like toad's session database. One
JSON object per session in the platform state directory, newest last.
"""

import dataclasses
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time

import platformdirs


MAX_SESSIONS = 200
"""Sessions kept."""


def recent_path() -> Path:
    """Where the list is kept (``$ACPTUI_SESSIONS`` overrides it)."""
    if override := os.environ.get("ACPTUI_SESSIONS"):
        return Path(override)
    return platformdirs.user_state_path("acptui") / "sessions.jsonl"


@dataclass(frozen=True)
class RecentSession:
    """A session acptui had open."""

    agent_id: str
    """The registry id of the agent (or the program, for an ad-hoc command)."""
    agent_name: str
    command: tuple[str, ...]
    """The command line (for agents that are not in the registry); empty for URLs."""
    url: str | None
    session_id: str
    cwd: str
    title: str = ""
    time: float = 0.0


class RecentSessions:
    """The recent sessions file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or recent_path()

    def load(self) -> list[RecentSession]:
        """The sessions, newest first, one entry per session (the latest wins)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[tuple[str, str], RecentSession] = {}
        for line in lines:
            try:
                data = json.loads(line)
                session = RecentSession(
                    agent_id=str(data["agent_id"]),
                    agent_name=str(data["agent_name"]),
                    command=tuple(str(part) for part in data.get("command", ())),
                    url=data.get("url") if isinstance(data.get("url"), str) else None,
                    session_id=str(data["session_id"]),
                    cwd=str(data["cwd"]),
                    title=str(data.get("title", "")),
                    time=float(data.get("time", 0.0)),
                )
            except ValueError, KeyError, TypeError:
                continue
            key = (session.agent_id, session.session_id)
            if (old := latest.get(key)) is not None and not session.title:
                session = dataclasses.replace(session, title=old.title)
            latest.pop(key, None)
            latest[key] = session
        return list(reversed(latest.values()))

    def record(self, session: RecentSession) -> bool:
        """Add or update a session; whether it was written."""
        data = asdict(session) | {"command": list(session.command), "time": time.time()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(data) + "\n")
            self._trim()
        except OSError:
            return False
        return True

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 4 * MAX_SESSIONS:
            keep = list(reversed(self.load()[:MAX_SESSIONS]))
            text = "".join(
                json.dumps(asdict(s) | {"command": list(s.command)}) + "\n" for s in keep
            )
            self.path.write_text(text, encoding="utf-8")
