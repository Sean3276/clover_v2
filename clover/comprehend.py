"""Phase 3 — comprehension pipeline (the first AI phase).

Pure & stateless: comprehend_thread(archive, thread, backend, profile) -> record. Reads Phase-2
threads + the .eml archive; writes <archive>/comprehension.jsonl (idempotent by thread_id).
Whole-thread by default; iterative-refine for giants. Classification via a profile-driven 2-tier
council with a deterministic precedence referee. Facts are verified against the source.
"""
from __future__ import annotations

import json
import re
import time
from html import unescape
from pathlib import Path

from .comprehenders import Comprehender
from .profiles import Profile, get_profile
from .threads import read_threads, render_message

_TAG = re.compile(r"(?s)<[^>]+>")
_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS = re.compile(r"\s+")

_DISTILL_SCHEMA = {"abstract": "str", "summary": "str", "event": "str (<=30 chars)",
                   "facts": {"project": "str", "parties": ["str"], "refs": ["str"],
                             "dates": ["str"], "amounts": ["str"]}}
_CLASSIFY_SCHEMA = {"domain": "str", "category": "str", "confidence": "number 0..1",
                    "dispute": "bool", "dissent": "str"}


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
        out.append(f"From: {b.get('from','')}  Date: {b.get('date','')}\n{_block_text(b)}")
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


def estimate_thread_tokens(archive, thread: dict) -> int:
    """Cheap pre-call estimate from .eml byte size (no body read). Conservative (overestimates)."""
    total = 0
    for m in thread.get("members", []):
        for loc in (m.get("locations") or [])[:1]:
            try:
                total += (Path(archive) / loc["path"]).stat().st_size
            except Exception:
                pass
    return max(1, total // 4)


# ---------------------------------------------------------------- prompts
def _comprehend_prompt(thread, text):
    return (f"You are reading an email thread (subject: {thread.get('subject','')}). Produce a "
            "faithful, chronological comprehension of the WHOLE thread: who wrote, what was "
            "asked / decided / changed, and the current state. Base everything strictly on the "
            "text — do not invent. The thread may mix English and Chinese.\n\nTHREAD:\n" + text)


def _refine_prompt(thread, running, chunk):
    return ("Continue a faithful chronological comprehension of this email thread. Below is the "
            "comprehension so far, then the next messages — update it to include them, accurately, "
            "without inventing.\n\nSO FAR:\n" + (running or "(none yet)")
            + "\n\nNEXT MESSAGES:\n" + "\n\n----\n\n".join(chunk))


def _distill_prompt(comprehension):
    return ("From this thread comprehension produce: an accurate abstract paragraph; a one-line "
            "summary; an event tag of AT MOST 30 characters; and facts grounded ONLY in the "
            "comprehension (project name, parties, references like RFI/EOT/VO numbers, dates, "
            "money amounts). Do not invent.\n\nCOMPREHENSION:\n" + comprehension)


def _classify_prompt(profile: Profile, comprehension, full: bool):
    taxonomy = "; ".join(f"{d}: {', '.join(profile.categories(d))}" for d in profile.domain_names())
    extra = ("\nThis is a DISPUTED case — weigh the costliest-if-wrong reading; a commercial / "
             "contractual matter must never be filed as routine info." if full else "")
    return ("Classify this email thread by MEANING (not keywords). Choose a DOMAIN, then a "
            f"CATEGORY within that domain.\nTaxonomy — {taxonomy}\n"
            f"High-stakes safety-net category: {profile.safety_net}.{extra}\n"
            "Give confidence 0..1 and set dispute=true if genuinely ambiguous.\n\n"
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
    small = backend.generate("classify", _classify_prompt(profile, comprehension, False),
                             schema=_CLASSIFY_SCHEMA) or {}
    domain = small.get("domain") or profile.domain_names()[0]
    category = small.get("category") or ""
    conf = _f(small.get("confidence"))
    dispute = bool(small.get("dispute")) or conf < 0.6 or category not in profile.categories(domain)
    if not dispute:
        return {"domain": domain, "category": category, "confidence": conf,
                "council": "small", "consensus": "unanimous", "dissent": ""}
    # escalate to full council
    full = backend.generate("classify_full", _classify_prompt(profile, comprehension, True),
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
            "council": "full", "consensus": consensus, "dissent": str(full.get("dissent", ""))}


def _verify_facts(facts: dict, thread_text: str) -> tuple[dict, list]:
    """Keep only facts that actually appear in the thread (deterministic). Returns (facts, dropped)."""
    low = (thread_text or "").lower()
    facts = dict(facts or {})
    dropped = []

    def present(v):
        return bool(v) and str(v).strip().lower() in low

    for key in ("refs", "dates", "amounts", "parties"):
        keep = []
        for v in (facts.get(key) or []):
            if present(v):
                keep.append(v)
            elif v:
                dropped.append(f"{key}:{v}")
        facts[key] = keep
    proj = facts.get("project") or ""
    if proj and not present(proj):
        dropped.append(f"project:{proj}")
        facts["project"] = ""
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


# ---------------------------------------------------------------- pipeline
def comprehend_thread(archive, thread: dict, backend: Comprehender, profile: Profile,
                      *, max_chars: int = 120_000, model: str = "?") -> dict:
    msgs = _thread_messages(archive, thread)
    full = "\n\n----\n\n".join(msgs)
    if len(msgs) <= 1 or len(full) <= max_chars:
        method = "whole"
        comprehension = backend.generate("comprehend", _comprehend_prompt(thread, full))
    else:
        method = "refine"
        comprehension = _refine(backend, thread, msgs, max_chars)
    comprehension = (comprehension if isinstance(comprehension, str) else str(comprehension)).strip()

    distilled = backend.generate("distill", _distill_prompt(comprehension), schema=_DISTILL_SCHEMA) or {}
    facts, dropped = _verify_facts(distilled.get("facts") or {}, full)
    classification = _classify(backend, profile, comprehension, full)

    return {
        "thread_id": thread.get("thread_id"), "root_id": thread.get("root_id"),
        "subject": thread.get("subject"),
        "comprehension": comprehension,
        "abstract": str(distilled.get("abstract") or "").strip(),
        "summary": str(distilled.get("summary") or "").strip(),
        "event": str(distilled.get("event") or "").strip()[:30],
        "facts": facts,
        "classification": classification,
        "method": method, "model": model, "profile": profile.name,
        "verified": {"facts_ok": not dropped, "dropped_facts": dropped,
                     "grounded": bool(comprehension)},
    }


def run_comprehension(archive, *, backend: Comprehender, profile: Profile | None = None,
                      budget_tokens: int = 200_000, log=print,
                      should_stop=lambda: False, allowed=lambda: True) -> dict:
    """Comprehend not-yet-done threads (recent first) within a token-estimate budget. Idempotent,
    resumable. `allowed()` is the policy gate (subscription/tier plugs in here later)."""
    archive = Path(archive)
    profile = profile or get_profile()
    if not allowed():
        log("Comprehension blocked by policy gate.")
        return {"done": 0, "pending": 0, "spent": 0, "blocked": True}
    done_ids = comprehended_ids(archive)
    todo = [t for t in read_threads(archive) if t.get("thread_id") not in done_ids]
    spent = done = 0
    for t in todo:
        if should_stop():
            break
        est = estimate_thread_tokens(archive, t)
        if done > 0 and spent + est > budget_tokens:
            log(f"Budget reached (~{spent} est. tokens) — stopping; re-run to continue.")
            break
        try:
            rec = comprehend_thread(archive, t, backend, profile, model=getattr(backend, "model", "?"))
            _append(archive, rec)
            spent += est
            done += 1
            c = rec["classification"]
            log(f"  ✓ {t.get('thread_id')} [{t.get('n')} msgs] -> {c['domain']}/{c['category']} ({c['consensus']})")
        except Exception as e:
            log(f"  ! {t.get('thread_id')}: {type(e).__name__}: {e}")
    return {"done": done, "pending": len(todo) - done, "spent": spent, "blocked": False}
