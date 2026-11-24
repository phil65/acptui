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
  - `widgets/sidebar.py`: `SideBar` (`ctrl+b`), collapsible Plan (`PlanPanel`, synced from
    `SessionScreen._update_plans` on plan changes, auto-open via the `sidebar` setting) and
    Files (`widgets/files.py` `FileTree`) sections. While it is open the screen has the
    class `-sidebar-open` and the conversation's `PlanView` shows only its summary line.
  - `screens/picker.py`: pinned agents (`pinned_agents`) first, `1`-`9` start them; `p`
    here or in `screens/agent.py` posts `PinToggled`, the app saves the list and refills
    the picker.
  - `screens/`: `permission.py`, `elicitation.py`, `dialogs.py` (session list, config,
    fork, rename, confirm, help), `providers.py`, `picker.py`, `log.py`.
- `src/acptui/ui/app.py` also keeps the **tabs** (one `SessionScreen` per Textual mode,
  `open_tab` / `close_tab` / `switch_tab`, the picker in mode `picker`), **attention**
  (`ui/attention.py` messages -> bell, sound, notification via `notify.py`, terminal
  title), the settings screen (`ui/screens/settings.py`, saved with tomlkit) and link
  clicks from agent Markdown.
- `ui/markdown.py`: `AgentMarkdown` (no raw HTML, links bubble to the app) and
  `inline_markdown` for the v2 Markdown description fields; plain fields stay `Content`.
- `ui/widgets/image.py`: images via textual-image (probed in `cli.main` before the app
  starts); `ui/widgets/tabs.py`; `ui/block_actions.py` (block cursor text / SVG);
  `ui/screens/agent.py` (agent details, install / login in the suspended terminal).
- `history.py`, `recent.py` (state dir, `ACPTUI_HISTORY` / `ACPTUI_SESSIONS`; tests point
  them at temp files), `protocol/export.py` (timeline -> Markdown), `notify.py`.
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
| A new setting | `config.py` `Settings`, `FIELDS` in `ui/screens/settings.py`, `on_settings_changed` in `ui/app.py`, `docs/usage.md` |
| A layout setting | `LayoutSettings` in `ui/screens/session.py`, `AcpTui.layout_settings` / `apply_layout` |
| toad feature status | `docs/toad-comparison.md` (keep it in sync) |

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
- Background tabs: a dialog of a tab that is not shown waits for it (`_until_shown`);
  code that pushes screens for a session must go through `_ask`.
- Commit hooks run the whole suite (about 4 minutes): commit in the background and do not
  edit the tree meanwhile (the hook stashes unstaged changes).
- UI tests use `App.run_test()` with the real demo agent; wait with the `until`
  helper in `tests/test_app.py`, never fixed sleeps alone.

## Pitfalls

- Copier-managed (`copier-phil65`): `.github/workflows`, `duties.py`, `mkdocs.yml`,
  most of `pyproject.toml`; add dependencies below the `# Only add below (Copier)`
  markers. `[tool.ty.src]` includes `tests` on purpose.
- Problems in aclientp are fixed in aclientp (worktree, tests, release), not worked
  around here.
- The registry lists agents that do not speak v2 yet (gemini, opencode): they get the
  "unsupported protocol" error, which is intended. agentpool (2.11+) speaks v2: its
  test models (`model: {type: test, call_tools: [...], tool_args: {...}}` in an
  `agentpool.yml` of the project, `serve-acp --agent NAME`) make a free end-to-end check.
