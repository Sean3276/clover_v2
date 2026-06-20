# Clover Phase 3 — Per-Email Comprehension: Chain + Prompts (AS-BUILT + remaining hardening)

> **Status: PARTIALLY IMPLEMENTED.** The core best-of-art prompts and the decomposition are **SHIPPED**:
> the 7 prompts live in `clover/comprehend_prompts.py` (canonical) and are wired in `clover/comprehend.py`
> as the tasks `comprehend` / `comprehend_refine` (ledger-carrying) / `distill_facts` / `distill_summary`
> / `actions` / `qa` (semantic) / `verify_distill`. The deterministic floor backfill, attachment-text
> ingestion, and the QA + per-layer verify gate are also live. This document keeps the full design as the
> reference **and** the remaining-hardening roadmap. Still **PENDING** (design, not yet code): S0 de-quote
> + coverage accounting + loud render-fail flag; the Chinese atom floor (S1) + locale-ambiguous date guard
> (S1b); the S3 `excluded[]` escape + S3v currency-adjacent amount grounding; the S4c action-composition
> critic; the S7c citation-resolution gate; the conditional A/B adjudication (S9); and the two-tier
> no-miss badge (S8). The stage labels (S0…S9) below are the design scheme; the as-built task names are
> the ones listed above.

**Per-email call budget:** Per-email AI calls. STEADY STATE (typical thread, no escalation) = 7 AI calls: S2 COMPREHEND, S2v QA, S3 FACTS+CONTACTS, S4 ACTIONS, S4c ACTION-CRITIC, S5 CLASSIFY(5), S6 DISTILL, S6t TAGS, S7 VERIFY-DISTILL+ACTIONS = 9 if tags/distill kept separate; FOLD S6+S6t into one call and S3 contacts already inside S3 -> 7-8. Plus 5 FREE deterministic passes (S0 assemble+coverage, S1 atom-find EN+CN, S1b date-guard, S3v reconcile+backfill, S7c citation gate, S8 seal). WORST CASE up to 11: +1 re-comprehend on S2v fail, +1 full 10-member council on dispute, +N (=disagreement count) S9 role reviews (conditional, only on S3v atom conflicts). COST LEVERS: (a) on cheap/low-stakes corpora fold S6 DISTILL + S6t TAGS + S3 CONTACTS into fewer calls and skip S4c self-consistency; (b) S9 fires only when A and B disagree on an atom (agreements free); (c) the two passes that must NEVER be cut are S4c (action recall insurance) and the S2v re-comprehend retry (no-miss insurance) - the spec forbids trading these away. Net vs current chain: current is ~4-5 AI calls spent unsafely (one mega-distill, no atom backfill, no action layer, no coverage/citation gates); new chain spends ~7-8 in steady state and closes the measured ~50% ref / ~40% date gap plus the entire action layer - the extra calls buy the product's core promise, not decoration.

## Proposed chain (per email)

1. S0 ASSEMBLE + COVERAGE (deterministic, no AI). Stitch members chronologically into one transcript; tag each message [M1..Mn] with From/Date. De-quote: strip reply tails (On…wrote:/From:/Sent:/-----Original Message----- boundaries and >-prefixed lines) so each [Mn] holds only ORIGINATED content; keep a quoted-shadow index so a value living ONLY in a quote is still surfaced tagged origin='quoted-only,orig=?'. Record per-message char_span offsets for citation resolution. COVERAGE: count members_total vs members_rendered; a render exception increments render_failed and emits a LOUD flag (replaces the silent except:continue at comprehend.py:72-74). ATTACHMENTS: inventory name/size/type; any actionable type (xlsx/pdf/docx) with no extracted text -> HARD attachment_unread flag. Rationale: every downstream gate compares against this assembled text, so a drop/mis-quote/unread-attachment here is invisible to the whole cascade — make it loud, not silent.

2. S1 RULE-FIND ATOMS (deterministic, no AI) — EN + CN. Run extract_atoms over the de-quoted transcript to build the ATOM SHEET (refs/dates/amounts/emails), each tagged with its origin [Mn] and char_span. EXTEND the English-only extractors with CN patterns (\d+年\d+月\d+日 incl. Chinese-numeral forms; 人民币/港币/¥/元 + 万/亿 multipliers, 80万->800000) and locale-aware canonicalization so 2025年3月14日 collapses with 2025-03-14. Rationale: this is the recall FLOOR computed BEFORE the AI so it can backfill drops; today it lives only in eval/ and is English-only, so CN threads get a FALSE-GREEN gate — wiring it in AND teaching it Chinese is the single biggest no-miss lever.

3. S1b LOCALE-AMBIGUOUS DATE GUARD (deterministic, no AI). For any numeric date where both fields <=12 (03/04/2025), do NOT silently canonicalize day-first; emit BOTH interpretations tagged ambiguous=true and prefer a spelled-out form ('4 March') if it appears elsewhere in the thread; else flag needs_review. Rationale: the hardcoded day-first canonicalizer (_DATE_NUM/_DATE_DOT) would otherwise enforce a wrong deadline as the highest-trust deterministic fact and normalize the AI's correct reading to match it.

4. S2 COMPREHEND -> (i) (AI, 1 call; iterative-refine only if oversize). Faithful chronological comprehension, every statement [Mn]-cited, EN+CN verbatim, NEW-vs-quoted attribution, explicit current-state resolution including SUPERSESSION (latest message wins for a ref's status), CONDITIONAL/NEGATION polarity, and stated PRECONDITIONS/future-events. ATOM SHEET injected as a no-drop checklist. Rationale: (i) is the single source of truth for every later verifier, so completeness, citation, polarity and supersession must be resolved HERE or the cascade ratifies the error.

5. S2-REFINE (AI, only when oversize). Append-only continuation that NEVER re-summarizes prior prose, plus a carry-forward OPEN-ITEMS ledger (retentions, holds, pending refs/amounts, unmet preconditions) echoed verbatim each chunk. Rationale: the live _refine regenerates 'running' from the model's own prose each batch, so weak local models summarize away an early semantic open-item (e.g. a retention-release obligation) with no atom to rescue it.

6. S2v QA COMPREHENSION (AI critic, 1 call; re-comprehend ONCE on fail). Hostile 'assume a defect exists' faithfulness+completeness review of (i) vs raw transcript, with message-by-message [Mn] coverage, ATOM SHEET coverage, AND first-class hunts for: un-atomable obligations, un-actioned preconditions, supersession errors, conditional-polarity errors. A deterministic sub-gate asserts every EN floor atom appears in (i). Fail -> one re-comprehend; still fail -> needs_review. Rationale: completeness != correctness-of-resolution; the critic must check temporal/polarity logic, not lean on the atom list.

7. S3 FACTS + CONTACTS (AI, 1 call). BARE values only, each [Mn]+span cited, found_in_source flag; signatures -> contacts. ATOM SHEET passed as confirm-and-place checklist WITH an explicit excluded[] escape so the AI can REJECT a coincidental atom (AC-2, WIN-10) with a reason instead of being forced to confirm it. Rationale: decomposed out of the distill mega-call so facts get a dedicated checkable pass; the excluded[] list is what stops anchoring from laundering false positives.

8. S3v FACTS RECONCILE + BACKFILL (deterministic, no AI). Canonicalize AI facts via the same extractors; diff vs ATOM SHEET. (1) keep grounded AI facts; (2) BACKFILL every floor atom the AI dropped AND did NOT list in excluded[] -> origin='rule', cited, coverage_flag='backfilled'; (3) any atom the AI EXCLUDED-with-reason -> route to a candidate/needs_review bucket, NOT grounded facts; (4) drop AI-only values not in source to needs_review. Tighten amount grounding: require a CURRENCY-ADJACENT match (re-run extract_amounts on source; AI amount must be in that canonical set), never a bare >=3-digit token match. Rationale: closes the measured ~50% ref / ~40% date AI gap WITHOUT laundering false positives or measurement-poisoned amounts as grounded.

9. S4 ACTION ITEMS (AI, 1 call — NEW, recall-first). Every to-do/request/commitment/decision-needed across the WHOLE thread: {action, owner, counterparty, direction, due_raw, due_canonical(resolved vs that message's own sent-date), refs, status(latest-message-wins), is_mine, priority, source[Mn], quote}. Key hand-off chains by (ref,current-owner) keeping owner_history — never collapse a flipping ownership chain into one item. Separate COURTESY markers (辛苦/谢谢/thanks) from REQUEST markers (请/麻烦/帮忙/确认/please): courtesy alone is NEVER an action; request+object IS. Low-confidence soft asks -> review bucket, not dropped. ATOM SHEET dates/refs cross-checked so no deadline is left un-owned. Rationale: the product's core deliverable does not exist today; over-capture+one-click-dismiss beats silent drop, but over-capturing pure courtesy erodes trust, so split the cue classes.

10. S4c ACTION COMPOSITION + COMPLETENESS CRITIC (AI, 1 call — NEW). Two jobs in one pass. (1) COMPOSITION: enumerate every stated PRECONDITION/CONTINGENCY ('X required before Y','subject to','once…then','cannot…until','逾期视为放弃') and every stated future EVENT/PLAN, then cross-join: an unmet precondition whose triggering event is scheduled becomes a derived action status='implied', implied=true, never auto-dismissed. (2) RECALL AUDIT: against deterministic candidate-surface hints (EN+CN request/imperative/by-<date>/waiver words), find obligations S4 missed and UNION them in. Rationale: the costliest miss is the cross-message implied obligation that no imperative triggers and no atom floors; this is the only in-band structural defense and its recall is a MEASURED hint, never a guarantee.

11. S5 CLASSIFY (AI 5/10 council + deterministic referee). Try learned rules.match first (hit wins, no AI). Else 5-member independent-then-aggregate council fed (i)+reconciled FACTS+outstanding ACTIONS (not just lossy (i)); escalate to 10 on dispute/low-conf/invalid-pair; deterministic precedence referee on SOURCE text (safety beats commercial) breaks ties; genuine split -> consensus='asked'. Rationale: preserves the proven architecture but feeds the classifier the verified facts/actions so a backfilled commercial ref can't be invisible to it.

12. S6 DISTILL abstract/one-liner/event (AI, 1 call). Derived strictly from (i) + reconciled FACTS + ACTIONS; abstract must carry every verified ref/deadline/amount and the principal outstanding obligation; event tag <=30 chars derived from CURRENT-STATE (not salience), ref-led when a dominant ref exists, polarity-correct. Rationale: split out so these three are individually verifiable; feeding verified facts forces the headline to include backfilled atoms and the correct (possibly superseded/conditional) state.

13. S6t TAGS (AI, 1 call). Faceted tags from profile vocabulary only, each [Mn]-justified; deterministic _clean_tags drops any off-vocab value. Rationale: closed-vocabulary + omit-if-unsure + post-filter prevents invented tags; its own small pass lets it require per-tag justification.

14. S7 VERIFY DISTILL + ACTIONS (AI critic, 1 call). (ii)/(iii)/(iv) each FAITHFUL + not-LOSSY vs (i); PLUS every action supported by (i) and no material obligation in (i) missing from the list; PLUS temporal/polarity checks: does event/status reflect the LATEST message touching each ref, and is conditional-rejection polarity preserved; PLUS does the live owner equal the owner in the latest message for that ref. Rationale: extends the cascade to gate the new action layer AND to catch self-ratifying supersession/polarity/owner-now errors the old (ii)-(iv)-vs-(i) check never looked for.

15. S7c CITATION + COVERAGE GATE (deterministic, no AI). For every fact and action, resolve its [Mn]+span: the span must exist and literally contain the claimed atom/obligation, and must NOT lie in a quoted region. 100% of actions must carry a resolvable, non-quoted citation. Rationale: trust = traceability the operator verifies in seconds; rejects quote-mis-attributed provenance the de-quote shadow index flags.

16. S8 GATE / SEAL (deterministic, no AI). COMPLETE only if: S2v.passed AND every EN floor atom present AND no un-injected hard miss AND no AI-excluded atom force-injected as grounded AND S7 distill+actions passed AND S7c citations 100% resolvable AND coverage clean (members_rendered==members_total AND render_failed==0 AND bytes_truncated==0 AND no attachment_unread flag). Emit a TWO-TIER no-miss badge: HARD (provable) for EN catchable atoms; SOFT (AI-recall-only) for actions and for CN/attachment-borne facts until the floor speaks Chinese and ingests attachments. CN-bearing or actionable-attachment threads -> HARD needs_review until extraction ships. ANY failure -> needs_review='loud', never silent ship. Rationale: honest two-tier labeling stops the rock-solid atom badge from laundering confidence onto the un-floored soft misses.

17. S9 A/B ADJUDICATE (AI roles, conditional — only on S3v atom disagreements). Atoms found by one witness only get strict/domain/skeptic roles; majority fixes the verdict and feeds back to tighten rules (A)/improve prompts (B). Skipped when A and B agree. Rationale: matches the existing adjudicate.py VERDICT_SCHEMA; cost scales with conflict only, so the no-miss net adds AI cost solely where the two witnesses actually conflict.

## Rewritten prompts

### S2 COMPREHEND -> (i)
```text
ROLE: You are a meticulous correspondence analyst reading ONE email thread. Produce a faithful, chronological account another reader could fully rely on without seeing the original.

INPUT: A de-quoted transcript. Each message is tagged [M1],[M2],... with its sender and date; quoted/replied text has been removed so each tag holds only that message's NEW content. Subject: {subject}

TASK: Write the comprehension in chronological order, message by message. For EACH message state: who wrote it, to whom, and what they asked, decided, agreed, changed, attached, or reported - including any reference number, date, deadline, amount, party, or document mentioned. End with a CURRENT STATE paragraph: what is settled, what is still open, and who owes the next action.

HARD RULES:
1. GROUND EVERYTHING. State nothing the transcript does not support. Do not infer intent, outcomes, or amounts that are not written. If something is ambiguous, say it is ambiguous - do not resolve it for the reader.
2. CITE EVERY POINT. After each statement put the message tag(s) it comes from, e.g. '(M3)'. A statement with no tag is not allowed.
3. NO SILENT MISS. Every message [M1]...[Mn] must be reflected at least once. Do not skip a short, one-line, or 'routine' message - those are exactly where obligations hide. If a message adds nothing new, write '(M7) acknowledgement only.'
4. BILINGUAL FIDELITY (English + 中文). Read and account for Chinese content with the same care as English - never drop a sentence because it is in Chinese. Keep names, company names, reference numbers, dates and amounts in their ORIGINAL form exactly as written (e.g. '2025年3月14日', '人民币80万', 'EOT-05'); you may add a brief English gloss in parentheses but never replace or omit the original.
5. VERBATIM ATOMS. When you mention a reference, date, or amount, copy it character-for-character from the source. Do not normalize, round, or tidy it.
6. SUPERSESSION (latest-message-wins). If the same reference appears with conflicting decisions across messages (granted then rejected; one amount then a revised amount), the CURRENT STATE is the one stated in the LATEST message by date. Record BOTH the earlier decision AND the change in order; never silently overwrite, and never report the earlier (often more salient) decision as current. Watch for retraction cues: disregard / correction / superseded / no longer / 作废 / 更正 / 撤回.
7. POLARITY (do not flip a negation into an approval). Capture conditional or negated decisions exactly: 'will NOT approve VO-09 unless the quote drops below S$800,000' is a CONDITIONAL REJECTION, not an approval - state the condition. Watch: will not / unless / subject to / pending / provided that / 除非 / 否则 / 不予 / 待.
8. PRECONDITIONS & PLANS. When a message states a precondition/contingency ('X is required before Y', 'subject to', 'once...then', 'cannot...until') or a future plan/event with a date, surface it explicitly even if no one phrased it as a request - a later step depends on it.

OUTPUT: Plain prose only (no JSON, no preamble, no 'Summary:' headers). Begin directly with the first message.

TRANSCRIPT:
{transcript}
```

### S2-REFINE (oversize continuation of (i))
```text
ROLE: You are continuing the faithful chronological comprehension of ONE email thread too long to read at once.

You are given the COMPREHENSION SO FAR, an OPEN-ITEMS LEDGER, and the NEXT batch of messages (each tagged [Mn] with sender/date, quoted text removed). Extend the comprehension to incorporate the next messages.

HARD RULES:
1. APPEND ONLY. Do NOT delete, shorten, summarize, or reword the comprehension so far - only ADD to it. Keep all existing (Mn) citations intact. Do not re-summarize prior prose.
2. ECHO THE LEDGER VERBATIM. The OPEN-ITEMS LEDGER lists obligations/holds still unresolved from earlier chunks (retentions, pending refs/amounts, unmet preconditions, awaiting-reply items). Re-state every ledger item that is still open at the end of your output, verbatim, UNLESS a NEXT message resolves it - in which case state how it was resolved and cite the message. An open item must never vanish without a resolution message.
3. For each NEW message append who wrote it and what was asked/decided/changed/attached, with its (Mn) citation, in chronological order; account for EVERY new message including acknowledgement-only ones.
4. Apply the SAME rules as the full comprehension: bilingual fidelity (never drop Chinese; verbatim atoms in original form); supersession (latest message wins for a ref's status, record both); polarity (do not flip negations/conditionals); record new preconditions and dated plans.
5. Update the CURRENT STATE paragraph at the very end. Ground everything; cite everything; invent nothing.

OUTPUT: Plain prose only - the FULL updated comprehension (so-far plus new content), not just the new part.

COMPREHENSION SO FAR:
{running}

OPEN-ITEMS LEDGER (still unresolved):
{open_items}

NEXT MESSAGES:
{chunk}
```

### S2v QA COMPREHENSION (critic)
```text
ROLE: You are a hostile QA reviewer whose job is to FIND the defect. Assume the COMPREHENSION has at least one fault and prove it; conclude it is clean only after a genuine search.

Compare the COMPREHENSION against the SOURCE transcript (messages [M1]...[Mn]) and the ATOM SHEET (catchable refs/dates/amounts/emails the deterministic scanner found, each with its origin message).

CHECK:
1. FAITHFULNESS: is every statement supported by the source? Flag anything fabricated, misattributed (credited to the wrong message/sender, e.g. a value that actually lives in a quoted reply tail), or distorted (an amount/date/decision changed).
2. COMPLETENESS: walk the messages - is every [Mn] reflected? Is any MATERIAL point omitted (a decision, request, commitment, deadline, amount, change, or attachment)? Pay special attention to short messages and to Chinese-language content - the usual silent-miss sources.
3. ATOM COVERAGE: is every ATOM SHEET item present in / consistent with the comprehension? List any missing atom.
4. UN-ATOMABLE OBLIGATIONS: hunt for to-dos that NO reference/date/amount would catch - a polite or indirect request, an implied obligation formed by combining two messages (e.g. 'the cert is a precondition for handover' + 'we plan handover Friday' => an unstated 'issue the cert before Friday'), a commitment buried in Chinese courtesy language. Do NOT rely on the atom list for these.
5. CURRENT-STATE CORRECTNESS (not just completeness): (a) SUPERSESSION - for each reference with conflicting decisions across messages, does the comprehension's current state match the LATEST message touching it? (b) POLARITY - is any conditional/negated decision reported as if it were unconditional/approved? (c) PRECONDITIONS - is any stated precondition left without being carried into current state?

SCORING: faithfulness and completeness each 0..1. passed=true ONLY if both are effectively perfect AND no atom is missing AND no current-state (supersession/polarity/precondition) error is found. Be conservative: when in doubt, fail.

List specific, located issues (cite the [Mn]). Output JSON ONLY - no prose, no markdown, no code fence:
{"passed":false,"faithfulness":0.0,"completeness":0.0,"issues":[]}

ATOM SHEET:
{atom_sheet}

COMPREHENSION:
{comprehension}

SOURCE:
{transcript}
```

### S3 FACTS + CONTACTS
```text
ROLE: You are a precise facts extractor for one email thread. Output ONLY structured facts grounded in the comprehension below - no analysis, no commentary.

You are also given an ATOM SHEET: reference numbers, dates, amounts, and email addresses a deterministic scanner already found in the source (with the [Mn] each was found in). The atom sheet is near-complete for these catchable items - your job is to CONFIRM and PLACE them, OR to REJECT a coincidental one with a reason. It is a checklist, not a guarantee of correctness.

EXTRACT:
- project: the single project/contract/job this thread concerns (bare name exactly as written; '' if none).
- parties: organisations or named people materially involved (bare names exactly as written).
- refs: every reference identifier (RFI/NCR/CR/SOI/EOT/VO/PO/IPC/TQ/CVI/MCI...), bare, exactly as written.
- dates: every date that carries meaning (deadline, meeting, submission, event), bare, in ORIGINAL form (keep '2025年3月14日' as-is).
- amounts: every monetary amount, bare, with its currency exactly as written ('S$878,000', '人民币80万').
- contacts: every person identifiable from a signature/header - {name, position, company, phone, email}; '' if unknown.

HARD RULES:
1. BARE VALUES ONLY. A fact value is the literal token - never append a description, deadline, status, or note ('EOT-05', not 'EOT-05 (granted)'). Polarity and status belong in the action items, not here.
2. ACCOUNT FOR EVERY ATOM, BUT YOU MAY REJECT. For each ATOM SHEET item, include it in the matching list IF it is a genuine fact of this thread. If an atom is a coincidental pattern (a model code like AC-2 / WIN-10 read as a ref, a measurement like '500 mm' read as money, a currency code read as a ref), DO NOT include it - instead add it to excluded[] with a one-line reason. Never silently omit an atom: it is either placed or explicitly excluded.
3. AN AMOUNT NEEDS A CURRENCY. A number with a unit (mm, m, kg, days, %) is NOT an amount. Only include a value in amounts if a currency token (S$, USD, RMB, 人民币, ¥, 元...) is adjacent to it in the source.
4. GROUND + CITE. Every value must be supported by the comprehension; tag each with its source message in cite. Set found_in_source=false for any value you cannot point to - do not drop it, flag it.
5. NO INVENTION. If a field is unknown, leave it empty. Do not guess a company from a name or a date from context.
6. BILINGUAL. Extract Chinese facts with equal care; keep them in their original script.
7. Output JSON ONLY - no prose, no markdown, no code fence - matching exactly:
{"facts":{"project":"","parties":[],"refs":[],"dates":[],"amounts":[]},"cites":[{"value":"","field":"","cite":"","found_in_source":true}],"excluded":[{"value":"","reason":""}],"contacts":[{"name":"","position":"","company":"","phone":"","email":""}]}

ATOM SHEET (confirm, place, or reject with reason):
{atom_sheet}

COMPREHENSION:
{comprehension}
```

### S4 ACTION ITEMS (recall-first)
```text
ROLE: You are an executive assistant turning one email thread into a no-miss to-do list. Your reader will ACT on this list, so a missed action is the worst error. When unsure whether something is an action, INCLUDE it and mark it for review - a silent miss is far worse than a one-click dismiss. But pure courtesy is NOT an action (see rule 3).

EXTRACT every actionable item, resolved across the WHOLE thread: requests, instructions, commitments/promises, deadlines, approvals needed, questions awaiting an answer, and decisions someone must make. Include soft/indirect/interrogative requests ('kindly advise', 'when you get a chance', 'are we still waiting on the cert?').

For EACH item output:
- action: short imperative ('Submit revised RFI-12 drawings').
- owner: who must do it now (name/role/company exactly as written; 'unclear' if not stated - do not guess).
- counterparty: who is owed / awaiting it ('unclear' if not stated).
- direction: 'inbound' if the operator side owes it, 'outbound' if a counterparty owes it, 'unknown' if the operator identity is not given. (Operator side = {operator_org_hint}; if that is empty, use 'unknown' - never guess is_mine.)
- due_raw: the deadline in its original form ('2025年3月20日', 'by Friday'); '' if none.
- due_canonical: that deadline as ISO YYYY-MM-DD, resolved against the SENT DATE of the message it appears in (so 'next Friday' resolves vs that message's own date); '' if none/unresolvable.
- refs: any reference/amount this action concerns (bare), else [].
- status: open | done | blocked | unclear - judged from the LATEST relevant message. If M2 asks and M5 confirms it was done, status=done (cite both). If approval is conditional/blocked on something, status=blocked with the condition in 'action'.
- is_mine: true if the operator side owes it, false if a counterparty owes it, null if operator identity unknown.
- priority: high | normal | low (high if it has a deadline, money, safety, or contractual/claim/waiver consequence).
- source: the message tag(s) [Mn] the action and its status come from.
- quote: a short verbatim snippet (original language) proving the action, copied exactly.
- confidence: high | review (use 'review' for soft/indirect asks you are including defensively).

HARD RULES:
1. GROUND EVERYTHING; every item needs a source [Mn] and a verbatim quote. No quote => do not output it.
2. NO INVENTED OWNERS/DUES. 'unclear'/'' beats a guess.
3. COURTESY != ACTION. A line that is ONLY thanks/greeting (谢谢 / 辛苦了 / thanks / 感谢 / regards) with no request is NOT an action - do not emit it. An action requires a REQUEST marker (请 / 麻烦 / 帮忙 / 确认 / 提交 / please / kindly / can you / could you / submit) AND an object (a ref, amount, document, or task). '辛苦了,麻烦您确认下IPC-09的金额' IS an action (麻烦+确认+IPC-09); '方便时' / 'when convenient' means due='' but status=open (no deadline does not mean no obligation).
4. BILINGUAL. Catch Chinese asks; keep names/refs/dates/amounts in original script. Treat waiver/forfeiture phrasing as high priority: 逾期 / 视为放弃 / 否则 / failing which / will be deemed waived.
5. HAND-OFF CHAINS - DO NOT COLLAPSE. If the same obligation's owner FLIPS across messages (C asks S to price VO-09; S returns it asking K to verify; K says E must instruct), emit ONE LIVE item keyed on (ref, CURRENT owner) showing the LATEST owner/counterparty/status, and record the prior owners in owner_history. Do not merge a flipping chain into a single item that loses who-owes-whom-now.
6. DEDUPE BY MEANING otherwise: the same task merely restated is ONE item (cite all [Mn]); a follow-up changing the deadline updates due and cites the change.
7. Cross-check the ATOM SHEET: any dated or referenced obligation there should map to an action unless already satisfied - do not leave a deadline with no owner.
8. Output JSON ONLY - no prose, no code fence:
{"actions":[{"action":"","owner":"","counterparty":"","direction":"unknown","due_raw":"","due_canonical":"","refs":[],"status":"open","is_mine":null,"priority":"normal","source":"","quote":"","confidence":"high","owner_history":[]}]}

ATOM SHEET (deadlines/refs to cross-check):
{atom_sheet}

COMPREHENSION:
{comprehension}

SOURCE:
{transcript}
```

### S4c ACTION COMPOSITION + COMPLETENESS CRITIC
```text
ROLE: You are a recall auditor for ACTION ITEMS with no tolerance for a missed obligation. You are given the COMPREHENSION, the raw SOURCE, the action items ALREADY extracted, and a list of deterministic CANDIDATE HINTS (politeness/request markers, 'by <date>' patterns, imperative verbs, EN+CN request and waiver words). Your ONLY job is to surface obligations the existing list MISSED. Do not repeat items already present (allow for paraphrase).

DO TWO THINGS:

PART 1 - COMPOSITION (cross-message implied obligations). First list every stated PRECONDITION / CONTINGENCY in the thread ('X is required before Y', 'subject to', 'cannot ... until', 'once ... then', '...的前提', '逾期视为放弃') and separately every stated FUTURE EVENT / PLAN with a date ('we plan handover this Friday', 'submission is on 14 Mar'). Then CROSS-JOIN them: whenever an unmet precondition's triggering event is scheduled, that is a DERIVED action even though NO single message phrased it as a request. Example: M2 'the fire-rating cert is a precondition for handover sign-off' + M5 'we plan handover this Friday 20 Jun' => derived action 'Issue the fire-rating cert before 20 Jun', status='implied'. Emit each as an action with implied=true; these must never be auto-dismissed.

PART 2 - RECALL AUDIT. Scan the source independently against the candidate hints and every message. For each, ask: does this imply a to-do NOT already in the list? Pay special attention to soft/polite/indirect requests, interrogative-as-request, obligations stated only in Chinese, and a request sitting near a deadline or named beneficiary. Ignore pure courtesy lines (thanks/greetings with no request).

Return ONLY the MISSED actions (composition + audit), each in the SAME shape as an extracted action item (action, owner, counterparty, direction, due_raw, due_canonical, refs, status, is_mine, priority, source, quote, confidence) plus an 'implied' boolean. If the existing list is genuinely complete, return an empty list. Recall is everything: when unsure, include it and set confidence='review'.

Output JSON ONLY - no prose, no code fence:
{"missed_actions":[{"action":"","owner":"","counterparty":"","direction":"unknown","due_raw":"","due_canonical":"","refs":[],"status":"open","is_mine":null,"priority":"normal","source":"","quote":"","confidence":"review","implied":false}]}

EXTRACTED ACTIONS (already found):
{actions_so_far}

DETERMINISTIC CANDIDATE HINTS (not authoritative):
{candidate_hints}

COMPREHENSION:
{comprehension}

SOURCE:
{transcript}
```

### S5 CLASSIFY (council)
```text
ROLE: Convene a panel of {members} INDEPENDENT classifiers for one email thread. Each member privately reads the comprehension and the verified facts/actions, then assigns a DOMAIN, then a CATEGORY within that domain, by MEANING - what the thread is really about and its CONSEQUENCE - not by keyword spotting. Members reason independently first; only then do you aggregate.

TAXONOMY (choose only from these; category must belong to its domain):
{taxonomy}
HIGH-STAKES SAFETY-NET CATEGORY: {safety_net} - when a reading is plausibly this, prefer it; misfiling a commercial/contractual/safety matter as routine is far costlier than the reverse. A payment claim, variation, EOT, claim, NCR with cost/time impact, instruction, or contract document is a commercial/contractual matter even when phrased politely as 'for your information'. A safety incident/permit/stop-work matter outranks a commercial reading.{extra}

USE THE EVIDENCE BELOW, not just the prose: the VERIFIED FACTS and OUTSTANDING ACTIONS may surface a commercial/contractual signal (a backfilled ref, a payment, a variation) that the comprehension underplayed.

PROCEDURE:
1. Each member picks domain+category and notes the one phrase/fact that decided it.
2. Aggregate: report the MAJORITY domain+category.
3. confidence 0..1 = share of members agreeing, lowered if the deciding evidence is thin.
4. dispute=true if the panel is genuinely split (no clear majority, or a strong minority on a costlier category).
5. votes = one-line tally. dissent = one line stating the strongest minority view and why.

BILINGUAL: judge Chinese content on equal footing; do not down-weight a thread because its key evidence is in Chinese.

Output JSON ONLY - no prose, no code fence:
{"domain":"","category":"","confidence":0.0,"dispute":false,"dissent":"","votes":""}

VERIFIED FACTS:
{facts}

OUTSTANDING ACTIONS:
{actions}

COMPREHENSION:
{comprehension}
```

### S6 DISTILL (abstract / one-liner / event)
```text
ROLE: You write the headline layers for one already-comprehended email thread. Use ONLY the comprehension, the verified facts, and the action items below - add nothing they do not support.

PRODUCE:
- abstract: one accurate paragraph (3-5 sentences) covering what the thread is about, what was decided/requested, and the CURRENT state. It must mention every reference, deadline, and amount in VERIFIED FACTS and name the principal outstanding obligation. Faithful and self-contained.
- summary: one line capturing the single most important point.
- event: a tag of AT MOST 30 characters naming the core CURRENT event. No trailing punctuation.

HARD RULES:
1. CURRENT STATE, NOT SALIENCE. If a decision was superseded (granted then rejected), the event/abstract must reflect the LATEST state, not the more eye-catching earlier one. If a decision is conditional/negated ('will not approve VO-09 unless quote < S$800,000'), do NOT phrase it as an approval - the event/abstract must preserve that polarity.
2. FAITHFUL + NOT LOSSY. Do not omit a material decision/deadline/amount/change; do not state anything the comprehension does not support.
3. Prefer the verified facts for any reference/date/amount/party; keep them in original form (English or 中文). Lead the event tag with the dominant reference when one exists (e.g. 'EOT-05 rejected', 'RFI-12 待回复').
4. If bilingual, write in the thread's PRIMARY language; if mixed roughly equally, write English and keep key Chinese terms verbatim.
5. No invention, no hedging filler, no 'this email...' preamble.
6. Output JSON ONLY - no prose, no code fence:
{"abstract":"","summary":"","event":""}

VERIFIED FACTS:
{facts}

ACTION ITEMS:
{actions}

COMPREHENSION:
{comprehension}
```

### S6t TAGS (faceted)
```text
ROLE: You apply controlled-vocabulary tags to one email thread, using ONLY the facet values listed. Tags are orthogonal labels on top of the domain/category - a thread may match several facets or none.

For each FACET below, pick the value(s) that CLEARLY apply based on the comprehension. Omit a facet entirely if unsure - a missing tag is fine; a wrong/invented tag is not. Never output a value not in the list.

FACETS (allowed values):
{facet_vocab}

HARD RULES:
1. CLOSED VOCABULARY. Use the exact strings given; do not invent facets or values, do not paraphrase.
2. JUSTIFY. For each tag give the message [Mn] / phrase that justifies it (used for verification, then discarded).
3. Judge Chinese content equally.
4. Output JSON ONLY - no prose, no code fence:
{"tags":[{"facet":"","value":"","cite":""}]}

COMPREHENSION:
{comprehension}
```

### S7 VERIFY DISTILL + ACTIONS (critic)
```text
ROLE: You are a strict reviewer. The COMPREHENSION is the single source of truth. Check the distilled layers AND the action items against it.

PART A - DISTILL. For EACH of abstract, one-liner summary, and event tag decide:
- FAITHFUL: states nothing the comprehension does not support (no added facts, no drift in any amount/date/ref/party).
- NOT LOSSY: omits no point that belongs at its level (abstract carries the material decision/deadline/amount and the principal obligation; one-liner the single most important point; event the core current event).
- CURRENT-STATE: the event/abstract reflect the LATEST state of any superseded decision and preserve the polarity of any conditional/negated decision (a conditional rejection must NOT read as an approval).
- The event tag is <=30 characters and any Chinese terms are preserved correctly.

PART B - ACTIONS. 
- SUPPORTED: every action item is supported by the comprehension (no fabricated obligation).
- NO MISSED OBLIGATION: no material outstanding obligation in the comprehension (a request awaiting reply, an NCR/VO/EOT/payment/submittal pending, an implied precondition) is missing from the action list. List any missed.
- OWNER-NOW: for each action, the listed owner equals the owner in the LATEST message touching that reference (a hand-off chain must show the current owner, not the first one). Wrong owner/counterparty/direction or stale status is a failure.

Set abstract_ok / summary_ok / event_ok / actions_ok; passed=true ONLY if all hold and there are no missed obligations. Be conservative: when in doubt, fail.

Output JSON ONLY - no prose, no code fence:
{"passed":false,"abstract_ok":false,"summary_ok":false,"event_ok":false,"actions_ok":false,"missed_actions":[],"issues":[]}

COMPREHENSION:
{comprehension}

ABSTRACT:
{abstract}

ONE-LINER:
{summary}

EVENT TAG:
{event}

ACTION ITEMS:
{actions}
```

### S9 A/B ADJUDICATE (reviewer role, conditional)
```text
ROLE: You are a {role} reviewer adjudicating ONE disputed candidate item that two independent extractors disagree on (a deterministic rule found it, or the AI found it - not both). Decide whether it is genuinely present and meaningful in the email.
{role_instruction}

Candidate {kind}: {value}

EMAIL TEXT (English and/or 中文 - read both equally):
{source_text}

Decide:
- present: does this EXACT value appear in the text (allowing only trivial format differences, e.g. MCI-018 == MCI-18, 2025-03-14 == 2025年3月14日, 80万 == 800000)? A value that appears ONLY inside a quoted reply tail does not count as originating here - mark present=false if it is only quoted.
- is_real: is it a genuine {kind} as used in this kind of correspondence - not a coincidental pattern match (a currency code read as a ref, a model/grid code like AC-2 read as a ref, a measurement like '500 mm' read as money, a phone fragment read as a date)?

Output JSON ONLY - no prose, no code fence:
{"present":false,"is_real":false,"reason":""}
```

## What this redesign changed vs the PRE-UPGRADE chain (historical baseline)

> The bullets below describe what the redesign changed **relative to the pre-upgrade code**. Symbols
> named here as the old baseline (`_distill_prompt`, `_DISTILL_SCHEMA`, the old single-call distill,
> the prose-regen `_refine`) have since been **removed**; line-number references are to that now-deleted
> code. Items describing the *decomposition*, the *action layer*, the *floor backfill*, the
> *ledger-carrying refine*, and the *role-split QA* are **now SHIPPED** (see the status banner at top).
> Items describing the *Chinese atom floor*, *coverage accounting / loud render-fail*, *de-quoting +
> citation gate*, *excluded[] non-laundering*, and the *S4c critic* remain **PENDING**.

- WIRED THE DETERMINISTIC FLOOR INTO THE LIVE CHAIN WITH TWO-DIRECTION BACKFILL. crosscheck_floor + extract_atoms exist only in clover/eval (scorer.py/extractors.py) and are NEVER called by comprehend.py _build_once; the live _verify_facts (comprehend.py:301-340) only SUBTRACTS ungrounded AI facts and never ADDS dropped atoms back. New S1 computes the ATOM SHEET before the AI; S3v backfills every floor atom the AI dropped (origin='rule', cited). This is the direct fix for the measured ~50% ref / ~40% date silent drop and is the single biggest no-miss lever.
- TAUGHT THE FLOOR CHINESE (S1) AND ADDED A LOCALE GUARD (S1b). extractors.py is verified English/Latin-only (_MON Latin, _AMT has no 万/亿/元, _REF needs uppercase Latin prefix), so CN-only atoms produce an EMPTY source set and hard_misses=0 -> FALSE GREEN on exactly the multilingual content the product promises. Added CN date/amount/ref regexes + locale-aware canonicalization (2025年3月14日 collapses with 2025-03-14; 80万->800000). Added a day-first/ambiguity guard so 03/04/2025 is not silently canonicalized to the wrong month and the AI's correct reading is not normalized to match a wrong floor value.
- MADE BACKFILL NON-LAUNDERING. The naive 'force-inject every dropped atom as grounded' would re-add a false-positive ref (AC-2/WIN-10) the AI correctly omitted, and the day-first/measurement bugs would enter as highest-trust facts. S3 now gives the AI an explicit excluded[] escape; S3v routes AI-EXCLUDED atoms to a candidate/needs_review bucket, NOT grounded facts, and only backfills atoms the AI dropped WITHOUT excluding.
- TIGHTENED AMOUNT GROUNDING TO KILL THE DIGIT-FALLBACK LAUNDER. _verify_facts (comprehend.py:318-321) keeps any amount whose >=3-digit number EQUALS a source number token, so 'S$500' can be validated by '500 mm'. S3v now requires a CURRENCY-ADJACENT match (AI amount must be in extract_amounts(source)), and the S3 prompt forbids treating a unit-bearing number (mm/m/kg/days/%) as money.
- DECOMPOSED THE DISTILL MEGA-CALL. The pre-upgrade single distill call emitted abstract+summary+event+facts+contacts+tags at once, which is why fact recall was poor and the per-layer verify was nearly meaningless. **NOW SHIPPED:** split into `distill_facts` (facts+contacts, each [Mn]-cited; `_FACTS_SCHEMA`) and `distill_summary` (abstract/one-liner/event/tags; `_SUMMARY_SCHEMA`), plus the separate `actions` pass — each individually reliable on weak local models and individually scorable.
- ADDED THE ACTION-ITEM LAYER - it did NOT exist pre-upgrade (no actions field anywhere in the old schema or the record). **NOW SHIPPED** via `_ACTIONS_SCHEMA` and the record's `actions` field: recall-first with owner/counterparty/direction/is_mine/due_raw/due_canonical(resolved vs each message's own sent-date)/refs/status(latest-wins)/priority/source[Mn]/quote, hand-off chains keyed on (ref,current-owner) with owner_history (never collapsed), courtesy markers separated from request markers, and low-confidence soft asks routed to a review bucket. (The S4c composition critic remains PENDING.)
- ADDED THE COMPOSITION + COMPLETENESS CRITIC (S4c) FOR CROSS-MESSAGE IMPLIED OBLIGATIONS. The stress-test's worst case (precondition in M2 + scheduled event in M5 => unstated 'issue cert before Friday') triggers no imperative and no atom, so neither the extractor nor a hint-based critic catches it. S4c explicitly enumerates preconditions and future events and CROSS-JOINS them into derived implied actions, then unions in any hint-audited misses. Its recall is logged as a measured hint, never a guarantee.
- ADDED COVERAGE ACCOUNTING (S0) AND TURNED THE SILENT MESSAGE DROP LOUD. _thread_messages (comprehend.py:72-74) does except:continue, dropping a message with zero trace (the '1/68' class). S0 counts members_rendered vs members_total, flags render_failed and bytes_truncated, and inventories attachments. messages_read<total OR render_failed>0 -> needs_review.
- MADE ATTACHMENTS A HARD GATE, NOT A SOFT FLAG. render_message returns attachments as name/size only; _thread_messages sends only _block_text (body), so an IPC amount or EOT in an xlsx/pdf never reaches any AI or regex and cannot even be counted as a hard miss. S0 now flags any unread actionable attachment (xlsx/pdf/docx) and S8 treats attachment_unread as HARD needs_review until extraction ships.
- IMPLEMENTED DE-QUOTING + PER-[Mn] CITATIONS + A CITATION GATE. _block_text keeps quoted reply tails and only extract_dates skips header noise (refs/amounts scan the whole blob), so a value quoted in M7 gets mis-credited to M7's sender/date. S0 strips reply tails before S1 so each atom's origin is its first NON-quoted occurrence, keeps a quoted-shadow index, and S7c rejects any citation whose span lies in a quoted region; 100% of actions must carry a resolvable non-quoted citation.
- ADDED CURRENT-STATE CORRECTNESS CHECKS (SUPERSESSION / POLARITY / OWNER-NOW) TO COMPREHEND AND VERIFY. The cascade checked faithfulness+completeness but never correctness-of-resolution, so a wrongly-resolved (i) (granted-then-rejected read as granted; conditional rejection read as approval; first owner of a hand-off chain) was faithfully ratified. S2 resolves these, S2v hunts for them, S7 gates the event/abstract/status/owner against the latest message.
- FED CLASSIFY THE VERIFIED FACTS + ACTIONS, NOT JUST THE LOSSY COMPREHENSION. _classify (comprehend.py:269-294) reads only the comprehension, so a backfilled commercial ref is invisible to it. S5 now passes facts+actions and spells out the 'polite FYI that is really a VO/payment claim' trap; precedence referee still runs on SOURCE and safety still outranks commercial.
- MADE THE REFINE PATH NON-LOSSY FOR SEMANTIC OPEN ITEMS. _refine (comprehend.py:343-353) regenerates 'running' from the model's own prose each chunk, summarizing away an early atom-less obligation (e.g. retention-release). S2-REFINE is append-only and carries a verbatim OPEN-ITEMS ledger each pass.
- REPLACED THE GREEN/RED SEAL WITH A HONEST TWO-TIER NO-MISS BADGE (S8). HARD/provable for EN catchable atoms; SOFT/AI-recall-only for actions and for CN/attachment-borne facts until the floor speaks Chinese and ingests attachments. Stops the rock-solid atom backfill from laundering confidence onto the un-floored soft misses.
- KEPT THE PROVEN ARCHITECTURE AND THE EXISTING SCHEMAS/CONTRACTS. 5/10 council + precedence referee + operator-ask (S5), re-comprehend-once on QA fail (S2v), verified cascade, and conditional A/B adjudication (S9) on atom disagreements only. Every JSON shape stays compatible with _CLASSIFY_SCHEMA / _QA_SCHEMA / _DISTILL_QA_SCHEMA / adjudicate's VERDICT_SCHEMA so the tolerant parser and existing gates keep working; new config (action cues, courtesy-vs-request markers, operator_org_hint, ref-prefix allowlist) is added to the Profile/config, keeping classification profile-driven and prompts vendor-neutral.

## Residual risks

- ACTION EXISTENCE HAS NO DETERMINISTIC FLOOR (confirmed: extractors.py header states it is NOT an action extractor; scorer floors only refs/dates/amounts/emails). S4/S4c/S7 are ALL AI, so 'SOURCE minus AI actions' is uncomputable - an implied/composed/CN-courtesy obligation that the extractor AND the critic both miss is unprovable in-band. The no-miss guarantee is genuinely TWO-TIERED: HARD for catchable EN atoms, SOFT (recall-biased) for actions. The only real closure is out-of-band: an out-of-distribution human existence-hunter on a gold set scoring CASE/action recall (oracle_escape_rate_EXISTENCE -> 0), with every production miss promoted into S4c's candidate-hint set. The badge must label the action layer SOFT until that exists; presenting it with the same confidence as backfilled refs would be lying by omission.
- THE CN FLOOR AND THE LOCALE/AMBIGUITY GUARD AND ATTACHMENT INGESTION ARE DESIGN, NOT YET CODE. Until the extractor actually parses 年/月/日 + 人民币/万/亿, defers ambiguous numeric dates, and an OCR/xlsx reader feeds attachment text into S1, those threads are SOFT-tier and must be HARD needs_review. Shipping the prompts without the extractor work leaves the CN/attachment false-green intact - the prompts alone cannot fix a code-level recall gap.
- BACKFILL PRECISION DEPENDS ON THE AI'S excluded[] JUDGEMENT. Routing AI-excluded atoms to a review bucket stops force-laundering, but a weak local model may FAIL to exclude a genuine false positive (silently confirming AC-2 as a ref) OR wrongly exclude a real ref. A profile-driven ref-prefix allowlist (RFI/NCR/EOT/VO/IPC/CR/SOI/TQ/CVI/MCI...) plus context-gating (a ref needs a nearby correspondence verb: issued/raised/responded/approved) reduces but does not eliminate this; precision is still refined against the gold set.
- SELF-RATIFICATION IS REDUCED, NOT ELIMINATED. S2v and S7 now hunt supersession/polarity/owner-now, but they are AI critics reading the same (i); if the same model systematically mis-resolves a subtle conditional or a 3-hop hand-off, the critic can share the blind spot. The deterministic citation/owner-vs-latest-message checks (S7c) catch structural cases but not every semantic mis-resolution.
- ASSEMBLY FIDELITY (S0) IS STILL THE UNVERIFIED CEILING. Every gate compares against the assembled transcript or (i), never the raw MIME bytes. A message dropped by a render exception is now LOUD (render_failed flag), but an EN/CN duplicate pair where only one copy carries a unique fact, or a mis-threaded/merged message, can still take its content out of the corpus before S1 scans it. Mitigation requires reconciling per-message count and atom counts against the raw source and never deduping an EN/CN pair without confirming each copy's unique-fact set is a subset of the kept copy - partially addressed by the quoted-shadow index but not fully closed.
- COST/LATENCY ON LOCAL OLLAMA. Steady-state ~7-8 AI calls/thread (vs ~4-5 today) plus conditional escalations; on slow local boxes this raises per-thread latency. The folding levers (combine distill+tags+contacts on cheap corpora, skip S4c self-consistency) trade recall for speed and must be operator-gated, never applied to high-stakes (commercial/safety/CN/attachment) threads where the extra passes are the no-miss insurance.
