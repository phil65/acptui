"""Agent registry and user configuration.

The built-in registry lists well-known ACP agents. Users add their own agents (or
override built-in ones) in ``config.toml`` in the platform config directory::

    default_agent = "codex"
    theme = "tokyo-night"
    diff_view = "auto"            # "unified", "split" or "auto"

    [agents.my-agent]
    name = "My agent"
    command = ["my-agent", "--acp"]
    env = { MY_FLAG = "1" }

    [agents.remote]
    name = "Remote agent"
    url = "https://example.com/acp"
    headers = { "X-API-Key" = "${MY_API_KEY}" }   # ${VAR} expands from the environment
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Literal

from aclientp.schema.v2 import EnvVariable, HttpHeader, McpServerHttp, McpServerStdio
import platformdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


DiffViewMode = Literal["unified", "split", "auto"]
"""How diffs are shown: one column, side by side, or side by side when wide enough."""

ImageMode = Literal["auto", "tgp", "sixel", "halfcell", "unicode", "off"]
"""How images are drawn: the best the terminal supports (``auto``), one way, or not at all."""

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace ``${NAME}`` references with environment variables (unset ones become empty)."""
    env = os.environ if environ is None else environ
    return _ENV_REFERENCE.sub(lambda match: env.get(match.group(1), ""), value)


class AgentSpec(BaseModel):
    """How to reach an agent: a command to launch (stdio) or a URL (HTTP / WebSocket)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """Short identifier, used on the command line (``acptui run <id>``)."""

    name: str
    """Display name."""

    command: tuple[str, ...] = ()
    """Program and arguments of a local (stdio) agent."""

    url: str | None = None
    """URL of a remote agent (``http(s)://`` or ``ws(s)://``)."""

    env: Mapping[str, str] = Field(default_factory=dict)
    """Extra environment variables for a local agent (``${VAR}`` is expanded)."""

    headers: Mapping[str, str] = Field(default_factory=dict)
    """HTTP headers for a remote agent (``${VAR}`` is expanded)."""

    description: str = ""
    """One line about the agent."""

    homepage: str = ""
    """Where to get it."""

    install: str = ""
    """Command that installs the agent."""

    builtin: bool = False
    """Whether the agent comes from the built-in registry."""

    options: Mapping[str, str | bool] = Field(default_factory=dict)
    """Initial session config options (``config_id`` -> value), e.g. ``{"model": "opus"}``."""

    @model_validator(mode="after")
    def _check_target(self) -> AgentSpec:
        if bool(self.command) == bool(self.url):
            msg = f"agent {self.id!r} needs exactly one of `command` and `url`"
            raise ValueError(msg)
        return self

    @property
    def is_remote(self) -> bool:
        """Whether the agent is reached over the network."""
        return self.url is not None

    @property
    def available(self) -> bool:
        """Whether the agent can be started: its program is on ``PATH`` (or it is remote)."""
        if self.url is not None:
            return True
        program = self.command[0]
        return Path(program).is_file() or shutil.which(program) is not None

    @property
    def target(self) -> str:
        """The command line or URL, for display."""
        return self.url if self.url is not None else " ".join(self.command)

    def resolved_env(self) -> dict[str, str]:
        """The extra environment, with ``${VAR}`` references expanded."""
        return {key: expand_env(value) for key, value in self.env.items()}

    def resolved_headers(self) -> dict[str, str]:
        """The headers, with ``${VAR}`` references expanded."""
        return {key: expand_env(value) for key, value in self.headers.items()}


BUILTIN_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="claude",
        name="Claude Code",
        command=("claude-agent-acp",),
        description="Anthropic's Claude Code via the claude-agent-acp adapter",
        homepage="https://github.com/agentclientprotocol/claude-agent-acp",
        install="npm install -g @agentclientprotocol/claude-agent-acp",
        builtin=True,
    ),
    AgentSpec(
        id="codex",
        name="Codex",
        command=("codex-acp",),
        description="OpenAI Codex via the codex-acp adapter",
        homepage="https://github.com/agentclientprotocol/codex-acp",
        install="npm install -g @agentclientprotocol/codex-acp",
        builtin=True,
    ),
    AgentSpec(
        id="gemini",
        name="Gemini CLI",
        command=("gemini", "--experimental-acp"),
        description="Google's Gemini CLI in ACP mode",
        homepage="https://github.com/google-gemini/gemini-cli",
        install="npm install -g @google/gemini-cli",
        builtin=True,
    ),
    AgentSpec(
        id="opencode",
        name="OpenCode",
        command=("opencode", "acp"),
        description="SST's OpenCode in ACP mode",
        homepage="https://opencode.ai",
        install="curl -fsSL https://opencode.ai/install | bash",
        builtin=True,
    ),
    AgentSpec(
        id="agentpool",
        name="AgentPool",
        command=("agentpool", "serve-acp"),
        description="agentpool's configurable agents served over ACP",
        homepage="https://github.com/phil65/agentpool",
        install="uv tool install agentpool",
        builtin=True,
    ),
    AgentSpec(
        id="demo",
        name="Demo agent",
        command=(sys.executable, "-m", "acptui.demo"),
        description="Scripted v2 agent bundled with acptui (no LLM, no API key)",
        homepage="https://github.com/phil65/acptui",
        builtin=True,
    ),
)
"""Agents acptui knows out of the box."""


class UserAgent(BaseModel):
    """An agent entry of ``config.toml`` (the table key is its id)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    command: str | Sequence[str] | None = None
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    options: dict[str, str | bool] = Field(default_factory=dict)

    def to_spec(self, agent_id: str) -> AgentSpec:
        """Build the agent spec."""
        match self.command:
            case None:
                command: tuple[str, ...] = ()
            case str():
                command = tuple(self.command.split())
            case _:
                command = tuple(self.command)
        return AgentSpec(
            id=agent_id,
            name=self.name or agent_id,
            command=command,
            url=self.url,
            env=self.env,
            headers=self.headers,
            description=self.description,
            options=self.options,
        )


class McpServerConfig(BaseModel):
    """An MCP server passed to the agent on session setup (stdio or HTTP)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str | None = None
    """Program of a stdio server (resolved on ``PATH``; ACP requires an absolute path)."""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    """URL of an HTTP server."""
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_target(self) -> McpServerConfig:
        if bool(self.command) == bool(self.url):
            msg = f"MCP server {self.name!r} needs exactly one of `command` and `url`"
            raise ValueError(msg)
        return self

    def to_schema(self) -> McpServerStdio | McpServerHttp:
        """The ACP model (``${VAR}`` is expanded in env and header values)."""
        if self.url is not None:
            headers = [HttpHeader(name=k, value=expand_env(v)) for k, v in self.headers.items()]
            return McpServerHttp(name=self.name, url=self.url, headers=headers)
        command = self.command or ""
        resolved = shutil.which(command) or str(Path(command).expanduser().resolve())
        env = [EnvVariable(name=k, value=expand_env(v)) for k, v in self.env.items()]
        return McpServerStdio(name=self.name, command=resolved, args=self.args, env=env)


class Settings(BaseModel):
    """Contents of ``config.toml``."""

    model_config = ConfigDict(extra="forbid")

    default_agent: str | None = None
    """Agent to start when ``acptui`` is run without one (instead of the picker)."""

    theme: str = "textual-dark"
    """Textual theme name."""

    diff_view: DiffViewMode = "auto"
    """How diffs are shown."""

    expand_thoughts: bool = False
    """Show agent thoughts expanded instead of collapsed."""

    images: ImageMode = "auto"
    """How images are drawn: ``auto`` (the best the terminal supports), ``tgp`` (kitty),
    ``sixel``, ``halfcell`` (colored half blocks, any terminal), ``unicode`` or ``off``."""

    agents: dict[str, UserAgent] = Field(default_factory=dict)
    """User-defined agents; an id of a built-in agent replaces it."""

    agent_options: dict[str, dict[str, str | bool]] = Field(default_factory=dict)
    """Initial session config options per agent id (also for built-in agents)."""

    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    """MCP servers passed to every session (if the agent supports the transport)."""

    system_prompt: str | None = None
    """Extra system instructions for every session (if the agent supports it)."""

    system_prompt_mode: Literal["append", "override"] = "append"
    """Append to the agent's own instructions, or replace them (if supported)."""

    additional_directories: list[str] = Field(default_factory=list)
    """More workspace roots for every session (if the agent supports it)."""

    mounts: dict[str, str] = Field(default_factory=dict)
    """Folders of the files panel besides the workspace: name -> any fsspec / upathtools URL
    (``github://owner:repo@main/docs``, ``s3://bucket/key``, ``~/notes``, ...)."""

    @model_validator(mode="after")
    def _check_agents(self) -> Settings:
        self.all_agents()  # user agents must be valid specs
        return self

    def all_agents(self) -> list[AgentSpec]:
        """Built-in and user agents; user agents override built-in ones with the same id."""
        merged = {spec.id: spec for spec in BUILTIN_AGENTS}
        for agent_id, agent in self.agents.items():
            merged[agent_id] = agent.to_spec(agent_id)
        for agent_id, options in self.agent_options.items():
            if (spec := merged.get(agent_id)) is not None:
                merged[agent_id] = spec.model_copy(update={"options": {**spec.options, **options}})
        return list(merged.values())

    def find_agent(self, agent_id: str) -> AgentSpec | None:
        """The agent with this id, if any."""
        return next((spec for spec in self.all_agents() if spec.id == agent_id), None)


class ConfigError(Exception):
    """The config file could not be read."""


def config_path() -> Path:
    """Path of ``config.toml`` (``$ACPTUI_CONFIG`` overrides the platform default)."""
    if override := os.environ.get("ACPTUI_CONFIG"):
        return Path(override)
    return platformdirs.user_config_path("acptui") / "config.toml"


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings; a missing file gives the defaults.

    Raises:
        ConfigError: The file is not valid TOML or has invalid values.
    """
    path = path or config_path()
    if not path.is_file():
        return Settings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Settings.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as exc:
        msg = f"invalid config file {path}: {exc}"
        raise ConfigError(msg) from exc


def command_spec(command: Sequence[str]) -> AgentSpec:
    """An ad-hoc agent spec for a command line."""
    return AgentSpec(id=Path(command[0]).name, name=Path(command[0]).name, command=tuple(command))


def url_spec(url: str, headers: Mapping[str, str] | None = None) -> AgentSpec:
    """An ad-hoc agent spec for a remote agent."""
    return AgentSpec(id=url, name=url, url=url, headers=dict(headers or {}))
