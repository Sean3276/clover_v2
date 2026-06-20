"""Deterministic scorer (no AI).

Two jobs, both pure and reproducible (CLOVER_V2_COMPREHENSION_SPEC §2a/§5/§6):

1. `crosscheck_floor` — the §2a no-miss FLOOR: every T1 atom present in the SOURCE but absent
   from the AI's output is a HARD MISS. Needs no human gold — it's the deterministic safety net.
2. `score_against_gold` — recall per class vs the human-confirmed gold required-set, plus the
   AND-gated per-thread `zero_miss` headline (CLOVER-NOMISS).

Both canonicalise BOTH sides through the same `extractors` normalizers, so a value match is by
canonical value (14 Mar 2025 == 2025-03-14, "SGD 1,000" == "SGD 1000"), never by surface form.
"""
from __future__ import annotations

from .extractors import extract_amounts, extract_atoms, extract_dates, extract_emails, extract_refs

_FLOOR = ("refs", "dates", "amounts")          # parties come from headers, not a text scan of the body


def _amt_str(a: dict) -> str:
    return f"{a['currency']} {a['value']}"


def _source_sets(text: str) -> dict:
    a = extract_atoms(text or "")
    return {"refs": set(a["refs"]), "dates": set(a["dates"]),
            "amounts": {_amt_str(x) for x in a["amounts"]}, "emails": set(a["emails"])}


def _canon(values, kind: str) -> set:
    """Canonicalise a list of free-form AI output strings into the comparable canonical set."""
    text = " ; ".join(str(v) for v in (values or []))
    if kind == "refs":
        return set(extract_refs(text))
    if kind == "dates":
        return set(extract_dates(text))
    if kind == "amounts":
        return {_amt_str(x) for x in extract_amounts(text)}
    if kind == "emails":
        return set(extract_emails(text))
    return {str(v).strip().lower() for v in (values or [])}      # generic (e.g. action text)


def _ai_sets(ai_facts: dict) -> dict:
    f = ai_facts or {}
    return {"refs": _canon(f.get("refs"), "refs"),
            "dates": _canon(f.get("dates"), "dates"),
            "amounts": _canon(f.get("amounts"), "amounts"),
            "emails": _canon(f.get("parties") or f.get("emails"), "emails")}


def crosscheck_floor(source_text: str, ai_facts: dict) -> dict:
    """§2a floor: T1 atoms in SOURCE but missing from AI output = hard misses. No gold needed."""
    src, ai = _source_sets(source_text), _ai_sets(ai_facts)
    out: dict = {}
    for k in _FLOOR:
        req, got = src[k], ai[k]
        out[k] = {"required": len(req), "covered": len(req & got), "missed": sorted(req - got)}
    out["hard_misses"] = sum(len(out[k]["missed"]) for k in _FLOOR)
    return out


def score_against_gold(gold_atoms: dict, ai_facts: dict) -> dict:
    """Recall per class vs the human-confirmed gold + the per-thread AND-gated `zero_miss` headline.
    `gold_atoms` = {class: [canonical values]}; classes are whatever gold carries (incl. `actions`)."""
    ai = _ai_sets(ai_facts)
    res: dict = {}
    zero_miss = True
    for k, required in (gold_atoms or {}).items():
        required = set(required)
        got = ai.get(k) if k in ai else _canon(_lookup(ai_facts, k), k)
        covered = required & got
        missed = required - covered
        res[k] = {"required": len(required), "covered": len(covered),
                  "missed": sorted(missed),
                  "recall": round(len(covered) / len(required), 4) if required else 1.0}
        if missed:
            zero_miss = False
    res["zero_miss"] = zero_miss
    return res


def _lookup(ai_facts: dict, key: str):
    """Pull an arbitrary class (e.g. `actions`) out of the AI facts for gold classes beyond the floor."""
    return (ai_facts or {}).get(key)
