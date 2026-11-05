# AGENTS.md

Instructions for agents working in this repo. Read this before changing code or touching a running server.

## What this is

Flashcap: a personal reading list and flashcard app. Telegram is the inbox: links saved in a channel are pulled into a local SQLite library, studied as flashcards, and archived to a done list when finished.

The Python package is `capture/`. It is a single Flask app: library, cards, review, notes, quiz, Telegram ingestion. This repo was split out of the old `readlist` repository; Lecture Today lives separately and is not used here.

## Where things run

- Production is hermes, a Tailscale-only box. Code at `/home/cathreya/flashcap`. systemd user unit `flashcap.service`. Reached at `https://hermes.taila17c7c.ts.net`. The unit runs `python -m capture.server` on `127.0.0.1:8765`; Tailscale Serve terminates TLS.
- Local dev is `./capture/run.sh` on the Mac. Fine for tests and UI work, but stop it before hermes polls. Two processes with the same bot token fight over `getUpdates` and cause 409s.
- The Mac also runs `com.flashcap.noteagent`, which opens Obsidian URIs. Leave that one alone.

## Run, test, deploy

```bash
./capture/run.sh                      # local server on :8765
.venv/bin/python -m pytest capture/tests -q
.venv/bin/python -m capture.backfill  # load an export into the library
.venv/bin/python -m capture.titles    # fill empty titles from page metadata
```

Deploy is a code sync. Hermes is a git checkout on `main` with origin set:

```bash
ssh hermes 'cd ~/flashcap && git fetch origin main && git reset --hard origin/main && systemctl --user restart flashcap.service'
```

If GitHub credentials are missing on hermes, send a bundle from the Mac:

```bash
git bundle create /tmp/flashcap.bundle main
scp /tmp/flashcap.bundle hermes:/tmp/
ssh hermes 'cd ~/flashcap && git fetch /tmp/flashcap.bundle main:refs/remotes/origin/main && git reset --hard origin/main && systemctl --user restart flashcap.service'
```

Check the deploy:

```bash
ssh hermes 'systemctl --user is-active flashcap.service; journalctl --user -u flashcap.service -n 20 --no-pager'
curl -s https://hermes.taila17c7c.ts.net/api/health
```

`git reset --hard` is safe for `.env`, `capture/flashcards.db`, `.venv`, and `certs`, because they are untracked. Never add them to git.

## Environment

- `.env` at the repo root is ignored and never committed. It holds the LLM key and the Telegram ids.
- Hermes has its own `.env`. Do not copy the Mac `.env` over it. Append keys instead.
- The database is `capture/flashcards.db` unless `FLASHCAP_DB` says otherwise. Hermes holds the real cards and library. Do not overwrite it during a deploy.
- Hermes installs from `capture/requirements.txt`. Locally, `uv sync` uses `pyproject.toml`.

## Telegram rules

- The bot must be an admin in the reading channel. Admin is what lets it read every post and delete a finished message. Add it as a member of the done list group.
- The bot uses long polling. There is no webhook, and the box's only public port is already used.
- Chat ids are numeric, usually `-100...`. The invite code in a `t.me/+...` link is not a chat id. Putting one in `TELEGRAM_CHAT_IDS` silently drops every message while advancing the offset.
- To get ids, stop the poller, post once in each chat, then:
  ```bash
  curl -s "https://api.telegram.org/bot$TOKEN/getUpdates?limit=100"
  ```
  Read `message.chat.id` or `channel_post.chat.id`. For public chats, `getChat?chat_id=@name` also works.
- Only one process may call `getUpdates` with a given token. Stop the service before running the command by hand.
- `TELEGRAM_CHAT_IDS` is an allowlist. If it is wrong, updates are consumed and filtered out. A rising `offset` in `/api/health` with no new rows is the symptom.
- Telegram documents a 48 hour delete window. A failed delete leaves the item archived and sets `archive_error`, which the UI shows as `archive issue`.

## Reading list data model

`reading_items` is owned by `capture/store.py`. One row per saved link or note.

- `url_key` is the identity. It is the canonical URL from `obsidian.normalize_url`: `www` stripped, tracking params removed, query sorted, fragment and trailing slash removed. A note uses `tg:<chat>:<msg>` or `export:<message_id>`.
- Status is one of `unread`, `reading`, `done`, `dropped`.
- Re-ingesting the same message must never overwrite a user's status. The upsert only fills empty title, site, and description.
- `archived_at` and `archive_message_id` record the done-list copy. `archive_error` means the source delete failed.
- Done flow: forward to `TELEGRAM_DONE_CHAT_ID`, then delete from the source channel. Forward first. A failed forward reverts the status. A failed delete keeps the item done and flags it.
- Undo flow: `POST /api/library/:id/undo`. If the source was deleted, repost it, then delete the forwarded copy. If the source survived, only delete the forwarded copy. Reset the row to `unread`. A failed repost keeps the row `done`.
- The Telegram offset lives in `ingest_state` and advances only after the batch writes commit. Replays are idempotent.

## Backfill

`python -m capture.backfill --html <path>` parses a Telegram export and is the history path. Both export shapes work: `messages.html` and the JSON `result.json` (pass it with `--html`). It is safe to rerun. It does not read `lecture_today.db`; that old import was removed. Export rows have no live message id, so they cannot be forwarded or undone through Telegram.

The JSON export carries no link previews, so imported links arrive untitled. `python -m capture.titles` fills those from each page (`og:title`/`<title>`, oEmbed for YouTube and X, the abstract page for arXiv PDFs). It only writes to empty titles, so it never replaces one you or Telegram supplied. It is a one-shot command; do not start it from `create_app()`.

## Testing

- `pytest capture/tests` must pass. The suite builds a temporary SQLite file per test and never calls Telegram or the LLM.
- Add behavior tests at the API or function boundary, not implementation tests.
- Start background workers only from `main()`. `create_app()` is the test entry point; a thread there would run in every test.
- The deployment venv has no pip, uv, or ensurepip. `beautifulsoup4` and `soupsieve` are pure Python and were copied in. If you need a compiled package, recreate the venv with a Python that has pip.

## Commands and surfaces

- `/library` and `/api/library` are the reading list, paged 200 at a time.
- `/api/library/:id/undo` reverses a done item.
- `/api/health` exposes the Telegram state. Check it first when ingestion looks dead.
- `messages.html` is the Telegram export. `capture/backfill.py` reads it, or a JSON `result.json` export.

## Git

- `main` is trunk, and this repo has a single remote (`origin`). The old combined `readlist` repo is separate; do not push this work there.
- The working tree may have untracked project files. Commit only the files a change needs.
- Do not commit `.env`, `capture/flashcards.db`, or `certs`.

## Traps found the hard way

1. Invite codes are not chat ids. This was the first ingestion failure.
2. A wrong allowlist consumes updates silently. The offset rises and no rows appear.
3. Two pollers on one token return 409 and split updates.
4. Starting the server on the Mac duplicates the hermes service. One host only.
5. The hermes venv has no package installer. Do not assume `pip` exists.
6. A bot cannot restore a deleted message. Undo reposts it as a new message.
7. The export has no link titles. The UI derives a label from the URL path.
8. `create_app()` runs in every test. Worker startup belongs in `main()`.
