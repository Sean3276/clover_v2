# 🍀 Clover v2

A mail-archive → comprehension → intelligence pipeline. **📍 Full plan:
[docs/CLOVER_V2_ROADMAP.md](docs/CLOVER_V2_ROADMAP.md)** (5 phases + 2 cross-cutting tracks).

**Status:** Phase 1 (archiving) implemented; Phases 2–5 planned. Pluggable mail sources
(`clover/sources/`) — IMAP available, the rest of the suite added one by one.

---

## Phase 1 — IMAP → local `.eml` archive

Given an IMAP mailbox, archive **every email** from the folders you choose
(INBOX / Sent / Trash / named folders) to a local path you choose, as `.eml` files
organized by folder and Message-ID. The same email appearing in Inbox **and** Sent
is linked by Message-ID via `_index.jsonl`.

**Why `.eml`:** it's the complete raw RFC822 message — headers, body, and all
**embedded** attachments in one self-contained, legal-grade file, re-openable in any
mail client. (Link-shared files — SharePoint/Dropbox/Drive download links — are *not*
inside the `.eml`; capturing those is a later phase.)

## Install
```
cd .clover_v2_github
python bootstrap.py
pip install -r requirements.txt
```

## Run
```
python -m uvicorn app.main:app --port 8765      # → http://127.0.0.1:8765
```
1. **Setup** — enter IMAP host/port/user/password (e.g. `you@example.com`),
   click **Test IMAP**, set the archive destination path, **Save**.
2. **Archive** — tick the folders to archive, optionally set a per-folder limit
   (start with e.g. 20 to trial), click **Archive selected**. Re-running is
   resumable — already-archived messages are skipped.

## Output
```
<archive_path>/
   INBOX/<message-id>.eml
   Sent/<message-id>.eml
   ...
   _index.jsonl     # {id, folder, key, validity, from, subject, date, path, size, sha256} per email
```

## Layout
```
.clover_v2_github/   app/ (web UI)  clover/ (config, paths, safe_name, archive, sources/)
                     tests/  bootstrap.py  requirements.txt  docs/ (roadmap + history)
.clover_v2/          clover_config.json  logs/        (runtime; archive_path is separate/user-defined)
```

## Test
```
python -m pytest tests -q
```

## Safety
IMAP password in OS keyring (never logged); read-only fetch (`BODY.PEEK[]`, never marks
mail seen); never deletes or sends anything; resumable & idempotent.
