"""Turn scripts of the demo agent that exercise the rest of ACP v2."""

import asyncio
import base64
import struct
from typing import TYPE_CHECKING
import uuid
import zlib

from aclientp.schema.v2 import (
    AddDiffChange,
    AgentMessage,
    Annotations,
    AudioContent,
    BackgroundTerminal,
    BlobResourceContents,
    BooleanPropertySchema,
    CommandPermissionSubject,
    CompactionSummaryChunk,
    CompactionUpdate,
    CompleteElicitationNotification,
    Content,
    Diff,
    DiffPatch,
    ElicitationSchema,
    EmbeddedResource,
    EnumOption,
    FormElicitationRequest,
    IdleStateSessionUpdate,
    IdleStateUpdate,
    ImageContent,
    IntegerPropertySchema,
    MoveDiffChange,
    MultiSelectPropertySchema,
    Notice,
    NumberPropertySchema,
    PermissionOption,
    PlanEntry,
    PlanItems,
    PlanMarkdown,
    PlanRemoved,
    PlanUpdate,
    PromptSuggestion,
    PromptSuggestionsUpdate,
    RequestPermissionRequest,
    ResourceLink,
    RunningStateSessionUpdate,
    RunningStateUpdate,
    SelectedPermissionOutcome,
    SessionCancelCapabilities,
    SessionMessage,
    SessionMessageChunk,
    SessionReference,
    StringMultiSelectItems,
    StringPropertySchema,
    SubagentSessionCapabilities,
    SubagentUpdate,
    Terminal as TerminalContent,
    TerminalOutputChunk,
    TerminalSize,
    TerminalUpdate,
    TextContent,
    TextResourceContents,
    ToolCallCancelCapabilities,
    ToolCallCapabilities,
    ToolCallSessionUpdate,
    UnknownContent,
    UnknownSessionUpdate,
    UnknownToolCallContent,
    UrlElicitationRequest,
)
import anyio


if TYPE_CHECKING:
    from aclientp.schema.v2 import BackgroundWork

    from acptui.demo import DemoAgent, DemoSession


def _png(width: int = 96, height: int = 48) -> str:
    """A small generated PNG (a sunset: sky gradient, sun, sea), base64 encoded."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            sun = (x - width * 0.65) ** 2 + (y - height * 0.45) ** 2 < (height * 0.18) ** 2
            if y > height * 0.6:  # sea, with a reflection under the sun
                shimmer = abs(x - width * 0.65) < height * 0.12 and y % 3 == 0
                pixel = (250, 200, 90) if shimmer else (20, 50 + y, 110 + y)
            elif sun:
                pixel = (255, 215, 80)
            else:
                t = y / (height * 0.6)
                pixel = (int(40 + 210 * t), int(30 + 90 * t), int(120 - 40 * t))
            rows.extend(pixel)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


# A generated PNG and a 44-byte WAV header with no samples.
PNG = _png()
WAV = base64.b64encode(
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00"
    b"\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
).decode()


async def media(agent: DemoAgent, session: DemoSession) -> None:
    """Every kind of content block, in a message and in a tool call."""
    readme = f"{session.cwd}/README.md"
    blocks = [
        TextContent(text="Here is every kind of content block:"),
        ImageContent(data=PNG, mime_type="image/png"),
        AudioContent(data=WAV, mime_type="audio/wav"),
        ResourceLink(
            name="README.md",
            uri=f"file://{readme}",
            title="Project readme",
            description="What the project is",
            size=1234,
        ),
        EmbeddedResource(
            resource=TextResourceContents(
                uri=f"file://{session.cwd}/hello.py", text="print('hello')\n"
            )
        ),
        EmbeddedResource(
            resource=BlobResourceContents(uri=f"file://{session.cwd}/data.bin", blob="AAEC")
        ),
        TextContent(
            text="A note only for the model.", annotations=Annotations(audience=["assistant"])
        ),
        UnknownContent.model_validate({"type": "_demo_widget", "value": 1}),
    ]
    await agent._send(session, AgentMessage(message_id=f"media-{uuid.uuid4()}", content=blocks))
    await agent._send(
        session,
        ToolCallSessionUpdate(
            tool_call_id="media-tool",
            title="Generate assets",
            kind="other",
            status="completed",
            raw_input={"prompt": "a dot"},
            raw_output={"files": 2},
            content=[
                Content(content=ImageContent(data=PNG, mime_type="image/png")),
                Content(
                    content=ResourceLink(name="report.pdf", uri="https://example.com/report.pdf")
                ),
                UnknownToolCallContent.model_validate({"type": "_chart", "points": [1, 2, 3]}),
                Diff(
                    changes=[
                        MoveDiffChange(
                            old_path=f"{session.cwd}/old.txt", path=f"{session.cwd}/new.txt"
                        ),
                        AddDiffChange(
                            path=f"{session.cwd}/logo.png",
                            file_type="binary",
                            mime_type="image/png",
                        ),
                    ]
                ),
                Diff(
                    changes=[AddDiffChange(path=f"{session.cwd}/x.txt")],
                    patch=DiffPatch(format="_custom", text="+x"),
                ),
            ],
        ),
    )


async def subagent(agent: DemoAgent, session: DemoSession) -> None:
    """A child session: subagent updates, session messages, its own conversation."""
    from acptui.demo import DemoSession as Session

    child = Session(
        str(uuid.uuid4()), session.cwd, parent_id=session.session_id, title="Researcher"
    )
    agent.sessions[child.session_id] = child
    caps = SubagentSessionCapabilities(cancel=SessionCancelCapabilities())
    await agent._send(
        session,
        SubagentUpdate(
            session_id=child.session_id,
            title="Researcher",
            description="Looks things up for the **main** agent",
            capabilities=caps,
            state=RunningStateUpdate(),
            cwd=session.cwd,
        ),
    )
    await agent._send(
        session,
        ToolCallSessionUpdate(
            tool_call_id="delegate-1",
            title="Delegate to the researcher",
            kind="other",
            status="in_progress",
            content=[SessionReference(session_id=child.session_id)],
        ),
    )
    outgoing = f"msg-{uuid.uuid4()}"
    await agent._send(
        session,
        SessionMessage(
            message_id=outgoing,
            sender_session_id=session.session_id,
            recipient_session_id=child.session_id,
            content=[TextContent(text="Please find the README.")],
        ),
    )
    await agent._send(child, RunningStateSessionUpdate())
    await agent._stream(child, "I found the README in the project root.")
    await agent._send(
        child,
        ToolCallSessionUpdate(
            tool_call_id="child-read", title="Read README.md", kind="read", status="completed"
        ),
    )
    await agent._send(child, IdleStateSessionUpdate(stop_reason="end_turn"))
    reply = f"msg-{uuid.uuid4()}"
    await agent._send(
        session,
        SessionMessage(
            message_id=reply,
            sender_session_id=child.session_id,
            recipient_session_id=session.session_id,
        ),
    )
    await agent._send(
        session, SessionMessageChunk(message_id=reply, content=TextContent(text="Found it: "))
    )
    await agent._send(
        session, SessionMessageChunk(message_id=reply, content=TextContent(text="README.md"))
    )
    await agent._send(
        session,
        SubagentUpdate(
            session_id=child.session_id,
            state=IdleStateUpdate(stop_reason="end_turn"),
            capabilities=None,
        ),
    )
    await agent._send(session, ToolCallSessionUpdate(tool_call_id="delegate-1", status="completed"))
    await agent._stream(session, "The researcher is done.")


async def elicit_form(agent: DemoAgent, session: DemoSession) -> None:
    """Ask for structured input with a form."""
    schema = ElicitationSchema(
        title="Project details",
        properties={
            "name": StringPropertySchema(title="Name", min_length=2),
            "license": StringPropertySchema(
                title="License", enum=["MIT", "AGPL-3.0"], default="MIT"
            ),
            "level": StringPropertySchema(
                title="Level",
                one_of=[
                    EnumOption(const="low", title="Low"),
                    EnumOption(const="high", title="High"),
                ],
            ),
            "ratio": NumberPropertySchema(title="Ratio", minimum=0, maximum=1),
            "count": IntegerPropertySchema(title="Count", default=3),
            "public": BooleanPropertySchema(title="Public", default=True),
            "tags": MultiSelectPropertySchema(
                title="Tags", items=StringMultiSelectItems(enum=["cli", "tui"])
            ),
        },
        required=["name"],
    )
    response = await agent.conn.create_elicitation(
        FormElicitationRequest(
            message="Tell me about the project",
            session_id=session.session_id,
            requested_schema=schema,
        )
    )
    await agent._stream(session, f"Elicitation result: `{response.model_dump_json(by_alias=True)}`")


async def elicit_url(agent: DemoAgent, session: DemoSession) -> None:
    """Ask the user to visit a URL, then report it complete."""
    elicitation_id = f"url-{uuid.uuid4()}"
    request = UrlElicitationRequest(
        message="Sign in to the demo service",
        session_id=session.session_id,
        elicitation_id=elicitation_id,
        url="https://example.com/sign-in",
    )
    task = asyncio.create_task(agent.conn.create_elicitation(request))
    await anyio.sleep(0.3)
    await agent.conn.complete_elicitation(
        CompleteElicitationNotification(elicitation_id=elicitation_id)
    )
    response = await task
    await agent._stream(session, f"URL elicitation: {response.action}")


async def compact(agent: DemoAgent, session: DemoSession) -> None:
    """A context compaction with a streamed summary."""
    await agent._send(session, CompactionUpdate(compaction_id="c1", status="in_progress"))
    await agent._send(
        session,
        CompactionSummaryChunk(compaction_id="c1", content=TextContent(text="Summary: we talked.")),
    )
    await agent._send(session, CompactionUpdate(compaction_id="c1", status="completed"))


async def suggest(agent: DemoAgent, session: DemoSession) -> None:
    """Suggest what to send next."""
    await agent._stream(session, "Here are some ideas for what to do next.")
    await agent._send(
        session,
        PromptSuggestionsUpdate(
            suggestions=[
                PromptSuggestion(prompt=[TextContent(text="/echo try the suggestion")]),
                PromptSuggestion(prompt=[TextContent(text="/media")]),
            ]
        ),
    )


async def plans(agent: DemoAgent, session: DemoSession) -> None:
    """A markdown plan and an item plan that is removed again."""
    await agent._send(
        session,
        PlanUpdate(
            plan=PlanMarkdown(
                plan_id="md", content="1. Think\n2. Act", path=f"{session.cwd}/PLAN.md"
            )
        ),
    )
    items = [PlanEntry(content="Temporary", priority="low", status="pending")]
    await agent._send(session, PlanUpdate(plan=PlanItems(plan_id="tmp", entries=items)))
    await agent._send(session, PlanRemoved(plan_id="tmp"))


async def notices(agent: DemoAgent, session: DemoSession) -> None:
    """Notices of every severity."""
    for severity in ("info", "warning", "error"):
        await agent._send(
            session,
            Notice(severity=severity, title=f"A {severity} notice", description="Details here."),
        )


async def background(agent: DemoAgent, session: DemoSession) -> list[BackgroundWork]:
    """A dev server that keeps running after the turn."""
    await agent._send(
        session,
        ToolCallSessionUpdate(
            tool_call_id="server-1",
            title="npm run dev",
            kind="execute",
            status="completed",
            content=[TerminalContent(terminal_id="bg-1")],
        ),
    )
    await agent._send(
        session,
        TerminalUpdate(
            terminal_id="bg-1",
            command="npm run dev",
            cwd=session.cwd,
            size=TerminalSize(cols=80, rows=24),
        ),
    )
    await agent._send(session, TerminalOutputChunk.from_bytes("bg-1", b"listening on :3000\n"))
    await agent._stream(session, "The dev server keeps running in the background.")
    return [BackgroundTerminal(terminal_id="bg-1")]


async def extension(agent: DemoAgent, session: DemoSession) -> None:
    """Custom messages: a ``_`` notification and a custom session update."""
    await agent.conn.ext_notification("_demo/hello", {"greeting": "hi"})
    custom = UnknownSessionUpdate.model_validate({"sessionUpdate": "_demo_custom", "x": 1})
    await agent._send(session, custom)
    await agent._stream(session, "Sent custom messages.")


async def command_permission(agent: DemoAgent, session: DemoSession) -> None:
    """Permission for a command (instead of a tool call)."""
    response = await agent.conn.request_permission(
        RequestPermissionRequest(
            session_id=session.session_id,
            title="Run a command?",
            subject=CommandPermissionSubject(command="rm -rf build", cwd=session.cwd),
            options=[
                PermissionOption(option_id="yes", name="Run", kind="allow_once"),
                PermissionOption(option_id="never", name="Never", kind="reject_always"),
            ],
        )
    )
    outcome = response.outcome
    choice = (
        outcome.option_id if isinstance(outcome, SelectedPermissionOutcome) else outcome.outcome
    )
    await agent._stream(session, f"Permission outcome: {choice}")


async def cancellable_tool(agent: DemoAgent, session: DemoSession) -> None:
    """A tool call the client can cancel with ``tool_call/cancel``."""
    caps = ToolCallCapabilities(cancel=ToolCallCancelCapabilities())
    await agent._send(
        session,
        ToolCallSessionUpdate(
            tool_call_id="long-1",
            title="Long download",
            kind="fetch",
            status="in_progress",
            capabilities=caps,
        ),
    )
    for _ in range(200):
        if "long-1" in session.cancelled_tools:
            await agent._send(
                session,
                ToolCallSessionUpdate(tool_call_id="long-1", status="cancelled", capabilities=None),
            )
            await agent._stream(session, "The download was cancelled.")
            return
        await anyio.sleep(0.05)
    await agent._send(
        session, ToolCallSessionUpdate(tool_call_id="long-1", status="completed", capabilities=None)
    )
