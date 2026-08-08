# Privacy

`pickup` is designed as a local terminal utility for existing Claude Code, Codex CLI, OpenCode, Kimi Code CLI, and Cursor Agent CLI users.

## Data It Reads

- Claude Code history under `~/.claude/projects/`.
- Codex CLI history under `~/.codex/sessions/`.
- Codex session names from `~/.codex/session_index.jsonl` when present.
- Cursor Agent CLI history under `~/.cursor/chats/` (per-chat `meta.json` / `store.db`)
- OpenCode history from its SQLite database at `~/.local/share/opencode/opencode.db` (or the
  directory pointed to by `OPENCODE_DATA_DIR`), opened with a read-only connection (`mode=ro`).
  The tool never writes to this database.
- Kimi Code CLI history under `~/.kimi-code/sessions/` (per-session `state.json` metadata and the
  `agents/main/wire.jsonl` conversation log).
- Cursor's user-level hook configuration at `~/.cursor/hooks.json`, solely to inspect and preserve
  existing entries while managing pickup's own live-state observer entries.

The tool reads these files to build a recent-session list, extract a compact preview, and prepare native resume or cross-runtime handoff commands.

## Data It Writes

- Generated title cache under `~/.cache/pickup/titles.json`.
- A lock file under `~/.cache/pickup/titles.lock` while title generation is running.
- Sidebar memory under `~/.cache/pickup/sidebar-layout.sqlite3`: session groups (which hosted
  sessions are shown side-by-side in the right pane), their generated fruit names, collapsed and
  pinned state, the last focused session, and whether the sidebar is hidden. Session keys and
  project paths only — no conversation content. Shared by every pickup window on the machine.
  Older versions kept this in `~/.cache/pickup/split-layout.json` and `~/.cache/pickup/ui-prefs.json`;
  those files are imported once and then left untouched (never rewritten, renamed, or deleted).
- Update-check state under `~/.cache/pickup/update.json` (which version you last dismissed, and on
  which day) — only written when you click "dismiss" on the update notification or run `pickup update`.
- Content-free session attention state under `~/.cache/pickup/session-attention.sqlite3`. It stores
  runtime/session identifiers, opaque activity/question tokens, timestamps, the current attention
  kind, and read baselines. It does not store prompts, answers, titles, tool output, or conversation text.
- Pickup-managed Cursor observer entries in `~/.cursor/hooks.json`. The TUI installs or repairs these
  entries idempotently in the background; unrelated hook entries are preserved. Before changing an
  existing file, pickup writes a user-only backup under
  `~/.cache/pickup/cursor-hooks-backups/`, then replaces the config atomically. You can inspect,
  preview, repair, or remove this integration with `pickup observer status cursor`,
  `pickup observer install cursor --dry-run`, `pickup observer install cursor`, and
  `pickup observer uninstall cursor`. Uninstall removes only pickup-managed entries.
- A bounded derived-performance database under `~/.cache/pickup/performance-cache.sqlite3`. It may
  contain parsed session metadata and conversation preview text copied from history files that your
  OS user can already read. Entries are keyed by exact source-file signatures and rebuilt when those
  files change. The directory is user-only, the database is user-readable/writable, and nothing in it
  is uploaded. Inspect it with `pickup cache status`, preview deletion with
  `pickup cache clear --dry-run`, clear it with `pickup cache clear`, or disable it with
  `PICKUP_CACHE=0`.
  Note: since the full-text search feature (`Ctrl+F`), the TUI warms a search index in the
  background shortly after startup, which parses the conversation text of every scanned session
  rather than only the ones you open. This does not read anything your OS user could not already
  read and still uploads nothing, but it does mean the derived-performance database above fills up
  with conversation text sooner and more broadly than before. The search index itself lives only in
  memory and is never written to disk. `PICKUP_CACHE=0` still disables the on-disk part.

It does not write attention state into Claude Code, Codex CLI, OpenCode, Kimi Code CLI, or Cursor
conversation history. The Cursor write described above changes only the user-level hook configuration.

## Network And Account Usage

The core scanner, TUI, preview screen, and JSON output do not make network requests by themselves.

**Client auto-update.** Each time the TUI starts, it makes one HTTPS request to the public GitHub API
(`https://api.github.com/repos/x0c/pickup/releases/latest`) to check the latest published version
number. No session content, file paths, or any other local data is sent — only that one request to
that one endpoint. If your install can't be auto-upgraded (a source/dev checkout), this check is
skipped entirely and nothing is requested. If a newer version is found, a small notice appears in the
bottom-right corner; clicking it runs the same install command your install channel already uses
(`brew upgrade pickup` or `pip install --upgrade`), then offers to restart `pickup`. You can also
trigger this manually any time with `pickup update`, or dismiss the notice for the day.

Optional title generation distributes batches among locally installed Claude, Codex, OpenCode, Kimi, and Cursor CLIs (or honors an explicit `PICKUP_TITLE_GENERATOR`, with legacy `SC_TITLE_GENERATOR` still accepted). That command sends short session excerpts to the corresponding model provider under your own account and credentials. If one assistant fails, another available assistant takes over that batch; if all fail, the tool keeps using local fallback titles.

Title generation uses non-persistent one-shot modes for Claude and Codex, so those derived requests are not saved as their sessions. OpenCode, Kimi, and Cursor may retain their own derived request, but pickup marks it and excludes it from the user session list.

Failed, timed-out, invalid, or incomplete title results are recorded locally for the current cache
version. Later launches do not automatically submit those sessions again, preventing repeated quota
usage; a future cache-version upgrade may retry them under updated rules.

## Attention Status And Cursor Observer

The yellow/green/red attention dots are derived locally. Claude Code, Codex CLI, OpenCode, and Kimi
Code use explicit events in their existing local history. Cursor history is probed only when a session
is live or its relevant files changed, avoiding repeated database reads for cold sessions. Existing
history is treated as read on the first upgraded launch, so installing the feature does not create a
wall of unread alerts.

Cursor's live turn boundaries are delivered to a short-lived local pickup hook process. It uses only
the hook event name, conversation/session identifier, and generation identifier, then records the
local receipt time needed to update attention state; prompt and response bodies are neither stored in
the attention database nor logged by the hook. Malformed input, configuration errors, permission errors, or local database
failures are fail-open: the hook exits successfully and never blocks Cursor from continuing.

Attention state never triggers network requests, sounds, system notifications, or remote telemetry.
The observer management commands support JSON output; install/uninstall also support strict
`--dry-run`, which makes no configuration, backup, or directory changes.

When you resume or hand off a session, the selected runtime process takes over the terminal. From that point on, Claude Code or Codex CLI behaves according to its own configuration.

## Keep-Alive (Background tmux)

By default, sessions started or resumed from the TUI are wrapped in a dedicated background `tmux`
server (socket name `pickup-keepalive`) so the underlying process survives an SSH disconnect. This changes
what stays running after `pickup` exits:

- The wrapped runtime process (and everything it does) keeps running in the background until it exits
  on its own, is manually closed (`x` in the TUI), or is auto-reaped after being idle (default 6h, see
  `PICKUP_KEEPALIVE_IDLE_HOURS`, legacy name `SC_KEEPALIVE_IDLE_HOURS`).
- To detect which sessions are already backgrounded, `pickup` reads the local process table (`ps -eo
  pid,ppid`) and lists the tmux server's own sessions (`tmux -L pickup-keepalive list-sessions`). This is
  local process metadata, not file content, and is not written anywhere.
- On a machine shared with other local users, anyone able to run commands as your OS user (or root) can
  attach to `tmux -L pickup-keepalive` and see the live terminal content of a backgrounded session — the
  same exposure any tmux session already has under your account; `pickup` does not add encryption or
  access control on top of it.
- Disable entirely with `pickup --no-keepalive` for one run, or `PICKUP_KEEPALIVE=0` (legacy
  `SC_KEEPALIVE=0`) permanently. The full-screen attach form is skipped when `pickup` is already
  running inside `tmux`/`screen`; embedded panes don't attach and are unaffected.

## Cross-Runtime Handoff

For handoff between runtimes, the tool passes the original history location (a file path, or a
SQLite database path plus session ID for OpenCode) and a short format hint to the target runtime.
It does not copy the full conversation into command-line arguments and does not modify the source
session.

The target runtime may choose to read that local history after it starts.

## Repository Hygiene

Do not commit real session history, generated caches, logs, tokens, API keys, or local environment files. The project `.gitignore` excludes common local artifacts, but contributors should still review changes before publishing.
