"""Images drawn in the terminal (textual-image), with a caption and fallbacks."""

import base64
import io
from typing import TYPE_CHECKING

from PIL import Image
import pytest

from acptui.config import Settings
from acptui.demo_scripts import PNG
from acptui.ui.app import AcpTui
from acptui.ui.widgets.blocks import AgentMessageView, ToolCallView, message_segments
from acptui.ui.widgets.image import ImageView, decode, image_key
from tests.test_app import SIZE, connected, send, stop_reason, until


if TYPE_CHECKING:
    from pathlib import Path

    from acptui.config import AgentSpec, ImageMode


def _png(size: tuple[int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 0, 0)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_decode_scales_large_images_down() -> None:
    image = decode(_png((4000, 1000)))
    assert not isinstance(image, str)
    assert image.size == (1600, 400)


def test_decode_reports_bad_data() -> None:
    assert decode("not base64!") == "invalid base64 data"
    assert str(decode(base64.b64encode(b"GIF89a broken").decode())).startswith("cannot decode")


def test_image_key_tells_images_apart() -> None:
    assert image_key(PNG) == image_key(PNG)
    assert image_key(PNG) != image_key(_png((2, 2)))


def test_message_segments_keep_the_order() -> None:
    from aclientp.schema.v2 import ImageContent, TextContent

    image = ImageContent(data=PNG, mime_type="image/png")
    blocks = [TextContent(text="a"), TextContent(text="b"), image, TextContent(text="c")]
    assert [kind for kind, _ in message_segments(blocks)] == ["markdown", "image", "markdown"]
    assert len(message_segments(blocks)[0][1]) == 2


@pytest.mark.parametrize("mode", ["auto", "halfcell", "unicode", "off"])
async def test_images_in_messages_and_tool_calls(
    demo_spec: AgentSpec, workdir: Path, mode: ImageMode
) -> None:
    app = AcpTui(Settings(images=mode), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        await send(pilot, "/media")
        await until(pilot, lambda: stop_reason(screen) == "end_turn")
        await until(pilot, lambda: len(screen.query(ImageView)) == 2)
        message = next(m for m in screen.query(AgentMessageView) if m.query_children(ImageView))
        assert message.source.startswith("Here is every kind of content block:")
        assert "image/png" not in message.source  # drawn, not a placeholder
        tool = next(t for t in screen.query(ToolCallView) if t.entry.tool_call_id == "media-tool")
        assert tool.query(ImageView)
        for view in screen.query(ImageView):
            caption = str(view.query_one(".image-caption").render())
            assert caption.startswith("image: image/png")
            if mode == "off":
                assert view.error == "images are turned off"
                assert not view.query(".image-pixels")
            else:
                assert view.error is None
                assert "96x48 px" in caption
                pixels = view.query_one(".image-pixels")
                assert pixels.size.width > 0
                assert pixels.size.height > 0


async def test_a_broken_image_shows_why(demo_spec: AgentSpec, workdir: Path) -> None:
    app = AcpTui(Settings(), workdir, demo_spec)
    async with app.run_test(size=SIZE) as pilot:
        screen = await connected(pilot, app)
        view = ImageView(base64.b64encode(b"nope").decode(), "image/png", uri="https://x/y.png")
        await screen.conversation.mount(view)
        await pilot.pause()
        caption = str(view.query_one(".image-caption").render())
        assert "https://x/y.png" in caption
        assert "cannot decode" in caption
