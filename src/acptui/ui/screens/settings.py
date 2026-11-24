"""The settings screen (``f3``, palette "Settings"): change settings and save them.

After toad's settings screen. The entries are the display settings of ``config.toml``;
choosing one cycles through its values (themes open a list). Changes apply at once where
they can and are written back to ``config.toml`` with its comments and layout kept
(tomlkit); agents, MCP servers and the other tables are left as they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, get_args

from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option
import tomlkit

from acptui.config import NotificationSettings, Settings
from acptui.ui.screens.dialogs import DIALOG_CSS


if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult


SettingValue = str | bool


@dataclass(frozen=True)
class SettingField:
    """One entry of the settings screen."""

    key: str
    """``name`` or ``notifications.name``."""
    title: str
    help: str
    restart: bool = False
    """Takes effect for new tabs (or after a restart) only."""


FIELDS: tuple[SettingField, ...] = (
    SettingField("theme", "Theme", "Colors of the app"),
    SettingField("diff_view", "Diff view", "Unified, split, or split when wide enough"),
    SettingField("expand_thoughts", "Expand thoughts", "Show the agent's thinking expanded"),
    SettingField(
        "tool_expand", "Tool calls", "auto (by kind), always, never, or failed ones", True
    ),
    SettingField("tabs", "Tab bar", "Always, with several tabs, or never"),
    SettingField(
        "images", "Images", "How images are drawn (auto: the best the terminal has)", True
    ),
    SettingField("auto_copy", "Copy on select", "Copy text to the clipboard when you select it"),
    SettingField("history", "History", "Keep prompts and recent sessions across sessions", True),
    SettingField("notifications.when", "Notify", "Outside the app: never, when away, always"),
    SettingField("notifications.method", "Notify via", "Desktop, terminal, or auto"),
    SettingField("notifications.sounds", "Sounds", "Play a sound with notifications"),
    SettingField("notifications.turn_end", "Notify on turn end", "Not only on questions"),
    SettingField("notifications.title", "Terminal title", "Show the session, mark waiting"),
)


def choices(key: str) -> tuple[SettingValue, ...]:
    """The values a setting can take (from the settings model), ``()`` for themes."""
    model: type[Settings | NotificationSettings] = Settings
    name = key
    if key.startswith("notifications."):
        model, name = NotificationSettings, key.removeprefix("notifications.")
    annotation = model.model_fields[name].annotation
    if annotation is bool:
        return (True, False)
    if getattr(annotation, "__origin__", None) is Literal:
        return tuple(get_args(annotation))
    args = get_args(annotation)  # a type alias of a Literal
    value = getattr(annotation, "__value__", None)
    if value is not None:
        return tuple(get_args(value))
    return tuple(arg for arg in args if isinstance(arg, str))


def get_value(settings: Settings, key: str) -> SettingValue:
    """A setting's current value."""
    if key.startswith("notifications."):
        value = getattr(settings.notifications, key.removeprefix("notifications."))
    else:
        value = getattr(settings, key)
    if not isinstance(value, str | bool):
        msg = f"{key} is not a simple setting"
        raise TypeError(msg)
    return value


def with_value(settings: Settings, key: str, value: SettingValue) -> Settings:
    """``settings`` with one setting changed (validated)."""
    data = settings.model_dump()
    if key.startswith("notifications."):
        data["notifications"][key.removeprefix("notifications.")] = value
    else:
        data[key] = value
    return Settings.model_validate(data)


def save_setting(path: Path, key: str, value: SettingValue) -> None:
    """Write one setting into ``config.toml``, keeping everything else in the file."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    document = tomlkit.parse(text)
    if key.startswith("notifications."):
        table = document.get("notifications")
        if table is None:
            table = tomlkit.table()
            document["notifications"] = table
        table[key.removeprefix("notifications.")] = value
    else:
        document[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def show(value: SettingValue) -> str:
    """A value for the list."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


class SettingsChanged(Message):
    """A setting changed (the app applies it)."""

    def __init__(self, settings: Settings, key: str) -> None:
        super().__init__()
        self.settings = settings
        self.key = key


class SettingsScreen(ModalScreen[None]):
    """Change and save settings; ``enter`` cycles a value (themes: a list)."""

    DEFAULT_CSS = "SettingsScreen {" + DIALOG_CSS + " OptionList { max-height: 30; } }"
    BINDINGS: ClassVar = [Binding("escape", "back", "Back")]

    def __init__(self, settings: Settings, path: Path, themes: list[str]) -> None:
        super().__init__()
        self.settings = settings
        self.path = path
        self.themes = themes
        self.picking_theme = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Settings", id="dialog-title")
            yield OptionList(id="settings")
            yield Static(Content(f"enter: change · saved to {self.path}"), id="dialog-hint")

    def on_mount(self) -> None:
        self._show_fields()

    def _show_fields(self, highlight: int = 0) -> None:
        self.picking_theme = False
        options = self.query_one(OptionList)
        options.clear_options()
        for field in FIELDS:
            value = show(get_value(self.settings, field.key))
            note = "  (new tabs / restart)" if field.restart else ""
            options.add_option(
                Option(
                    Content.assemble(
                        (field.title, "bold"),
                        ": ",
                        (value, "$text-accent"),
                        (note, "$text-muted italic"),
                        "\n  ",
                        (field.help, "$text-muted"),
                    ),
                    id=field.key,
                )
            )
        options.highlighted = highlight
        options.focus()

    def _show_themes(self) -> None:
        self.picking_theme = True
        options = self.query_one(OptionList)
        options.clear_options()
        current = self.settings.theme
        for theme in self.themes:
            marker = "● " if theme == current else "  "
            options.add_option(Option(marker + theme, id=f"theme:{theme}"))
        options.highlighted = self.themes.index(current) if current in self.themes else 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = event.option_id or ""
        if key.startswith("theme:"):
            self.change("theme", key.removeprefix("theme:"))
            self._show_fields(0)
            return
        if key == "theme":
            self._show_themes()
            return
        values = choices(key)
        current = get_value(self.settings, key)
        new = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
        self.change(key, new)
        self._show_fields(next(i for i, field in enumerate(FIELDS) if field.key == key))

    def change(self, key: str, value: SettingValue) -> None:
        """Change a setting: in the app now, and in the config file."""
        self.settings = with_value(self.settings, key, value)
        try:
            save_setting(self.path, key, value)
        except (OSError, ValueError) as exc:
            self.notify(f"Couldn't save {key}: {exc}", severity="error")
        self.post_message(SettingsChanged(self.settings, key))

    def action_back(self) -> None:
        """Back to the settings, or close."""
        if self.picking_theme:
            self._show_fields(0)
        else:
            self.dismiss(None)
