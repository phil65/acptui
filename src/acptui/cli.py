"""Command line interface: ``acptui``, ``acptui run``, ``acptui connect``, ``acptui agents``."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import acptui
from acptui.config import ConfigError, command_spec, config_path, load_settings, url_spec


if TYPE_CHECKING:
    from collections.abc import Sequence

    from acptui.config import AgentSpec, Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acptui",
        description="Terminal UI for AI coding agents, built on ACP v2.",
        epilog="Without a command, acptui shows the agent picker (or starts `default_agent`).",
    )
    parser.add_argument("--version", action="version", version=f"acptui {acptui.__version__}")
    parser.add_argument("--cwd", type=Path, default=None, help="working directory of the session")
    parser.add_argument(
        "--config", type=Path, default=None, help="config file (default: platform config dir)"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser("run", help="start an agent: a registry id or a command line")
    run.add_argument("agent", help="agent id (see `acptui agents`) or a program")
    run.add_argument("args", nargs=argparse.REMAINDER, help="arguments for a program")
    run.add_argument("--resume", metavar="SESSION_ID", help="resume this session")

    connect = sub.add_parser("connect", help="connect to a remote agent (http(s):// or ws(s)://)")
    connect.add_argument("url")
    connect.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'NAME: VALUE'",
        help="HTTP header (repeatable; ${VAR} expands from the environment)",
    )
    connect.add_argument("--resume", metavar="SESSION_ID", help="resume this session")

    sub.add_parser("agents", help="list known agents and whether they are installed")
    sub.add_parser("config", help="print the config file path")
    return parser


def _resolve_run(settings: Settings, agent: str, args: Sequence[str]) -> AgentSpec:
    if not args and (spec := settings.find_agent(agent)) is not None:
        return spec
    return command_spec([agent, *args])


def _headers(values: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        name, sep, content = value.partition(":")
        if not sep:
            msg = f"invalid header {value!r}, expected 'Name: value'"
            raise SystemExit(msg)
        headers[name.strip()] = content.strip()
    return headers


def _list_agents(settings: Settings) -> None:
    for spec in settings.all_agents():
        installed = "installed" if spec.available else "not found"
        print(f"{spec.id:<12} {spec.name:<14} {installed:<10} {spec.target}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    options = _parser().parse_args(argv)
    try:
        settings = load_settings(options.config)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    cwd: Path = (options.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        raise SystemExit(f"not a directory: {cwd}")

    spec: AgentSpec | None = None
    resume: str | None = None
    match options.command:
        case "agents":
            _list_agents(settings)
            return
        case "config":
            print(options.config or config_path())
            return
        case "run":
            spec = _resolve_run(settings, options.agent, options.args)
            resume = options.resume
        case "connect":
            spec = url_spec(options.url, _headers(options.header))
            resume = options.resume
        case _:
            if settings.default_agent is not None:
                spec = settings.find_agent(settings.default_agent)
                if spec is None:
                    print(f"unknown default_agent {settings.default_agent!r}", file=sys.stderr)

    from acptui.ui.app import AcpTui

    AcpTui(settings, cwd, spec, resume_id=resume).run()


if __name__ == "__main__":
    main()
