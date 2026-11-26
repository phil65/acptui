"""Images from the agent, drawn in the terminal.

`textual-image <https://github.com/lnqs/textual-image>`_ draws them with the best
protocol the terminal has: the Terminal Graphics Protocol (kitty), Sixel, or colored
half cells (any color terminal; also the fallback when the output is not a terminal).
The terminal is probed when :mod:`textual_image.widget` is first imported, which must
happen before the app starts (``acptui.cli`` does it): :func:`prepare` imports it.

Images come from the block's ``data`` only; ACP v2 says clients should not fetch an
agent-supplied URI without user action, so ``uri`` is shown but never loaded.
"""

import base64
import binascii
import hashlib
import io
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from acptui.protocol.content import human_size


if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from textual.app import ComposeResult
    from textual.widget import Widget

    from acptui.config import ImageMode


MAX_PIXELS = 1600
"""Images are scaled down to at most this many pixels per side before drawing."""

MAX_BYTES = 20 * 1024 * 1024
"""Larger images are not decoded."""

_mode: ImageMode = "auto"


def prepare(mode: ImageMode) -> None:
    """Choose how images are drawn; ``auto`` probes the terminal (call before the app runs)."""
    global _mode
    _mode = mode
    if mode != "off":
        import textual_image.widget  # noqa: F401 - the import probes the terminal


def image_mode() -> ImageMode:
    """The mode set with :func:`prepare`."""
    return _mode


def decode(data: str) -> PILImage | str:
    """The image in base64 ``data``, scaled down if large, or why it cannot be shown."""
    from PIL import Image, UnidentifiedImageError

    if len(data) * 3 // 4 > MAX_BYTES:
        return "too large to show"
    try:
        raw = base64.b64decode(data, validate=True)
    except binascii.Error, ValueError:
        return "invalid base64 data"
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        return f"cannot decode: {type(exc).__name__}"
    if max(image.size) > MAX_PIXELS:
        image.thumbnail((MAX_PIXELS, MAX_PIXELS))
    return image


def image_key(data: str) -> str:
    """A short fingerprint of image data (to tell whether an image changed)."""
    return hashlib.blake2b(data.encode("ascii", "replace"), digest_size=8).hexdigest()


MAX_CELLS = (80, 24)
"""The largest an image is drawn: columns and rows."""


MIN_COLUMNS = 32
"""Small images are drawn at least this wide (when they have that many pixels)."""


def cell_size(width: int, height: int) -> tuple[int, int]:
    """Columns and rows for an image of ``width`` x ``height`` pixels.

    Its natural size in the terminal's cells, but at least :data:`MIN_COLUMNS` wide (tiny
    images stay visible) and at most :data:`MAX_CELLS`, keeping the aspect ratio.
    """
    from textual_image._terminal import get_cell_size  # cached by the probe

    cell = get_cell_size()
    cell_width, cell_height = max(cell.width, 1), max(cell.height, 1)
    columns = max(round(width / cell_width), min(MIN_COLUMNS, width), 1)
    columns = min(columns, MAX_CELLS[0])
    rows = max(1, round(columns * height / width * cell_width / cell_height))
    if rows > MAX_CELLS[1]:
        columns = max(1, round(columns * MAX_CELLS[1] / rows))
        rows = MAX_CELLS[1]
    return columns, rows


def _image_widget(image: PILImage, mode: ImageMode) -> Widget:
    from textual_image.widget import HalfcellImage, Image, SixelImage, TGPImage, UnicodeImage

    widget: Widget
    match mode:
        case "tgp":
            widget = TGPImage(image, classes="image-pixels")
        case "sixel":
            widget = SixelImage(image, classes="image-pixels")
        case "halfcell":
            widget = HalfcellImage(image, classes="image-pixels")
        case "unicode":
            widget = UnicodeImage(image, classes="image-pixels")
        case _:
            widget = Image(image, classes="image-pixels")
    columns, rows = cell_size(image.width, image.height)
    widget.styles.width = columns
    widget.styles.height = rows
    return widget


class ImageView(Vertical):
    """An image block: the picture, and a caption with its type, size and URI."""

    DEFAULT_CSS = """
    ImageView {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
        .image-pixels { max-width: 100%; }
        .image-caption { color: $text-muted; height: auto; }
    }
    """

    def __init__(
        self, data: str, mime_type: str, *, uri: str | None = None, mode: ImageMode | None = None
    ) -> None:
        super().__init__()
        self.data = data
        self.mime_type = mime_type
        self.uri = uri
        self.mode: ImageMode = mode or image_mode()
        self.key = image_key(data)
        self.error: str | None = None
        """Why the image is not drawn (``None`` if it is)."""

    def compose(self) -> ComposeResult:
        size = human_size(len(self.data) * 3 // 4)
        caption = f"image: {self.mime_type}, {size}"
        if self.uri:
            caption += f", {self.uri}"
        if self.mode == "off":
            self.error = "images are turned off"
        else:
            decoded = decode(self.data)
            if isinstance(decoded, str):
                self.error = decoded
            else:
                caption += f", {decoded.width}x{decoded.height} px"
                yield _image_widget(decoded, self.mode)
        if self.error:
            caption += f" ({self.error})"
        yield Static(Content(caption), classes="image-caption")
