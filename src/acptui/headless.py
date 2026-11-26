"""Talking to an agent without the UI (for ``acptui options``)."""

from typing import TYPE_CHECKING

from aclientp.schema.v2 import (
    BooleanSessionConfigOption,
    RequestPermissionResponse,
    SelectSessionConfigOption,
)
from aclientp.schema.v2.base import is_set

from acptui.completion import first_line
from acptui.protocol.connection import AgentConnection
from acptui.ui.screens.dialogs import _select_values


if TYPE_CHECKING:
    from pathlib import Path

    from aclientp.schema.v2 import (
        AuthStatusUpdateNotification,
        CompleteElicitationNotification,
        CreateElicitationRequest,
        CreateElicitationResponse,
        RequestPermissionRequest,
        UpdateSessionNotification,
    )
    from aclientp.schema.v2.base import JsonValue

    from acptui.config import AgentSpec


class QuietEvents:
    """AgentEvents that ignore everything and refuse interactive requests."""

    def session_update(self, notification: UpdateSessionNotification) -> None:
        pass

    async def request_permission(
        self, request: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        from acptui.ui.screens.permission import cancelled

        return cancelled()

    async def create_elicitation(
        self, request: CreateElicitationRequest
    ) -> CreateElicitationResponse:
        from aclientp.schema.v2 import DeclineElicitationResponse

        return DeclineElicitationResponse()

    def complete_elicitation(self, notification: CompleteElicitationNotification) -> None:
        pass

    def auth_status_update(self, notification: AuthStatusUpdateNotification) -> None:
        pass

    def ext_notification(self, method: str, params: JsonValue) -> None:
        pass

    def stderr_line(self, line: str) -> None:
        pass

    def traffic(self, direction: str, text: str) -> None:
        pass


async def describe_options(spec: AgentSpec, cwd: Path) -> str:
    """The agent's session options (``session/options``, else from a new session)."""
    connection = AgentConnection(spec, QuietEvents(), cwd)
    async with connection.open():
        if connection.supports_options:
            response = await connection.session_options()
            options = response.config_options if is_set(response.config_options) else []
            commands = response.available_commands if is_set(response.available_commands) else []
        else:
            session = await connection.new_session()
            options = session.config_options if is_set(session.config_options) else []
            commands = session.available_commands if is_set(session.available_commands) else []
            await connection.close_session(session.session_id)
    lines = [f"{connection.agent_name} {connection.agent_version}", "", "Config options:"]
    for option in options:
        match option:
            case SelectSessionConfigOption():
                values = ", ".join(value for value, _, _ in _select_values(option))
                lines.append(f"  {option.config_id} = {option.current_value}  ({values})")
            case BooleanSessionConfigOption():
                lines.append(
                    f"  {option.config_id} = {str(option.current_value).lower()}  (true, false)"
                )
            case _:
                lines.append(f"  {option.config_id} ({option.type})")
    lines += ["", "Commands:"]
    lines += [f"  /{command.name}  {first_line(command.description)}" for command in commands]
    return "\n".join(lines)
