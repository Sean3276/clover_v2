"""Phase 3 — comprehension pipeline (the first AI phase).

Pure & stateless: comprehend_thread(archive, thread, backend, profile) -> record. Reads Phase-2
threads + the .eml archive; writes <archive>/comprehension.jsonl (idempotent by thread_id).
Whole-thread by default; iterative-refine for giants. Classification via a profile-driven 2-tier
council with a deterministic precedence referee. Facts are verified against the source.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
from html import unescape
from pathlib import Path

from . import rules as rulesmod
from .comprehenders import Comprehender
from .profiles import Profile, get_profile
from . import attachments as attmod
from .threads import get_attachment, read_threads, render_message

_TAG = re.compile(r"(?s)<[^>]+>")
_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS = re.compile(r"\s+")
_ANNOT = re.compile(r"\s+(?:[—–;(]|-\s).*$")   # trailing model annotation: " — desc", " (desc)", " - desc"
_WEEKDAY = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*[,.\s]+", re.I)   # leading "Wed " on a date
_NUM = re.compile(r"\d[\d,]*\.?\d*")

_DISTILL_SCHEMA = {"abstract": "str", "summary": "str", "event": "str (<=30 chars)",
                   "facts": {"project": "str", "parties": ["str"], "refs": ["str"],
                             "dates": ["str"], "amounts": ["str"]},
                   "contacts": [{"name": "str", "position": "str", "company": "str",
                                 "phone": "str", "email": "str"}],
                   "tags": ['"Facet: Value" strings, only from the listed facet vocabularies']}
_CLASSIFY_SCHEMA = {"domain": "str", "category": "str", "confidence": "number 0..1",
                    "dispute": "bool", "dissent": "str", "votes": "str"}
_QA_SCHEMA = {"passed": "bool", "faithfulness": "number 0..1", "completeness": "number 0..1",
              "issues": ["str"]}
# step-8 (ii)-(iv) vs (i): verify the distilled abstract / one-liner / event tag against the comprehension
_DISTILL_QA_SCHEMA = {"passed": "bool", "abstract_ok": "bool", "summary_ok": "bool",
                      "event_ok": "bool", "issues": ["str"]}
_SMALL_COUNCIL, _FULL_COUNCIL = 5, 10


# ---------------------------------------------------------------- helpers
def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _block_text(block: dict) -> str:
    if block.get("body_text"):
        return block["body_text"]
    h = block.get("body_html") or ""
    h = _STYLE.sub(" ", h)
    h = _TAG.sub(" ", h)
    return _WS.sub(" ", unescape(h)).strip()


def _attachment_text(archive, location: dict, atts: list) -> str:
    """Extract text from a message's attachments so their content is comprehended too — LOUD on
    anything unread (image/OCR-needed, unsupported, parse error), never silently skipped."""
    import os
    import tempfile
    parts = []
    for a in atts:
        name = a.get("name") or "attachment"
        if a.get("img"):
            parts.append(f"[attachment NOT read: {name} — image, needs OCR (not enabled)]")
            continue
        try:
            got = get_attachment(archive, location, a.get("i", 0))
        except Exception:
            got = None
        if not got:
            continue
        fname, _ctype, data = got
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=Path(fname or name).suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data or b"")
            r = attmod.extract_attachment(tmp)
        except Exception as e:
            r = {"ok": False, "text": "", "note": f"{type(e).__name__}"}
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if r.get("ok") and r.get("text"):
            parts.append(f"[attachment: {fname or name}]\n{r['text']}")
        else:
            parts.append(f"[attachment NOT read: {fname or name} — {r.get('note', '')}]")
    return "\n\n".join(parts)


def _thread_messages(archive, thread: dict) -> list[str]:
    out = []
    for m in thread.get("members", []):
        locs = m.get("locations") or []
        if not locs:
            continue
        try:
            b = render_message(archive, locs[0])
        except Exception:
            continue
        text = f"From: {b.get('from','')}  Date: {b.get('date','')}\n{_block_text(b)}"
        att = _attachment_text(archive, locs[0], b.get("attachments") or [])
        if att:
            text += "\n\n" + att
        out.append(text)
    return out


# ---------------------------------------------------------------- data layer
def comprehension_path(archive) -> Path:
    return Path(archive) / "comprehension.jsonl"


def read_comprehensions(archive) -> list[dict]:
    p = comprehension_path(archive)
    out = []
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    return out


def comprehended_ids(archive) -> set:
    return {r.get("thread_id") for r in read_comprehensions(archive) if r.get("thread_id")}


def latest_by_thread(archive) -> dict:
    """{thread_id: latest record} — last write wins, matching get_comprehension."""
    out = {}
    for r in read_comprehensions(archive):
        if r.get("thread_id"):
            out[r["thread_id"]] = r
    return out


def latest_by_root(archive) -> dict:
    """{root_id: latest record}. root_id is the thread's STABLE identity — thread_id is a hash of the
    member message-ids and so changes whenever a message is added, which would orphan the comprehension.
    Linking by root_id lets a re-stitched thread find its prior comprehension (and be judged stale)."""
    out = {}
    for r in read_comprehensions(archive):
        if r.get("root_id"):
            out[r["root_id"]] = r
    return out


def comp_for_thread(archive, thread: dict) -> dict | None:
    """Latest comprehension for a thread, matched by stable root_id (falls back to thread_id for
    legacy records that predate root linkage)."""
    rec = latest_by_root(archive).get(thread.get("root_id"))
    return rec if rec else get_comprehension(archive, thread.get("thread_id"))


def thread_sig(thread: dict) -> dict:
    """The thread's identity for staleness: message count + latest-message date."""
    return {"n": thread.get("n"), "end": thread.get("end") or ""}


def is_stale(thread: dict, rec: dict | None) -> bool:
    """True if the thread changed since it was comprehended (a newer message arrived / count grew).
    Legacy records with no stored signature can't be judged, so they're treated as current (no nag)."""
    if not rec:
        return False
    src = rec.get("source")
    if not src:
        return False
    return (src.get("n") != thread.get("n")) or ((src.get("end") or "") != (thread.get("end") or ""))


def get_comprehension(archive, thread_id: str) -> dict | None:
    found = None
    for r in read_comprehensions(archive):       # latest record wins (re-comprehend supersedes)
        if r.get("thread_id") == thread_id:
            found = r
    return found


def _append(archive, record: dict, ts: float | None = None) -> None:
    record = dict(record)
    record["ts"] = ts if ts is not None else time.time()
    with comprehension_path(archive).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_comprehension(archive, record: dict) -> None:
    _append(archive, record)


def resolve_comprehension(archive, thread_id: str, domain: str, category: str, ts: str = "",
                          root_id: str = "") -> bool:
    """Operator override of a flagged thread's classification: set domain/category, consensus=resolved,
    clear needs_review. Rewrites the latest record for that thread in place. Returns False if absent.
    Matches by thread_id, falling back to the stable root_id (a re-stitched thread has a new thread_id)."""
    recs = read_comprehensions(archive)
    idx = None
    for i, r in enumerate(recs):
        if r.get("thread_id") == thread_id or (root_id and r.get("root_id") == root_id):
            idx = i                                  # latest matching record
    if idx is None:
        return False
    c = recs[idx].setdefault("classification", {})
    c["domain"], c["category"], c["consensus"] = domain, category, "resolved"
    if isinstance(recs[idx].get("qaqc"), dict):
        recs[idx]["qaqc"]["needs_review"] = False
    recs[idx]["resolved_ts"] = ts
    p = comprehension_path(archive)
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return True


def estimate_thread_tokens(archive, thread: dict) -> int:
    """Cheap pre-call estimate from .eml byte size (no body read). Each message is capped at 200 KB so a
    huge ATTACHMENT (base64 in the .eml) can't inflate the estimate to hundreds of millions of tokens and
    trip the budget after one thread — only the text actually sent to the model matters here."""
    total = 0
    for m in thread.get("members", []):
        for loc in (m.get("locations") or [])[:1]:
            try:
                total += min((Path(archive) / loc["path"]).stat().st_size, 200_000)
            except Exception:
                pass
    return max(1, total // 4)


# ---------------------------------------------------------------- prompts
def _comprehend_prompt(thread, text):
    return (f"You are reading an email thread (subject: {thread.get('subject','')}). Produce a "
            "faithful, chronological comprehension of the WHOLE thread. Going message by message in "
            "order, capture who wrote and what they asked / decided / committed / changed, and list "
            "EVERY deadline, amount, and reference number explicitly — never summarise these away. "
            "Then give the CURRENT status of each matter, applying supersession (a later message "
            "overrides an earlier one) and correct polarity (respect conditionals like 'only if…' and "
            "negations like 'not approved'). Note any referenced attachment. Base everything STRICTLY "
            "on the text — invent nothing. The thread may mix English and Chinese; keep both "
            "verbatim.\n\nTHREAD:\n" + text)


def _refine_prompt(thread, running, chunk):
    return ("Continue a faithful chronological comprehension of this email thread. Below is the "
            "comprehension so far, then the next messages — update it to include them accurately, "
            "inventing nothing. Carry forward EVERY still-open item (a pending request, an unmet "
            "deadline, a held amount, an unanswered reference) — do not drop or summarise away an "
            "earlier open item just because newer messages arrived.\n\nSO FAR:\n" + (running or "(none yet)")
            + "\n\nNEXT MESSAGES:\n" + "\n\n----\n\n".join(chunk))


def _distill_prompt(comprehension, profile: Profile | None = None):
    tagblock = ""
    if profile and profile.facets:
        vocab = "\n".join(f"  {f}: {', '.join(profile.facet_values(f))}" for f in profile.facet_names())
        tagblock = ("\n\nAlso assign TAGS — for each facet below, pick the value(s) that CLEARLY apply to this "
                    "thread (a thread can have several facets or none). Output each as a \"Facet: Value\" "
                    "string, using ONLY these exact values; omit a facet if unsure — do not invent values.\n"
                    "FACETS:\n" + vocab)
    return ("From this thread comprehension produce: an accurate abstract paragraph; a one-line "
            "summary; an event tag of AT MOST 30 characters; and FACTS grounded ONLY in the "
            "comprehension. For facts, list EVERY reference number, EVERY date, EVERY amount, EVERY "
            "party, and the project — do NOT summarise or omit any. Give each fact's BARE value "
            "EXACTLY as it appears (a date like '14 March 2025', an amount like 'S$878,000', a "
            "reference like 'EOT-05') with no added description, deadline, or commentary, and invent "
            "nothing. Also extract CONTACTS seen in the thread (especially email signatures): for each "
            "person give name, position, company, phone, email — leave a field empty if unknown, and "
            "don't invent." + tagblock + "\n\nCOMPREHENSION:\n" + comprehension)


def _classify_prompt(profile: Profile, comprehension, full: bool, members: int):
    taxonomy = "; ".join(f"{d}: {', '.join(profile.categories(d))}" for d in profile.domain_names())
    extra = ("\nThis is a DISPUTED case — weigh the costliest-if-wrong reading; a commercial / "
             "contractual matter must never be filed as routine info." if full else "")
    return (f"Convene a panel of {members} independent classifiers. Each member INDEPENDENTLY reads the "
            "thread and assigns a DOMAIN, then a CATEGORY within it, by MEANING (not keywords).\n"
            f"Taxonomy — {taxonomy}\n"
            f"High-stakes safety-net category: {profile.safety_net}.{extra}\n"
            f"Then report the PANEL result across the {members} members: the majority domain + category, "
            "a one-line `votes` summary of the split, confidence 0..1, dispute=true if the panel is "
            "genuinely split, and a one-line dissent for any strong minority view.\n\n"
            "COMPREHENSION:\n" + comprehension)


# ---------------------------------------------------------------- council + verification
def _precedence(profile: Profile, text: str) -> str | None:
    """First precedence rule whose keyword appears as a whole word/phrase wins. Word-boundary
    matching (not substring) so 'claim' doesn't fire on 'claimant', nor 'cost' on 'Costa'."""
    low = (text or "").lower()
    for rule in profile.precedence:
        for kw in rule.get("if_any", []):
            kw = kw.strip().lower()
            if kw and re.search(r"\b" + re.escape(kw) + r"\b", low):
                return rule.get("then")
    return None


def _classify(backend: Comprehender, profile: Profile, comprehension: str, thread_text: str) -> dict:
    small = backend.generate("classify", _classify_prompt(profile, comprehension, False, _SMALL_COUNCIL),
                             schema=_CLASSIFY_SCHEMA) or {}
    domain = small.get("domain") or profile.domain_names()[0]
    category = small.get("category") or ""
    conf = _f(small.get("confidence"))
    dispute = bool(small.get("dispute")) or conf < 0.6 or category not in profile.categories(domain)
    if not dispute:
        return {"domain": domain, "category": category, "confidence": conf,
                "council": "small", "members": _SMALL_COUNCIL, "consensus": "unanimous",
                "dissent": "", "votes": str(small.get("votes", ""))}
    # escalate to the full (larger) council
    full = backend.generate("classify_full", _classify_prompt(profile, comprehension, True, _FULL_COUNCIL),
                            schema=_CLASSIFY_SCHEMA) or {}
    domain = full.get("domain") or domain
    category = full.get("category") or category
    conf = _f(full.get("confidence"), conf)
    consensus = "majority"
    ref = _precedence(profile, thread_text)          # deterministic referee breaks ties by rule
    if ref and ref != category:
        category, consensus = ref, "split-resolved"
    if conf < 0.5 or category not in profile.categories(domain):
        consensus = "asked"                          # genuine doubt / invalid pair -> surface to operator
    return {"domain": domain, "category": category, "confidence": conf,
            "council": "full", "members": _FULL_COUNCIL, "consensus": consensus,
            "dissent": str(full.get("dissent", "")), "votes": str(full.get("votes", ""))}


def _norm(s) -> str:
    return _WS.sub(" ", str(s).lower()).strip()


def _verify_facts(facts: dict, thread_text: str) -> tuple[dict, list]:
    """Keep only facts grounded in the thread. The model often annotates a value ('14 Mar 2025 —
    deadline'); verify the BARE core (annotation stripped) appears, with a digit-match fallback for
    amounts (currency/comma formatting varies). Stores the cleaned core. Returns (facts, dropped)."""
    src = _norm(thread_text)
    src_nums = {re.sub(r"\D", "", n) for n in _NUM.findall(src)}   # source numbers, digits-only, per token
    src_nums.discard("")
    facts = dict(facts or {})
    dropped = []

    def verify(value, numeric=False):
        raw = str(value).strip()
        core = _WEEKDAY.sub("", _ANNOT.sub("", raw)).strip() or raw
        c = _norm(core)
        if c and re.search(r"\b" + re.escape(c) + r"\b", src):    # whole word/phrase, not any substring
            return core
        if numeric:
            nums = [re.sub(r"\D", "", n) for n in _NUM.findall(raw)]
            nums = [n for n in nums if len(n) >= 3]      # a meaningful number, not a lone digit
            if nums and all(n in src_nums for n in nums):  # each must EQUAL a real source number (not span two)
                return core
        return None

    for key in ("refs", "dates", "amounts", "parties"):
        keep = []
        for v in (facts.get(key) or []):
            r = verify(v, numeric=(key == "amounts"))
            if r is not None and r not in keep:         # de-dup (bare + annotated collapse to one)
                keep.append(r)
            elif r is None and v:
                dropped.append(f"{key}:{v}")
        facts[key] = keep
    proj = facts.get("project") or ""
    pr = verify(proj) if proj else None
    if proj and pr is None:
        dropped.append(f"project:{proj}")
        facts["project"] = ""
    elif pr:
        facts["project"] = pr
    return facts, dropped


def _refine(backend, thread, msgs, max_chars):
    running, chunk, size = "", [], 0
    for msg in msgs:
        if chunk and size + len(msg) > max_chars:
            running = str(backend.generate("comprehend_refine", _refine_prompt(thread, running, chunk)))
            chunk, size = [], 0
        chunk.append(msg)
        size += len(msg)
    if chunk:
        running = str(backend.generate("comprehend_refine", _refine_prompt(thread, running, chunk)))
    return running


def _clean_contacts(raw) -> list[dict]:
    """Normalize the model's contacts list to {name, position, company, phone, email}; drop empties."""
    out = []
    for c in (raw or []):
        if not isinstance(c, dict):
            continue
        rec = {k: str(c.get(k) or "").strip() for k in ("name", "position", "company", "phone", "email")}
        if rec["name"] or rec["email"]:
            out.append(rec)
    return out


def _clean_tags(raw, profile: Profile | None) -> list[str]:
    """Keep only AI-emitted tags that match the profile's facet vocabulary (deterministic verification).
    Accepts 'Facet: Value' strings or {facet,value}; returns canonical 'Facet: Value', deduped."""
    if not profile or not profile.facets:
        return []
    valid = {(f.lower(), v.lower()): f"{f}: {v}"
             for f in profile.facet_names() for v in profile.facet_values(f)}
    out, seen = [], set()
    for t in (raw or []):
        if isinstance(t, dict):
            fac, val = str(t.get("facet") or "").strip(), str(t.get("value") or "").strip()
        else:
            parts = str(t).split(":", 1)
            fac, val = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")
        canon = valid.get((fac.lower(), val.lower()))
        if canon and canon not in seen:
            out.append(canon); seen.add(canon)
    return out


# ---------------------------------------------------------------- pipeline
def _qa_prompt(comprehension, thread_text):
    return ("You are a strict QA reviewer — assume a defect exists and try to find it. Compare the "
            "COMPREHENSION against the SOURCE thread. Check FAITHFULNESS (every statement is supported "
            "by the source — nothing fabricated, misattributed, or distorted) and COMPLETENESS (no "
            "material point omitted — a decision, request, commitment, deadline, amount, or change, AND "
            "every reference number / date / amount that appears in the source). Also check the CURRENT "
            "status is right: latest message wins, and conditionals / negations are not flipped. Set "
            "passed=true ONLY if all hold; give faithfulness and completeness each 0..1; list specific "
            "issues if any.\n\nCOMPREHENSION:\n" + comprehension + "\n\nSOURCE:\n" + thread_text)


def _distill_qa_prompt(comprehension, abstract, summary, event):
    return ("You are a strict reviewer checking distilled outputs against the COMPREHENSION (the single "
            "source of truth). For EACH of the abstract, the one-liner summary, and the event tag, decide: "
            "is it FAITHFUL (states nothing the comprehension does not support) and not LOSSY (omits no "
            "material point that belongs at its level of detail)? Set abstract_ok / summary_ok / event_ok, "
            "passed=true ONLY if all three hold, and list specific issues.\n\nCOMPREHENSION:\n"
            + comprehension + "\n\nABSTRACT:\n" + (abstract or "") + "\n\nONE-LINER:\n" + (summary or "")
            + "\n\nEVENT TAG:\n" + (event or ""))


def _build_once(archive, thread, backend, profile, max_chars, model):
    msgs = _thread_messages(archive, thread)
    full = "\n\n----\n\n".join(msgs)
    if len(msgs) <= 1 or len(full) <= max_chars:
        method = "whole"
        comprehension = backend.generate("comprehend", _comprehend_prompt(thread, full))
    else:
        method = "refine"
        comprehension = _refine(backend, thread, msgs, max_chars)
    comprehension = (comprehension if isinstance(comprehension, str) else str(comprehension)).strip()

    distilled = backend.generate("distill", _distill_prompt(comprehension, profile), schema=_DISTILL_SCHEMA) or {}
    facts, dropped = _verify_facts(distilled.get("facts") or {}, full)
    # Deterministic floor backfill (recall fix): add every catchable ref/date/amount the AI dropped
    # but a rule finds in the SOURCE. Body-only (strip each message's From:/Date: header line) so the
    # email's own send-date is never injected as a content fact. Backfilled atoms are grounded by
    # construction (they came from the source) and recorded in verified.backfilled.
    from .eval import scorer as _scorer
    floor_src = "\n\n".join(m.split("\n", 1)[1] if "\n" in m else m for m in msgs)
    floor = _scorer.crosscheck_floor(floor_src, facts)
    backfilled = {}
    _have_amt_digits = {re.sub(r"\D", "", str(a)) for a in facts.get("amounts", [])}
    for _k in ("refs", "dates", "amounts"):
        _miss = floor[_k]["missed"]
        if _k == "amounts":          # don't duplicate an amount the AI already has in another format
            _miss = [m for m in _miss if re.sub(r"\D", "", m) not in _have_amt_digits]
        if _miss:
            facts.setdefault(_k, []).extend(_miss)
            backfilled[_k] = _miss
    ruled = rulesmod.match(archive, text=full, project=facts.get("project", ""),
                           senders=[m.get("from", "") for m in thread.get("members", [])])
    if ruled:                                            # a learned rule wins deterministically — no AI council
        classification = {"domain": ruled.get("domain", ""), "category": ruled.get("category", ""),
                          "confidence": 1.0, "council": "rule", "members": 0,
                          "consensus": "rule", "dissent": "", "votes": ""}
    else:
        classification = _classify(backend, profile, comprehension, full)
    rec = {
        "thread_id": thread.get("thread_id"), "root_id": thread.get("root_id"),
        "subject": thread.get("subject"),
        "source": thread_sig(thread),                    # thread state when comprehended (staleness check)
        "comprehension": comprehension,
        "abstract": str(distilled.get("abstract") or "").strip(),
        "summary": str(distilled.get("summary") or "").strip(),
        "event": str(distilled.get("event") or "").strip()[:30],
        "facts": facts,
        "contacts": _clean_contacts(distilled.get("contacts")),
        "tags": _clean_tags(distilled.get("tags"), profile),
        "classification": classification,
        "method": method, "model": model, "profile": profile.name,
        "verified": {"facts_ok": not dropped, "dropped_facts": dropped,
                     "grounded": bool(comprehension), "backfilled": backfilled},
    }
    return rec, full


def comprehend_thread(archive, thread: dict, backend: Comprehender, profile: Profile,
                      *, max_chars: int = 120_000, model: str = "?", qaqc: bool = True) -> dict:
    """Build the record, then run the FULL task verification before the task counts as COMPLETE
    (Phase-3 spec step 8). Two gates: (a) the comprehension (i) is checked for faithfulness +
    completeness vs the source — re-comprehend ONCE on failure; and (b) the distilled abstract /
    one-liner / event tag (ii)-(iv) are each verified against the comprehension (i). The deterministic
    fact-check must also pass. Any failure -> `needs_review` (never silently shipped)."""
    rec, full = _build_once(archive, thread, backend, profile, max_chars, model)
    if not qaqc:
        return rec

    # (a) comprehension (i) vs raw source — faithfulness + completeness; re-comprehend once on failure
    comp = None
    for attempt in (1, 2):
        qa = backend.generate("qa", _qa_prompt(rec["comprehension"], full), schema=_QA_SCHEMA) or {}
        passed = bool(qa.get("passed")) and rec["verified"]["facts_ok"]
        issues = [str(i) for i in (qa.get("issues") or [])][:6]
        if passed or attempt == 2:
            comp = {"passed": passed, "faithfulness": _f(qa.get("faithfulness")),
                    "completeness": _f(qa.get("completeness")), "issues": issues, "attempts": attempt}
            break
        try:
            rec, full = _build_once(archive, thread, backend, profile, max_chars, model)   # one retry
        except Exception as e:                 # retry failed -> keep the first attempt, flag for review
            comp = {"passed": False, "faithfulness": _f(qa.get("faithfulness")),
                    "completeness": _f(qa.get("completeness")),
                    "issues": issues + [f"retry failed: {type(e).__name__}"], "attempts": attempt}
            break

    # (b) distilled (ii)-(iv) vs the comprehension (i) — the step-8 check the task must also pass
    dq = backend.generate("verify_distill",
                          _distill_qa_prompt(rec["comprehension"], rec["abstract"], rec["summary"],
                                             rec["event"]), schema=_DISTILL_QA_SCHEMA) or {}
    rec["verified"].update({"abstract_ok": bool(dq.get("abstract_ok", True)),
                            "summary_ok": bool(dq.get("summary_ok", True)),
                            "event_ok": bool(dq.get("event_ok", True))})
    distill_passed = bool(dq.get("passed", True)) and all(
        rec["verified"][k] for k in ("abstract_ok", "summary_ok", "event_ok"))

    # the TASK is COMPLETE only when BOTH the comprehension and every distilled layer verify
    rec["qaqc"] = {**comp, "distill_passed": distill_passed,
                   "issues": (comp["issues"] + [str(i) for i in (dq.get("issues") or [])])[:8],
                   "needs_review": (not comp["passed"]) or (not distill_passed)}
    return rec


def select_threads(archive, *, only=None, redo=False, include_stale=True) -> list[dict]:
    """Which threads a run would process: never-comprehended (always), stale (when include_stale),
    and everything (when redo). `only` restricts to a set of thread_ids (e.g. one project / date range)."""
    done = latest_by_root(archive)               # link by stable root_id (thread_id changes on new msgs)
    threads = read_threads(archive)
    if only is not None:
        only = set(only)
        threads = [t for t in threads if t.get("thread_id") in only]
    todo = []
    for t in threads:
        rec = done.get(t.get("root_id"))
        if rec is None or redo or (include_stale and is_stale(t, rec)):
            todo.append(t)
    return todo


def run_comprehension(archive, *, backend: Comprehender, profile: Profile | None = None,
                      budget_tokens: int = 200_000, log=print,
                      should_stop=lambda: False, allowed=lambda: True,
                      only=None, redo: bool = False, include_stale: bool = True,
                      limit: int | None = None, concurrency: int = 1,
                      progress=lambda **k: None) -> dict:
    """Comprehend the threads that need it within a token-estimate budget. Idempotent, resumable.
    Processes never-comprehended threads, plus stale ones (changed since comprehension) when
    `include_stale`, plus everything when `redo`. `only` restricts to a set of thread_ids (one
    project, a date range). `allowed()` is the policy gate (subscription/tier plugs in here later)."""
    archive = Path(archive)
    profile = profile or get_profile()
    if not allowed():
        log("Comprehension blocked by policy gate.")
        return {"done": 0, "pending": 0, "spent": 0, "total": 0, "blocked": True}
    backlog = select_threads(archive, only=only, redo=redo, include_stale=include_stale)
    todo = backlog[:limit] if limit else backlog
    total = len(todo)
    spent = done = errors = needs = 0
    last_done = ""

    def _record(t, est, rec, err):
        """Fold one thread's result into the run totals + log. Always called in THIS thread."""
        nonlocal spent, done, errors, needs, last_done
        if err is not None:
            errors += 1
            log(f"  ! {t.get('thread_id')}: {type(err).__name__}: {err}")
            return
        _append(archive, rec)
        spent += est
        done += 1
        last_done = t.get("subject") or t.get("thread_id") or ""
        c = rec["classification"]
        nr = rec.get("qaqc", {}).get("needs_review")
        needs += 1 if nr else 0
        log(f"  ✓ {t.get('thread_id')} [{t.get('n')} msgs] -> {c['domain']}/{c['category']} ({c['consensus']})"
            + (" ⚠ NEEDS-REVIEW" if nr else ""))

    def _one(t):
        return comprehend_thread(archive, t, backend, profile, model=getattr(backend, "model", "?"))

    if max(1, int(concurrency or 1)) <= 1:
        for t in todo:                                  # sequential (default; unchanged behaviour)
            if should_stop():
                break
            progress(done=done, total=total, current=(t.get("subject") or t.get("thread_id") or ""),
                     errors=errors, last_done=last_done)
            est = estimate_thread_tokens(archive, t)
            if done > 0 and spent + est > budget_tokens:
                log(f"Budget reached (~{spent} est. tokens) — stopping; re-run to continue.")
                break
            try:
                _record(t, est, _one(t), None)
            except Exception as e:
                _record(t, est, None, e)
    else:                                               # concurrent — per-thread work is independent + I/O-bound
        work, acc = [], 0                               # budget pre-bounded up-front (always allow the first)
        for t in todo:
            est = estimate_thread_tokens(archive, t)
            if work and acc + est > budget_tokens:
                log(f"Budget reached (~{acc} est. tokens) — stopping; re-run to continue.")
                break
            work.append((t, est)); acc += est

        def _job(item):
            t, est = item
            if should_stop():
                return None
            try:
                return (t, est, _one(t), None)
            except Exception as e:                      # report per-thread; keep the rest going
                return (t, est, None, e)

        progress(done=0, total=total, current="", errors=0, last_done="")
        # Results are folded in THIS thread as they complete, so the counters + _append need no lock;
        # only the backend's token tally is touched by workers (made thread-safe in the comprehender).
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(concurrency)) as ex:
            for fut in concurrent.futures.as_completed([ex.submit(_job, it) for it in work]):
                r = fut.result()
                if r is None:
                    continue
                _record(*r)
                progress(done=done, total=total, current="", errors=errors, last_done=last_done)

    progress(done=done, total=total, current="", errors=errors, last_done=last_done)
    return {"done": done, "pending": len(backlog) - done, "total": total, "backlog": len(backlog),
            "errors": errors, "needs_review": needs, "spent": spent, "last_done": last_done,
            "tokens": getattr(backend, "tokens", 0), "cost": getattr(backend, "cost", 0.0), "blocked": False}
