# acptui – agent guide

## Overview

acptui is a Textual terminal UI for AI coding agents speaking the Agent Client
Protocol **v2 only** (draft schema pinned by aclientp). The protocol layer uses
aclientp's typed v2 client (`aclientp.v2.client`, models in `aclientp.schema.v2`);
there is no v1 code path. AGPL-3.0-or-later; a few UI pieces are adapted from toad
(see `NOTICE`).

## Architecture

- `src/acptui/protocol/` – no Textual imports, testable alone.
  - `connection.py`: `AgentConnection` launches the agent (stdio subprocess via
    aclientp `spawn_stdio_transport(on_stderr=)`, or `connect_remote_transport` for
    http(s)/ws(s)), negotiates v2 in `initialize`, exposes every agent method as a
    typed call and capability property, and forwards everything the agent sends to
    the `AgentEvents` protocol. `SessionSetup` = MCP servers, system prompt,
    additional directories, initial config options (filtered by capabilities).
  - `state.py`: `SessionState.apply(update) -> list[Change]` applies `session/update`
    upserts with v2 patch semantics (`MISSING` = unchanged, `None` = clear) into
    timeline entries (`MessageEntry`, `ToolCallEntry`, `PlanEntry`, `NoticeEntry`,
    `CompactionEntry`, `SubagentEntry`) plus terminals, cursors, commands, config
    options, usage, suggestions, background work.
  - `prompt.py` (prompt text + `@` mentions -> content blocks), `content.py` (blocks
    -> Markdown), `diff.py` (git patch parser).
- `src/acptui/ui/`
  - `app.py`: `AcpTui` app, command palette provider (`session_commands`).
  - `screens/session.py`: `SessionScreen` implements `AgentEvents`; routes updates by
    session id (`self.sessions`: main session + subagent sessions), runs the agent in a
    worker, shows dialogs via `_ask` (closed on `$/cancel_request` / disconnect).
  - `widgets/conversation.py`: `ConversationView` renders `Change`s in order from a
    queue (one render loop per view); `widgets/blocks.py` has one widget per entry
    kind; `diff.py`, `terminal.py`, `prompt.py` (editor + completion), `status.py`.
  - `screens/`: `permission.py`, `elicitation.py`, `dialogs.py` (session list, config,
    fork, rename, confirm, help), `providers.py`, `picker.py`, `log.py`.
- `src/acptui/demo.py` + `demo_scripts.py`: scripted v2 agent (`acptui run demo`,
  `python -m acptui.demo`) exercising the whole spec; used by tests and screenshots.
  `ACPTUI_DEMO_DELAY` sets the streaming delay (tests use 0).
- `src/acptui/config.py` (registry `BUILTIN_AGENTS`, `Settings` from `config.toml`),
  `setup.py` (`build_setup`), `cli.py` (argparse), `headless.py` (`acptui options`).

## Where to look

| Task | Place |
| --- | --- |
| New session update kind | `protocol/state.py` `apply`, a view in `ui/widgets/blocks.py`, `make_view` |
| New agent method | `protocol/connection.py`, an action in `ui/screens/session.py`, palette in `ui/app.py` |
| Agent-to-client request | `AgentEvents` + `_ClientHandler` in `connection.py`, `SessionScreen`, `tests/helpers.py` `RecordingEvents`, `headless.QuietEvents` |
| Built-in agents | `config.py` `BUILTIN_AGENTS` |
| Keys / help text | `SessionScreen.BINDINGS`, `ui/screens/dialogs.py` `HELP`, `docs/usage.md` |
| Spec coverage table | `docs/spec-coverage.md` (keep it in sync with code and tests) |
| Screenshots | `uv run python scripts/screenshots.py` -> `docs/images/*.svg` |

## Commands

```bash
uv sync --all-extras
uv run pytest                 # protocol tests run the demo agent as a subprocess
uv run ruff check . && uv run ruff format --check .
uv run ty check               # src and tests; must report nothing
uv run mkdocs-mknodes build   # docs: README + docs/*.md + generated API pages
uv run acptui run demo        # try it
```

## Conventions

- Fully typed: no `Any` in our APIs, no `type: ignore`. Test v2 optional fields with
  `is_set()` (from `aclientp.schema.v2.base`), never truthiness (`MISSING` is truthy).
- Textual name clashes: ty catches overrides of `Widget` internals (`_render`,
  `_name`, `history`, ...); Textual also treats `_default_<reactive>` methods
  specially. Pick distinct names.
- `AgentEvents.session_update` must stay synchronous: aclientp starts notification
  handlers in order, and not awaiting keeps that order.
- Markdown views sync to the full text (`StreamingMarkdown.sync`) instead of appending
  chunks: rendering lags behind the state.
- UI tests use `App.run_test()` with the real demo agent; wait with the `until`
  helper in `tests/test_app.py`, never fixed sleeps alone.

## Pitfalls

- Copier-managed (`copier-phil65`): `.github/workflows`, `duties.py`, `mkdocs.yml`,
  most of `pyproject.toml`; add dependencies below the `# Only add below (Copier)`
  markers. `[tool.ty.src]` includes `tests` on purpose.
- Problems in aclientp are fixed in aclientp (worktree, tests, release), not worked
  around here.
- The registry lists agents that do not speak v2 yet (gemini, opencode, agentpool):
  they get the "unsupported protocol" error, which is intended.
