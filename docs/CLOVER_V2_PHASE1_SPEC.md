# 🍀 Clover v2 — Phase 1 Spec: IMAP → local `.eml` archive

> Status: **BUILT** (this doc captures the *design intent*, extracted after the fact for parity with
> the Phase 2/3 specs; the original design lived in the Rev-D full spec under `history/` + the
> roadmap). **For what it actually does today, see [`HOW_CLOVER_WORKS.md`](HOW_CLOVER_WORKS.md).**
> Produces the `.eml` archive + `_index.jsonl` that Phase 2 (threading) consumes.

## Purpose

Given an IMAP mailbox, archive **every selected email** to a local path as self-contained `.eml`
files — a full-fidelity, legal-grade, offline copy that any mail client can re-open. This is the
foundation the rest of the pipeline reads; it is captured **once**, read-only, and never re-fetched
to do later work.

## Principles

- **Full coverage, full fidelity.** The raw RFC822 `.eml` keeps headers, body, and all *embedded*
  attachments in one file. No redaction — a redacted archive defeats the purpose.
- **Read-only & non-destructive.** `BODY.PEEK[]` never marks mail seen; Clover never deletes,
  moves, or sends anything.
- **Resumable & idempotent.** Dedup by `(folder, UIDVALIDITY, UID)`; re-running resumes, never
  re-downloads. Filters change only the *selected set*, never the dedup identity.
- **Secrets stay local.** Password in the OS keyring (never in config, never logged).
- **Pluggable ingestion.** A `MailSource` ABC + registry; IMAP is the first source, the rest of the
  provider suite added one by one without touching the archive format.

## Scope

In: IMAP connect/auth, folder selection, date/size **selection filters**, read-only fetch to
`.eml`, the incremental `_index.jsonl` catalogue, an **integrity/reconcile** check, and a thin
Setup + Archive web UI. **Add-on:** share-link **harvest + download** (see §Link-shares).
Out (later phases): threading (P2), comprehension (P3), issue clustering (P4), forecasting (P5).

## Architecture

1. **`MailSource` (ABC + registry)** — ingestion interface; `clover/sources/` holds the IMAP
   implementation (others pluggable).
2. **`clover/archive.py`** — the archiver: prep-scan → selection → read-only fetch → write `.eml` +
   append `_index.jsonl`; plus `reconcile()`.
3. **Web UI** — FastAPI Setup (credentials, Test IMAP, Save) + Archive (folders, filters, run/stop,
   status, reconcile) pages.

## Selection filters

- **Folders:** any subset of the mailbox's folders (INBOX / Sent / Trash / named).
- **Date:** received-date, inclusive both ends — presets *All time · Yesterday · Last 3 days ·
  Last 7/30/90 days · This year · Custom range*.
- **Size:** *any · at least N MB · largest-N per folder.*
- Computed from a cheap **prep scan** (`FETCH INTERNALDATE RFC822.SIZE` — metadata only, no bodies),
  shown with a progress bar. Default (no filter) archives everything.

## Dedup, resume & naming

- **Dedup key:** `(folder, UIDVALIDITY, UID)` — survives re-runs and is independent of any filter.
- **UIDVALIDITY** recorded per message; a server UID reset is detected and doesn't cause silent
  mis-dedup.
- **Naming:** `<archive>/<folder>/<derived-name>.eml`; a genuinely different message that derives the
  same name gets a `_<uid>` suffix (no overwrite).
- **Incremental index:** each saved message appends one line to `_index.jsonl` and flushes, so an
  interrupted run leaves a consistent partial archive.

## Output — `<archive>/`

```
<archive>/<Folder>/<name>.eml          one self-contained RFC822 message
<archive>/_index.jsonl                 one row per archived message:
   {id, folder, key (UID), validity (UIDVALIDITY), from, subject, date, path, size, sha256}
```
`id` = `Message-ID` (or synthetic `uid_<key>` when the header is absent). `sha256` is over the raw
bytes (integrity). Cross-folder copies of one message are separate index rows (one per folder copy),
re-linked into a single logical message by Phase 2.

## Integrity — reconcile

`reconcile()` cross-checks `_index.jsonl` against the `.eml` on disk per folder and reports
`indexed / on-disk / missing / orphans`. A healthy archive: `indexed == on-disk`, 0 missing, 0
orphans. `_linkfiles/` (downloaded link files, not archived mail) is excluded from the count.

## Link-shares (add-on: 4a detect · 4b/4c download)

Many emails reference files behind share links instead of attaching them. Captured separately so
nothing is lost:
- **Harvest (4a):** scan every `.eml` body for provider links (SharePoint/OneDrive, Google Drive,
  Dropbox, WeTransfer, Box) → append to `<archive>/link_shares.jsonl`
  `{message_id, folder, eml, provider, url, status, file, size, confirmed}`, `status="pending"`.
- **Download (4b/4c):** httpx direct fast-path else headless browser; **URL-dedup** (one download per
  unique link, reused across emails); a **size-confirm gate** (oversize → `needs-confirm` until the
  user OKs it). Files saved to `<archive>/_linkfiles/<message-id>/<filename>` (collision-safe,
  Unicode-preserving). The `link_shares.jsonl` row is the **join**: it ties each `.eml` (`eml` path +
  `message_id`) to its links and to the downloaded `file`.

## Edge cases

- **No Message-ID:** keyed by synthetic `uid_<key>`; can't be threaded later, so it becomes a
  singleton (handled in P2).
- **Cross-folder duplicate:** same message in two folders → two index rows (one per copy); P2
  collapses them to one member with two locations.
- **Unparseable date:** stored verbatim in the index; P2 normalizes/orders defensively.
- **Flaky connection:** folder-list + fetch retry with backoff; failures surface a plain-language
  reason (e.g. "No internet — couldn't reach the mail server [NET-11001]").

## Safety invariants

Read-only mail access; never delete/send/move/flag; password in keyring, never logged; every write
dedup'd and idempotent; the published repo carries no mail data or credentials.

## Acceptance criteria

- Every selected message lands on disk as a valid `.eml`; `_index.jsonl` reconciles (indexed ==
  on-disk, 0 missing/orphans); re-running is idempotent and marks nothing seen.
- Filters change only the selected set, not the dedup key (narrow-then-widen backfills cleanly).
- No credential is written to disk or logs.
- Archiver logic is unit-tested (selection, dedup/resume, naming, index schema, reconcile, link
  harvest/fetch incl. dedup + size gate) on synthetic fixtures — no live mailbox needed.
