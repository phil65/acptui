"""Test helpers: a recording AgentEvents implementation and polling."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aclientp.schema.v2 import RequestPermissionResponse, SelectedPermissionOutcome

from acptui.protocol.state import SessionState


if TYPE_CHECKING:
    from collections.abc import Callable

    from aclientp.schema.v2 import RequestPermissionRequest, UpdateSessionNotification


TIMEOUT = 20.0


async def wait_for(condition: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    """Poll until ``condition`` holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


class RecordingEvents:
    """AgentEvents that apply updates to a SessionState and answer permissions."""

    def __init__(self, answer: str | None = "allow") -> None:
        self.state: SessionState | None = None
        self.answer = answer
        self.permissions: list[RequestPermissionRequest] = []
        self.stderr: list[str] = []
        self.traffic_lines: list[tuple[str, str]] = []
        self.updates: list[UpdateSessionNotification] = []

    def track(self, session_id: str, cwd: str) -> SessionState:
        """Start tracking a session."""
        self.state = SessionState(session_id, cwd)
        return self.state

    def session_update(self, notification: UpdateSessionNotification) -> None:
        self.updates.append(notification)
        if self.state is not None and notification.session_id == self.state.session_id:
            self.state.apply(notification.update)

    async def request_permission(
        self, request: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        self.permissions.append(request)
        option = next(
            o for o in request.options if self.answer is None or o.option_id == self.answer
        )
        return RequestPermissionResponse(
            outcome=SelectedPermissionOutcome(option_id=option.option_id)
        )

    def stderr_line(self, line: str) -> None:
        self.stderr.append(line)

    def traffic(self, direction: str, text: str) -> None:
        self.traffic_lines.append((direction, text))
