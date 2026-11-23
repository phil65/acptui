"""Shell mode: commands the user runs from the prompt (``!command``), next to the agent.

On POSIX one interactive bash or zsh runs in a pseudo-terminal for the whole session,
so ``cd``, ``export`` and an activated virtualenv carry over from one command to the
next. Its prompt and echo are switched off; before each prompt the shell itself prints
a marker with the exit code and working directory (``PROMPT_COMMAND`` / ``precmd``), so
commands may span lines, contain comments or read from stdin. Elsewhere (Windows, or
neither bash nor zsh installed) each command runs as its own process in the directory
the previous ``cd``-free commands left off: nothing carries over.

Pagers are set to ``cat``; full-screen programs (editors, ``htop``) need a real terminal.

The PTY approach (``setsid``, the controlling terminal, the working directory read
from an escape sequence) follows toad's shell (https://github.com/batrachianai/toad,
``src/toad/shell.py``), Copyright (c) Will McGugan, AGPL-3.0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import signal
import sys


INIT_TIMEOUT = 10.0
"""Seconds to wait for a new shell to come up (rc files may be slow)."""

MAX_OUTPUT_BYTES = 1024 * 1024
"""Output kept per command (the start is dropped beyond this)."""

MAX_CONTEXT_LINES = 200
"""Lines of a command's output sent to the agent (head and tail beyond this)."""

MAX_CONTEXT_CHARS = 20_000

# The marker the shell prints before each prompt: ESC ] 6973 ; exit status ; cwd BEL
_MARK = re.compile(rb"\x1b\]6973;(\d+);([^\x07]*)\x07")
_MARK_PRINTF = 'printf \'\\033]6973;%s;%s\\007\' "$?" "$PWD"'

_BASH_INIT = (
    "bind 'set enable-bracketed-paste off' 2>/dev/null; PS1=''; PS2=''; "
    f"__acptui_mark() {{ {_MARK_PRINTF}; }}; PROMPT_COMMAND=__acptui_mark; stty -echo"
)
_ZSH_INIT = (
    "unsetopt zle prompt_sp prompt_cr; PROMPT=''; RPROMPT=''; PS2=''; preexec_functions=(); "
    f"__acptui_mark() {{ {_MARK_PRINTF} }}; precmd_functions=(__acptui_mark); stty -echo"
)

# Escape sequences: SGR (colors) is kept for display, the rest is dropped.
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(rb"\x1b\[([0-?]*)[ -/]*([@-~])")
_OTHER_ESC = re.compile(rb"\x1b[=>78]|\x1b[()][0-9A-Za-z]")
_SGR = re.compile(r"\x1b\[[0-9;]*m")

SHELL_ENV = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "CLICOLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "MANPAGER": "cat",
    "DISABLE_AUTO_UPDATE": "true",  # oh-my-zsh: no "time to update?" prompt
    "ACPTUI": "1",
}
"""Environment for the shell on top of acptui's own."""


@dataclass(eq=False)
class ShellCommand:
    """One command the user ran, with its output and (once finished) exit code."""

    command: str
    cwd: str
    """Working directory the command started in."""
    output: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    """``None`` while running; ``-1`` if the shell went away."""

    @property
    def done(self) -> bool:
        """Whether the command finished."""
        return self.exit_code is not None

    def append(self, data: bytes) -> None:
        """Add output, keeping at most :data:`MAX_OUTPUT_BYTES`."""
        self.output.extend(data)
        if len(self.output) > MAX_OUTPUT_BYTES:
            del self.output[: len(self.output) - MAX_OUTPUT_BYTES]

    @property
    def display_output(self) -> bytes:
        """The output with colors, without other control sequences (for display)."""
        return clean_output(bytes(self.output))

    def context_text(self) -> str:
        """The command and its output as plain text for the agent (long output cut)."""
        text = plain_text(bytes(self.output))
        lines = text.splitlines()
        if len(lines) > MAX_CONTEXT_LINES:
            head, tail = MAX_CONTEXT_LINES // 5, MAX_CONTEXT_LINES - MAX_CONTEXT_LINES // 5
            omitted = len(lines) - head - tail
            lines = [*lines[:head], f"[... {omitted} lines omitted ...]", *lines[-tail:]]
        body = "\n".join(lines)
        if len(body) > MAX_CONTEXT_CHARS:
            body = (
                f"[... output cut to its last {MAX_CONTEXT_CHARS} characters ...]\n"
                + (body[-MAX_CONTEXT_CHARS:])
            )
        status = "still running" if self.exit_code is None else f"exit code {self.exit_code}"
        return f"$ {self.command}\n({status}, in {self.cwd})\n\n{body}\n"


def clean_output(data: bytes) -> bytes:
    """Terminal output without escape sequences other than SGR (colors)."""
    data = _OSC.sub(b"", data)
    data = _CSI.sub(lambda match: match.group(0) if match.group(2) == b"m" else b"", data)
    return _OTHER_ESC.sub(b"", data)


def plain_text(data: bytes) -> str:
    """Terminal output as plain text: no escape sequences, carriage-return redraws resolved."""
    text = _SGR.sub("", clean_output(data).decode("utf-8", errors="replace"))
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        segments = [segment for segment in line.split("\r") if segment]
        lines.append(segments[-1] if segments else "")
    return "\n".join(lines).strip("\n")


OutputCallback = Callable[[ShellCommand], None]


class Shell:
    """Runs the user's commands one at a time; subclasses decide how."""

    def __init__(self, cwd: str | Path, on_output: OutputCallback | None = None) -> None:
        self.cwd = str(cwd)
        """Where the next command starts (follows ``cd`` in a persistent shell)."""
        self.on_output = on_output
        """Called with the running command whenever it has new output or finishes."""
        self.current: ShellCommand | None = None
        self.persistent = False
        """Whether state (``cd``, variables) carries over between commands."""

    @property
    def busy(self) -> bool:
        """Whether a command is running."""
        return self.current is not None

    async def run(self, command: str) -> ShellCommand:
        """Start a command; it finishes in the background (see :attr:`ShellCommand.done`)."""
        raise NotImplementedError

    async def send_input(self, text: str) -> None:
        """Type a line into the running command."""
        raise NotImplementedError

    async def interrupt(self) -> None:
        """Interrupt the running command (Ctrl+C)."""
        raise NotImplementedError

    def close(self) -> None:
        """End the shell and whatever runs in it."""

    def _notify(self, command: ShellCommand) -> None:
        if self.on_output is not None:
            self.on_output(command)

    def _finish(self, command: ShellCommand, exit_code: int) -> None:
        command.exit_code = exit_code
        if self.current is command:
            self.current = None
        self._notify(command)


def pick_shell() -> str | None:
    """Bash or zsh for a persistent shell: ``$SHELL`` if it is one, else what's installed."""
    if sys.platform == "win32":
        return None
    configured = os.environ.get("SHELL", "")
    if Path(configured).name in {"bash", "zsh"} and Path(configured).exists():
        return configured
    return shutil.which("bash") or shutil.which("zsh")


def open_shell(cwd: str | Path, on_output: OutputCallback | None = None) -> Shell:
    """A persistent PTY shell where possible, else one process per command."""
    program = pick_shell()
    if program is None:
        return ProcessShell(cwd, on_output)
    return PtyShell(program, cwd, on_output)


class PtyShell(Shell):
    """An interactive bash / zsh in a pseudo-terminal, started on the first command."""

    def __init__(self, program: str, cwd: str | Path, on_output: OutputCallback | None) -> None:
        super().__init__(cwd, on_output)
        self.program = program
        self.persistent = True
        self._master: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._buffer = bytearray()
        self._ready: asyncio.Event | None = None
        self._exit_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the shell process is up."""
        return self._master is not None

    async def start(self) -> None:
        """Start the shell and wait until it is set up (prompt and echo off)."""
        if sys.platform == "win32":
            msg = "No pseudo-terminals on Windows"
            raise OSError(msg)
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()

        def controlling_terminal() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self._process = await asyncio.create_subprocess_exec(
            self.program,
            "-i",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.cwd,
            env={**os.environ, **SHELL_ENV},
            preexec_fn=controlling_terminal,
        )
        os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._ready = asyncio.Event()
        asyncio.get_running_loop().add_reader(master, self._on_readable)
        init = _ZSH_INIT if Path(self.program).name == "zsh" else _BASH_INIT
        self._write(f" {init}\n")
        try:
            await asyncio.wait_for(self._ready.wait(), INIT_TIMEOUT)
        except TimeoutError:
            self.close()
            msg = f"{self.program} did not come up within {INIT_TIMEOUT:.0f}s"
            raise OSError(msg) from None

    async def run(self, command: str) -> ShellCommand:
        if self.busy:
            msg = "A command is still running"
            raise RuntimeError(msg)
        if not self.running:
            await self.start()
        entry = ShellCommand(command, self.cwd)
        self.current = entry
        self._write(command.rstrip("\n") + "\n")
        return entry

    async def send_input(self, text: str) -> None:
        self._write(text.rstrip("\n") + "\n")

    async def interrupt(self) -> None:
        self._write(b"\x03")

    def _write(self, data: str | bytes) -> None:
        if self._master is None:
            return
        with contextlib.suppress(OSError):
            os.write(self._master, data.encode() if isinstance(data, str) else data)

    def _on_readable(self) -> None:
        if self._master is None:
            return
        try:
            data = os.read(self._master, 64 * 1024)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._gone()
            return
        self._buffer.extend(data)
        self._drain()

    def _drain(self) -> None:
        while match := _MARK.search(self._buffer):
            status, cwd = int(match.group(1)), match.group(2).decode(errors="replace")
            before = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            if cwd:
                self.cwd = cwd
            if self._ready is not None and not self._ready.is_set():
                self._ready.set()  # set up: what came before (rc file noise) is dropped
                continue
            if (command := self.current) is not None:
                if before:
                    command.append(before)
                self._finish(command, status)
        if (command := self.current) is not None and self._buffer:
            # Keep a possible partial marker; everything before it is output.
            keep = self._buffer.rfind(b"\x1b")
            cut = keep if keep != -1 and b"\x07" not in self._buffer[keep:] else len(self._buffer)
            if cut:
                command.append(bytes(self._buffer[:cut]))
                del self._buffer[:cut]
                self._notify(command)

    def _gone(self) -> None:
        """The shell exited (``exit``) or its terminal closed."""
        process = self._process
        self._process = None
        self._teardown()
        if (command := self.current) is None:
            return
        if process is None:
            self._finish(command, -1)
            return

        async def finish() -> None:
            # The terminal closes before the exit status is known
            try:
                code = await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                code = -1
            self._finish(command, code)

        self._exit_task = asyncio.get_running_loop().create_task(finish())

    def _teardown(self) -> None:
        if self._master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._master)
            with contextlib.suppress(OSError):
                os.close(self._master)
        self._master = None
        self._buffer.clear()

    def close(self) -> None:
        process = self._process
        self._teardown()
        if sys.platform != "win32" and process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGHUP)
        self._process = None


class ProcessShell(Shell):
    """One process per command (Windows, or no bash / zsh): nothing carries over."""

    def __init__(self, cwd: str | Path, on_output: OutputCallback | None) -> None:
        super().__init__(cwd, on_output)
        self._process: asyncio.subprocess.Process | None = None

    async def run(self, command: str) -> ShellCommand:
        if self.busy:
            msg = "A command is still running"
            raise RuntimeError(msg)
        entry = ShellCommand(command, self.cwd)
        self.current = entry
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
                env={**os.environ, **SHELL_ENV},
            )
        except OSError as exc:
            entry.append(f"{exc}\n".encode())
            self._finish(entry, -1)
            return entry
        self._process = process
        asyncio.get_running_loop().create_task(self._pump(entry, process))
        return entry

    async def _pump(self, entry: ShellCommand, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            entry.append(chunk)
            self._notify(entry)
        code = await process.wait()
        self._process = None
        self._finish(entry, code)

    async def send_input(self, text: str) -> None:
        process = self._process
        if process is not None and process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.write(text.rstrip("\n").encode() + b"\n")
                await process.stdin.drain()

    async def interrupt(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        self._process = None
