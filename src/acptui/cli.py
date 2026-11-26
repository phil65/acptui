"""Command line interface: ``acptui``, ``acptui run``, ``acptui connect``, ``acptui agents``."""

import argparse
import os
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
    parser.add_argument(
        "-o",
        "--option",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="initial session config option, e.g. model=opus or mode=auto (repeatable)",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="additional workspace root for the session (repeatable)",
    )
    parser.add_argument("--system-prompt", metavar="TEXT", help="extra system instructions")
    parser.add_argument(
        "--system-prompt-mode",
        choices=["append", "override"],
        help="append to the agent's instructions (default) or replace them",
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

    serve = sub.add_parser(
        "serve",
        help="serve acptui in the browser (textual-serve); the rest is the acptui command",
        description=(
            "Serve acptui as a web app: every browser tab runs `acptui [OPTIONS] COMMAND` "
            "in a new process. A random token in the URL protects it (printed at start); "
            "WebSockets from other sites are refused."
        ),
    )
    serve.add_argument("--host", default="localhost", help="address to bind (default: localhost)")
    serve.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    serve.add_argument("--public-url", metavar="URL", help="URL the browser uses, behind a proxy")
    serve.add_argument(
        "--token",
        default=os.environ.get("ACPTUI_SERVE_TOKEN"),
        help="the access token (default: $ACPTUI_SERVE_TOKEN, else a random one per run)",
    )
    serve.add_argument(
        "--no-token", action="store_true", help="no token (only on a loopback address)"
    )
    serve.add_argument(
        "served", nargs=argparse.REMAINDER, metavar="COMMAND", help="e.g. `run demo`"
    )

    sub.add_parser("agents", help="list known agents and whether they are installed")
    options = sub.add_parser("options", help="print the session options an agent offers")
    options.add_argument("agent", help="agent id or program")
    options.add_argument("args", nargs=argparse.REMAINDER, help="arguments for a program")
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


V2_LABELS = {True: "v2", False: "not v2", None: "v2?"}


def _list_agents(settings: Settings) -> None:
    width = max(len(spec.name) for spec in settings.all_agents())
    for spec in settings.all_agents():
        installed = "installed" if spec.available else "not found"
        v2 = V2_LABELS[spec.acp_v2]
        print(f"{spec.id:<12} {spec.name:<{width}} {installed:<10} {v2:<7} {spec.target}")


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
        case "serve":
            raise SystemExit(serve_web(options, list(argv if argv is not None else sys.argv[1:])))
        case "options":
            spec = _resolve_run(settings, options.agent, options.args)
            raise SystemExit(print_options(spec, cwd))
        case "run":
            args = list(options.args)
            resume = options.resume
            if settings.find_agent(options.agent) is not None and args[:1] == ["--resume"]:
                if len(args) != 2:  # noqa: PLR2004
                    raise SystemExit("usage: acptui run AGENT --resume SESSION_ID")
                resume, args = args[1], []
            if args and settings.find_agent(options.agent) is not None:
                msg = (
                    f"{options.agent!r} is a registry agent; "
                    "run a program with: acptui run -- PROGRAM ARGS"
                )
                raise SystemExit(msg)
            spec = _resolve_run(settings, options.agent, args)
        case "connect":
            spec = url_spec(options.url, _headers(options.header))
            resume = options.resume
        case _:
            if settings.default_agent is not None:
                spec = settings.find_agent(settings.default_agent)
                if spec is None:
                    print(f"unknown default_agent {settings.default_agent!r}", file=sys.stderr)

    from acptui.setup import build_setup, parse_option
    from acptui.ui.app import AcpTui
    from acptui.ui.widgets.image import prepare

    prepare(settings.images)  # probes the terminal: before the app takes it over

    setup = None
    if spec is not None:
        try:
            overrides = dict(parse_option(item) for item in options.option)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        setup = build_setup(
            settings,
            spec,
            options=overrides,
            additional_directories=options.add_dir,
            system_prompt=options.system_prompt,
            system_prompt_mode=options.system_prompt_mode,
        )
    AcpTui(settings, cwd, spec, resume_id=resume, setup=setup, config_file=options.config).run()


def served_command(argv: Sequence[str], served: Sequence[str]) -> list[str]:
    """The command each browser session runs.

    acptui with the global options given before ``serve`` and the command after it.
    """
    index = list(argv).index("serve")
    return [sys.executable, "-m", "acptui", *argv[:index], *served]


def serve_web(options: argparse.Namespace, argv: Sequence[str]) -> int:
    """Run the web server until ctrl+c."""
    import shlex

    from acptui.serve import SecureServer, is_loopback, new_token

    if options.no_token and not is_loopback(options.host):
        print("error: --no-token is only allowed on a loopback address", file=sys.stderr)
        return 2
    token = None if options.no_token else (options.token or new_token())
    command = shlex.join(served_command(argv, options.served))
    title = shlex.join(["acptui", *options.served]) if options.served else "acptui"
    SecureServer(
        command,
        host=options.host,
        port=options.port,
        title=title,
        public_url=options.public_url,
        token=token,
    ).serve()
    return 0


def print_options(spec: AgentSpec, cwd: Path) -> int:
    """Print the config options and commands a new session of the agent would have."""
    import anyio

    from acptui.headless import describe_options

    try:
        print(anyio.run(describe_options, spec, cwd))
    except Exception as exc:  # noqa: BLE001 - shown to the user as an error message
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
