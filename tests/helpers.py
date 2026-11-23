"""Test helpers: a recording AgentEvents implementation and polling."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aclientp.schema.v2 import (
    AcceptElicitationResponse,
    DeclineElicitationResponse,
    FormElicitationRequest,
    RequestPermissionResponse,
    SelectedPermissionOutcome,
)

from acptui.protocol.state import SessionState


if TYPE_CHECKING:
    from collections.abc import Callable

    from aclientp.schema.v2 import (
        AuthStatusUpdateNotification,
        CompleteElicitationNotification,
        CreateElicitationRequest,
        CreateElicitationResponse,
        RequestPermissionRequest,
        UpdateSessionNotification,
    )
    from aclientp.schema.v2.base import JsonValue


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
        self.states: dict[str, SessionState] = {}
        """All sessions with updates (subagent sessions included)."""
        self.answer = answer
        self.permissions: list[RequestPermissionRequest] = []
        self.stderr: list[str] = []
        self.traffic_lines: list[tuple[str, str]] = []
        self.updates: list[UpdateSessionNotification] = []
        self.elicitations: list[CreateElicitationRequest] = []
        self.completed: list[str] = []
        self.auth_updates: list[AuthStatusUpdateNotification] = []
        self.extensions: list[tuple[str, JsonValue]] = []
        self.form_answer: dict[str, str | int | float | bool | list[str]] = {"name": "acptui"}

    def track(self, session_id: str, cwd: str) -> SessionState:
        """Start tracking a session."""
        self.state = SessionState(session_id, cwd)
        self.states[session_id] = self.state
        return self.state

    def session_update(self, notification: UpdateSessionNotification) -> None:
        self.updates.append(notification)
        state = self.states.get(notification.session_id)
        if state is None:
            state = self.states[notification.session_id] = SessionState(
                notification.session_id, "/"
            )
        state.apply(notification.update)

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

    async def create_elicitation(
        self, request: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        self.elicitations.append(request)
        if isinstance(request, FormElicitationRequest):
            return AcceptElicitationResponse(content=self.form_answer)
        await wait_for(lambda: bool(self.completed))
        return AcceptElicitationResponse() if self.completed else DeclineElicitationResponse()

    def complete_elicitation(self, notification: CompleteElicitationNotification) -> None:
        self.completed.append(notification.elicitation_id)

    def auth_status_update(self, notification: AuthStatusUpdateNotification) -> None:
        self.auth_updates.append(notification)

    def ext_notification(self, method: str, params: JsonValue) -> None:
        self.extensions.append((method, params))

    def stderr_line(self, line: str) -> None:
        self.stderr.append(line)

    def traffic(self, direction: str, text: str) -> None:
        self.traffic_lines.append((direction, text))
