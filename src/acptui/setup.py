"""Building the session setup (MCP servers, system prompt, directories, options)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aclientp.schema.v2 import InitialConfigOptionBoolean, InitialConfigOptionId

from acptui.protocol.connection import SessionSetup


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aclientp.schema.v2 import InitialSessionConfigOption

    from acptui.config import AgentSpec, Settings


def parse_option(text: str) -> tuple[str, str | bool]:
    """``KEY=VALUE`` (``true`` / ``false`` become booleans).

    Raises:
        ValueError: There is no ``=``.
    """
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        msg = f"expected KEY=VALUE, got {text!r}"
        raise ValueError(msg)
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return key.strip(), lowered == "true"
    return key.strip(), value.strip()


def initial_options(options: Mapping[str, str | bool]) -> tuple[InitialSessionConfigOption, ...]:
    """Initial config option values for session setup requests."""
    return tuple(
        InitialConfigOptionBoolean(config_id=key, value=value)
        if isinstance(value, bool)
        else InitialConfigOptionId(config_id=key, value=value)
        for key, value in options.items()
    )


def build_setup(
    settings: Settings,
    spec: AgentSpec,
    *,
    options: Mapping[str, str | bool] | None = None,
    additional_directories: Sequence[str] = (),
    system_prompt: str | None = None,
    system_prompt_mode: str | None = None,
) -> SessionSetup:
    """Combine config file settings, the agent's options and command line overrides."""
    directories = [*settings.additional_directories, *additional_directories]
    return SessionSetup(
        additional_directories=tuple(str(Path(d).expanduser().resolve()) for d in directories),
        mcp_servers=tuple(server.to_schema() for server in settings.mcp_servers),
        system_prompt=system_prompt or settings.system_prompt,
        system_prompt_mode=system_prompt_mode or settings.system_prompt_mode,
        config_options=initial_options({**spec.options, **(options or {})}),
    )
