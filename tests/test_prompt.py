"""Building prompt content blocks from text and @-mentions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.schema.v2 import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    SkillReference,
    TextContent,
    TextResourceContents,
)

from acptui.protocol.prompt import (
    PromptOptions,
    build_prompt,
    find_mentions,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_find_mentions() -> None:
    text = r"look at @README.md, @src/main.py and @notes\ file.txt; mail me@example.com"
    assert find_mentions(text) == ["README.md", "src/main.py", "notes file.txt"]


def test_text_only_agent_gets_links(workdir: Path) -> None:
    blocks = build_prompt("explain @README.md @src", workdir, PromptOptions())

    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == "explain @README.md @src"
    readme, src = blocks[1:]
    assert isinstance(readme, ResourceLink)
    assert readme.uri == (workdir / "README.md").as_uri()
    assert readme.mime_type == "text/markdown"
    assert isinstance(src, ResourceLink)
    assert src.mime_type == "inode/directory"


def test_embedded_context_embeds_small_text_files(workdir: Path) -> None:
    blocks = build_prompt(r"read @notes\ file.txt", workdir, PromptOptions(embedded_context=True))

    embedded = blocks[1]
    assert isinstance(embedded, EmbeddedResource)
    assert isinstance(embedded.resource, TextResourceContents)
    assert embedded.resource.text == "some notes\n"


def test_images_are_attached_only_when_supported(workdir: Path) -> None:
    linked = build_prompt("see @image.png", workdir, PromptOptions(embedded_context=True))
    attached = build_prompt("see @image.png", workdir, PromptOptions(images=True))

    assert isinstance(linked[1], ResourceLink)
    assert isinstance(attached[1], ImageContent)
    assert attached[1].mime_type == "image/png"


def test_audio_is_attached_only_when_supported(workdir: Path) -> None:
    (workdir / "voice.wav").write_bytes(b"RIFF")
    blocks = build_prompt("hear @voice.wav", workdir, PromptOptions(audio=True))
    assert isinstance(blocks[1], AudioContent)
    assert isinstance(build_prompt("@voice.wav", workdir, PromptOptions())[1], ResourceLink)


def test_mentions_outside_cwd_or_missing_are_ignored(workdir: Path) -> None:
    blocks = build_prompt("@../etc/passwd @nope.txt", workdir, PromptOptions())
    assert len(blocks) == 1


def test_skills_become_skill_blocks(workdir: Path) -> None:
    options = PromptOptions(skills=frozenset({"review"}))
    blocks = build_prompt("/review the code", workdir, options)
    plain = build_prompt("/other thing", workdir, options)

    assert isinstance(blocks[0], SkillReference)
    assert blocks[0].name == "review"
    assert isinstance(blocks[1], TextContent)
    assert blocks[1].text == "the code"
    assert isinstance(plain[0], TextContent)
    assert plain[0].text == "/other thing"
