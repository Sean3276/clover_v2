# 🍀 Clover v2 — Spec: Resolve flagged threads + learned classification rules (Phase 3 #2)

> Status: **BUILT.** Closes the comprehension feedback loop: the operator answers a
> flagged classification, and that answer can become an inspectable, deterministic rule that
> auto-classifies similar threads. Created 2026-06-19. Behaviour → [`HOW_CLOVER_WORKS.md`](HOW_CLOVER_WORKS.md).

## Purpose

When the council is split (`consensus: "asked"`) or QAQC flags `needs_review`, let the operator set the
correct classification (override) and, optionally, **teach a rule** so future matching threads are
classified the same way — transparently and reversibly.

## Principles

- **Transparent & reversible.** Rules are human-readable, listed in a UI, and deletable. No opaque
  model-learning in v1.
- **Deterministic precedence.** A matching rule classifies the thread directly (no AI council), so
  rules reliably win and the result is predictable.
- **Safe.** Resolving only changes that thread's stored classification; a rule never sends/deletes.

## Rules — `<archive>/rules.jsonl` (one per line)

```json
{ "type": "keyword|sender|project", "match": "retention sum", "domain": "Project",
  "category": "Commercial", "ts": "…" }
```
- **keyword:** `match` (case-insensitive) appears in the thread text.
- **sender:** `match` is contained in any of the thread's From addresses (so a full address or a domain).
- **project:** `match` equals the thread's extracted `facts.project` (normalized).
- **Conflict:** last-match-wins (newest rule first); the Rules list shows order so it's auditable.

## Application (during comprehension, before the council)

In the pipeline, after facts are extracted, check rules with `{text, senders, project}`. On a match,
classification = `{domain, category, confidence: 1.0, council: "rule", consensus: "rule", members: 0}`
and the AI council is **skipped**. No match → the normal 5/10-member council runs. (QAQC still reviews
the comprehension prose regardless.)

## Resolve (the operator's answer)

`POST /threads/{id}/resolve` with `domain`, `category`, and optional `rule_type` + `rule_match`:
- Updates the thread's record: classification → chosen domain/category, `consensus: "resolved"`,
  `needs_review: false`, plus `resolved_ts`. (comprehension.jsonl is rewritten in place for that thread.)
- If a rule was requested, append it to `rules.jsonl`.

## Rules management

- `GET /rules` — list every rule (type · match · → domain/category · added), each with **Delete**.
- `POST /rules/delete` — remove one (by index). Reached from the Resolve dialog and a link on Mail.

## UI

- Flagged thread: a **Resolve** control (domain → category selects; optional "also make a rule:
  keyword/sender/project + value") → submit clears the flag.
- Rules page: the inspectable list + delete.

## Acceptance

- A keyword/sender/project rule classifies a matching thread directly (`consensus: "rule"`, no council
  call) — verified with the stub (no AI). Non-matching threads still run the council.
- Resolve updates the record (override + flag cleared) and optionally adds a rule; rules list + delete work.
- Rules are read/written deterministically; pipeline still runs with the stub in tests.
