# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot for the Studiosuccess SMM agency (`bot.py`, Python 3.9, `python-telegram-bot` 22.5,
OpenAI SDK). It serves two audiences in one bot:

- **Clients** — fill out a brief via a guided button/text flow; the answers create a new client
  project folder automatically.
- **Staff** (Telegram IDs in `STAFF_USERS`) — pick an active client project, read its brief, read
  internal instructions, and ask GPT questions about a project with that project's accumulated
  memory as context. Client project group chats can also be registered so the bot silently logs
  conversation and answers when @-mentioned.

There is no database — all state lives in JSON/Markdown files under `client_projects/<slug>/` and
`config/`.

## Commands

```bash
# Local run
source venv/bin/activate
python3 bot.py

# Syntax check before considering any change done (this is what deploy.sh runs)
python3 -m py_compile bot.py
python3 -m py_compile services/*.py
python3 -m py_compile scripts/*.py

# Memory Engine — daily consolidation script (see below). Always dry-run first.
python3 scripts/daily_memory_update.py --dry-run
python3 scripts/daily_memory_update.py

# One-off import of externally-prepared memory files into client_projects/
python3 scripts/import_project_memories.py --dry-run
python3 scripts/import_project_memories.py

# Deploy to production (rsync + systemd restart on the remote host)
./deployment/deploy.sh
```

There are no automated tests and no linter configured in this repo.

## Architecture

```
bot.py                          single-file bot: handlers, brief state machine, project chat
services/
  context_manager.py            Memory Engine — assembles GPT context for a project (live path)
  prompt_builder.py              assembles the final system prompt from ContextManager's output
  instruction_manager.py         reads instructions/*.md verbatim, no processing
scripts/
  daily_memory_update.py         offline GPT consolidation of chat_context.md -> memory.md
  import_project_memories.py     bulk-imports imports/pending/*_memory.md into client_projects/
config/
  projects.json                  registry of client projects — the single source of truth for
                                  which projects staff see (folder/memory/brief file paths, is_active)
  chats.json                     registry of Telegram group chats -> project_slug mapping
client_projects/<slug>/
  brief.md, brief.json           client's filled-in brief
  memory.md                      durable long-term knowledge about the project, in `[section]` blocks
  chat_context.md                raw append-only log of every message in the project's group chat
  memory_state.json              two independent offsets into chat_context.md (see below)
  memory_index.json              keyword/priority index rebuilt from memory.md (debugging aid; not
                                  actually consulted by section selection at prepare_context time)
  pending_memory.md               candidate facts awaiting human review before promotion to memory.md
instructions/0N_*.md             internal SOPs shown to staff via the "Инструкции" menu, read as-is
deployment/deploy.sh              rsync + systemd deploy to the remote host
deployment/studiosuccess-memory-update.{service,timer}   only the daily-memory-update unit lives here
```

### Memory Engine — two independent paths write to the same `memory.md`

**1. Live path (`ContextManager.prepare_context`, called on every GPT turn in a project chat):**
- Reads new bytes from `chat_context.md` since `memory_state.json["last_processed_offset"]`.
- Extracts candidate knowledge *locally* via regex indicator phrases (`KNOWLEDGE_RULES` — no GPT
  call), dedupes against `memory.md`/`pending_memory.md`, and appends survivors to
  `pending_memory.md` for human review — it does **not** write to `memory.md` directly.
- Picks which `memory.md` sections to feed the model via keyword scoring against `SECTION_KEYWORDS`
  (not via `memory_index.json`, despite that file being rebuilt on every call).
- Rebuilds `memory_index.json` as a side effect (currently informational only).
- Returns a dict consumed by `prompt_builder.build_project_system_prompt`, which stacks: base
  prompt → memory sections → new-knowledge block → brief (only if the query looks brief-relevant,
  via `BRIEF_KEYWORDS`) → recent chat tail → unprocessed new messages.
- After a successful GPT reply, `ContextManager.update_state` advances
  `last_processed_offset` — this offset is scoped to the *interactive bot*.

**2. Offline path (`scripts/daily_memory_update.py`, cron/systemd-timer driven):**
- Uses its **own** offset, `last_daily_update_offset`, in the same `memory_state.json` — the two
  paths never race on the same offset.
- Sends new chat text + a short per-section summary to GPT (`gpt-4o-mini`, JSON mode) and gets back
  `additions` / `updates` / `pending` structured directly against named sections.
- Applies `additions`/`updates` straight into `memory.md` (old lines are struck through with
  `~~...~~` rather than deleted, so history is preserved in place), writes ambiguous items to
  `pending_memory.md`, takes a timestamped backup of `memory.md` before writing and rolls back on
  error, and prunes backups beyond `MAX_BACKUPS` (14).
- Guarded by an `fcntl` lock file so overlapping runs no-op instead of racing each other.
- This is the only path that promotes information into `memory.md` automatically; the live path
  only ever stages candidates in `pending_memory.md`.

Both paths key `memory.md` sections with the same `^#{0,3}\s*\[(\w+)\]` pattern; recognized section
names are listed in `SECTION_KEYWORDS` (context_manager.py) — stick to those when writing memory by
hand so both the live selector and the daily consolidator can find them.

### Deploy / systemd

`deployment/deploy.sh` targets an SSH host alias `studiosuccess-server` (Yandex Cloud VM, user
`yc-user`) and remote path `/home/yc-user/telegram-gpt-bot`. It compiles all `.py` files, rsyncs
the repo (excluding `.git`, `venv`, `logs/`, `client_projects/`, caches), installs pip deps only if
`requirements.txt` changed in the last commit, installs the memory-update systemd unit + timer,
restarts `studiosuccess-bot.service` and the memory timer, then tails `journalctl` for the bot.

Two things to know if you touch this:
- **`studiosuccess-bot.service` is not in this repo** — only the memory-update service/timer are.
  The main bot's unit file exists on the server but isn't version-controlled here.
- The remote venv is `.venv/` (deploy.sh installs into and runs from `.venv/bin/...`), while local
  dev uses `venv/` (no dot) — don't assume they're the same path.
- The timer fires `OnCalendar=*-*-* 06:00:00 UTC`, i.e. 09:00 Moscow time.

## Known gotchas / state to be aware of

- **The other root-level docs are stale.** `ARCHITECTURE.md`, `ROADMAP.md`, `CHANGELOG.md`,
  `NEXT_SESSION.md`, and `QUICKSTART.md` describe an earlier version of the bot (per-employee
  `/project`, `/personal`, `/post`, `/stories`, `/reels`, `/audit`, `/report`, `/current`, `/help`
  commands, the OpenAI Responses API, model `gpt-5.5`). None of that exists in current `bot.py` —
  it now centers on the brief flow, project group-chat logging, an instructions menu, and the chat
  registry. Treat `bot.py` as the source of truth, not those docs; update or remove them rather than
  trusting them.
- `ACTIVE_PROJECTS`, `BRIEF_STATES`, and `LAST_BOT_MESSAGE` in `bot.py` are plain in-memory dicts —
  a client mid-brief or a staff member's selected project is lost on bot restart.
- `bot.py` logs via bare `print()`; only `scripts/daily_memory_update.py` uses the `logging` module
  with a file handler. Keep that in mind if you're chasing an issue in production — `bot.py`'s
  output only shows up in `journalctl`/stdout, not in `logs/`.
- There are no commits in this repository yet (working tree is all untracked files) — the first
  commit is still pending.
