"""Matters — the Focus + Happenings surfaces, plus the per-user focus learner.

Focus = what needs YOU: your open obligations, items matching your learned focus-keywords, and items you
pinned. Happenings = what's going on: a digest of comprehended threads (others' commitments, decisions,
flagged issues). Pins, important/normal overrides, learned keywords, and the saved layout persist in
matters.json keyed by a STABLE item key so they survive re-comprehension. Deterministic; AI themes layer on top.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

_STORE = "matters.json"
_SUGGEST_THRESHOLD = 4          # learned-keyword score at which an item is suggested for Focus
_GAUGE_HORIZON = 30             # days out at which the urgency gauge is (nearly) empty

# the fact-tags an item can display; domain+category are the calm default, the rest are opt-in per tab
AVAILABLE_TAGS = ("domain", "category", "refs", "amounts", "percents", "facet_tags")
DEFAULT_TAGS = ["domain", "category"]


def gauge_fill(it: dict) -> int:
    """Urgency as a 0-100 fill for the optional timer gauge: overdue = full (red), nearer deadline = fuller,
    far-off = nearly empty, no deadline = empty. Lets a box visually 'fill up' and redden as expiry approaches."""
    d = it.get("days_left")
    if d is None:
        return 0
    if d < 0:
        return 100
    return max(4, min(100, round((_GAUGE_HORIZON - min(d, _GAUGE_HORIZON)) / _GAUGE_HORIZON * 100)))
_NORM = re.compile(r"[^a-z0-9]+")


def _default() -> dict:
    return {"pins": [], "importance": {}, "keywords": [], "themes": [], "layout": {}}


# ---- stable item identity ---------------------------------------------------------------------
def item_key(thread_id, text: str) -> str:
    """A stable id for a to-do/happening: hash(thread + normalized text). Survives re-comprehension so
    pins, importance overrides, and learning stay attached to the same item even as wording shifts slightly."""
    base = f"{thread_id}|{_NORM.sub(' ', (text or '').lower()).strip()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


# ---- prefs store (atomic JSON) ----------------------------------------------------------------
def store_path(archive) -> Path:
    return Path(archive) / _STORE


def read_store(archive) -> dict:
    p = store_path(archive)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {**_default(), **d}
        except Exception:
            pass
    return _default()


def write_store(archive, store: dict) -> None:
    tmp = store_path(archive).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(store_path(archive))


def set_pin(archive, key: str, on: bool) -> None:
    store = read_store(archive)
    pins = set(store.get("pins") or [])
    pins.add(key) if on else pins.discard(key)
    store["pins"] = sorted(pins)
    write_store(archive, store)


def set_importance(archive, key: str, level: str) -> None:
    """Operator override of an item's priority: 'high' (important) or 'normal'. '' clears the override."""
    store = read_store(archive)
    imp = dict(store.get("importance") or {})
    if level in ("high", "normal"):
        imp[key] = level
    else:
        imp.pop(key, None)
    store["importance"] = imp
    write_store(archive, store)


def set_layout(archive, layout: dict) -> None:
    store = read_store(archive)
    store["layout"] = {**(store.get("layout") or {}), **(layout or {})}
    write_store(archive, store)


def set_keywords(archive, keywords: list[dict]) -> None:
    """Operator replaces the learned keyword set (edit/curate). Each item: {term, weight}."""
    clean = [{"term": str(k.get("term") or "").strip().lower(), "weight": int(k.get("weight") or 1)}
             for k in (keywords or []) if str(k.get("term") or "").strip()]
    store = read_store(archive)
    store["keywords"] = clean
    write_store(archive, store)


def set_themes(archive, themes: list[dict]) -> None:
    """Store the AI-inferred focus themes (each {label, terms[]}) — the fuzzy, meaning-level layer above the
    literal keyword learner. Used only to SUGGEST matching items, never to auto-add."""
    clean = []
    for t in (themes or []):
        label = str(t.get("label") or "").strip()
        terms = [str(x).strip().lower() for x in (t.get("terms") or []) if str(x).strip()]
        if label and terms:
            clean.append({"label": label, "terms": terms[:10]})
    store = read_store(archive)
    store["themes"] = clean[:6]
    write_store(archive, store)


# ---- Happenings (the digest of what's going on) -----------------------------------------------
def _fact_tags(rec: dict) -> dict:
    """The selectable fact-tags an item can show (refs / amounts / percents / facet tags) — formatted for
    display. Domain + category are the defaults; these are the extras the operator can switch on per tab."""
    facts = rec.get("facts") or {}
    amounts = [f"{a.get('currency', '')} {a.get('value', '')}".strip() if isinstance(a, dict) else str(a)
               for a in (facts.get("amounts") or [])]
    return {"refs": facts.get("refs") or [], "amounts": amounts,
            "percents": facts.get("percents") or [], "facet_tags": rec.get("tags") or []}


def _soonest_due(rec: dict, today: str) -> tuple[str, int | None]:
    """The thread's nearest open deadline (for expiry sorting/gauge): the earliest due_canonical among its
    not-done actions, with days-left vs today. ('', None) when the matter carries no dated obligation."""
    dues = sorted(a.get("due_canonical") for a in (rec.get("actions") or [])
                  if a.get("due_canonical") and str(a.get("status") or "open").lower() not in ("done", "superseded"))
    if not dues:
        return "", None
    try:
        return dues[0], (date.fromisoformat(dues[0]) - date.fromisoformat(today)).days
    except Exception:
        return dues[0], None


def _has_my_open_obligation(rec: dict) -> bool:
    return any(a.get("is_mine") is True and str(a.get("status") or "open").lower() not in ("done", "superseded")
               for a in (rec.get("actions") or []))


def _item(rec: dict, today: str) -> dict:
    """One uniform matter — the same shape whether it ends up in Focus or Happenings. Line 1 = event
    headline (falls back to subject); Line 2 = the summary TLDR. Carries tags, nearest expiry, review flag."""
    cls = rec.get("classification") or {}
    rid = rec.get("root_id") or rec.get("thread_id")
    due, days = _soonest_due(rec, today)
    return {
        "key": item_key(rid, rec.get("summary") or rec.get("subject")),
        "thread_id": rec.get("thread_id"),
        "subject": rec.get("subject") or "(no subject)",
        "event": rec.get("event") or "",                       # line 1 headline
        "summary": rec.get("summary") or rec.get("abstract") or "",   # line 2 TLDR
        "domain": cls.get("domain") or "",
        "category": cls.get("category") or "",
        "date": (rec.get("source") or {}).get("end") or "",
        "due": due, "days_left": days, "overdue": days is not None and days < 0,
        "needs_review": bool((rec.get("qaqc") or {}).get("needs_review")),
        **_fact_tags(rec),
    }


def items(records: list[dict], today: str, store: dict | None = None) -> list[dict]:
    """Every comprehended matter as a uniform item, flagged: `focus` (the operator ★-selected it — the ONLY
    way into Focus; Clover never auto-adds), `important` (operator override), and — for unstarred items only
    — `suggested` (the learner thinks it may be yours: a matching learned-keyword score or an open obligation
    that looks yours). Suggestions surface an 'Add to Focus?' prompt; they never move the item by themselves."""
    store = store or _default()
    pins = set(store.get("pins") or [])
    importance = store.get("importance") or {}
    keywords = store.get("keywords") or []
    themes = store.get("themes") or []
    out = []
    for rec in records:
        it = _item(rec, today)
        it["gauge"] = gauge_fill(it)
        it["focus"] = it["key"] in pins
        it["important"] = importance.get(it["key"]) == "high"
        it["suggested"] = (not it["focus"]) and (
            _has_my_open_obligation(rec)                                          # looks like your obligation
            or (bool(keywords) and score_item(it, keywords) >= _SUGGEST_THRESHOLD)  # matches learned keywords
            or _matches_theme(it, themes))                                       # matches an AI-inferred theme
        out.append(it)
    return out


def _expiry_key(it: dict) -> tuple:
    d = it.get("days_left")
    return (0 if d is not None else 1, d if d is not None else 0, it.get("subject") or "")


def sort_items(its: list[dict], key: str = "expiry") -> list[dict]:
    """Order matters by the operator's chosen key. 'expiry' (default) = soonest deadline first, undated last;
    'recency' = newest message first; 'domain'/'category' = grouped, then by expiry within the group."""
    if key == "recency":
        return sorted(its, key=lambda it: it.get("date") or "", reverse=True)
    if key == "importance":                              # important first, then soonest-expiry within each group
        return sorted(its, key=lambda it: (0 if it.get("important") else 1, _expiry_key(it)))
    if key in ("domain", "category"):
        return sorted(its, key=lambda it: ((it.get(key) or "~").lower(), _expiry_key(it)))
    return sorted(its, key=_expiry_key)


def _sort_key(store: dict | None) -> str:
    return ((store or {}).get("layout") or {}).get("sort") or "expiry"


def focus(records: list[dict], today: str, store: dict | None = None) -> list[dict]:
    """The Focus list — ONLY the matters the operator has ★-selected — in the saved sort order (default expiry)."""
    return sort_items([it for it in items(records, today, store) if it["focus"]], _sort_key(store))


def happenings(records: list[dict], today: str, store: dict | None = None) -> list[dict]:
    """Everything not in Focus — same display, same saved sort; suggested ones carry the 'Add to Focus?' prompt."""
    return sort_items([it for it in items(records, today, store) if not it["focus"]], _sort_key(store))


# ---- the deterministic focus-keyword learner --------------------------------------------------
_STOP = set("the a an and or to of for in on at is are be by with from this that your our you we "
            "please kindly can could would should will shall need needs ensure make sure".split())


def extract_signals(item: dict) -> list[tuple[str, int]]:
    """Weighted keyword signals from an item the operator promoted to Focus. Structured fields (refs,
    project, category, domain) weigh more than loose salient words — they identify the matter precisely."""
    sig: dict[str, int] = {}

    def bump(term, w):
        term = (term or "").strip().lower()
        if term and term not in _STOP:
            sig[term] = max(sig.get(term, 0), w)

    for r in (item.get("refs") or []):
        bump(r, 3)
    bump(item.get("project"), 3)
    bump(item.get("category"), 2)
    bump(item.get("domain"), 1)
    for p in (item.get("parties") or []):
        bump(p, 2)
    text = f"{item.get('action') or ''} {item.get('summary') or ''} {item.get('subject') or ''}"
    for w in re.findall(r"[a-z][a-z-]{3,}", text.lower()):
        bump(w, 1)
    return sorted(sig.items(), key=lambda kv: -kv[1])[:14]


def _item_terms(item: dict) -> set[str]:
    return {t for t, _ in extract_signals(item)}


def learn_from_pin(archive, item: dict) -> dict:
    """Promoting an item to Focus teaches the learner: accumulate its weighted signals into the keyword set."""
    store = read_store(archive)
    by_term = {k["term"]: k for k in (store.get("keywords") or [])}
    for term, w in extract_signals(item):
        if term in by_term:
            by_term[term]["weight"] = int(by_term[term].get("weight") or 0) + w
        else:
            by_term[term] = {"term": term, "weight": w}
    store["keywords"] = sorted(by_term.values(), key=lambda k: -k["weight"])
    write_store(archive, store)
    return store


def find_item(records: list[dict], today: str, key: str) -> dict | None:
    """Locate a matter by its stable key (so a star action can learn from the real item)."""
    for it in items(records, today):
        if it.get("key") == key:
            return it
    return None


def score_item(item: dict, keywords: list[dict]) -> float:
    """How strongly an item matches the learned focus-keywords — the sum of weights of keywords it carries.
    Used to SUGGEST 'this looks like something you care about' (never to auto-add)."""
    terms = _item_terms(item)
    return float(sum(int(k.get("weight") or 0) for k in (keywords or []) if k.get("term") in terms))


# ---- AI themes: the fuzzy, meaning-level suggestion layer above literal keywords ---------------
_THEMES_SCHEMA = {"themes": [{"label": "str (short theme name)", "terms": ["str (lowercase terms)"]}]}
_THEMES_PROMPT = (
    "You help a user organise their work. Below are the matters they have chosen to FOCUS on. Identify 2-5 "
    "higher-level THEMES that group these by MEANING (not just shared words). For each theme give a short "
    "human label and 4-8 lowercase characterizing terms (words, references, or categories) that a future "
    "related matter would likely contain. Return ONLY JSON of the form {\"themes\":[{\"label\":\"...\","
    "\"terms\":[\"...\"]}]}.\n\nFOCUSED MATTERS:\n")


def _matches_theme(item: dict, themes: list[dict]) -> bool:
    """An item matches an AI-inferred theme when it shares >=2 of the theme's characterizing terms."""
    terms = _item_terms(item)
    return any(len(terms & {str(t).lower() for t in (th.get("terms") or [])}) >= 2 for th in (themes or []))


def infer_themes(records: list[dict], today: str, backend, store: dict | None = None) -> list[dict]:
    """Ask the AI to name the higher-level themes across the matters the operator has STARRED — the fuzzy
    layer the literal keyword learner can't see. Needs >=2 starred items and a backend; returns [] otherwise.
    Output is validated to {label, terms[]} and capped; the caller persists it via set_themes."""
    starred = [it for it in items(records, today, store) if it.get("focus")]
    if len(starred) < 2 or backend is None:
        return []
    digest = "\n".join(
        f"- {it.get('event') or it.get('subject')}: {it.get('summary')} "
        f"[{', '.join(it.get('refs') or [])}] ({it.get('domain')}/{it.get('category')})" for it in starred[:40])
    out = backend.generate("matters_themes", _THEMES_PROMPT + digest, schema=_THEMES_SCHEMA) or {}
    raw = out.get("themes") if isinstance(out, dict) else None
    clean = []
    for t in (raw or []):
        label = str(t.get("label") or "").strip()
        terms = [str(x).strip().lower() for x in (t.get("terms") or []) if str(x).strip()]
        if label and terms:
            clean.append({"label": label, "terms": terms[:10]})
    return clean[:6]
