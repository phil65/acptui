"""The command line."""

import asyncio
import subprocess
from typing import TYPE_CHECKING

import pytest

from acptui.cli import main
from acptui.setup import parse_option


if TYPE_CHECKING:
    from pathlib import Path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.startswith("acptui ")


def test_agents_and_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agents.mine]\ncommand = "my-agent"\n', encoding="utf-8")
    main(["--config", str(config), "agents"])
    out = capsys.readouterr().out
    assert "demo" in out
    assert "mine" in out
    main(["--config", str(config), "config"])
    assert capsys.readouterr().out.strip() == str(config)


def test_options_command(capsys: pytest.CaptureFixture[str], workdir: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--cwd", str(workdir), "options", "demo"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "mode = ask  (ask, auto)" in out
    assert "thinking = true  (true, false)" in out
    assert "/tour" in out


def test_bad_input(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("nope = [", encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid config file"):
        main(["--config", str(bad), "agents"])
    with pytest.raises(SystemExit, match="not a directory"):
        main(["--cwd", str(tmp_path / "missing"), "agents"])
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        main(["--config", str(tmp_path / "none.toml"), "-o", "novalue", "run", "demo"])


def test_registry_agents_take_no_arguments(tmp_path: Path) -> None:
    config = str(tmp_path / "none.toml")
    with pytest.raises(SystemExit, match="registry agent"):
        main(["--config", config, "run", "demo", "--flag"])
    with pytest.raises(SystemExit, match="usage"):
        main(["--config", config, "run", "demo", "--resume"])


def test_parse_option() -> None:
    assert parse_option("model=opus") == ("model", "opus")
    assert parse_option("thinking=False") == ("thinking", False)


def test_served_command_keeps_the_global_options() -> None:
    import sys

    from acptui.cli import served_command

    argv = ["--cwd", "/work", "serve", "--port", "9000", "run", "demo"]
    assert served_command(argv, ["run", "demo"]) == [
        sys.executable,
        "-m",
        "acptui",
        "--cwd",
        "/work",
        "run",
        "demo",
    ]


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(tmp_path: Path, *options: str) -> tuple[subprocess.Popen[bytes], int]:
    import sys

    port = _free_port()
    command = [sys.executable, "-m", "acptui", "--cwd", str(tmp_path), "serve"]
    server = subprocess.Popen(
        [*command, "--port", str(port), *options, "run", "demo"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return server, port


async def _wait_up(port: int, server: subprocess.Popen[bytes]) -> None:
    import aiohttp

    async with asyncio.timeout(30):
        while True:
            try:
                async with (
                    aiohttp.ClientSession() as client,
                    client.get(f"http://127.0.0.1:{port}/"),
                ):
                    return
            except aiohttp.ClientError:
                assert server.poll() is None
                await asyncio.sleep(0.2)


async def test_serve_needs_the_token(tmp_path: Path) -> None:
    import aiohttp

    server, port = _serve(tmp_path, "--token", "s3cret")
    base = f"http://127.0.0.1:{port}"
    try:
        await _wait_up(port, server)
        jar = aiohttp.CookieJar(unsafe=True)  # browsers keep cookies of IP hosts, aiohttp not
        async with aiohttp.ClientSession(cookie_jar=jar) as client:
            async with client.get(f"{base}/") as response:
                assert response.status == 401
            async with client.get(f"{base}/?token=wrong") as response:
                assert response.status == 401
            async with client.get(f"{base}/static/js/textual.js") as response:
                assert response.status == 401
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect(f"{base}/ws")
            assert refused.value.status == 401
            async with client.get(f"{base}/?token=s3cret") as response:
                assert response.status == 200
                page = await response.text()
                assert "acptui run demo" in page
                assert "acptui_token_" in response.headers.get("Set-Cookie", "")
                assert "HttpOnly" in response.headers["Set-Cookie"]
            # The cookie now authenticates the page's own requests.
            async with client.get(f"{base}/static/js/textual.js") as response:
                assert response.status == 200
            with pytest.raises(aiohttp.WSServerHandshakeError) as foreign:
                await client.ws_connect(f"{base}/ws", origin="https://evil.example")
            assert foreign.value.status == 403
            socket = await client.ws_connect(f"{base}/ws", origin=base)
            await socket.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


async def test_serve_without_a_token_only_on_loopback(tmp_path: Path) -> None:
    import aiohttp

    from acptui.serve import is_loopback

    assert is_loopback("localhost")
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert not is_loopback("0.0.0.0")
    assert main_exit(["serve", "--host", "0.0.0.0", "--no-token", "run", "demo"]) == 2

    server, port = _serve(tmp_path, "--no-token")
    try:
        await _wait_up(port, server)
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/") as response:
                assert response.status == 200
            with pytest.raises(aiohttp.WSServerHandshakeError) as foreign:
                await client.ws_connect(
                    f"http://127.0.0.1:{port}/ws", origin="https://evil.example"
                )
            assert foreign.value.status == 403
    finally:
        server.terminate()
        server.wait(timeout=10)


def main_exit(argv: list[str]) -> int | str | None:
    """The exit code ``main(argv)`` ends with."""
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    return stopped.value.code
