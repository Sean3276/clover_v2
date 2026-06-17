# 🍀 Clover v2 — Full Development Specification (Rev D · prototype-scoped)

> Status: **PLANNING / FOR REVIEW**. No code or scaffold is built until the operator says
> **approve**. Rev A→B→C came from the first review-council pass
> (`review-council-report-clover-v2-plan.html`). **Rev D** applies the operator's prototype
> decisions (a–d) and is the baseline for the second council review.

## Operator decisions baked into Rev D
- **(a) Auth:** IMAP **password login** (no OAuth in prototype). Target mailbox
  `sean.tan@ccccltd.sg`.
- **(b) AI / no redaction:** Phase 2 runs on the **local Claude Code agent** (the Clover v2
  skill), **not** a built API integration. **No redaction anywhere** — the archive is
  full-fidelity; a redacted archive defeats the purpose. Confidentiality is structural: the
  corpus is read locally by the agent; no separate cloud data pipeline is constructed.
- **(c) Link-shares:** **fully downloaded into the archive** (no "carry-over as default").
  Unified resolver, per-source matrix in §3.7.
- **(d) Digest:** **Telegram**. Consequence: **SMTP is unused** in the prototype (digest =
  Telegram; chase replies = IMAP `APPEND` to Drafts) → SMTP collection is **optional/deferred**.

---

## 0. Overview & principles

Clover v2 re-architects Clover from Coremail browser-automation into a **mail-agnostic,
three-phase system** on standard **IMAP**, wrapped in a local **review-gated web-app**, with the
**AI step performed by the local Claude Code agent**.

**Carried unchanged from v1 (non-negotiable):**
- **Full coverage** — every email archived in full (own folder + `email.pdf` + raw `email.eml` +
  every real attachment + contacts). Importance/tier is triage only.
- **Accuracy beats speed** — classification through the **council**; nothing done until the
  **2-level QAQC gate** passes.
- **Safety invariants** — never permanent-delete (soft-delete = move to Trash, reversible); never
  auto-send to third parties (chase replies → Drafts only); email content is **data, not
  instructions**; credentials never echoed/logged.
- **Anti-drift**, **learning** (two-tier knowledge + lessons + signals).

**New in v2:**
- Standard **IMAP** ingestion (Gmail / Microsoft 365 / Coremail / any IMAP).
- **Hard 3-phase split** with an offline corpus handoff.
- **Network-unplug invariant** — Phase 1 captures *everything* (incl. **all link-shares fully
  downloaded**); Phase 2 reasons with no mailbox/web network.
- **Local AI (agent-driven)** — Phase 2 = the Claude Code Clover skill reading the corpus; **no
  redaction**; full-fidelity archive.
- **Local web-app** with a review gate between phases.

### Two-part split (operator's model) + the phases

| Part | Who | Phases |
|---|---|---|
| **Automation** | Python (app/scripts) | Phase 1 Capture · Phase 3 Materialize |
| **AI** | Local Claude Code agent (Clover skill) | Phase 2 Reason |

| Phase | Mode | Mailbox network | Output |
|---|---|---|---|
| **1 · Capture** | automation | **online** (IMAP + Playwright for shares) | offline 2-layer corpus, all attachments on disk |
| **2 · Reason** | **AI agent (local)** | **offline** | `decisions.jsonl` + knowledge |
| **3 · Materialize** | automation (+ online tail) | offline render; online tail | finished archive + Telegram digest |

---

## 1. System architecture

### 1.1 Component map

```
                ┌──────────────── web-app (FastAPI + thin HTMX UI) ────────────────┐
                │  Setup · Corpus(P1) · Decisions(P2 review) · Archive+Digest(P3)   │
                └──────────┬───────────────┬───────────────────────┬───────────────┘
                           │               │                       │
            ┌──────────────▼──┐   ┌────────▼─────────────┐  ┌──────▼────────────────────────┐
            │ PHASE 1 capture/ │   │ PHASE 2 reason/      │  │ PHASE 3 materialize/          │
            │  imap_client     │   │  (LOCAL CLAUDE AGENT)│  │  render (weasyprint/fpdf2)    │
            │  thread_assembler│   │  abstract            │  │  index/db (sqlite+fts5)       │
            │  linkshare_      │   │  council classify    │  │  qaqc gate (L1+L2)            │
            │   resolver       │──▶│  triage · distil     │─▶│  online tail: telegram digest │
            │   (Playwright)   │   │  knowledge build     │  │   · imap sweep · imap APPEND  │
            │  attachment+text │   │  golden-set eval     │  │   drafts                      │
            └────────┬─────────┘   └─────────┬────────────┘  └──────────────┬───────────────┘
                     ▼                        ▼                              ▼
           .clover_v2/capture/       .clover_v2/decisions/         .clover_v2/archive/ + index/db

   shared engine (ported from v1): engine/  SOP · GLOSSARY · QAQC · TEMPLATES · PREFERENCES · LEARNING · council
```

Phase 2 is **not a code module that calls an API** — it is the Claude Code agent (the Clover v2
skill) reading `capture/` and writing `decisions/`. The web-app *facilitates* (shows the corpus,
accepts/show decisions) but does not implement the reasoning.

### 1.2 Folder layout

```
auto_clover/
├─ .clover_v2_github/                  # ENGINE TEMPLATE (shippable, secret-free, git-tracked)
│   ├─ app/  (main.py, routes/, templates/, static/)
│   ├─ phases/  phase1_capture/  phase3_materialize/      # phase2 = agent skill, not code
│   ├─ skill/clover_v2/  (SKILL.md router + references/imap-smtp.md, link-shares.md, classification-council.md)
│   ├─ engine/  (CLOVER_SOP.md, GLOSSARY, QAQC, TEMPLATES, PREFERENCES.default, LEARNING)
│   ├─ schemas/  (packet, corpus, decisions, config)
│   ├─ tools/  (contacts.py, build_binders.py, build_knowledge.py, distil_knowledge.py,
│   │           clover_assist.py, rebuild_index_db.py, email_to_pdf.py, send_digest.py,
│   │           linkshare_resolver.py)
│   ├─ tests/  (golden-set fixtures, unit tests)
│   ├─ clover_config.example.json   requirements.txt   bootstrap_clover_v2.py
│
└─ .clover_v2/                         # RUNTIME (git-ignored; secrets in OS keyring)
    ├─ clover_config.json                # settings (no raw secrets)
    ├─ CLOVER_PREFERENCES.<user>.md
    ├─ capture/  corpus.jsonl  capture_manifest.json  <msgid-hash>/{email.eml, packet.json, attachments/}
    ├─ decisions/  decisions.jsonl  clover_knowledge.json  clover_lessons.jsonl
    │              clover_signals.jsonl  clover_council_log.jsonl  accuracy_report.json
    ├─ archive/  <Project>/<Tier>/<folder>/...  <Project>/Binders/  <Project>/contacts.txt
    ├─ clover_index.jsonl  clover_archive.db  clover_qaqc_log.jsonl
    ├─ carried_over.jsonl  unrecovered_attachments.json  round_recovery.json
    ├─ browser_profile/                  # Playwright persistent profile (operator-authenticated)
    ├─ _temp_download/   logs/
```

### 1.3 Tech stack
- **Python 3.11+**; **FastAPI + Uvicorn**; HTMX/Jinja (no JS build).
- **Mail:** `imap-tools` (IMAP **password login**); IMAP `APPEND` for Drafts. *(SMTP deferred.)*
- **Link-shares:** `playwright` (persistent authenticated profile) + `requests`/`httpx` (direct).
- **Render/text:** `weasyprint`, `fpdf2`+NotoCJK, `pypdf`, `python-docx`, `openpyxl`,
  `beautifulsoup4`.
- **Store:** SQLite+FTS5; JSONL.
- **Secrets:** OS `keyring` (IMAP password, Telegram bot token).
- **AI:** **local Claude Code agent** (the Clover v2 skill). **No `anthropic` API dependency.**
- **Digest:** Telegram via stdlib (`send_digest.py`).

---

## 2. Data model & contracts (the spine)

### 2.1 Identity
- **Key = Message-ID.** `key = safe(sha1(message_id))`. Never IMAP UID (folder-scoped, resets on
  UIDVALIDITY change).
- Unit of work = a **thread** (chain across folders); representative = latest message; all member
  ids recorded.

### 2.2 `capture/<key>/packet.json` (Phase 1 → Phase 2) — full-fidelity, network-free
```jsonc
{
  "id": "<message-id>", "key": "<sha1 safe-name>",
  "received": "ISO8601", "from": "...", "to": [...], "cc": [...], "subject": "...",
  "thread": [ {"seq":1,"author":"...","date":"...","folder":"Sent","text":"clean plain text"} ],
  "thread_members": ["<msg-id>", "..."],
  "attachments": [
    {"filename":"...","type":"mime","size":N,"sha256":"...",
     "kind":"direct|link","provider":"sharepoint|dropbox|gdrive|...|null",
     "status":"on_disk|dead", "path":"attachments/...","text_path":"attachments/....txt",
     "no_text_layer":false, "source_url":"<stored on disk, never echoed>"}
  ],
  "capture": {"complete": true, "links_resolved": N, "links_dead": N}
}
```
`email.eml` = lossless RFC822, written **read-only** (legal-grade immutability). **No redaction** —
content is verbatim.

### 2.3 `capture/corpus.jsonl`
```jsonc
{"id":"...","key":"...","received":"...","from":"...","subject":"...",
 "packet":"<key>/packet.json","n_att":2,"att_complete":true,"capture_complete":true}
```

### 2.4 `decisions/decisions.jsonl` (Phase 2 agent → Phase 3)
```jsonc
{"key":"...","id":"<message-id>",
 "project":"<Project>","tier":"Commercial|Technical|Operation|Safety|QAQC",
 "tldr":"<reference-led folder TLDR>", "clover_note":"WHO did WHAT — WHAT HAPPENED[; → NEXT:]",
 "triage":"red|amber|green|black", "needs_user": true,
 "council":{"path":"small|full","consensus":"unanimous|majority|split-resolved|asked",
            "dissent":"<minority|null>","confidence":0.0},
 "knowledge_facts":{...}}
```
**Consensus gate:** `unanimous|majority|split-resolved` → auto-file; `asked` / low-confidence →
flagged for operator review in the Decisions UI before Phase 3. *(No egress field — no redaction.)*

### 2.5 Index / DB (Phase 3)
`clover_index.jsonl` (one row/email) + `clover_archive.db` (`emails` + `emails_fts` FTS5) — as v1.

### 2.6 Knowledge stores (v1, written only on clean close)
`clover_knowledge.json` (Tier-1 specifics + Tier-2 patterns + watchlist), `clover_lessons.jsonl`,
`clover_signals.jsonl`, `clover_council_log.jsonl`, `clover_qaqc_log.jsonl`.

### 2.7 Config & secrets
`clover_config.json` (non-secret): `{auth:{imap:{host,port,security,user}},
folders:[INBOX,Sent,Archive], capture:{batch,order,date_range,resolve_link_shares:true,
extract_text:true}, digest:{channel:"telegram", chat_id}, knowledge:{...}}`.
**Secrets in OS keyring:** IMAP password, Telegram bot token. *(SMTP block optional, off by
default.)* Nothing secret in JSON/logs/UI.

---

## 3. Phase 1 — Capture (online automation)

**Goal:** connection-tested IMAP login + a complete, network-free corpus with **all attachments
(direct + link-shared) fully on disk**.

### 3.1 Components
`imap_client` · `thread_assembler` · `attachment_handler` · **`linkshare_resolver`** ·
`text_extractor` · `corpus_writer` · `manifest`.

### 3.2 Inputs
IMAP host/port/security + **user + password** (keyring); folders (default INBOX+Sent+Archive);
capture settings (batch 3, oldest-first, optional date/max, resolve_link_shares on,
extract_text on); one-time **Playwright profile login** to the providers the operator uses
(SharePoint/Drive/etc.). *(SMTP optional, skipped.)*

### 3.3 Outputs
Keyring IMAP password + `clover_config.json` + IMAP test result; `capture/` corpus (read-only
`email.eml`, `attachments/` + `.txt`, `packet.json`); `corpus.jsonl`; `capture_manifest.json`
(resumable); redacted `logs/`.

### 3.4 Workflow
1. **Onboard & test** — Setup UI collects IMAP creds; test = login + select folders; store
   password in keyring. Operator opens the Clover Playwright profile and logs into their share
   providers once.
2. **List** — per target folder, list envelopes (id, refs, date); build batch (carried-over
   first, then oldest-first).
3. **Assemble threads** — group by `Message-ID`/`References`/`In-Reply-To` **across folders**;
   unit = thread; representative = latest.
4. **Fetch raw** — download each member RFC822; save representative as read-only `email.eml`.
5. **Normalize** — parse thread → ordered turns; HTML→clean text; strip quote-bloat/signatures/
   tracking; drop inline `cid:` images.
6. **Attachments (ALL on disk)** — direct MIME saved; **link-shares fully downloaded** via the
   resolver (§3.7); validate; MOVE into `attachments/`; verify on disk.
7. **Extract text** — PDF/docx/xlsx → `<file>.txt`; flag `no_text_layer` (scanned/CAD; OCR roadmap).
8. **Write** — `packet.json` + `corpus.jsonl` row + manifest (resumable).

### 3.5 Acceptance criteria
IMAP test green; threads cross-folder; Message-ID keyed; every email → read-only `email.eml` +
`packet.json`; **every real attachment ON DISK** (inline excluded; scanned flagged); only a
verified-dead link is `dead` (+ chase in Phase 3); `corpus.jsonl` == folders; manifest reconciles;
re-run skips done; **network-unplug test passes**; no secret/token/URL in UI/logs.

### 3.6 Failure handling
IMAP fail → re-enter. Session expiry → resume from manifest. Share download fail → escalate
resolver tiers (§3.7); transient fail → retry next round (carried-over); verified-dead → chase.
Truncated download → re-fetch. Mega-thread → captured fully; Phase 2 segments.

### 3.7 Link-share resolution — the unified resolver (answers "best way per source")

**Principle:** a link share is **data to retrieve**, not a dead end. Always (0) extract the
**true URL with tokens from the raw `.eml`** (rendered/list HTML strips tokens), classify
provider + share-type, then resolve by the cheapest method that works. **Backbone = a Playwright
persistent profile the operator authenticates once**, so any gated provider downloads like a human.

**Tiered strategy**
- **Tier 1 — Direct HTTP** (`requests`/`httpx`, no browser): tokenized/public links. Fast.
- **Tier 2 — Authenticated browser** (Playwright, operator's `browser_profile/`): session-gated
  providers; `expect_download` captures the file cleanly (handles Chrome "keep"); folders are
  enumerated and each file fetched.
- **Tier 3 — Provider API (post-prototype)**: Microsoft Graph / Google Drive API via OAuth for
  unattended runs. Out of scope now.
- **Dead → chase**: only after applicable tiers genuinely fail (verified expired / 403 / 404). A
  folder or viewer is **never** "dead".

**Per-source matrix**

| Source | Share type | Best prototype method | Key detail |
|---|---|---|---|
| **Dropbox** | file / folder | Tier 1: force `?dl=1` (or `dl.dropboxusercontent.com`); folder → `?dl=1` zip | token in URL; public |
| **WeTransfer** | bundle | Tier 1: resolve transfer API → signed download URL | short-lived token |
| **Google Drive (link-shared)** | file | Tier 1: `uc?export=download&id=<id>` (+ confirm-token for large) | "anyone with link" |
| **Google Drive (private/Workspace)** | file / folder | **Tier 2** authed profile | needs Google login |
| **Box** | file | Tier 1: shared `?dl=1` / direct endpoint; else Tier 2 | token |
| **SharePoint / OneDrive** `/:b: :w: :x: :p:/` | single file | **Tier 2** authed: open + Download (or `&download=1`) | corporate session |
| **SharePoint / OneDrive** `/:f:/` | folder | **Tier 2** authed: folder "Download" zip, **or** enumerate `GetFolderByServerRelativeUrl(...)/Files` → navigate each `/_layouts/15/download.aspx?SourceUrl=` | cookie-auth, no token in URL |
| **Autodesk ACC** (autode.sk) | file / folder | **Tier 2** authed profile | construction-cloud login |
| **Coremail netdisk** (ccccltd.sg) | file | Tier 1: provider download endpoint w/ embedded token; else Tier 2 same-origin | likely relevant for `ccccltd.sg` |
| **Generic URL** | file | Tier 1 HEAD → if `Content-Disposition: attachment` stream; else Tier 2 | fallback |

**Validate every download** (non-zero size; `zipfile.testzip()` for zips — agent/browser
downloads can truncate large external zips). **MOVE** into the email folder (download dir ends
empty). Reliability notes carried from v1: top-level navigation per file beats scripted anchors
(Chrome multi-download block); same-origin `fetch→Blob` for large files non-blocking; >10 MB /
nested `message/rfc822` prefer the Playwright path.

---

## 4. Phase 2 — Reason (local Claude Code agent)

**Goal:** turn the corpus into auditable per-email decisions + updated knowledge, **fully offline
to the mailbox/web**, performed by the local agent — **no redaction, full content**.

### 4.1 Execution model (agent-driven, no API code)
The Clover v2 **skill** (Claude Code) reads `capture/corpus.jsonl`, opens each `packet.json` (clean
thread + attachment text), and writes `decisions.jsonl`. There is **no `anthropic` API
integration to build** — the agent IS the AI. The web-app/CLI hands the agent the corpus path and
displays the resulting decisions. Mega-threads are summarized map-reduce style by the agent to fit
context. **Confidentiality:** content is processed by the local agent session; the prototype builds
**no separate cloud data pipeline** and performs **no redaction** — the operator sees complete
information in their archive.
> *Future (post-prototype):* a fully on-device model could replace the agent for air-gapped runs;
> the contract (`packet.json` → `decisions.jsonl`) is unchanged.

### 4.2 Per-email pipeline
1. **Abstract** — comprehend the full thread → abstract (what/who/when/refs/ask/amounts).
2. **Classify (council)** — small 3-lens council; dispute → full council (specialists +
   document-type adjudicator + project-context lens + precedence referee + devil's advocate +
   chair). Project = content + recipient-GROUP correlation; tier = meaning + sender role +
   precedence rules. Records **consensus strength**.
3. **Triage** — role-lens NEEDS-YOU + colour (cost/time ≠ ⚫).
4. **Distil** — reference-led folder TLDR, Clover note, knowledge facts → `decisions.jsonl`.

### 4.3 Per-round
`build_knowledge.py` (Tier-1 + `project_activity`) → `distil_knowledge.py --due` (Tier-2 patterns)
→ AI **watchlist** (cost/time/quality/safety) → `clover_signals.jsonl`, `clover_council_log.jsonl`.
Written **only on clean close** (after Phase-3 QAQC).

### 4.4 Quality bar & evaluation
Golden-set fixtures (operator-labelled) scored each run → `accuracy_report.json`. **Targets ≥90%
project / ≥85% tier.** Below target → block auto-file, force review. Consensus + confidence gate
auto-file vs ask.

### 4.5 Acceptance / failure
Every corpus row → a decision; council ran with consensus recorded; disputes show full-council or
ASK; golden-set report produced; mailbox/web offline. Low-confidence/`asked` → Decisions UI.

---

## 5. Phase 3 — Materialize (offline automation + online tail)

### 5.1 Offline build (per decision)
Folder `archive/<Project>/<Tier>/<yyyymmdd> <COMPANY-code> <TLDR>` (safe-name, ≤60, unique) →
render `email.pdf` (full thread + Clover note, T1) + place read-only `email.eml` + MOVE
attachments → `contacts.py` company-grouped `contacts.txt` → stage index+DB.

### 5.2 QAQC gate (v1, verify-only)
- **L1 V1–V8** per email; **L2 2A R1–R8** (coverage, containment, no-dup, no-unclass, reclass,
  attachments, index=db=disk, count reconcile) → authorise merge; **2B R9–R14** (integrity,
  binders, sweep UI-confirmed, digest delivered, recovery empty, knowledge updated). Bounded
  remediation loop (PASS/DOWNLOADED/DEAD/DEFER); tick-templates → `clover_qaqc_log.jsonl`. COMPLETE
  only when 2A+2B pass.
- **Binders** (`build_binders.py`) per project-month-tier; **index/DB** merge.

### 5.3 Online tail
- **Digest → Telegram** via `send_digest.py` (bot token from keyring; T3-verified, never
  hand-composed; 🍀-framed; NEEDS-YOU / per-project / 🔮 Heads-up / 🧹 Round).
- **Inbox sweep** — needs-action stays; no-action **soft-deleted via IMAP** (move to Trash) **only
  after on-disk verify**; confirm by Message-ID in Trash. Never permanent-delete.
- **Chase drafts** — for verified-dead/outstanding links, saved to **Drafts via IMAP APPEND**
  (never sent).

### 5.4 Acceptance / failure
Every decision → own folder + full-thread pdf + read-only `.eml` + attachments + contacts; both
QAQC levels tick; binders present; Telegram digest delivered; sweep UI-confirmed; drafts saved
(not sent); recovery empty; knowledge written on clean close only. FAIL → bounded remediation;
unresolved → carried-over.

---

## 6. Web-app specification

| Page | Phase | Shows / does |
|---|---|---|
| **Setup** | 1 | IMAP form + **Test IMAP**; folders + capture settings; Telegram bot token + chat id + **Test Telegram**; link to open the Playwright profile for provider login |
| **Corpus** | 1 | captured-email table; `capture.complete`; attachment/link status; network-unplug readiness; **Run Capture** / **Hand to Agent (Phase 2)** |
| **Decisions** | 2 | per-email project/tier/TLDR/triage/consensus from `decisions.jsonl`; flags `asked`/low-confidence; accuracy report; **Approve & Materialize** |
| **Archive + Digest** | 3 | archive tree; QAQC tick status; Telegram digest preview; sweep plan + restore; **Run Materialize** / **Deliver & Sweep** |

**API:** `POST /setup/test-imap`, `/setup/test-telegram`, `/setup/save`; `POST /phase1/run`,
`GET /phase1/corpus`; `GET /phase2/decisions` (read agent output), `POST /phase2/review`;
`POST /phase3/run`, `POST /phase3/deliver-sweep`; `GET /qaqc`. State in runtime dir; long runs
stream progress; no secrets in responses.

---

## 7. Security & threat model
Two boundaries (the third — cloud egress pipeline — **does not exist** in the prototype):
1. **Mailbox** — IMAP password in OS keyring (never plaintext/echoed; operator authorises).
   Soft-delete only; never permanent-delete; never auto-send to third parties. Playwright profile
   holds the operator's provider sessions locally.
2. **Disk** — `.clover_v2` git-ignored + restricted perms; `email.eml` read-only; share tokens in
   packets stay local, never echoed; logs redacted. Cloud-sync caveats (verify after write;
   rename-aside vs delete; SQLite in local tmp then copy).
3. **AI** — Phase 2 runs in the **local Claude Code agent**; no built cloud data pipeline; **no
   redaction**; archive is full-fidelity. (If a future unattended deployment adds a programmatic
   cloud API, egress controls are revisited then — not in this prototype.)
Retention/legal-hold: immutable `.eml`, configurable retention, restore-from-Trash, audit via
QAQC/council logs.

---

## 8. Engine carryover from v1
Reused as-is: `GLOSSARY`, `QAQC_CHECKLIST`, `TEMPLATES`, `PREFERENCES.default`, `LEARNING`,
`references/classification-council.md`. **Adapted:** `CLOVER_SOP.md` (ingestion → IMAP; round
pipeline mapped to the 3 phases; digest → Telegram). Tools reused: `contacts.py`,
`build_binders.py`, `build_knowledge.py`, `distil_knowledge.py`, `clover_assist.py`,
`rebuild_index_db.py`, `email_to_pdf.py`, `send_digest.py`. New: `imap_client`,
`thread_assembler`, `linkshare_resolver.py`, `text_extractor`. Dropped: `coremail-api.md` →
replaced by `references/imap-smtp.md` + `references/link-shares.md`. **No egress/redaction module.**

---

## 9. Dependencies
`python>=3.11`, `fastapi`, `uvicorn`, `jinja2`, `imap-tools`, `keyring`, `playwright`,
`requests`/`httpx`, `beautifulsoup4`, `pypdf`, `python-docx`, `openpyxl`, `weasyprint`, `fpdf2`,
`fonttools`, `pytest`. **No `anthropic`** (agent is the AI). OCR libs deferred. SMTP via stdlib if
ever enabled.

---

## 10. Observability & testing
Per-phase redacted logs; capture/round manifest reconcile; durable QAQC tick-logs; golden-set
`accuracy_report.json` surfaced in UI; council log; unit tests (threader, safe-name, inline-vs-real
attachment filter, link-share provider classifier, packet/decisions schema, QAQC checks).

---

## 11. Development plan / milestones

| M | Milestone | Deliverables | Acceptance |
|---|---|---|---|
| **M0** | Scaffold + config | folder layout, `bootstrap_clover_v2.py`, config + keyring, schemas, engine port | bootstrap idempotent; config round-trips; schemas validate |
| **M1** | **Phase 1 capture** | imap_client (password), cross-folder threader, **linkshare_resolver (Playwright + direct)**, attachments, text extract, corpus writer, Setup+Corpus UI | §3.5; **all link-shares downloaded** on a real batch; network-unplug passes |
| **M2** | **Phase 2 agent** | Clover v2 skill (router + engine) reading corpus → `decisions.jsonl`; golden-set eval; Decisions UI | §4.5; ≥90%/85% on golden set |
| **M3** | **Phase 3 materialize** | render/index/contacts/binders, QAQC L1+L2, **Telegram digest**, IMAP sweep + APPEND drafts, Archive UI | §5.4; full round COMPLETE on a real batch |
| **M4** | Knowledge + learning | build/distil knowledge, signals, watchlist, QAQC/accuracy view | knowledge on clean close; watchlist in digest |
| **M5** | Hardening | OAuth2 option, provider-API tier, OCR spike, retention controls | optional providers/auth work |

Each milestone ends in a **review gate**.

---

## 12. Open decisions & risks
**Resolved (a–d):** IMAP password login · local-agent AI + **no redaction** · link-shares fully
downloaded (resolver §3.7) · Telegram digest.
**New/раised:**
- **SMTP dropped** from prototype (unused) — confirm OK.
- **Playwright one-time provider login** is required for gated shares — acceptable operator step?
- **`ccccltd.sg` mail system** — confirm IMAP host/port (likely Coremail IMAP; netdisk shares via
  resolver). *[requires the operator's IMAP settings]*

**Risks:** IMAP-password may be disabled by some corporate tenants *(verify for ccccltd.sg)*;
gated-share download depends on a live Playwright session (mitigated by persistent profile);
scanned-doc text loss (OCR roadmap); misclassification (council + QAQC + golden-set).

---

## 13. Glossary delta from v1
**Capture / Reason / Materialize** (the 3 phases) · **Network-unplug invariant** · **Packet**
(clean AI-ready record) · **Corpus** (offline `.eml` + packets) · **Link-share resolver** (tiered
direct→authed-browser downloader, §3.7) · **Local agent AI** (Phase 2 = Claude Code skill, no API,
no redaction). All other terms keep their v1 GLOSSARY meaning.
