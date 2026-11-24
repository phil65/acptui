# Usage

## Starting

```bash
acptui                           # agent picker (or `default_agent` from the config)
acptui run claude                # a registry agent (`acptui agents` lists them)
acptui run demo                  # the bundled demo agent: no LLM, no API key
acptui run -- my-agent --acp     # any program speaking ACP v2 on stdio
acptui connect https://host/acp -H "Authorization: Bearer ${TOKEN}"
acptui connect wss://host/acp
```

Options go before the command:

| Option | Effect |
| --- | --- |
| `--cwd DIR` | Working directory of the session (default: the current one) |
| `-o ID=VALUE` | Initial session config option, e.g. `-o model=opus -o mode=plan` |
| `--add-dir DIR` | Additional workspace root (if the agent supports it) |
| `--system-prompt TEXT` | Extra system instructions (if the agent supports it) |
| `--system-prompt-mode append\|override` | Append to or replace the agent's instructions |
| `--config FILE` | Another config file |

`acptui run AGENT --resume SESSION_ID` resumes a session and replays its history.
`acptui options AGENT` prints the config options (with their ids and values) and the
commands an agent offers, without starting the UI.

![The agent picker](images/picker.svg)

In the picker, `i` shows the highlighted agent's details: what it is, its command,
whether it speaks ACP v2, and actions to install it and to log in (they run in the
terminal, acptui steps aside meanwhile). `d` changes the project directory, `c` takes
a command or URL of an agent that is not listed, and `r` lists recent sessions of every
agent and directory, to resume one in a new tab.

## The session screen

![A turn of the demo agent](images/session.svg)

The conversation shows user messages, the agent's streamed Markdown answer, its
thoughts (collapsed), plans, tool calls and notices. Tool calls show their status,
kind, title and locations; their content is text, diffs (unified or side by side,
syntax highlighted) and live terminals. `tab` moves the focus into the
conversation: `enter` on a tool call toggles its details, `r` shows its raw input
and output, `x` cancels it (if the agent allows it).

The status line shows the agent, the session title, whether the agent is working,
the account, background work, mode and model, the context window and the cost.

### Keys

| Key | Action |
| --- | --- |
| `enter` | Send (or accept a completion) |
| `shift+enter`, `ctrl+j` | New line |
| `/` | Complete the agent's commands and skills |
| `@` | Complete a path; the file is attached to the prompt |
| `tab` (empty prompt) | Take the agent's suggested next prompt |
| `up` / `down` | Prompt history (kept across sessions; `history = false` turns that off) |
| `escape` | Cancel the turn |
| `shift+tab` | Next mode |
| `ctrl+o` | Session options: mode, model, thought level, ... |
| `ctrl+n` | New session |
| `ctrl+r` | Resume a session (`d` delete, `a` archive, `h` show archived) |
| `ctrl+f` | Fork the session, whole or at a point the agent offers |
| `f2` | Rename the session |
| `ctrl+d` | Diff view: unified / split / auto |
| `ctrl+l` | Agent log: stderr, and `t` for the protocol traffic |
| `alt+up` / `alt+down` | Block cursor: select a block; `c` copy, `p` to the prompt, `v` save as SVG, `m` maximize, `escape` back |
| `ctrl+t` | New tab: pick an agent (and with `d` a directory) for another session |
| `alt+left` / `alt+right` | Previous / next tab |
| `alt+w` | Close the tab (stops its agent) |
| `ctrl+s` | All sessions: the tabs, their state and directory |
| `ctrl+p` | Command palette: everything above, login / logout, providers, option values |
| `f5` | Restart the agent |
| `f1` | Help |
| `ctrl+b` | Files panel: the workspace and mounts as a tree (see Files) |
| `!` / `$` | On an empty prompt: shell mode (see Shell mode); `esc` goes back |
| `ctrl+g` | Drop the shell output waiting for the next prompt |
| `f12` | Screenshot: the screen as SVG and as text (Downloads), text copied to the clipboard |
| `ctrl+q` | Quit |

## In the browser

```bash
acptui serve run demo                        # http://localhost:8000
acptui --cwd ~/src/app serve --port 9000 run claude
acptui serve --host 0.0.0.0 --public-url https://box.example.com run codex
```

`acptui serve` runs acptui as a web app ([textual-serve](https://github.com/Textualize/textual-serve)):
every browser tab starts `acptui OPTIONS COMMAND` (the options before `serve`, the
command after its own options) in a new process on the server. There is no
authentication: anyone who reaches the address can use your agents, so it binds to
localhost unless you pass `--host` (acptui warns then); put it behind an authenticating
proxy to share it. Images are drawn with half blocks in the browser.

## acptui's own commands

Commands starting with `/acptui:` are acptui's, not the agent's (completion marks them
*(acptui)*):

| Command | Does |
| --- | --- |
| `/acptui:export` | Save the conversation as Markdown (Downloads) and copy it |
| `/acptui:clear` | Clear the view (the session keeps its history) |
| `/acptui:rename TITLE` | Rename the session |
| `/acptui:new` | New session in this tab |
| `/acptui:tab`, `/acptui:close` | New tab, close this tab |
| `/acptui:about` | Versions, paths, terminal, agent (for bug reports) |

## Tabs

![Two tabs: one session idle, one working](images/tabs.svg)

Each tab is a session with its own agent process: run Claude Code in one tab and Codex
in another, or two sessions of one agent in different directories. `ctrl+t` opens the
agent picker for a new tab (`escape` goes back, `d` changes the project directory);
the tab bar appears once there are two tabs (`tabs = "always"`, `"multiple"` or
`"never"` in the config) and shows what each session does: `◐` working, `?` waiting
for you, `●` idle, `✘` disconnected. Click a tab or use `alt+left` / `alt+right`;
`ctrl+s` lists all of them, `alt+w` closes one. A question from a tab in the background
waits until you switch to it (the tab shows `?`, and you get a notification).

## Notifications

When the agent asks for permission or input, the terminal bell rings. If you are not
looking (the terminal is not focused, or the session is in another tab), acptui also
sends a notification and plays a sound, for questions and when a turn ends: desktop
notifications (`notify-send`, `osascript`) on a local machine, terminal notifications
(OSC 777 / 9 / 99: foot, WezTerm, Ghostty, kitty, iTerm2, Windows Terminal, ...) over
SSH or with `method = "terminal"`. The terminal title shows the session and starts with
`●` while an agent waits for you. See `[notifications]` in the configuration.

## Images

![An image from the agent, drawn with half blocks](images/images.svg)

Images the agent sends (in messages and tool output) are drawn in the terminal with the
best method it supports: the kitty graphics protocol, Sixel, or colored half blocks
(any color terminal, also in the browser with `acptui serve`). acptui asks the terminal
once at start; `images = "halfcell"` (or `tgp`, `sixel`, `unicode`, `off`) in the
config picks one. Large images are scaled down; images are only taken from the data
the agent sends, an image URI is shown but never fetched.

## Attachments

`@path` mentions (inside the working directory) are attached to the prompt: small
text files are embedded when the agent accepts embedded context, images and audio are
attached when it accepts them, anything else is sent as a resource link. Escape spaces
in paths with a backslash (`@my\ file.txt`); the completion does that for you. After
the entries of the directory you are in, the completion lists fuzzy matches from the
whole workspace: `@convview` finds `src/ui/conversation_view.py`.

## Shell mode

`!` (or `$`) on an empty prompt switches it to shell commands: the border turns
orange and `enter` runs the command in the session's working directory, shown in the
conversation with its output and exit code. `esc`, or `backspace` on an empty prompt,
goes back to messages for the agent; `up` / `down` walk the shell history.

On Linux and macOS one bash or zsh (your `$SHELL` if it is one of them) runs for the
whole session, so `cd`, `export` and an activated virtualenv carry over between
commands; while a command runs, `enter` types the line into it (a `read`, a `y/n`
question) and `ctrl+c` or `esc` interrupts it. On Windows each command runs on its
own. Pagers are set to `cat`; full-screen programs (editors, `htop`) need a real
terminal.

The agent sees what you ran: commands finished since your last prompt are attached to
the next one, with their output (long output cut to its start and end). The line above
the prompt lists them; `ctrl+g` drops them.

## Files

`ctrl+b` opens the files panel: the working directory under `workspace/`, and one folder
per mount (see `mounts` in the configuration). Files acptui can look into open like
folders: archives by their members, Markdown files by section, source files by class
and function (with `upathtools[tree-sitter]`, which acptui installs). Files the agent
read or changed in this session are marked with `●`.

`a` mentions the highlighted node in the prompt. A plain file in the workspace is
attached as above; a section, a class or function, or a file on a mount is read by
acptui and embedded (when the agent accepts embedded context, else linked), so you can
hand the agent exactly one function, one section of a long document, or a file it has
no access to. Mount files are written `@mount:path`, the rest `@path`.

## Permissions

![A permission request with a diff](images/permission.svg)

The agent's options are buttons; number keys pick one, `y` the first "allow"
option, `n` the first "reject" option and `escape` rejects once. Tool calls show
their diff; commands show the command line and directory. If the agent withdraws the
request (it cancelled the turn), the dialog closes by itself.

## Forms and URLs

![A form the agent asks for](images/elicitation.svg)

Agents can ask for structured input (elicitation): acptui builds a form from the
schema (text, choices, numbers, switches, multi-select) and checks required fields
and limits; `ctrl+s` submits, `escape` cancels. For URL requests (e.g. to sign in)
`o` opens the browser; the dialog closes when the agent reports completion.

## Subagents

![A subagent session](images/subagent.svg)

Agents can run child sessions. They appear as a block in the conversation; `enter`
opens the child's conversation, `x` cancels its work (if allowed). Messages between
sessions are shown with their sender and recipient.

## Sessions, options and login

![Session options](images/options.svg)

`ctrl+o` lists the agent's config options; modes and models are shown in the status
line. Initial values can be set per agent in the config (`[agent_options.ID]`) or
with `-o`. If the agent needs a login, acptui says so and the command palette offers
its login methods: "agent" methods log in through the agent, "terminal" methods run
the agent's login command in the terminal. LLM providers (where agents support it)
are configured from the palette too.

## Configuration

`config.toml` in the platform config directory (`acptui config` prints the path, the
`ACPTUI_CONFIG` environment variable overrides it):

```toml
default_agent = "codex"
theme = "tokyo-night"              # any Textual theme
diff_view = "auto"                 # unified, split or auto (split from 120 columns)
expand_thoughts = false
history = true                     # prompt history and recent sessions, across sessions
tabs = "multiple"                  # tab bar: always, multiple (tabs), never
images = "auto"                    # auto, tgp (kitty), sixel, halfcell, unicode or off
system_prompt = "Prefer small commits."
system_prompt_mode = "append"
additional_directories = ["~/src/shared"]

[notifications]                    # when the agent asks something or finishes
when = "unfocused"                 # never, unfocused (or in another tab), always
method = "auto"                    # auto, terminal (OSC 777/9/99, works over SSH), desktop
sounds = true
turn_end = true                    # also when a turn ends, not only for questions
title = true                       # terminal title: session, ● while the agent waits

[mounts]                           # more folders in the files panel: any fsspec URL
docs = "github://phil65:agentpool@main/docs"
notes = "~/notes"

[agents.my-agent]                  # new agents, or overrides of built-in ones
name = "My agent"
command = ["my-agent", "--acp"]
env = { MY_FLAG = "1" }            # ${VAR} expands from the environment
options = { model = "fast" }

[agents.remote]
url = "https://example.com/acp"    # http(s):// = Streamable HTTP, ws(s):// = WebSocket
headers = { Authorization = "Bearer ${MY_TOKEN}" }

[agent_options.claude]
model = "opus"

[[mcp_servers]]                    # passed to sessions of agents that support the transport
name = "docs"
command = "docs-mcp"               # resolved on PATH
args = ["--stdio"]

[[mcp_servers]]
name = "search"
url = "https://example.com/mcp"
headers = { "X-API-Key" = "${SEARCH_KEY}" }
```
