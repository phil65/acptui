# acptui and toad

[toad](https://github.com/batrachianai/toad) (Will McGugan, Textualize; AGPL-3.0) is
the best-known Textual front end for ACP agents. acptui aims to have everything good in
toad, on ACP **v2** (toad speaks v1). This page is the feature inventory, made from
toad's default branch (commit `dd4f90e`, May 2026, version 0.6.20): what acptui has,
what it does differently, and what it leaves out and why.

Status: **yes** (acptui has it), **partly**, **deferred**, **no** (left out on purpose),
**n/a** (a v1 or toad-only concept). Where acptui adapted toad code, `NOTICE` says so.

## Command line

| toad | acptui | Status |
| --- | --- | --- |
| `toad [PATH]` (project directory) | `acptui --cwd PATH` | yes |
| `toad -a AGENT` (skip the launcher) | `acptui run AGENT`, `default_agent` | yes |
| `toad acp "COMMAND"` (any ACP command) | `acptui run -- COMMAND ARGS` | yes |
| `toad acp --title` | the command's name is the title | no: not needed |
| — | `acptui connect URL` (Streamable HTTP, WebSocket) | acptui only |
| `toad serve`, `toad run --serve`, `--host/--port/--public-url` | `acptui serve [--host --port --public-url --token] COMMAND`, with a token in the URL (Jupyter-style cookie) and an Origin check for WebSockets; toad's server has neither | yes |
| `toad settings` (path of the settings file) | `acptui config` | yes |
| `toad about` | palette "About acptui" / `/acptui:about` | yes |
| `toad replay LOG` (replay an agent log as a fake agent) | the bundled demo agent covers testing; the log screen shows traffic | no: little value for users |
| `--version` | `--version` | yes |
| — | `acptui options AGENT`, `acptui agents` | acptui only |

## Agents and the launcher

| toad | acptui | Status |
| --- | --- | --- |
| Agent store: grid of ~20 agents with descriptions (TOML per agent) | Agent picker over the built-in registry (toad's agents included, with ACP v2 support: yes / not yet / unknown) and user agents (`config.toml`) | yes (a list, not a grid) |
| Agent details (Markdown help, homepage) | `i` in the picker: description, command, v2 support, homepage, install and login commands | yes (short descriptions instead of toad's long help texts) |
| Install actions (run the install command) | `i` then Install / Log in: runs the command in the real terminal (acptui suspended) | yes |
| "Show in launcher" quick-launch list (digits start an agent) | pinned agents: `p` in the picker or the agent details pins / unpins, pinned ones come first, numbered, `1`-`9` start them; `pinned_agents` in `config.toml` (saved with comments kept); `default_agent` skips the picker | yes (a numbered list on top of the picker, not a separate grid) |
| Change the project directory on the launcher | `d` in the picker | yes |
| Resume a session from the launcher (local session DB, `ctrl+r`) | `r` in the picker: recent sessions of every agent and directory (`sessions.jsonl`); `ctrl+r` in a session lists the agent's own (`session/list`) | yes |
| v1 agents | refused with a clear message: v2 only | n/a |

## Sessions

| toad | acptui | Status |
| --- | --- | --- |
| Concurrent sessions as tabs, sessions bar (always / multiple / never) | tabs, `tabs` setting | yes |
| `ctrl+[` / `ctrl+]` previous / next session | `alt+left` / `alt+right` (`ctrl+[` is `escape` in most terminals) | yes |
| Sessions overview (`ctrl+s`, grid with state) | `ctrl+s` list with state and directory | yes |
| Session state (busy / asking / idle) | `◐` working, `?` waiting, `●` idle, `✘` disconnected, `◌` starting | yes |
| `ctrl+h` home (launcher while sessions run) | `ctrl+t` (picker; `escape` back) | yes |
| `/toad:session-new`, `/toad:session-close` | `ctrl+t`, `alt+w`, `/acptui:tab`, `/acptui:close`, palette | yes |
| `/toad:rename` | `f2` (`session/set_title`) | yes |
| Resume (`ctrl+r`) | `ctrl+r`, `--resume` | yes |
| — | fork, archive, delete, status, close (v2) | acptui only |

## Conversation

| toad | acptui | Status |
| --- | --- | --- |
| Streaming Markdown (fences, tables) | yes | yes |
| Thoughts (setting to show them) | collapsible, `expand_thoughts` | yes |
| Tool calls (expand policy setting: never / always / success / fail) | `tool_expand`: auto (by kind and content) / always / never / failed; `enter` toggles | yes (no "success only": a successful call is not more interesting than a running one) |
| Diffs: unified / split / auto, syntax highlighted | yes, `ctrl+d`, `diff_view` | yes |
| Diff annotations (`+`/`-`), wrap setting | `+`/`-` signs always; `diff_wrap` for unified diffs | yes |
| Plans (sidebar and inline) | the sidebar's plan section follows every `plan_update` / removal live (entries with status and priority, Markdown plans); it opens by itself when a plan arrives on a terminal of 120+ columns (`sidebar = "auto"`, or `open` / `closed`); while it is open the plan in the conversation is one summary line (progress and the current entry), the full block otherwise | yes |
| Terminals in tool calls | live terminal view (agent-owned in v2) | yes (terminal work by another session) |
| Images | drawn (kitty graphics, Sixel, half blocks) | yes: toad shows none |
| Block cursor (`alt+up/down`), block menu: copy, copy to prompt, open as SVG, maximize | `alt+up/down`, then `c` / `p` / `v` / `m` (keys instead of a menu) | yes |
| Conversation pruning (keep N lines) | `max_blocks` (blocks, not lines; the session and the export keep everything) | yes |
| `/toad:clear` | `/acptui:clear` | yes |
| Throbber (thinking animation) | spinner in the status line | yes |
| Flash messages | Textual toasts | yes |
| Markdown descriptions (v2) | permission, subagent, commands, options, auth methods, plan entries | acptui only |

## Prompt

| toad | acptui | Status |
| --- | --- | --- |
| Multi-line editor, `enter` sends | yes | yes |
| Markdown syntax highlighting in the prompt | — | no (for now): needs overriding `TextArea` internals in the prompt widget, which shell mode is changing; Textual's tree-sitter Markdown highlights next to nothing |
| `/` command completion with descriptions | yes (descriptions as Markdown) | yes |
| `@` file mention with fuzzy search over the project, `tab` for a tree | `@name` completes the directory's entries, then fuzzy matches from the whole workspace: ripgrep-rs lists it (`.gitignore`-aware), frizbee matches it (typos, `^`/`$`/`'`/`!` syntax), file names rank first, matched characters highlighted; the sidebar's files section (`ctrl+b`, `a` mentions) is the tree | yes |
| History (persistent file) | persistent (`history.jsonl` in the state directory), prompts and shell commands apart | yes |
| Mode switcher (`ctrl+o`) | `ctrl+o` options, `shift+tab` next mode | yes |
| Shell mode (`!`, PTY shell, completion, danger warnings) | shell mode (`!` / `$`) | yes (by another session); danger warnings deferred: terminal work in progress elsewhere |
| Shell tab completion of paths / commands | — | deferred: terminal work in progress elsewhere |
| "Allow commands" (words that switch to shell mode) | — | deferred: terminal work in progress elsewhere |

## Permissions and questions

| toad | acptui | Status |
| --- | --- | --- |
| Permission screen with diffs, keys per option kind | yes (numbers, `y`, `n`, `escape`) | yes |
| `j` / `k` between several pending requests | one dialog at a time | no: v2 requests come one by one |
| — | forms and URLs (elicitation), withdrawn requests close | acptui only |

## Notifications

| toad | acptui | Status |
| --- | --- | --- |
| Desktop notifications (never / when blurred / always) | `[notifications] when` (never / unfocused or other tab / always) | yes |
| Sounds (question, turn over) | yes (toad's sounds) | yes |
| Notify when the turn ends | `turn_end` | yes |
| Only warnings and errors as desktop notifications | questions, turn ends and failures only | yes (different filter) |
| Blinking terminal title while input is required | terminal title with `●` while waiting | yes (steady mark, no blinking) |
| — | terminal notifications (OSC 777/9/99) for SSH | acptui only |

## Settings and UI

| toad | acptui | Status |
| --- | --- | --- |
| Settings screen (edit in the app) | `f3`: display and notification settings, applied at once where possible and saved into `config.toml` with its comments kept; agents and MCP servers stay in the file | yes |
| Theme | `theme` (and Textual's palette theme switcher) | yes |
| Prompt placeholder, compact input, footer, info bar, status line toggles | `compact_input` (no border above and below the prompt: two lines), `footer`, `status_line` (acptui's status line is toad's info bar and status line in one) | yes; no placeholder setting: the placeholder explains the prompt's keys and changes for shell mode and suggestions, a custom text would only hide that |
| Column mode (fixed width) | `column_width` | yes |
| Scrollbar size | — | no: Textual default |
| Auto copy on selection | `auto_copy` (off by default: it overwrites the clipboard on every selection) | yes |
| pyperclip clipboard fallback | OSC 52 (Textual) | no: OSC 52 works in most terminals |
| Sidebar: plan and project tree | one sidebar (`ctrl+b`), docked left like toad's: collapsible Plan and Files sections, the tree with markers for files the agent touched and `a` to mention; `esc` goes back to the prompt; 40 columns, at most 45% of the width (80x24 keeps a 44-column conversation) | yes |
| Help panel (`f1`) | `f1` help | yes |
| Update check (version file on batrachian.ai) | — | no: no network calls by default |
| Anonymous usage statistics (PostHog) | — | no: no telemetry |
| Tweet a testimonial (`/toad:testimonial`) | — | no |
| GitHub gist sharing (`gist.py`) | `/acptui:export` (Markdown file, copied); share it with `gh gist create` | no: unused in toad (not wired up, needs a login) |
| Process title (`setproctitle`) | — | no |
| Screenshot (`f12`?) | `f12`: SVG and text | yes |

## Protocol

toad's ACP layer is v1: hand-written dicts, client-side `fs/*` and `terminal/*`
methods. None of that applies to v2, where agents own files and processes. acptui uses
aclientp's typed v2 client end to end; see the [coverage table](spec-coverage.md).

## What acptui adapted from toad

`NOTICE` lists it: the pill and plan widgets, the tool output heuristic, the PTY shell
and the shell-mode switch, the notification sounds, the agent list (names, commands,
install and login commands), the sidebar layout and the numbered launcher. Everything else is new code on acptui's v2 layer: tabs,
notifications, image drawing, `acptui serve`, Markdown handling, the block cursor, the
settings screen, history, recent sessions, the fuzzy file search and the export.

## Product decisions

- **Keys**: `ctrl+[` is `escape` in most terminals, so tabs switch with `alt+left` /
  `alt+right`; `ctrl+w` deletes a word in the prompt, so tabs close with `alt+w`.
- **Tab bar** only from two tabs on by default (`tabs = "multiple"`; toad: always), so a
  single session looks as before.
- **Questions from background tabs** wait for their tab (and notify) instead of popping
  up over the tab you are working in.
- **Notifications**: desktop-notifier (native D-Bus / macOS / WinRT) with the `notify`
  extra, else `notify-send` / `osascript`, terminal notifications over SSH; a steady `●` in the title
  instead of blinking.
- **ACP v2 support of toad's agents** is not probed automatically (that would launch
  foreign programs); `t` in the agent details tests one on request and remembers it.
- **No network calls of its own**: no update check, no statistics; images are never
  fetched from URIs; install commands run only when you pick them, shown first.
- **Sidebar** opens by itself only for a plan, only on wide terminals (120+ columns) and
  only until you toggle it yourself in that tab; toad shows it always (or hides it to the
  edge). While it is open the conversation's plan shrinks to one line instead of showing
  the plan twice.
- **Pinned agents** are numbered in pin order (a new pin goes last, so the numbers you
  know stay); toad puts a new one first.
- **Auto copy** is off by default (it would overwrite the clipboard on every selection).
- **`acptui serve`** requires a token per run and checks the WebSocket `Origin`
  (textual-serve has no authentication); `--no-token` only on loopback.
- **Agent HTML** is shown as text, links need a click (and a confirmation unless
  `http(s)`).
