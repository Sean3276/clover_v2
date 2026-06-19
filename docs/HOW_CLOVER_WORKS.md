# 🍀 How Clover Works — behaviour reference (Phase 1 & 2)

> **What this is:** a *living* description of what Clover actually **does** when you use it —
> per action: the trigger, the steps it runs, the considerations behind them, and what it writes.
> Unlike the **specs** in this folder (which capture *design intent*, written before building) and
> the **README** (quick how-to-run), this doc tracks **current behaviour**. Keep it updated when
> behaviour changes. Last updated 2026-06-19.

---

## Mental model

```
IMAP mailbox ──(Phase 1)──► local .eml archive ──(Phase 2)──► thread trees ──(Phase 3)──► comprehension
                 archive            _index.jsonl       threads.jsonl          comprehensions.jsonl
```

- **Phase 1 = ingestion** (deterministic, read-only): copy mail to `.eml`, catalogue share links, download link files.
- **Phase 2 = organization** (deterministic, offline, no AI): link `.eml` into chronological threads + a browser to read them.
- Each phase only *reads* the previous phase's output, so any stage can be re-run without re-fetching mail.

### Where things live
```
<repo>/                         the app + code (user-agnostic)
<runtime>/                      git-ignored; default = .clover_v2 beside the repo (override: CLOVER_V2_HOME)
   clover_config.json           settings (NOT the password — that's in the OS keyring)
   logs/
<archive_path>/                 you choose this; default <runtime>/eml_archive
   <Folder>/<name>.eml          one file per message (raw RFC822, all embedded attachments inside)
   _index.jsonl                 one row per archived message (the catalogue)
   threads.jsonl                Phase 2 thread index
   link_shares.jsonl            catalogue of share links found in the mail
   _linkfiles/<message-id>/…    files downloaded from share links
   comprehensions.jsonl         Phase 3 output (if run)
```

---

## Phase 1 — Setup

### Action: **Test IMAP** · `POST /setup/test-imap`
- **Trigger:** enter host / port / user / password, click *Test IMAP*.
- **Does:** opens a throwaway IMAP connection (SSL), logs in, lists folders, disconnects. Nothing is stored.
- **Considerations:** confirms credentials + reachability *before* saving; a failure returns a plain-language reason (e.g. "No internet — couldn't reach the mail server [NET-11001]").

### Action: **Save** · `POST /setup/save`
- **Trigger:** click *Save* on the Setup page.
- **Does:** writes settings to `clover_config.json`; stores the **password in the OS keyring** (never in the file, never logged); records the archive destination path.
- **Output:** `clover_config.json` (+ keyring entry). You're sent to the Archive page.

---

## Phase 1 — Archive

### Action: **Archive selected** · `POST /archive/run`
- **Trigger:** tick the folders to archive, optionally add date/size filters, click *Archive selected*.
- **Does:**
  1. **Prep scan** (only if filters are set): `FETCH INTERNALDATE RFC822.SIZE` (metadata only — no bodies) to compute the selected set; a progress bar shows the scan.
  2. Connects IMAP **read-only** — `BODY.PEEK[]`, which **never marks mail as seen**.
  3. **Dedup:** skips anything already archived, keyed by `(folder, UIDVALIDITY, UID)`. Re-running **resumes** instead of re-downloading.
  4. Writes each message as `<archive>/<folder>/<name>.eml` (collision-safe — a genuinely different message with the same derived name gets a `_<uid>` suffix) and appends a row to `_index.jsonl`:
     `{id, folder, key (UID), validity (UIDVALIDITY), from, subject, date, path, size, sha256}`.
  5. When new mail was saved, **auto-rebuilds the thread index** (Phase 2).
- **Considerations:** read-only & non-destructive (never deletes/sends/flags); **resumable & idempotent**; survives flaky links (retries + plain-language errors); filters only change the *selected set* — they never change the dedup key, so narrowing then widening later still backfills cleanly.
- **Output:** `.eml` files + `_index.jsonl` (+ refreshed `threads.jsonl`).

#### Date / size filters (optional)
- **Date presets:** *All time · Yesterday · Last 3 days · Last 7 / 30 / 90 days · This year · Custom range* (received-date, **inclusive** both ends).
- **Size:** *Any · At least N MB · Largest N per folder.*
- Filters are computed from the prep-scan metadata; default (no filter) archives everything.

### Action: **Stop** · `POST /archive/stop`
- **Trigger:** *Stop* during a run.
- **Does:** sets a stop flag honoured between messages; the current message finishes, then the run ends cleanly. Already-saved mail stays; the next run resumes.

### Action: **Reconcile / Archive integrity** · `POST /archive/reconcile`
- **Trigger:** *Check archive integrity*.
- **Does:** cross-checks `_index.jsonl` against the `.eml` files on disk, per folder — reports `total indexed`, `total on disk`, `missing` (indexed but no file) and `orphans` (file but not indexed). `_linkfiles/` is excluded (those aren't archived mail).
- **Considerations:** read-only audit; a healthy archive reads `indexed == on disk`, `0 missing`, `0 orphans`.

---

## Phase 1 add-on — Share links

Many emails reference files behind share links (SharePoint/OneDrive, Google Drive, Dropbox, WeTransfer, Box) instead of attaching them. These actions catalogue and fetch those files so nothing is silently lost.

### Action: **Harvest links** · `POST /threads/harvest-links`
- **Trigger:** *🔗 Harvest links* on the Threads page.
- **Does:** scans every archived `.eml` body for share links, appends new ones to `link_shares.jsonl` (idempotent on `(message_id, url)`), each `status: "pending"`. Detection only — no downloads. Runs in the background.
- **Output:** `link_shares.jsonl` rows `{message_id, folder, eml, provider, url, status, file, size, confirmed}`.

### Action: **Fetch files** · `POST /threads/fetch-links`
- **Trigger:** *⬇ Fetch files* on the Threads page.
- **Does:** downloads `pending` links in the background:
  1. **URL-dedup:** if the *same* link was already downloaded, the record reuses that file — no re-transfer. (On the real corpus ~85% of records are repeats, so this avoids most downloads.)
  2. **Direct fast-path** (httpx) for Dropbox/Drive direct links, streamed; otherwise a **headless browser** (Playwright/Chromium) that dismisses cookie banners and clicks the provider's Download control.
  3. **Size gate:** a download over the threshold (default ~1 GB) is **not kept** — the record becomes `needs-confirm` with its size, so you can decide (see below). Direct links stop mid-stream; browser links are detected after they stream to a temp file (their size isn't known up front).
  4. Saved files go to `_linkfiles/<message-id>/<filename>` — **collision-safe** (`name (2).ext`) and **Unicode/CJK names preserved**. HTML login/interstitial pages are rejected, not saved as "files".
- **Status outcomes:** `downloaded · needs-confirm · needs-auth (gated/expired — open it yourself) · dead (404/410) · error`.
- **Considerations:** background, **one link task at a time** (no concurrent writes); re-runnable (only touches `pending`); cancellable via *Stop* (`POST /threads/stop-links`). The Threads list shows a live status summary.

### Action: **Download anyway** (confirm a large link) · `POST /threads/confirm-link`
- **Trigger:** for a `needs-confirm` link, click *Download anyway* in the thread (the row names the link, its email, and its size).
- **Does:** clears the size gate for that link and re-queues it (`confirmed`); the next *Fetch files* downloads it in full.

### Action: **Open a saved / linked file** · `GET /linkfile/{path}`
- **Trigger:** click *⬇ saved file* on a link in the reader.
- **Does:** serves the downloaded file inline. Path-guarded — only files under `_linkfiles/` can be served.

---

## Phase 2 — Threads

### Action: **Open the Threads page** · `GET /threads`
- **Does:** lists threads from `threads.jsonl` (subject · participants · message count · date range), shows which are comprehended (🍀), and a **share-link status summary**. The index auto-builds after archiving new mail and auto-builds if missing.

### Action: **Rebuild index** · `POST /threads/rebuild`
- **Does:** re-links every `.eml` into threads by `Message-ID` / `In-Reply-To` / `References` (union-find; header-only; no AI, no network). **Cross-folder duplicates** (same Message-ID in Sent *and* Trash) become one thread member with multiple locations. Pure transform — identical result every run. Writes `threads.jsonl`.

### Action: **Open / read a thread** · `GET /threads/{id}` → `GET /threads/{id}/msg/{idx}`
- **Does:** shows members in one chronological scroll. Each message body loads **on demand** when expanded (long threads stay collapsed for speed). Bodies render in a **locked-down sandboxed `<iframe>`** with a CSP that **blocks remote content** (tracking pixels / remote calls don't fire); inline `cid:` images are inlined as data URIs so they still show. Order toggle: oldest/newest first.
- **Considerations:** the `.eml` archive is never modified — content is stitched live, never duplicated to disk.

### Action: **Open / download an attachment** · `GET /threads/{id}/msg/{idx}/att/{n}`
- **Does:** extracts a genuine attachment from the `.eml` and serves it inline (so PDFs/images open, and you can save them). Inline *images* (signature logos, embedded screenshots referenced by the body) are shown in the body, not listed as attachments; inline PDFs/docs *are* listed.

### Action: **Comprehend** · `POST /threads/{id}/comprehend` (Phase 3 entry point)
- **Trigger:** *Comprehend* on a thread.
- **Does:** runs the AI comprehension pipeline for that thread and stores the result in `comprehensions.jsonl`; the thread then shows the 🍀 stamp. *(Behaviour detailed in `CLOVER_V2_PHASE3_SPEC.md`.)*

---

## Safety guarantees (hold across all of the above)
- **Read-only mail access** — `BODY.PEEK[]`; Clover never deletes, sends, moves, or marks mail.
- **Non-destructive & resumable** — every write is dedup'd/idempotent; re-running resumes.
- **Deterministic & offline where claimed** — threading and reconcile use no network and no AI.
- **No remote content** in the reader — sandbox + CSP neutralise tracking pixels and remote calls.
- **Secrets stay local** — password in the OS keyring; the published repo carries no data or identity.

---

*Design intent (the "why we built it this way") lives in `CLOVER_V2_ROADMAP.md` and the
`CLOVER_V2_PHASE2/3_SPEC.md` files. This doc is the "what it does today" companion.*
