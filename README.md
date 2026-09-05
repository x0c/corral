# corral

**Languages:** English | [简体中文](README.zh-CN.md)

[![test](https://github.com/x0c/corral/actions/workflows/test.yml/badge.svg)](https://github.com/x0c/corral/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Corral is a terminal session handoff tool for Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Cursor Agent CLI, and Pi.

`corral` scans your local Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Cursor Agent CLI, and Pi history, shows recent coding sessions in a terminal UI (built with [Textual](https://github.com/Textualize/textual)), and lets you resume the selected session in its native runtime. It can also hand off a session from one runtime to another (e.g. Claude to Codex, or Pi to Claude) by starting a new target session with a structured pointer to the original history.

Keywords: Claude Code session manager, Codex CLI resume, OpenCode session manager, Kimi Code CLI session manager, terminal TUI, AI coding agent workflow, JSONL chat history, cross-runtime handoff.

![Session list with right-pane conversation preview](docs/screenshots/list.png)

Press `Ctrl+F` to search the conversation bodies of every session and jump straight to the matching line:

![Full-text search across session conversations, with matching lines highlighted](docs/screenshots/search.png)

## Why Use It

- Browse recent Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Cursor Agent CLI, and Pi sessions from one terminal screen.
- Resume with the original runtime using native commands such as `claude --resume`, `codex resume`, `opencode -s <id>`, and `kimi -S <id>`, and `agent --resume`.
- Select a finished session to preview the full conversation in the right pane (live/hosted sessions show embedded terminals instead), or keep up to three active sessions side by side.
- See which session needs attention without opening it: yellow means the agent is waiting for an answer, green means it is working, and red means a new result is unread. The same state is written in the detail header, so color is not the only cue.
- Full-text search everything you ever said: `Ctrl+F` searches conversation bodies across every runtime and shows the matching lines, so you can find a session by what was discussed instead of remembering which project it was in.
- Hand off unfinished work between runtimes without rewriting or faking session files.
- Reuse a bounded local cache and native hot-path accelerator so repeat launches, previews, and live panes stay fast.
- Use JSON output for scripts and launchers.

## Privacy Model

The tool is local-first.

- It reads local history under `~/.claude/projects/`, `~/.codex/sessions/`, `~/.kimi-code/sessions/`, `~/.cursor/chats/`, `~/.pi/agent/sessions/`, and (read-only) OpenCode's SQLite database at `~/.local/share/opencode/opencode.db`.
- It does not upload session history by itself.
- Cross-runtime handoff passes the original history file path to the target runtime instead of copying the whole conversation into command-line arguments.
- Optional title generation distributes batches evenly among installed Claude Code, Codex, OpenCode, Kimi Code, Cursor Agent, and Pi CLIs; a failed assistant automatically yields its batch to another available one. It may consume the corresponding account quota.
- Title and derived performance caches are stored under `~/.cache/corral/`; they can be inspected or cleared locally.
- Attention state is local and content-free: it stores only runtime/session identifiers, opaque change tokens, timestamps, and read state. Cursor live-state support adds corral-managed entries to your user-level hooks file without replacing existing hooks.

See [PRIVACY.md](PRIVACY.md) for the detailed privacy and data-flow notes.

## Requirements

- Python 3.10 or newer.
- `tmux` 3.2 or newer (hard requirement — session hosting, embedded panes, and SSH keep-alive are all built on it; `corral` checks the version at startup and refuses to run on older tmux, since `new-session -e` environment injection requires 3.2+).
- macOS or Linux terminal (any modern ANSI-capable terminal works; the UI is built with Textual, not curses).
- Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Cursor Agent CLI, and/or Pi installed if you want to resume those sessions.

## Install

### Homebrew (macOS/Linux)

Homebrew core already has an unrelated formula named `corral` (the Pony language). Always install from this tap:

```bash
brew install x0c/tap/corral
```

Do not run `brew install corral` — that installs the unrelated Pony formula.

### Install Script

```bash
curl -fsSL https://raw.githubusercontent.com/x0c/corral/main/install.sh | bash
```

Requires Python 3.10+. On supported macOS/Linux machines the script installs a prebuilt native wheel, falling back to a source build only when no matching wheel exists. It prints a `PATH` hint if the install directory isn't already on it.

### From Source

```bash
git clone https://github.com/x0c/corral.git
cd corral
python3 -m pip install --user .
```

Then run:

```bash
corral
```

### From Source (editable)

```bash
git clone https://github.com/x0c/corral.git
cd corral
python3 -m pip install --user -e .
```

Then run:

```bash
corral
# or: python3 -m corral
```

Do not run a deleted root-level `corral.py`; the package lives under `src/corral/`.

## Usage

```bash
corral                  # open the interactive TUI
corral --limit 30       # show up to 30 sessions per runtime
corral --json           # print sessions as JSON and exit
corral --json --limit 5 # script-friendly small result set
corral --no-input       # force non-interactive JSON output
corral -v               # show version, install path, and install channel
corral -d               # enable detailed diagnostic logging
corral -q               # suppress non-essential startup messages
corral --no-color       # disable colors (also respects NO_COLOR)
corral update           # manually check for and install the latest version
corral cache status     # inspect the bounded local performance cache
corral cache clear      # clear derived metadata and conversation cache
corral observer status cursor                    # inspect Cursor live-state integration
corral observer install cursor --dry-run --json  # preview its user-level hook changes
corral observer install cursor                   # repair/install it explicitly
corral observer uninstall cursor                 # remove only corral-managed hooks
corral shim status                               # command interception status (type `claude`, get corral)
corral shim install                              # repair/install interception now
corral shim uninstall                            # remove interception
```

Common aliases are supported: `-h` / `--help`, `-v` / `-V` / `--version`,
and `-d` / `--debug` / `--verbose`.

JSON output includes runtime, session ID, title, working directory, update time, size, status, resume command, and history path.

The TUI defaults to English. If your system locale is Chinese (`zh*`), the interface switches to Chinese automatically. Force a language with `CORRAL_LANG=en` or `CORRAL_LANG=zh`.

The derived cache defaults to 256 MiB and invalidates entries whenever the source history changes. Set `CORRAL_CACHE=0` to disable it, `CORRAL_CACHE_MAX_MB` to change its bound, or `CORRAL_NATIVE=0` to force the portable Python fallback for troubleshooting.

## Embedded Panes (work on multiple sessions at once)

`corral` is a unified, time-ordered session timeline: Claude Code, Codex CLI, OpenCode, Kimi Code,
and Cursor Agent sessions appear in one list rather than separate runtime tabs. Each card uses three
rows for `<dot> project title`, runtime, and update time. While a title is being generated the
card just shows its fallback title with no loading animation, then updates in place once the generated
title lands. The right side follows the selection: finished sessions show their full
conversation pinned to the newest message, while hosted sessions render live terminals. The runtime
buttons above the right side can add another agent in the same project, up to three side-by-side panes;
the active pane combination is remembered. Once the list is shown its order is stable — cards never jump
around when their content updates; only genuinely new sessions appear, always prepended at the top.

Opening two to four sessions as a split now creates a persistent sidebar group with a stable fruit name
such as `Group Apple` or `Group Pineapple`. The group takes three rows, while its sessions move underneath
as an indented tree instead of being duplicated in the top-level timeline. Child rows omit the project name
prefix — that already lives on the group card. Unpinned groups follow the same stable store order as
independent sessions — once the list is shown they stay put even when a member’s mtime updates; only
genuinely new sessions are prepended at the top, and only items pinned with `p` or `Ctrl+P` sort to the top of the list (they still scroll with the rest).
The group title has no attention
dot — dots remain on the individual sessions. Only the active group title and the currently focused child are
highlighted. Press `Space` on a group to collapse it. Press `p` or `Ctrl+P` to pin/unpin an independent session or an
entire group; a member inside a group pins the whole group. `Ctrl+P` also works while a live pane has input.

The small dot at the very start of row one is intentionally simple:

- yellow — the agent asked a structured question and is waiting for your answer;
- green — the current turn is still running and is not waiting for an answer;
- red — the agent produced a new result or terminal state you have not read yet;
- no dot — the session is idle and read.

Only one dot is shown, with `yellow > green > red` priority. Yellow and green therefore never overlap:
a waiting question temporarily takes precedence, while ordinary work still shows green. Dots never reorder,
filter, or count sessions, and they do not trigger sounds or system notifications. A red dot clears as soon as the right-pane content is actually visible;
split view clears every on-screen session together. Quickly moving past a card, a failed preview,
or switching away from corral does not mark it read. Existing history is baselined as read on the first upgraded
launch, so old sessions do not all light up at once.

Claude Code, Codex CLI, OpenCode, and Kimi Code derive these signals from local history. Cursor also exposes
live turn boundaries through user-level hooks; corral installs its entries idempotently in the background,
preserves unrelated entries, backs up changed files, writes atomically, and fails open so observer problems never
block Cursor. Use the `corral observer ... cursor` commands above to audit, preview, repair, or remove that integration.

- The first two rows never scroll away. `+ New session` (Chinese: `＋ 新建会话`) still starts a blank
  hosted session. The row under it is **Active sessions** (Chinese: `活跃会话`): it auto-tiles up to
  four hosted sessions that currently need you — waiting for an answer, working, or a new unread
  result. The label shows how many of those sessions there are (`Active sessions  ·  3 sessions`).
  The board is a three-line card: name on the first line, clickable previous/next on the
  second when there is more than one page (wrapping from last back to first), and a blank
  third line. The footer also spells out `[` previous page / `]` next page while the list
  has focus (laptop keyboards without Page Up / Page Down still work). A session that just finished stays
  on the current page for a moment so the grid does not jump while you are still reading it.
  Sessions idle in another window are left out (no live picture). A yellow dot means more waiting
  off the current page. Opening a specific sidebar session leaves the board and restores your usual
  single pane or split. The board never writes a named split group.
- Click a runtime button above the right side to add that agent as another pane in the current project.
  Up to four panes may run together; click a pane to focus it and sync the sidebar selection.
- A small **session card floats in the top-right corner of each live pane**, so every split
  tells you at a glance what that session is about and how far along it is. It is expanded by default, listing
  every timestamped prompt you typed (oldest to newest). When collapsed it shows the two ends — `▶ 12 prompts`, then
  `First <your very first prompt>` and `Latest <your newest one>`:
  the first prompt says what this session set out to do, the latest says where it is now. Click it
  (or press `Ctrl+G`) to toggle the two forms. Long prompts fold to two lines with an ellipsis; continuation lines stay aligned
  under the first. Prompt rows use a light zebra stripe. The card has a maximum height: when there are
  more prompts than fit, the body scrolls under a pinned header and footer, sticking to the latest prompts by
  default (scroll up to read earlier ones—including the middle, which is never dropped). Runtime-injected prompts (plan attachments, handoff
  text, conductor role prompts) are hidden. The card is drawn on live panes and on static
  transcript previews. Note that
  whatever it covers is hidden from the agent's screen and the mouse wheel cannot reach through it;
  click it or press `Ctrl+G` whenever you want the compact three-line view.
- `Ctrl`/`Cmd`-click (or `Space`) toggles multi-select on sidebar cards; with two to four selected,
  `Enter` opens them as a split (ended sessions show conversation preview; live/hosted sessions
  embed). `Esc` clears multi-select first. Plain click or arrow keys exit multi-select.
- `Enter` resumes the selected session in the right-hand pane (or reconnects an already-hosted
  live terminal there) **and hands keyboard input to that pane** — start typing to the agent
  right away, no click needed. Arrow-key browsing never steals focus (and never starts an agent),
  so the list stays usable; `Ctrl-\` gives input back to it.
- Transcripts are rendered as Markdown: headings, lists, tables, emphasis and fenced code all come
  out formatted rather than as raw `#`/`*`/backtick noise. Colour is reserved for telling speakers
  apart — each message starts with a rule and a role line in that speaker's colour, and the body
  itself stays in the normal foreground so long transcripts don't glare.
- An ended session shows its transcript instead of a live screen, and **selecting it never starts
  anything** — clicking its card just brings the transcript up, exactly like arrow-key browsing.
  `Enter` is what restarts it: from the sidebar, and also with focus already in the right-hand pane
  (both the transcript preview and the `Session ended` screen you get when an agent exits inside a
  pane). Restarting a member of a split group keeps the rest of the split in place.
- Click the card of a session that is still running to do the same thing as `Enter`. Clicking is a
  symmetric toggle: click the card of the pane that currently holds input and keyboard control goes
  back to the sidebar (same as `Ctrl-\`, the session keeps running); click it again to step back in.
  Clicking a pane directly is an equivalent way to take it over.
- When focus is on the sidebar, live panes are dimmed and their status bar says input is not
  going there — so you never type into a pane that isn't listening.
- While the right pane has focus, `Ctrl-\` returns keyboard focus to the list. Hosted sessions keep
  running in the background.
- The wheel follows where the mouse is, independent of keyboard focus: over the right pane it
  scrolls conversation preview or live history; over the left sidebar it scrolls the session list.
  At the live edge, agents that request wheel input receive it directly; otherwise corral browses
  tmux history.
- Drag to select text in the embedded pane — releasing the mouse copies it through OSC 52
  automatically (including over SSH when the terminal supports it). `Ctrl+C` still re-copies
  the current selection if you need it.
- The terminal cursor is parked at the agent's own cursor position, so IME preedit popups
  (e.g. CJK input methods) appear right at the agent's input box, not at the bottom of the screen.
- Dark/light theme detection inside panes is repaired on tmux ≥ 3.5a: `corral` probes your real
  terminal's background color at startup and feeds it to each hosted pane
  (`refresh-client -r`), so agents that query OSC 11 get the true value. Agents that were
  already running keep their earlier guess — restart them or set their theme manually once.
- `c` closes the focused pane; its hosted session keeps running in the background and can be reopened
  with `Enter`.
- `q` on a backgrounded / in-progress session ends it after a second `q` confirmation;
  quitting `corral` with `Esc` never kills anything — everything stays alive in tmux.

## Direct Launch

`corral claude [args...]`, `corral codex [args...]`, `corral opencode [args...]`,
`corral kimi [args...]`, `corral cursor [args...]`, and `corral pi [args...]` start a brand-new session.
In a real terminal they open the same sidebar TUI with the new session already hosted and
focused in the right-hand pane; outside a real terminal (piped/scripted) or with
`--no-keepalive` they take over the terminal the classic way instead.

Two forms after the runtime name:

1. **Project shortcut** — first argument does **not** start with `-` (e.g. `corral claude subswap`):
   fuzzy-match a local project (session history cwds ∪ git roots under `$HOME`, overridable with
   `CORRAL_PROJECT_ROOTS`), then open a blank session in that directory. Multiple matches → numbered
   picker. Extra args after the project name are rejected.
2. **Passthrough** — no args, or first arg starts with `-` (e.g. `corral claude --resume id`):
   remaining args go straight to the underlying CLI; `corral` only prepends the runtime's
   auto-approve flag unless you already included it, and hosts with
   [Keep-Alive](#keep-alive-survive-ssh-disconnects).

```bash
corral claude                       # blank Claude session in the current directory (TUI-hosted)
corral claude subswap               # blank Claude session in the matched project directory
corral claude --print "hi"          # passthrough flags/args to claude
corral codex --resume <id>          # `codex --resume`, auto-approved and hosted in the TUI
corral opencode                     # blank OpenCode TUI session, hosted in the TUI
corral kimi                         # blank auto-approved Kimi session, hosted in the TUI
corral pi                           # blank auto-approved Pi session, hosted in the TUI
corral --no-keepalive claude        # classic full-terminal launch without the background tmux wrapper
```

OpenCode uses its own `--auto` flag (auto-approve every permission request that is not explicitly
denied) and corral adds it for you. Placement matters: `--auto` belongs to the main command and to
`opencode run` only, and it must come *after* the subcommand — `opencode --auto run …` is parsed as
"open the TUI in a directory named `run`". So `corral opencode run …` becomes
`opencode run --auto …`, while subcommands that reject the flag (`stats`, `export`, `auth`, …) are
passed through untouched. If your opencode does not know `--auto` yet, upgrade it.

Cursor also answers to the names you actually type: `corral agent` and `corral cursor-agent` are exact
aliases for `corral cursor` (Cursor's installer ships both `agent` and `cursor-agent` entry points).

## Command Interception (type the real command, get corral)

corral automatically enables interception during installation and whenever it first opens interactively. Typing `claude`, `codex`, `opencode`, `kimi`, `cursor-agent`, `agent` (when it is Cursor's CLI), or `pi` in your
terminal is the same as typing `corral <runtime>`: the new session is hosted, auto-approved, and
survives disconnects.

```bash
corral shim status                    # what's installed for the current shell
corral shim install                   # install (adds one marked block to your shell config)
corral shim install --dry-run --json  # preview only, writes nothing
corral shim install --include agent   # force-intercept `agent` if it wasn't recognized as Cursor
corral shim uninstall                 # remove only corral's block, leave everything else intact
```

Supports bash / zsh / fish and detects the current shell automatically. The setup is idempotent, preserves
unrelated configuration, and backs up a changed configuration file before writing. `corral shim uninstall`
removes only corral's marked block.

These always run the real command untouched:

- headless/scripted calls (`claude -p "..."`, `codex exec ...`, pipes, CI, editor extensions, agents
  spawning agents);
- management subcommands (`claude update`, `pi install`, `agent login`, `agent about`, …);
- commands already inside a corral-hosted session (no double wrapping). Your own tmux/screen sessions
  are still intercepted;
- when `corral` isn't on PATH (e.g. you uninstalled it) — your original command always still works.

`agent` is intercepted automatically only when corral recognizes it as Cursor's CLI (the official
install, or a local wrapper that still launches Cursor). Other tools with that name are left alone;
use `--include agent` if recognition misses your install.

## Keep-Alive (survive SSH disconnects)

Sessions started or resumed from the TUI are, by default, wrapped in a dedicated background `tmux`
server (`tmux -L corral-keepalive`, using a bundled config — never your own `~/.tmux.conf`). If your SSH
connection drops or you close your laptop, the underlying `claude`/`codex` process keeps running on
the remote machine. Reopen `corral` and the session shows `后台运行中` (running in background); pressing
`Enter` reattaches instead of starting a competing second process.

- Press `Ctrl-\` (no prefix needed) to detach and return to your shell while the session keeps running;
  the standard `Ctrl-b d` also works.
- Press `q` on a backgrounded / in-progress session to end it (press `q` again to confirm).
- Idle sessions (no tmux activity) are auto-reaped after 2h by default; tune with
  `CORRAL_KEEPALIVE_IDLE_HOURS` (`0` disables reaping; the legacy name `SC_KEEPALIVE_IDLE_HOURS`
  still works). Reaping only closes the background tmux session — history stays on disk.
- Soft cap of 12 hosted agent processes (`CORRAL_KEEPALIVE_MAX_SESSIONS`; `0` disables).
  When over the cap, Corral closes the longest-idle sessions that are **not** actively working
  and have had no tmux activity for over 10 minutes
  (`CORRAL_KEEPALIVE_PRESSURE_IDLE_MINUTES`). Sessions still executing are never pressure-reaped;
  if nothing qualifies, the count may temporarily exceed 12.
- Disable keep-alive for a single run with `corral --no-keepalive`, or permanently with
  `CORRAL_KEEPALIVE=0` (legacy `SC_KEEPALIVE=0` also works).
- Keep-alive of the full-screen attach form is skipped when `corral` is already running inside a
  `tmux`/`screen` session (no nesting); embedded panes don't attach and work fine there.

## Agent / Automation

`corral` also exposes read-only, structured subcommands meant for AI agents to query local session
history — list, search, inspect, build a handoff context package, and produce a native continuation
plan. None of them launch or resume anything; what to do with the data and plan is left to the
caller.

```bash
corral list --cwd my-app --status pending --top 5 --compact # compact, capped session list
corral search weather app --top 3 --compact                 # relevance-ranked topic search
corral search weather app --deep                            # include full conversation search
corral show <session-id-prefix> --messages 10 --compact     # session detail + recent conversation
corral show <session-id-prefix> --full --out /tmp/corral.json # write large full output to a file
corral context <session-id-prefix>          # handoff package: history path, suggested prompt, resume command
corral plan continue <runtime:id> --instruction "Continue the remaining work" # argv/cwd plan; does not start it
corral describe [command]                   # machine-readable command/argument/field reference
```

Every command prints a JSON envelope (`{ok, data, error, meta}`) and uses fine-grained exit codes
(`0` success, `2` usage error, `3` not found, `5` ambiguous session reference). Running `corral` with no
subcommand outside a real terminal (piped, scripted, or invoked by an agent) also falls back to a
JSON session list instead of trying to start the terminal UI.

For `list` and `search`, `--limit` is scan depth per runtime and `--top` is the returned result
count cap. `search` returns `score`, `matched_via`, and `matched_fields`; `list`/`search` rows
include `resumable` and `resume_command` so automation can decide whether to resume in place or
start fresh. `corral plan continue` turns that decision into a structured, read-only execution plan
(`argv` and `cwd`), never a shell command string and never a launched process.

See [docs/SKILL.md](docs/SKILL.md) for the full command reference, field semantics, and typical
agent workflows.

## Key Bindings

| Key | Action |
| --- | --- |
| `Up` / `Down` / `j` / `k` | Move selection |
| `/` | Focus the sidebar filter box (case-insensitive fuzzy match on group name, project name, path and session title) |
| `Ctrl+F` | Full-text search across session conversations; results show the matching lines, newest session first. `Enter` opens the selected session in the sidebar |
| `Ctrl+P` | Pin / unpin the current window or its split group (works even while a live pane has input; Textual's command palette is disabled) |
| `Enter` | Resume selected session with the native runtime (reattach if it's already running in the background); on the pinned `+ New session` row, start the new-session flow; on **Active sessions**, open the live board. For a session whose process is gone this is the **only** way to restart it — clicking its card only shows the transcript. Also works with focus in the right-hand pane whenever that pane holds a conversation preview or a `Session ended` screen |
| `a` | Open advanced handoff actions |
| `q` | End a backgrounded / in-progress (keep-alive) session; press `q` again in the confirm dialog |
| `x` | Permanently delete the selected local session (or every session in the selected group); press `x` again in the confirm dialog |
| `c` | Close the focused right-side pane without ending its hosted session |
| `p` | Pin / unpin the selected independent session or the selected session group (a member inside a group pins the whole group) |
| `Ctrl+Shift+B` | Show / hide the sidebar (also the ◀/▶ control on the runtime top bar) |
| `Ctrl+G` | Expand / collapse the session card floating in the top-right corner of a live pane (clicking it does the same) |
| `Home` / `End` / `PgUp` / `PgDn` | Scroll the right-pane conversation preview (also mouse wheel over the pane) |
| `F12` | Save a local diagnostic screenshot under `~/.cache/corral/screenshots/` |
| `Esc` | Clear search / close dialog, or quit (clicking outside a dialog closes it too) |

`Enter` (or a click) hands input to a hosted agent; `Ctrl-\` returns keyboard focus to the sidebar
without ending the process. `Ctrl+Shift+B` (or the ◀/▶ control on the left of the runtime top bar) toggles
the sidebar so the panes can use the full width; the preference is remembered across launches.
(`Ctrl+B` is left free because Claude Code uses it to background a running task.)
While a live pane has input, sidebar shortcuts step aside so the keys
reach the agent. Mouse wheel over either pane works regardless of which side has focus.

## Cross-Runtime Handoff

Press `Enter` on a session for native resume (same assistant, full original context).

Advanced action (`a`) first item **exports** the same share transcript as `corral share` and copies the file path; second item **copies** the session beside the original; picking an assistant always starts a **new** session that reads the source history—whether you pick another assistant or the same one (useful when the original session is stuck or buggy). Copy and handoff open beside the source in a split view. The prompt includes:

- source runtime name;
- original session title;
- original working directory;
- original history location (a JSONL file for Claude/Codex/Kimi, or a SQLite database plus session ID for OpenCode);
- a short format hint for reading that history.

The original session history is left untouched (opened read-only). The target runtime decides what history it needs to read before continuing the work.

## Title Generation

The TUI first shows a local fallback title so the first screen is immediate. A detached background
process can then generate better Chinese titles in small batches through an available installed runtime.

Pi title requests use `--no-session --no-tools --print`: they do not create Pi history and cannot invoke tools.

Cost controls:

- generated titles are cached by runtime and session ID;
- a file lock prevents duplicate title-generation workers;
- available assistants are equal candidates: batches start at a random candidate and then rotate, rather than permanently preferring one assistant;
- failed, timed-out, invalid, or missing results keep the local fallback title;
- a failed title is not retried automatically on later launches, so it does not repeatedly consume
  account quota. A future title-cache upgrade may retry it under updated rules.

Title generation is optional in practice: if no generator is available or generation fails, the
Corral still works.

## Client Auto-Update

Each time the TUI starts, it checks in the background whether a newer release is available (one
HTTPS request to the public GitHub API, see [Privacy Model](#privacy-model)). If your install can be
upgraded in place (Homebrew tap, `pipx`, or `pip`-based install), a small notice appears in the
bottom-right corner; click it to update, then optionally restart `corral` right there. The upgrade
installs the prebuilt wheel published with the release, so no Rust toolchain is needed. Dismissing
it for the day is one click — that works in the failed state too, where the notice also shows a
short reason. Source/dev checkouts are never nagged — the check is skipped entirely for that
install path.

You can also trigger the same check manually at any time, without opening the TUI:

```bash
corral update
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `src/corral/` | installable package (src-layout) |
| `src/corral/cli.py` | process entry, argparse, direct-launch dispatch |
| `src/corral/store.py` | session store / snapshot refresh |
| `src/corral/display.py` | width, cards, preview, filtering helpers |
| `src/corral/theme.py` | OSC probe and runtime label colors |
| `src/corral/ui/` | Textual UI: main screen, modals, session list, split-pane area, runtime top bar, embed pane |
| `src/corral/ui/search_modal.py` | full-text search modal (`Ctrl+F`) |
| `src/corral/search.py` | in-memory full-text index over session conversations |
| `src/corral/split_layout.py` | persistent session groups, collapsed state and sidebar pinning |
| `src/corral/embed.py` | embedded-pane host (`capture-pane` / `send-keys`) |
| `src/corral/agent_api.py` | read-only `list`/`search`/`show`/`context`/`describe` |
| `src/corral/keepalive.py` | tmux-backed keep-alive wrapper |
| `src/corral/models.py` | shared session / handoff / launch-plan models |
| `src/corral/runtime/` | runtime adapters |
| `src/corral/scan/` | per-assistant history scanners |
| `src/corral/titles.py` / `titlegen.py` | title cache and generators |
| `src/corral/updater.py` | client auto-update: version check, channel detection, in-place upgrade |
| `tests/` | unit tests |
| `docs/SKILL.md` | agent-facing command reference |

## Development

```bash
python3 -m pip install --user -e .
python3 -m compileall -q src/corral tests
python3 -m unittest discover -s tests -v
```

For UI changes, run a real terminal smoke test as well (`bash selftest.sh` for embed/keepalive paths).

Maintainer notes live in [AGENTS.md](AGENTS.md) and [docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md).

## License

MIT. See [LICENSE](LICENSE).
