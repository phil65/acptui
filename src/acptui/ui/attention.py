"""Messages from sessions that want the user's attention, and how the app answers them.

A session screen posts :class:`Attention` (the agent asks for permission or input, a
turn ended, the agent failed); the app rings the bell, plays a sound and shows a desktop
or terminal notification, as the ``[notifications]`` settings say. The terminal title
names the session and is marked while an agent waits for the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from textual.message import Message


if TYPE_CHECKING:
    from textual.screen import Screen

    from acptui.notify import Sound


AttentionKind = Literal["question", "turn_end", "error"]
"""Why: the agent waits for an answer, a turn ended, something failed."""

SOUNDS: dict[AttentionKind, Sound | None] = {
    "question": "question",
    "turn_end": "turn-over",
    "error": None,
}

WAITING_MARK = "● "
"""Prefix of the terminal title while an agent waits for the user."""


class Attention(Message):
    """A session wants the user's attention."""

    def __init__(self, kind: AttentionKind, title: str, message: str, source: Screen) -> None:
        super().__init__()
        self.kind: AttentionKind = kind
        self.title = title
        self.message = message
        self.source = source


@dataclass(frozen=True)
class Alert:
    """An alert the app raised (kept for the record and for tests)."""

    kind: AttentionKind
    title: str
    message: str
    outside: bool
    """Whether it was sent outside the app (notification, sound)."""


class TitleChanged(Message):
    """Something the terminal title shows changed (session title, waiting state)."""
