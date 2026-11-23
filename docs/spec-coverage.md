# ACP v2 coverage

acptui implements the client side of ACP v2 as defined by the draft schema
`schema-v2.0.0-beta.3` (pinned and conformance-checked by
[aclientp](https://github.com/phil65/aclientp) 0.3.0). This page maps every
client-facing item of the spec to the code that handles it and the test that covers
it. Paths are relative to `src/acptui/`; tests are in `tests/`.

Abbreviations: `conn` = `protocol/connection.py` (`AgentConnection`), `state` =
`protocol/state.py` (`SessionState`), `session` = `ui/screens/session.py`
(`SessionScreen`), `SC` = `test_spec_coverage.py`, `AS` = `test_app_spec.py`,
`A` = `test_app.py`, `C` = `test_connection.py`, `S` = `test_state.py`.

## Connection and version negotiation

| Spec item | Handled in | Tests |
| --- | --- | --- |
| `initialize` (client info, capabilities) | `conn._initialize`, `CLIENT_CAPABILITIES` | C `test_tour_with_permission` |
| Version negotiation: anything but v2 is refused with a clear message | `conn._initialize` (`UnsupportedProtocolError`, `-32022`, invalid v2 result) | C `test_v1_agent_is_rejected_clearly`, `test_v1_shaped_response_claiming_v2`; A `test_v1_agent_gets_a_clear_message` |
| stdio transport (agent subprocess, stderr captured) | `conn._open_transport` (aclientp `spawn_stdio_transport(on_stderr=)`) | C `test_crash_reports_exit_code_and_stderr`; A `test_crash_shows_exit_code_and_stderr` |
| Streamable HTTP and WebSocket transports | `conn._open_transport` (aclientp `connect_remote_transport`), `acptui connect` | C `test_unreachable_url` |
| `$/cancel_request` (outgoing) | aclientp: cancelled calls send it (e.g. on quit) | A `test_quit_stops_the_agent` |
| `$/cancel_request` (incoming): close the dialog of a withdrawn request | `session._ask` | AS `test_agent_cancelling_a_permission_request_closes_the_dialog` |
| Errors (`-32000` auth required, other request errors) | `session._session_error`, notices, `spawn` | SC `test_authentication`; AS `test_auth_required_then_login` |

## Client capabilities acptui advertises

| Capability | Meaning in acptui | Tests |
| --- | --- | --- |
| `auth.terminal` | Terminal login methods run the agent's login command in the terminal (`session._terminal_login`) | AS `test_auth_required_then_login` (palette offers it) |
| `elicitation.form` | `FormElicitationScreen` | SC `test_form_and_url_elicitation`; AS `test_form_elicitation` |
| `elicitation.url` | `UrlElicitationScreen` | AS `test_url_elicitation_closes_on_completion`, `test_url_dialog_closes_on_complete` |
| `session.promptSuggestions` | Suggestion as prompt placeholder, `tab` takes it | AS `test_media_plans_notices_compaction_suggestions` |
| `session.commands.skill` | Skills in `/` completion, sent as `skill` blocks | SC `test_skill_prompt`; `test_prompt.py::test_skills_become_skill_blocks` |

## Agent capabilities acptui uses

| Capability | Used for | Tests |
| --- | --- | --- |
| `session.prompt.image` / `audio` / `embeddedContext` | `@file` attachments as image / audio / embedded resource blocks (`protocol/prompt.py`) | `test_prompt.py` (all) |
| `session.prompt.skill` | `skill` blocks | SC `test_skill_prompt` |
| `session.mcp.stdio` / `http` / `acp` | Which configured MCP servers are sent (`conn._setup_fields`) | SC `test_session_setup_is_sent`; AS `test_session_setup_from_settings` |
| `session.additionalDirectories` | `--add-dir`, `additional_directories` | same |
| `session.systemPrompt` (+ `mode.override`) | `--system-prompt`, `system_prompt[_mode]` (override falls back to append) | same |
| `session.delete` / `fork` / `archive` / `status` / `options` / `title` | Enable the matching actions | SC `test_capabilities_are_all_detected`, `test_session_management` |
| `auth.status` | `auth/status` on connect, account in the status line | SC `test_authentication` |
| `providers` | Providers dialog | SC `test_providers`; AS `test_providers_dialog` |
| `authMethods` (agent, terminal, unknown) | Palette "Log in: ..." for agent and terminal methods; unknown ones are listed | AS `test_auth_required_then_login` |

## Agent methods (client to agent)

| Method | Handled in | Tests |
| --- | --- | --- |
| `auth/login` | `conn.login`, `session.login` | SC `test_authentication`; AS `test_auth_required_then_login` |
| `auth/logout` | `conn.logout`, `session.logout` | same |
| `auth/status` | `conn.auth_status` | same |
| `providers/list` / `set` / `disable` | `conn.*_provider(s)`, `ui/screens/providers.py` | SC `test_providers`; AS `test_providers_dialog` |
| `session/new` (cwd, MCP servers, system prompt, directories, initial config options) | `conn.new_session(SessionSetup)` | SC `test_session_setup_is_sent` |
| `session/resume` with `replayFrom: start` (the listed title outlasts replayed ones) | `conn.resume_session`, `session._resume`, `acptui run --resume` | C `test_list_and_resume_with_replay`; A `test_new_and_resume_session`; AS `test_resume_keeps_the_title` |
| `session/fork` (whole or at a cursor; the fork shows the timeline up to it) | `conn.fork_session`, `ForkScreen` (`ctrl+f`), `state.forked` | SC `test_session_management`; AS `test_fork_rename_and_session_list`; S `test_a_whole_fork_copies_the_timeline`, `test_a_fork_at_a_cursor_copies_what_comes_before_it` |
| `session/close` | `conn.close_session` (on new session) | A `test_new_and_resume_session` |
| `session/list` (cursor pages, `archived` filter) | `SessionListScreen` (`ctrl+r`, "load more", `h`) | SC `test_session_management`; AS `test_fork_rename_and_session_list` |
| `session/delete` | `SessionListScreen` (`d` + confirm) | same |
| `session/archive` / `unarchive` | `SessionListScreen` (`a`) | same |
| `session/status` | Palette "Session status", after resume | SC `test_session_management` |
| `session/options` | `acptui options AGENT` (`headless.py`) | SC `test_session_management`; `test_cli.py::test_options_command` |
| `session/set_title` | `f2` | SC `test_session_management`; AS `test_fork_rename_and_session_list` |
| `session/set_config_option` (`id` and `boolean`) | `ConfigScreen` (`ctrl+o`), `shift+tab`, palette | C `test_config_options_and_mode`; A `test_options_mode_and_palette` |
| `session/prompt` (with idempotency key) | `conn.prompt`, `session.on_prompt_input_submitted` | A `test_echo_with_attachment_and_history` |
| `session/cancel` | `escape` | C `test_cancel_and_failures`; A `test_cancel_turn` |
| `tool_call/cancel` | `x` on a tool call that allows it | SC `test_cancel_a_tool_call`; AS `test_command_permission_and_cancel_tool` |
| `terminal/cancel` | Cancel button of a running terminal that allows it | SC `test_cancel_a_terminal` |
| `session/cancel` for a subagent session | `x` on a subagent (if `capabilities.cancel`) | (UI path shared with `escape`) |

## Client methods and notifications (agent to client)

| Method | Handled in | Tests |
| --- | --- | --- |
| `session/request_permission`: tool call subject (applied as a tool call upsert), command subject, no / unknown subject; all option kinds | `PermissionScreen` | C `test_tour_with_permission`, `test_rejected_permission_fails_the_tool_call`; A `test_reject_and_escape_in_permission_dialog`; SC `test_extensions_and_command_permission`; AS `test_command_permission_and_cancel_tool` |
| `elicitation/create` form: string, enum, titled enum (`oneOf`), number, integer, boolean, multi-select (plain and titled), required, min/max length, minimum/maximum, min/max items, defaults; accept / decline / cancel | `FormElicitationScreen` | SC `test_form_and_url_elicitation`; AS `test_form_elicitation` |
| `elicitation/create` URL (session or request scoped) | `UrlElicitationScreen` (`o` opens the browser) | AS `test_url_elicitation_closes_on_completion` |
| `elicitation/create` unknown mode | Declined with a notice | (`session.create_elicitation`) |
| `elicitation/complete` | Closes the URL dialog as accepted | SC `test_form_and_url_elicitation`; AS `test_url_dialog_closes_on_complete` |
| `auth/status_update` | Account in the status line | SC `test_authentication`; AS `test_auth_required_then_login` |
| `session/update` | `session.session_update` → `SessionState.apply` per session id (see below) | all |
| `_` extension notifications | Logged in the agent log | SC `test_extensions_and_command_permission`; AS `test_media_plans_notices_compaction_suggestions` |
| `_` extension requests | Answered `-32601` (aclientp default) | — |

## Session updates

| Update | Handled in | Tests |
| --- | --- | --- |
| `user_message_chunk`, `user_message` (adopts the local message, replay) | `state._message_chunk/_message_upsert` | S `test_agent_reported_user_message_adopts_the_local_one`, `test_user_chunk_replaces_the_local_content_once`, `test_replayed_user_messages_are_new_entries` |
| `agent_message_chunk`, `agent_message` | Streamed Markdown (`AgentMessageView`) | S `test_agent_chunks_stream_into_one_message`, `test_message_upsert_replaces_and_null_clears`; A `test_tour_renders_every_kind_of_block` |
| `agent_thought_chunk`, `agent_thought` | Collapsible `ThoughtView` | S `test_thoughts_and_messages_with_the_same_id_are_separate`; A `test_tour_renders_every_kind_of_block` |
| `state_update`: running, idle (all stop reasons, error, usage), requires_action, unknown, custom; `background` | `state._state_update`, status line | S `test_turn_states_and_stop_reasons`; SC `test_background_work_usage_and_cursors` |
| `tool_call_update` (patch semantics: name, title, kind, status incl. paused, content, locations, raw input/output, capabilities) | `ToolCallEntry.apply`, `ToolCallView` | S `test_tool_call_patch_semantics`; AS `test_media_plans_notices_compaction_suggestions` |
| `tool_call_content_chunk` | `state._tool_call_chunk` | S `test_tool_call_content_chunks_append` |
| Tool call content: `content` (every block kind), `diff`, `terminal`, `session`, unknown | `ToolCallView._content_widget` | SC `test_every_content_block_kind`; A `test_tour_renders_every_kind_of_block` |
| Diffs: add / delete / modify / move / copy / unknown changes, file type, `git_patch` (unified or split, highlighted), unknown patch formats | `ui/widgets/diff.py`, `protocol/diff.py` | `test_diff.py`; SC `test_every_content_block_kind` |
| `terminal_update` (command, cwd, output snapshot, exit status, size, capabilities), `terminal_output_chunk` | `TerminalState`, `TerminalView` (live, ANSI, `\r` progress lines) | S `test_terminal_snapshot_chunks_and_exit`; SC `test_background_work_usage_and_cursors` |
| `plan_update` (items with priority and status; markdown with path), `plan_removed` | `PlanView` | S `test_plans_update_in_place_and_can_be_removed`; SC `test_compaction_suggestions_plans_notices` |
| `available_commands_update` (command with input hint, skill, unknown) | `/` completion | `test_completion.py::test_complete_command`; A `test_slash_and_path_completion` |
| `config_option_update` (select flat and grouped, boolean, unknown; categories) | `ConfigScreen`, status line | C `test_config_options_and_mode`; A `test_options_mode_and_palette` |
| `session_info_update` (title, updated at) | Status line | S `test_session_level_updates` |
| `usage_update` (used, size, cost) | Status line | S `test_session_level_updates` |
| `notice` (info, warning, error) | `NoticeView` | SC `test_compaction_suggestions_plans_notices` |
| `compaction_update`, `compaction_summary_chunk` | `CompactionView` | S `test_compactions`; SC `test_compaction_suggestions_plans_notices` |
| `replay_complete` | `state.replaying` | S `test_replayed_user_messages_are_new_entries` |
| `prompt_suggestions_update` | Prompt placeholder, `tab` | SC `test_compaction_suggestions_plans_notices`; AS `test_media_plans_notices_compaction_suggestions` |
| `cursor_update` (all anchor kinds, fork capability) | `state._cursor_update`, `ForkScreen` | SC `test_background_work_usage_and_cursors`; AS `test_fork_rename_and_session_list` |
| `subagent_update` (title, description, capabilities, state, cwd, directories) and the child session's own updates | `SubagentView`, `SubagentScreen` | SC `test_subagent_sessions_and_session_messages`; AS `test_subagent_view_and_screen` |
| `session_message`, `session_message_chunk` | `SessionMessageView` | same |
| Custom / future update kinds | Ignored | S `test_unknown_updates_are_ignored`; SC `test_extensions_and_command_permission` |

## Content blocks

| Block | In messages | In prompts | Tests |
| --- | --- | --- | --- |
| `text` | Markdown | typed text | many |
| `image` | Placeholder with type and size | `@image.png` when supported | SC `test_every_content_block_kind`; `test_prompt.py` |
| `audio` | Placeholder with type and size | `@clip.wav` when supported | same |
| `resource_link` | Link (http) or path, title, description, size | `@file` / `@dir` (always allowed) | same |
| `resource` (text / blob) | Highlighted code block / placeholder | small text files when `embeddedContext` | same |
| `skill` | `skill: /name` | `/skill` when supported | SC `test_skill_prompt` |
| Unknown | `[type]` | — | SC `test_every_content_block_kind` |
| `annotations.audience` | Blocks only for the assistant are hidden | — | SC `test_every_content_block_kind` |

## Plain text and Markdown

v2 display strings are plain text unless the spec says Markdown. acptui renders the
Markdown fields with `AgentMarkdown` (a document) or `inline_markdown` (one-line places:
lists, completions, the palette) from `ui/markdown.py`, and escapes plain fields where it
builds Markdown itself (`protocol/content.escape_markdown`). Raw HTML in agent Markdown
is shown as text, not interpreted; links open only on click: `http(s)` directly (the
full target is shown), anything else after a confirmation. Tests: `test_markdown.py`.

| Field | Rendered as | Where |
| --- | --- | --- |
| `text` content blocks | Markdown | messages, thoughts, tool output |
| Plan entry `content`, Markdown plan `content` | inline Markdown / Markdown | `PlanView` |
| Auth method `description` | inline Markdown, Markdown | auth-required panel, palette, `LoginScreen` |
| Command and skill `description` | inline Markdown (first line) | `/` completion |
| Config option and select value `description` | inline Markdown, Markdown for the highlighted one | `ConfigScreen` |
| Permission request `description` | Markdown | `PermissionScreen` |
| Subagent `description` | Markdown | `SubagentView` |
| Tool call and session titles, names, labels, hints, notices, resource link name and description, elicitation texts, auth account labels, error messages | plain text (escaped in generated Markdown and in palette markup) | everywhere |

## Not applicable to a TUI client

| Spec item | Why |
| --- | --- |
| `fs/read_text_file`, `fs/write_text_file`, client `terminal/*` methods | Not part of v2: agents own files and processes; v2 terminals are display-only (rendered live, cancellable). |
| `mcp/message` (requests and notifications) and the `acp` MCP transport | MCP over ACP serves MCP servers that the *client* provides. acptui provides none (it passes the user's stdio and HTTP servers to the agent instead), so agents have no reason to send `mcp/message`; it is answered `-32601`. |
| `proxy/initialize`, `proxy/successor` | Proxy chains (conductors), not clients. |
| Prompt suggestions with images, audio or embedded resources (`promptSuggestions.image/audio/embeddedContext`) | Suggestions become prompt text; acptui only advertises the base capability (text and resource links). |
| Icons (`icons` on resource links), `annotations.priority`, `annotations.lastModified` | Display hints for graphical clients; a terminal shows the name, type and size instead. |
| `_meta` fields | No meaning without a matching extension; kept by aclientp, ignored here. |
