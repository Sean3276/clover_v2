# 🍀 Clover v2 — Development Roadmap

> The authoritative phase plan for Clover v2. Supersedes the phase structure in
> `history/CLOVER_V2_SPEC.md` (archived for reference). Last updated: 2026-06-18.

## How many phases?

**Five sequential pipeline phases** form the backbone — each consumes the previous
phase's output and adds one layer of structure:

```
P1 RAW            P2 ORGANIZED        P3 UNDERSTOOD       P4 ISSUES          P5 FORESIGHT
.eml dump   ─►   thread trees   ─►   comprehension  ─►   issue registry ─►  knowledge +
(per email)      (per thread)        (per thread)        (cross-thread)     forecasting
 automation       automation          AI (council)        AI + rules         AI + stats
```

Plus **two cross-cutting tracks** that are *not* phases (they layer across all of the
above and are built incrementally): **(A) UI / cockpit** and **(B) delivery & inbox
actions** (digest, soft-delete sweep, draft chase replies). See "Cross-cutting" below.

So: **5 phases + 2 cross-cutting tracks.** Five is the right count — splitting further
fragments the pipeline; merging any two mixes deterministic and AI work that should stay
separate (so each can be tested and re-run independently).

**Status legend:** ✅ done · 🟡 in progress · ⬜ not started · 💡 conceptual (design only)

---

## Phase 1 — Archiving to local
**`user input  →  local .eml archive`** · automation · **🟡 (IMAP prototype ✅, full suite ⬜)**

Pull every email from selected mailbox locations into a faithful local `.eml` archive.

**Steps**
1. **Archive email — IMAP first, then the full provider suite (one by one).** The system
   uses a **pluggable source** architecture (`clover/sources/`): a common `MailSource`
   interface (test · folders · select→validity · message_keys · fetch_raw) with one
   implementation per provider/protocol, so a new provider is added **without touching the
   archiver**. IMAP is the first implementation (✅). The suite to develop one by one:

   | Source | Protocol | Status |
   |--------|----------|--------|
   | IMAP (generic) | IMAP | ✅ available |
   | Gmail / Google Workspace | Gmail API, or IMAP + OAuth2 (XOAUTH2) | ⬜ planned |
   | Microsoft 365 / Outlook.com | Microsoft Graph API | ⬜ planned |
   | Exchange (on-prem) | EWS | ⬜ planned |
   | Yahoo Mail | IMAP (app password) | ⬜ planned |
   | iCloud Mail | IMAP (app-specific password) | ⬜ planned |
   | Coremail (NetEase 163/126, QQ, etc.) | IMAP / Coremail API | ⬜ planned |
   | Zoho Mail | IMAP / API | ⬜ planned |
   | Fastmail | JMAP / IMAP | ⬜ planned |
   | Proton Mail | IMAP via Proton Bridge | ⬜ planned |
   | Generic POP3 | POP3 | ⬜ planned |
   | Local import | .eml / .mbox / Maildir / .pst | ⬜ planned |

   - *Note on SMTP:* SMTP is **send-only**, not an archiving protocol — outbound (drafting
     chase replies) lives in cross-cutting Track B. "Full suite" here = full **read/ingest**
     coverage, not send.
   - **Inputs:** source (provider creds + folders: INBOX / Sent / Trash / named folders),
     destination (local archive path, user-defined, changeable). ✅
   - **Selection filters (✅).** Beyond whole-folder, the user can narrow what's archived by
     **date range** (received-from/to, inclusive; quick presets) and by **size** — either a
     `≥ N MB` threshold or the **largest N per folder**. Server-side IMAP `UID SEARCH`
     (`SINCE`/`BEFORE`/`LARGER`) is the primary path with a client-side metadata fallback
     (`message_meta` via `FETCH INTERNALDATE RFC822.SIZE`); a prep-phase progress bar shows the
     scan. Filtering only changes the *selected set* — resume/dedup `(folder, validity, key)` is
     unaffected. Auto-resume on disconnect is now always-on (no toggle).
2. **Keep the Message-ID key.** Every saved email is keyed by **Message-ID**, recorded with
   its folder + UID + UIDVALIDITY in `_index.jsonl`, across INBOX / Sent / Trash / etc. so
   the same email in multiple folders is linkable. ✅
3. **Verify all `.eml` kept correctly.** Read-only `BODY.PEEK[]` (never marks seen), sha256
   per file, resumable. *Completion adds* a reconcile/verify step (server count == index
   rows == files on disk) surfaced in the UI.

**Output:** `<archive>/<folder>/<message-id>.eml` + `_index.jsonl`
`{id, folder, key (UID), validity (UIDVALIDITY), from, subject, date, path, size, sha256}`.

> **Built ✅** incl. date/size **selection filters** (All time · Yesterday · Last 3 days · Last 7/30/90 ·
> This year · Custom; size: any / min MB / largest-N) and a **reconcile/integrity** check. **Add-on:**
> share-link **harvest + download** (SharePoint/OneDrive, Drive, Dropbox, WeTransfer, Box) with
> URL-dedup + a size-confirm gate. A **one-click installer** (`run_clover.bat`) sets everything up.
> Design: [`CLOVER_V2_PHASE1_SPEC.md`](CLOVER_V2_PHASE1_SPEC.md) · behaviour: [`HOW_CLOVER_WORKS.md`](HOW_CLOVER_WORKS.md).

**Acceptance:** every selected message on disk as valid `.eml`; index reconciles; re-run
is idempotent; no message marked seen; no credential logged.

---

## Phase 2 — Per-thread organization
**`disorganized .eml  →  organized thread archive`** · automation · ✅ **BUILT** (see [CLOVER_V2_PHASE2_SPEC.md](CLOVER_V2_PHASE2_SPEC.md) for design, [HOW_CLOVER_WORKS.md](HOW_CLOVER_WORKS.md) for behaviour)

Turn the flat `.eml` pile into linked, chronological **thread trees**. No AI. Decided: header-only
linking (deterministic; subject/semantic grouping deferred to P4), `threads.jsonl` index, and a thin
"Threads" browser with an on-demand **stitched reader** (option C). Full design in the spec above.

**Steps**
1. **Correlate by Message-ID.** Parse `Message-ID`, `References`, `In-Reply-To` from every
   `.eml`; union-find them into threads (a Sent reply + its INBOX copy + an Archive copy all
   join the same thread). Cross-folder, deduplicated.
2. **Sort chronologically.** Order members within each thread by a normalized UTC timestamp
   (parse `Date`; handle mixed timezones / naive dates — do **not** sort raw strings).
3. **Materialize the per-key full thread.** For each thread, build a working record (in a
   temp/work area, e.g. `<runtime>/threads/` or a `threads.jsonl`): the ordered list of
   member message-ids + their `.eml` paths + a canonical `thread_id` (e.g. the root
   Message-ID). This is the unit Phase 3 consumes.

**Output:** `threads.jsonl` — `{thread_id, root_id, members:[{message_id, folder, date, path}], n}`
(+ optionally per-thread folders for browsing).

**Acceptance:** every `.eml` belongs to exactly one thread; members chronological; threads
re-buildable from the archive alone (Phase 2 is a pure, repeatable transform — re-runnable
any time without touching the `.eml` files).

---

## Phase 3 — Comprehension
**`organized thread archive  →  council-cleared comprehension`** · AI (local agent) · ✅ **BUILT** (see [CLOVER_V2_PHASE3_SPEC.md](CLOVER_V2_PHASE3_SPEC.md))

Read each thread tree and produce a quality-gated cascade of understanding. This is the
**only** phase that needs the AI. Each output passes a **2-tier council** (small accuracy
review → escalate to full council on doubt) before it is accepted — no hallucinated or
lossy distillation gets through.

**Per thread, input = the correlated `.eml` tree (chronological); outputs:**

| # | Output | Distilled from | Council clears for |
|---|--------|----------------|--------------------|
| (i) | **Full detailed comprehension** of the whole thread, in chronological order | the raw `.eml` tree | **faithfulness + completeness** — nothing fabricated, no material fact omitted vs the raw thread |
| (ii) | **Abstract** | (i) | accuracy of the abstract **against (i)** |
| (iii) | **One-liner summary** | (i) | accuracy of the summary **against (i)** |
| (iv) | **Event wording, < 30 chars** (the folder/timeline event tag) | (ii) | accuracy of the wording **against (i)** |

**2-tier council (the accuracy gate).** A **small council** (a focused reviewer / few
lenses) checks each output; on disagreement or low confidence it **escalates to the full
council**, which clears or sends back for revision. (i) is checked against the raw thread;
(ii)–(iv) are checked against (i) as the single source of truth. Records consensus strength.

**Output:** per-thread `comprehension.json` `{thread_id, detail (i), abstract (ii),
summary (iii), event (iv), council:{per-output consensus}, facts:{refs, parties, project,
dates, amounts}}`. The `facts` block is the structured seed Phase 4 needs.

**Acceptance:** every thread has all four outputs, each council-cleared; (iv) ≤ 30 chars;
no output contradicts (i); structured `facts` extracted.

---

## Phase 4 — Per-issue tracking  💡 *conceptual — proposed approach below*
**`comprehension  →  cross-thread issue registry`** · AI + deterministic rules

An **issue** is a real-world matter (an EOT claim, a façade RFI chain, a payment dispute, an
NCR→rectification→closeout) that can span **multiple threads / Message-IDs** over time.
Phase 2 links emails *within* a thread; Phase 4 links *threads into issues*.

**Proposed mechanism (hybrid — high-precision rules first, AI to judge the rest):**
1. **Reference-based clustering (deterministic, high precision).** Phase 3's `facts` extract
   the governing reference of each thread (RFI-12, EOT-05, SOI-50, VO-09, NCR-07, a drawing
   no., a claim no.). Threads citing the **same reference** are the same issue. This alone
   resolves most construction correspondence, which is reference-driven.
2. **Similarity linking (AI, for the rest).** For threads with no shared explicit reference:
   match on **project + parties + semantic similarity** of abstracts (embeddings), plus
   explicit cross-references ("further to our email of …"). Produces *candidate* links.
3. **Issue adjudication (AI council).** An "issue adjudicator" reviews each candidate cluster
   + unclustered threads: confirms membership, **names the issue**, classifies its type, and
   writes an issue summary. Council-cleared like Phase 3 (so a wrong merge/split is caught).
4. **Lifecycle / state.** Each issue carries a **status** (open → in-progress → resolved /
   closed / escalated) and a **chronological event timeline** assembled from the (iv) event
   tags of all its threads. As new mail arrives, Phase 3 → match to an existing issue or
   spawn a new one; the issue's status + exposure update.

**Output (proposed):** `issues.jsonl` — `{issue_id, title, type, project, status,
references:[…], parties:[…], thread_ids:[…], timeline:[{date, event, thread_id}],
cost_time_exposure, opened, last_update}`.

**Open questions for you:** (a) is "one governing reference = one issue" the right primary
rule, or do some references share an issue? (b) how far back should an issue auto-absorb new
threads — by reference only, or also by similarity? (c) who confirms an AI-proposed
issue merge — auto-accept on unanimous council, ask you on a split?

---

## Phase 5 — Knowledge accumulation & forecasting  💡 *conceptual — proposed approach below*
**`issue history  →  knowledge pool → forecasts of cost/time risk`** · AI + statistics

From the history of resolved/ongoing issues (Phase 4), build a knowledge pool that **warns
of problems before they bite**, focused on **cost & time** (and quality/safety) impact.

**Proposed mechanism (two layers — mirrors what worked in Clover v1's learning design):**
1. **Layer 1 — deterministic memory (facts & base rates).** Aggregate issue outcomes into
   reproducible statistics: median days-to-close by issue type; % of EOT/claims that get
   under-certified or rejected; which parties respond slowly; which link-sources die; which
   issue types historically blow cost/time. These are *priors*, computed, not "learned."
2. **Layer 2 — AI-reasoned patterns (the actual intelligence).** Distill durable,
   project-agnostic **patterns** from resolved issues: e.g. "EOT claims lacking
   contemporaneous records get rejected", "façade RFIs open > 30 days cascade into program
   delay", "this subcontractor under-certifies — verify before paying". A pattern graduates
   only with multi-issue support (so one-offs don't pollute it).
3. **Forecasting.** A new issue/thread is matched against the pool → surface the most similar
   past issues, their outcomes, their cost/time impact, and the **early-warning signals** that
   preceded escalation → emit a forecast (a 🔮 heads-up): "looks like a payment dispute; the
   last 3 of this pattern ran ~60 days and ended under-certified — check the cert."
4. **Live watchlist + closing the loop.** Maintain a risk register of current open issues
   ranked by predicted cost/time exposure. Log every forecast with its later **outcome**
   (came true / not) → refine pattern confidence over time (a forecast ledger).

**Output (proposed):** `knowledge.json` (base-rate stats + graduated patterns + watchlist) +
`signals.jsonl` (forecast → outcome ledger). Optionally a vector store of past issues for
"find similar situations" retrieval.

**Open questions for you:** (a) which impacts matter most to forecast first — cost, time, or
both? (b) is a quantitative base-rate ("median 60 days") useful to you, or do you want
narrative warnings only? (c) acceptable to use embeddings/a local vector store for similarity?

---

## Cross-cutting tracks (layer across phases — not sequential phases)

- **Track A — UI / Cockpit.** A browser cockpit that grows with each phase: P1 archive
  browser → P2 thread view → P3 comprehension reader → P4 issue board → P5 forecast
  dashboard. Built incrementally; each phase adds its panel.
- **Track B — Delivery & inbox actions.** The "operations" layer carried from v1: the 🍀
  digest/brief (Telegram), soft-delete sweep of no-action mail (move to Trash, reversible,
  after verified archived), and replies/forwards. **Reply / Reply-all / Forward with direct
  SMTP send is BUILT** (see [CLOVER_V2_SENDING_SPEC.md](CLOVER_V2_SENDING_SPEC.md)) but
  **shipped disabled** (operator enables it post-phases; confirm-every-send, save-to-Sent,
  fail-closed). Still pending in this track: Telegram digest, soft-delete sweep.

**Recommendation (you asked me to decide):**
- **Track A (UI) — fold a *thin* slice into each phase; defer the polished cockpit.** You
  enforce "review before try", which *requires* seeing each phase's output to verify it — so a
  minimal viewer must ship **with** each phase (Phase 1 already has Setup + Archive). A unified,
  polished cockpit is a later consolidation, not per-phase work.
- **Track B (delivery & inbox actions) — keep separate and deferred until after Phase 3.** It
  (a) is the riskiest part (acting on the live mailbox — sweep/delete, draft replies),
  (b) *depends* on comprehension (P3) to know what's actionable, and (c) isn't needed to build
  the intelligence pipeline. Isolating it late keeps risk contained and the pipeline clean.

Net: **thin UI with every phase**, **actions/digest as a late, separate, carefully-gated track.**

---

## Sequencing & dependencies

```
P1 ─► P2 ─► P3 ─► P4 ─► P5
            └─ Track A (UI) grows alongside P1..P5
            └─ Track B (digest/sweep/drafts) attaches after P3
```

Each phase is independently **re-runnable** over the previous phase's on-disk output, so you
can re-organize (P2), re-comprehend (P3), or re-cluster issues (P4) without re-fetching mail.

## Current status snapshot
- **P1:** IMAP **built, reviewed, live-run & byte-certified** on a real mailbox (Trash + Sent
  archived, reconciled). **Date/size filters** (incl. Yesterday / Last 3 days) + **reconcile panel**
  shipped. **Add-on shipped:** share-link **harvest + download** (URL-dedup + size-confirm gate) and a
  **one-click installer**. Pending: the full provider suite; a full-corpus link fetch.
- **P2:** ✅ **built** — header-only threading + stitched Threads reader, attachment view/download,
  cross-folder dedup. (See [HOW_CLOVER_WORKS.md](HOW_CLOVER_WORKS.md).)
- **P3:** ✅ **built** — per-thread comprehension (4-tier + council + fact verification); pending: QAQC
  gate, project-name classification, contact consolidation.
- **P4–P5:** not started (conceptual; approaches proposed above).

## Open decisions (summary)
1. ✅ **Decided.** Phase 1: IMAP first; full provider suite (table above) developed one by one;
   pluggable `MailSource` architecture. Next provider chosen when you're ready.
2. ⏸️ **Parked (Phase 4).** Issue-linking rules — see "Open questions" in Phase 4. Sort later.
3. ⏸️ **Parked (Phase 5).** Forecast focus — see "Open questions" in Phase 5. Sort later.
4. ✅ **Decided.** Cross-cutting: thin UI folded into each phase; delivery & inbox actions a
   separate, deferred track that attaches after Phase 3.
