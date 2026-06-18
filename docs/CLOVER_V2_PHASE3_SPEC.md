# 🍀 Clover v2 — Phase 3 Spec: Comprehension (first AI phase)

> Status: **approved, building**. Consumes Phase 2 `threads.jsonl` + the `.eml` archive;
> produces `comprehension.jsonl`. The only phase that needs AI. Created 2026-06-18.

## Purpose

Read each thread and produce a quality-gated cascade of understanding + a grounded
classification, so the archive becomes searchable, summarised intelligence — and so Phase 4
(issues) and Phase 5 (forecasting) have a structured per-thread unit to build on.

## Principles

- **Quality & accuracy first.** Nothing unverified ships. Every output is checked against the
  source; structured facts are verified deterministically; genuine doubt is surfaced, not guessed.
- **Whole-thread by default.** Most threads are read in full (full fidelity). Only threads that
  exceed the model's reliable context use **iterative-refine** (read in chronological order,
  carrying a running comprehension) — never lossy map-reduce — and are flagged lower-confidence.
- **Pluggable backend.** A `Comprehender` interface (like `MailSource`): Claude CLI first; Ollama
  / hosted / API drop in later via config.
- **Pure & stateless pipeline.** `comprehend_thread(thread, backend, profile) -> record`. Runs
  identically client-side or server-side (server deployment is just a different host).
- **Profile-driven classification.** The taxonomy is config, not hard-coded (different roles
  classify differently). One default profile now; profile management later.
- **Idempotent.** `comprehension.jsonl` keyed by `thread_id`; re-runs skip done threads.
- **Policy gate + budget.** One `allowed()/budget` seam where subscription tiers plug in later.

## Architecture

- **`clover/comprehenders.py`** — `Comprehender` ABC + registry; `StubComprehender` (deterministic,
  for tests) + `ClaudeCliComprehender` (shells out to `claude -p … --output-format json`, Sonnet).
- **`clover/profiles.py`** — classification profiles (the 2-level taxonomy as data).
- **`clover/comprehend.py`** — token estimation, `comprehension.jsonl` read/write, the pipeline
  (`comprehend_thread`) and the budgeted runner (`run_comprehension`).

## Pipeline (per thread)

1. **Assemble** the thread text (whole-thread; iterative-refine if it exceeds context).
2. **(i) Comprehension** — full chronological understanding of the whole thread.
3. **(ii) Abstract** — accurate paragraph, distilled from (i).
4. **(iii) One-liner** — summary, from (i).
5. **(iv) Event tag** — ≤30 chars, from (ii).
6. **Facts** — `{project, parties, refs, dates, amounts}`, each grounded in the thread.
7. **Classification council** — `domain` (Project/Corporate) + `category`, profile-driven (below).
8. **Verify** — (i) vs raw thread; (ii)–(iv) vs (i); facts (refs/dates/amounts) must appear in the
   thread (deterministic); council consensus recorded. Low-confidence → flagged / operator ask.
9. **Record** — append to `comprehension.jsonl`.

## Classification — two-level, profile-driven 2-tier council

Default profile (construction; legacy-derived):

- **Level 1 — domain:** `Project` · `Corporate`
- **Level 2 (Project):** Commercial · Design & Technical · Operation · Quality · Safety
- **Level 2 (Corporate):** HR & Admin · Account & Finance · Design & Planning · Safety ·
  Commercial · Engineering & Operation

**Council (adapted from legacy `classification-council.md`, scaled for the prototype):**
- **Small council** — one structured pass returning each lens's `{claims?, category, confidence}`
  over the active profile's categories (Commercial is the high-stakes safety-net lens). One clear
  claim → filed. Conflict / none / multiple → escalate.
- **Full council** — only on dispute: a richer adjudication pass **plus a deterministic precedence
  referee** (ordered rules: instruction/variation → Commercial; signed contract → Commercial;
  claim/EOT/payment → Commercial; safety incident → Safety; else document-type). Still split →
  **ask the operator** (answer saved as a rule).
- **Output:** `{domain, category, confidence, council: small|full, consensus:
  unanimous|majority|split-resolved|asked, dissent}`. `consensus` is the quality weight on the
  classification (and on knowledge in later phases).
- The lenses, categories, and precedence rules are **generated from the active profile** — swap the
  profile, the council reconfigures. The legacy scheme is one preset.

## Correctness (how we ensure correct info)

Source-grounded review (best model generates, cheaper model can review) + **deterministic fact
verification** (an extracted ref/date/amount not present in the thread is rejected) + council
consensus weighting + **operator ask on genuine splits** + per-output confidence. Multilingual:
prompts handle mixed **English + Chinese**.

## Output — `<archive>/comprehension.jsonl` (one thread per line)

```json
{ "thread_id":"…", "root_id":"…", "subject":"…",
  "comprehension":"… full chronological understanding …",
  "abstract":"…", "summary":"…", "event":"≤30 chars",
  "facts":{ "project":"…", "parties":["…"], "refs":["…"], "dates":["…"], "amounts":["…"] },
  "classification":{ "domain":"Project|Corporate", "category":"…", "confidence":0.0,
                     "council":"small|full", "consensus":"…", "dissent":"…" },
  "method":"whole|refine", "model":"sonnet", "profile":"construction",
  "verified":{ "facts_ok":true, "grounded":true }, "ts":"…" }
```

## Run model

- **Autorun after an archive run**, within a **token-estimate budget** (estimated from thread
  size — not a thread count), most-recent first, **resumable** (skip done); hit budget → stop,
  mark pending. The budget lives behind the **policy gate** (config now; tier-driven later).
- **On-demand "Comprehend"** button on a thread page.
- Backend: **Claude CLI (Sonnet)**; **StubComprehender** for tests / offline build.

## UI (thin Phase-3 slice)

- Thread list: show the **one-liner** + **domain/category** badges once comprehended.
- Thread page: **abstract + summary + facts**, a **Comprehend** button, and a
  **confidence/consensus** indicator.

## Non-goals / deferred

Full multi-agent tribunal (scaled now); link-share download; Telegram digest; in-UI reconcile
panel; profile-management UI; Phases 4–5. Tracked separately, not lost.

## Acceptance

- Each comprehended thread has (i)–(iv) + facts + a council classification, all verifiable.
- Facts present in output appear in the source; council consensus recorded; splits asked.
- `comprehension.jsonl` rebuildable, idempotent; pipeline runs with the stub (no AI) in tests.
- Backend, profile, and budget are swappable via config.
