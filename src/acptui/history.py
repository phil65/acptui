"""Prompt history kept across sessions (``up`` / ``down`` in the prompt).

One JSON object per line in the platform state directory: the text, whether it was a
shell command, the working directory and the time. The file is trimmed to
:data:`MAX_ENTRIES` when it grows past twice that.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

import platformdirs


MAX_ENTRIES = 1000
"""Entries kept in the file (per kind, prompts and shell commands, together)."""


def history_path() -> Path:
    """Where the history is kept (``$ACPTUI_HISTORY`` overrides it)."""
    if override := os.environ.get("ACPTUI_HISTORY"):
        return Path(override)
    return platformdirs.user_state_path("acptui") / "history.jsonl"


@dataclass(frozen=True)
class HistoryEntry:
    """One prompt or shell command."""

    text: str
    shell: bool = False
    cwd: str = ""
    time: float = 0.0


class History:
    """The history file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or history_path()

    def load(self) -> list[HistoryEntry]:
        """All entries, oldest first (unreadable lines are skipped)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[HistoryEntry] = []
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(text := data.get("text"), str):
                entries.append(
                    HistoryEntry(
                        text,
                        shell=data.get("shell") is True,
                        cwd=str(data.get("cwd", "")),
                        time=float(data.get("time", 0.0)),
                    )
                )
        return entries

    def texts(self, *, shell: bool) -> list[str]:
        """The texts of one kind, oldest first, without repeats in a row."""
        texts: list[str] = []
        for entry in self.load():
            if entry.shell == shell and (not texts or texts[-1] != entry.text):
                texts.append(entry.text)
        return texts

    def append(self, text: str, *, shell: bool = False, cwd: str = "") -> bool:
        """Add an entry; whether it was written."""
        line = json.dumps({"text": text, "shell": shell, "cwd": cwd, "time": time.time()})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
            self._trim()
        except OSError:
            return False
        return True

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2 * MAX_ENTRIES:
            self.path.write_text("\n".join(lines[-MAX_ENTRIES:]) + "\n", encoding="utf-8")
