"""In-memory log of the agent's stderr and the JSON-RPC traffic."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from collections.abc import Callable


LogKind = Literal["stderr", "incoming", "outgoing", "info", "error"]

MAX_LOG_LINES = 5000
MAX_TRAFFIC_CHARS = 2000


@dataclass(frozen=True)
class LogLine:
    """One log line."""

    kind: LogKind
    text: str
    time: datetime = field(default_factory=datetime.now)


class AgentLog:
    """A bounded log with listeners (the log screen subscribes while it is open)."""

    def __init__(self, max_lines: int = MAX_LOG_LINES) -> None:
        self.lines: deque[LogLine] = deque(maxlen=max_lines)
        self._listeners: list[Callable[[LogLine], None]] = []

    def add(self, kind: LogKind, text: str) -> None:
        """Add a line and notify listeners."""
        if kind in {"incoming", "outgoing"} and len(text) > MAX_TRAFFIC_CHARS:
            text = text[:MAX_TRAFFIC_CHARS] + f"… ({len(text)} chars)"
        line = LogLine(kind, text)
        self.lines.append(line)
        for listener in list(self._listeners):
            listener(line)

    def subscribe(self, listener: Callable[[LogLine], None]) -> None:
        """Call ``listener`` for each new line."""
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[LogLine], None]) -> None:
        """Stop calling ``listener``."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def tail(self, kind: LogKind, count: int) -> list[str]:
        """The last ``count`` lines of one kind."""
        return [line.text for line in self.lines if line.kind == kind][-count:]
