from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from aclientp.schema.v2 import EmbeddedResource, TextContent, TextResourceContents
import pytest

from acptui.config import Settings
from acptui.protocol.content import describe_block
from acptui.protocol.prompt import PromptOptions, shell_block
from acptui.protocol.state import MessageEntry
from acptui.shell import ProcessShell, PtyShell, ShellCommand, clean_output, pick_shell, plain_text
from acptui.ui.app import AcpTui
from acptui.ui.widgets.terminal import ShellView
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    from acptui.config import AgentSpec
    from acptui.shell import Shell


POSIX_SHELL = pick_shell()
needs_pty = pytest.mark.skipif(POSIX_SHELL is None, reason="needs bash or zsh (POSIX)")


async def finished(shell: Shell, command: str, timeout: float = 15) -> ShellCommand:
    """Run a command and wait until it is done."""
    entry = await shell.run(command)
    async with asyncio.timeout(timeout):
        while not entry.done:
            await asyncio.sleep(0.02)
    return entry


def test_clean_output_keeps_colors_only() -> None:
    raw = b"\x1b]0;title\x07\x1b[?2004h\x1b[31mred\x1b[0m\x1b[K done\r\n"
    assert clean_output(raw) == b"\x1b[31mred\x1b[0m done\r\n"
    assert plain_text(raw) == "red done"


def test_plain_text_resolves_carriage_return_redraws() -> None:
    assert plain_text(b"10%\r50%\r100%\r\nok\r\n") == "100%\nok"


def test_context_text_cuts_long_output() -> None:
    command = ShellCommand("seq 1000", "/tmp", exit_code=0)
    command.append("".join(f"{i}\n" for i in range(1000)).encode())
    text = command.context_text()
    assert text.startswith("$ seq 1000\n(exit code 0, in /tmp)")
    assert "lines omitted" in text
    assert "\n0\n" in text  # the start
    assert text.rstrip().endswith("999")  # the end


def test_shell_block_is_an_embedded_resource() -> None:
    command = ShellCommand("ls -la", "/tmp", exit_code=0)
    command.append(b"a\r\nb\r\n")
    block = shell_block(command, PromptOptions(embedded_context=True))
    assert isinstance(block, EmbeddedResource)
    assert isinstance(block.resource, TextResourceContents)
    assert "a\nb" in block.resource.text
    assert describe_block(block) == "shell: $ ls -la"
    fallback = shell_block(command, PromptOptions())
    assert isinstance(fallback, TextContent)
    assert "ls -la" in fallback.text


@needs_pty
async def test_pty_shell_keeps_state_between_commands(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert POSIX_SHELL is not None
    shell = PtyShell(POSIX_SHELL, tmp_path, None)
    try:
        assert plain_text(bytes((await finished(shell, "echo hello")).output)) == "hello"
        moved = await finished(shell, "cd sub && export ACPTUI_TEST=42")
        assert moved.exit_code == 0
        assert shell.cwd == str(tmp_path / "sub")
        after = await finished(shell, "echo $ACPTUI_TEST # a comment")
        assert plain_text(bytes(after.output)) == "42"
        assert after.cwd == str(tmp_path / "sub")
        assert (await finished(shell, "false")).exit_code == 1
        loop = await finished(shell, "for i in 1 2; do\n  echo $i\ndone")
        assert plain_text(bytes(loop.output)) == "1\n2"
    finally:
        shell.close()


@needs_pty
async def test_pty_shell_input_interrupt_and_exit(tmp_path: Path) -> None:
    assert POSIX_SHELL is not None
    shell = PtyShell(POSIX_SHELL, tmp_path, None)
    try:
        reading = await shell.run("read name; echo hi $name")
        await asyncio.sleep(0.2)
        await shell.send_input("there")
        async with asyncio.timeout(10):
            while not reading.done:
                await asyncio.sleep(0.02)
        assert plain_text(bytes(reading.output)) == "hi there"

        sleeping = await shell.run("sleep 30")
        await asyncio.sleep(0.3)
        await shell.interrupt()
        async with asyncio.timeout(10):
            while not sleeping.done:
                await asyncio.sleep(0.02)
        assert sleeping.exit_code == 130

        assert (await finished(shell, "exit 3")).exit_code == 3
        assert not shell.running
        again = await finished(shell, "echo back")  # a new shell, same directory
        assert plain_text(bytes(again.output)) == "back"
    finally:
        shell.close()


@needs_pty
async def test_second_interrupt_ends_a_shell_that_stopped_reporting(tmp_path: Path) -> None:
    assert POSIX_SHELL is not None
    shell = PtyShell(POSIX_SHELL, tmp_path, None)
    try:
        replaced = await shell.run("exec sh")  # no prompt hook any more: never finishes
        await asyncio.sleep(0.5)
        await shell.interrupt()
        await asyncio.sleep(0.5)
        assert not replaced.done
        await shell.interrupt()
        assert replaced.exit_code == -1
        assert not shell.busy
        fresh = await finished(shell, "pwd")
        assert plain_text(bytes(fresh.output)) == str(tmp_path)
    finally:
        shell.close()


async def test_process_shell_runs_each_command(tmp_path: Path) -> None:
    shell = ProcessShell(tmp_path, None)
    entry = await finished(shell, "echo hello")
    assert entry.exit_code == 0
    assert "hello" in plain_text(bytes(entry.output))
    failing = await finished(shell, "exit 2" if sys.platform != "win32" else "exit /b 2")
    assert failing.exit_code == 2


async def test_shell_mode_sends_output_with_the_next_prompt(
    demo_spec: AgentSpec, workdir: Path
) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        prompt = screen.prompt
        await pilot.press("!")
        assert prompt.shell_mode
        assert prompt.text == ""  # the "!" switches mode, it isn't typed
        await send(pilot, "echo from-the-shell")
        await until(pilot, lambda: bool(screen.shell_context))
        assert screen.query(ShellView)
        assert prompt.shell_history == ["echo from-the-shell"]

        await pilot.press("escape")
        assert not prompt.shell_mode
        await send(pilot, "/echo what did it print?")
        await until(pilot, lambda: stop_reason(screen) is not None)
        assert screen.state is not None
        user = [e for e in screen.state.entries if isinstance(e, MessageEntry) and e.role == "user"]
        shells = [b for b in user[-1].blocks if isinstance(b, EmbeddedResource)]
        assert len(shells) == 1
        assert isinstance(shells[0].resource, TextResourceContents)
        assert "from-the-shell" in shells[0].resource.text
        assert not screen.shell_context  # sent once


async def test_shell_output_can_be_dropped(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await pilot.press("!")
        await send(pilot, "echo nope")
        await until(pilot, lambda: bool(screen.shell_context))
        await pilot.press("ctrl+g")
        assert not screen.shell_context
        await pilot.press("backspace")  # on an empty prompt: back to the agent
        assert not screen.prompt.shell_mode
