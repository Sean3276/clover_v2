# 🍀 Clover v2 — Phase 2 Spec: Per-thread organization + Threads browser

> Status: **approved, not yet built**. Authoritative design for Phase 2. Consumes Phase 1's
> `.eml` archive + `_index.jsonl`; produces `threads.jsonl` (the unit Phase 3 comprehends).
> Created 2026-06-18.

## Purpose

Turn the flat `.eml` archive into linked, **chronological thread trees** so a stored mailbox
becomes a browsable, re-readable record — and so Phase 3 has a clean per-thread unit to
comprehend. Deterministic, offline, no AI. (Option C: build the index **and** a thin viewer.)

## Principles

- **Deterministic & lossless.** A pure transform — re-runnable to an identical result, never
  touches the `.eml` files, no network. Splitting is safe; wrong-merging corrupts Phase 3, so
  Phase 2 never guesses.
- **One source of truth.** The `.eml` archive stays authoritative. Phase 2 stores only a small
  **index**; thread *content* is stitched on demand, never duplicated to disk.
- **Separation of concerns.** Phase 2 = deterministic threading. Cross-thread grouping into
  real-world *issues* (by reference, similarity, AI adjudication) is Phase 4's job, not Phase 2's.

## Scope

In: header-based threading, chronological ordering, the `threads.jsonl` index, and a thin
"Threads" browser (list + stitched reader) in the existing web app.
Out (later phases): subject/semantic linking, comprehension/AI (P3), issue clustering (P4),
attachment extraction, link-share download.

## Architecture

Two parts, mirroring the pipeline's deterministic/AI split:

1. **Builder — `clover/threads.py`** (pure, testable): reads the archive + `_index.jsonl`,
   links messages into threads, writes `<runtime>/threads.jsonl`. No network; idempotent.
2. **Browser** — new `/threads` routes + templates in the existing FastAPI app (new "Threads"
   nav tab). Reads `threads.jsonl` for the list; reads member `.eml` on demand for the reader.

## Linking & dedup (header-only)

- Parse `Message-ID`, `In-Reply-To`, `References` (header-only parse — fast). Normalize ids
  (strip `<>`, lowercase).
- **Union-find:** union each message with every id in its `In-Reply-To` + `References`.
  Connected components = threads. Referenced-but-absent ids (deleted originals) still connect
  their replies, but are not counted as members.
- **Cross-folder duplicates:** the same `Message-ID` in two folders is **one** logical message
  recorded once, with a list of `{folder, path}` locations — never double-counted.
- Each message also records normalized **subject** + **participants** (from/to) — carried as
  data for Phase 4, **not** used for linking.
- Messages with no `Message-ID`: keyed by their Phase-1 synthetic id (`uid_<key>`); they form
  singleton threads (they can't be linked).

*Validation against a real ~4.8k-email archive:* ~73% carry threading headers, 4 cross-folder
duplicates, 100% of dates parseable; union-find yields ~1.8k threads incl. multi-message
threads up to ~160 messages — confirming header-only threading is sound.

## Output — `<runtime>/threads.jsonl` (one thread per line)

```json
{
  "thread_id": "<stable key derived from member ids>",
  "root_id": "<earliest message-id, for display>",
  "subject": "<normalized subject>",
  "n": 7,
  "start": "2026-01-05T01:22:12Z",
  "end": "2026-03-10T09:00:00Z",
  "participants": ["alice@example.com", "bob@example.com"],
  "members": [
    {
      "message_id": "...", "date": "2026-01-05T01:22:12Z",
      "from": "...", "subject": "...",
      "locations": [{"folder": "Sent Items", "path": "Sent Items/xxx.eml"}]
    }
  ]
}
```

- `thread_id`: deterministic, rebuild-stable — derived from the sorted member ids (not from a
  date, so it doesn't drift). `root_id`: earliest member, for display.
- Dates normalized to **UTC**. `start`/`end` = earliest/latest member dates.

## Chronological sort & edge cases

- Members sorted by `Date` → UTC. Unparseable date → fall back to Phase-1 `INTERNALDATE` if
  recorded; else sort last and flag (none in the current corpus, but handled defensively).
- Thread-list default order: **most-recent-activity first** (`end` desc); toggles for size and
  oldest. Singletons appear as 1-message threads.
- Empty archive / missing `_index.jsonl` → empty index, clean empty-state in the browser.

## The stitched reader

- **List page:** rows of `subject · participants · message count · date range`; sortable;
  simple text filter. Click → thread page.
- **Thread page:** all members in one **chronological scroll**. Each block: from / to / date,
  then the body.
- **Body rendering:** prefer the HTML part inside a **locked-down sandboxed `<iframe>`**
  (`sandbox` with no scripts; CSP blocks remote content so tracking pixels / remote calls don't
  fire). Plain-text part rendered in a mono block when there's no HTML. Attachments listed by
  name + size (already embedded in the `.eml`; extraction is a later phase).
- **Build trigger:** rebuilds automatically once at the end of an archive run that saved new mail
  (skipped when nothing changed); plus a manual "Rebuild thread index" button, and auto-build if
  `threads.jsonl` is missing. Full rebuild (header-parsing the whole archive is seconds). No
  incremental complexity in v1.
- **Optional, on demand:** an "Export thread" action (save one thread as a single file) — a user
  action, not a maintained store.

## Non-goals

No stored stitched-content folder (derived live). No subject/semantic merging. No AI. No
mutation of the `.eml` archive. No attachment extraction or link-share download.

## Acceptance criteria

- Every `.eml` belongs to exactly one thread; members are chronological.
- `threads.jsonl` is rebuildable from the archive alone and identical across re-runs (pure
  transform).
- Cross-folder duplicates appear once, with both locations.
- The browser opens any thread and renders every message safely (no remote content fires).
- Builder logic is unit-tested (linking, dedup, sort, stable ids, edge cases) on synthetic
  fixtures — no live mailbox needed.
```
