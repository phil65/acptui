"""Details of a registry agent: what it is, its command, and actions to install and log in.

After toad's agent details (the agent store). Install and login commands run in the
real terminal while acptui is suspended, so their prompts and progress work as usual;
they are shown first and run only when the user picks the action.
"""

import asyncio
import shlex
import time
from typing import TYPE_CHECKING, ClassVar

from textual.app import SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from acptui.probe import ProbeResults, probe
from acptui.ui.screens.dialogs import DIALOG_CSS


if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import App, ComposeResult

    from acptui.config import AgentSpec
    from acptui.probe import ProbeResult


V2_TEXT = {
    True: ("speaks ACP v2", "$text-success"),
    False: ("does not speak ACP v2 yet: acptui will say so when it starts", "$text-warning"),
    None: ("ACP v2 support unknown: acptui says so if it answers with another version", ""),
}


async def run_in_terminal(app: App[object], command: str) -> int | None:
    """Run a shell command in the terminal, acptui suspended; its exit code.

    ``None``: the terminal cannot be handed over (e.g. in the browser).
    """
    try:
        with app.suspend():
            print(f"$ {command}", flush=True)
            process = await asyncio.create_subprocess_shell(command)
            return await process.wait()
    except SuspendNotSupported:
        return None


class AgentDetailsScreen(ModalScreen[str | None]):
    """An agent's details; returns ``start``, ``installed`` (after an install) or ``None``."""

    DEFAULT_CSS = (
        "AgentDetailsScreen {"
        + DIALOG_CSS
        + """ .detail { height: auto; margin: 0 0 1 0; }
        #actions { height: auto; }
        #actions Button { margin: 0 1 0 0; }
        }"""
    )

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close"),
        Binding("s,enter", "start", "Start"),
        Binding("i", "install", "Install"),
        Binding("l", "login", "Log in"),
        Binding("o", "homepage", "Homepage"),
        Binding("t", "test_v2", "Test ACP v2"),
        Binding("p", "pin", "Pin / unpin"),
    ]

    def __init__(
        self,
        spec: AgentSpec,
        cwd: Path,
        probe: ProbeResult | None = None,
        *,
        pinned: bool = False,
    ) -> None:
        super().__init__()
        self.pinned = pinned
        """Whether the agent is pinned to the top of the picker."""
        self.spec = spec
        self.cwd = cwd
        self.probe = probe
        """The last "Test v2" result for this agent, if any."""
        self.installed = False
        self.tested = False

    def compose(self) -> ComposeResult:
        spec = self.spec
        v2, style = V2_TEXT[spec.acp_v2]
        status = (
            Content("installed").stylize("$text-success")
            if spec.available
            else Content("not found").stylize("$text-error")
        )
        with Vertical(id="dialog"):
            yield Static(Content(spec.name), id="dialog-title")
            if spec.description:
                yield Static(Content(spec.description), classes="detail")
            lines = [
                Content.assemble(("command: ", "$text-muted"), spec.target, "  ", status),
                Content.assemble(("protocol: ", "$text-muted"), (v2, style)),
            ]
            if spec.homepage:
                lines.append(Content.assemble(("homepage: ", "$text-muted"), spec.homepage))
            if spec.install:
                lines.append(Content.assemble(("install: ", "$text-muted"), f"$ {spec.install}"))
            if spec.login:
                lines.append(Content.assemble(("log in: ", "$text-muted"), f"$ {spec.login}"))
            yield Static(Content("\n").join(lines), classes="detail")
            yield Static(self._probe_text(), id="probe", classes="detail")
            with Horizontal(id="actions"):
                yield Button("Start (s)", id="start", variant="primary", compact=True)
                if spec.install:
                    yield Button("Install (i)", id="install", compact=True)
                if spec.login:
                    yield Button("Log in (l)", id="login", compact=True)
                if spec.homepage:
                    yield Button("Homepage (o)", id="homepage", compact=True)
                yield Button("Test v2 (t)", id="test_v2", compact=True)
                yield Button(self._pin_label(), id="pin", compact=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id:
            await self.run_action(event.button.id)

    def _probe_text(self) -> Content:
        probe = self.probe
        if probe is None:
            return Content("tested: not yet (t runs initialize once)").stylize("$text-muted")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(probe.time))
        style = "$text-success" if probe.v2 else "$text-warning"
        return Content.assemble(("tested: ", "$text-muted"), (probe.summary, style), f"  ({when})")

    async def action_test_v2(self) -> None:
        """Run ``initialize`` once against the agent and show the protocol it negotiated."""
        panel = self.query_one("#probe", Static)
        panel.update(Content(f"testing {self.spec.target} …").stylize("$text-muted"))
        result = await probe(self.spec, self.cwd)
        ProbeResults().save(result)
        self.probe = result
        self.tested = True
        panel.update(self._probe_text())

    def _pin_label(self) -> str:
        return "Unpin (p)" if self.pinned else "Pin to top (p)"

    def action_pin(self) -> None:
        """Pin the agent to the top of the picker (``1``-``9`` start it), or unpin it."""
        from acptui.ui.screens.picker import PinToggled

        self.pinned = not self.pinned
        self.post_message(PinToggled(self.spec.id))
        self.query_one("#pin", Button).label = self._pin_label()

    def action_start(self) -> None:
        """Start a session with the agent."""
        self.dismiss("start")

    def action_close(self) -> None:
        """Close (``installed`` / ``tested`` if the picker should refresh)."""
        self.dismiss("installed" if self.installed else "tested" if self.tested else None)

    def action_homepage(self) -> None:
        """Open the agent's homepage in the browser."""
        if self.spec.homepage:
            self.app.open_url(self.spec.homepage)

    async def action_install(self) -> None:
        """Run the install command in the terminal."""
        if self.spec.install and await self._run(self.spec.install, "Install"):
            self.installed = True
            self.dismiss("installed")

    async def action_login(self) -> None:
        """Run the login command in the terminal."""
        if self.spec.login:
            await self._run(self.spec.login, "Log in")

    async def _run(self, command: str, title: str) -> bool:
        code = await run_in_terminal(self.app, command)
        if code is None:
            self.notify(
                f"Run it in a terminal: {command}", title=title, severity="warning", markup=False
            )
            return False
        if code != 0:
            self.notify(
                f"{shlex.split(command)[0]} exited with {code}", title=title, severity="error"
            )
            return False
        self.notify("Done", title=title, timeout=3)
        return True
