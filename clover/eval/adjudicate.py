"""A/B comparison + multi-role adjudication.

Run BOTH witnesses on an email — the deterministic rule-finder (A, `extractors`) and the AI
extractor (B, the comprehension facts) — and COMPARE. Where they AGREE, trust it for free (two
independent methods concurring is strong evidence). Where they DISAGREE, ask several independent
reviewer ROLES to decide which is correct; the verdicts feed back to improve BOTH (tighten A's
rules, improve B's prompts), and the disagreement set is the high-value sample for the answer key.

Only disagreements cost an AI review — agreements are free — so cost scales with conflict, not volume.
"""
from __future__ import annotations

from .scorer import _ai_sets, _source_sets

REVIEWER_ROLES = ("strict", "domain", "skeptic")
_CLASSES = ("refs", "dates", "amounts")

_ROLE_INSTR = {
    "strict": "Be strict: confirm only if the value is unambiguously and explicitly present in the text.",
    "domain": "Use domain judgment: is this a genuine such item as used in this kind of correspondence?",
    "skeptic": "Be skeptical of coincidental pattern matches: confirm only if it is truly a meaningful item.",
}
_KIND_NAME = {"refs": "reference number", "dates": "date", "amounts": "monetary amount"}

VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"present": {"type": "boolean"}, "is_real": {"type": "boolean"},
                   "reason": {"type": "string"}},
    "required": ["present", "is_real"],
}


def compare(source_text: str, ai_facts: dict) -> dict:
    """Per class: {agree, a_only (rule found, AI didn't), b_only (AI found, rule didn't)}."""
    src, ai = _source_sets(source_text), _ai_sets(ai_facts)
    out: dict = {}
    for k in _CLASSES:
        a, b = src[k], ai[k]
        out[k] = {"agree": sorted(a & b), "a_only": sorted(a - b), "b_only": sorted(b - a)}
    return out


def disagreements(cmp: dict) -> list[dict]:
    """Flatten compare() into the items that need a review (each found by exactly one witness)."""
    items = []
    for kind, d in cmp.items():
        items += [{"kind": kind, "value": v, "found_by": "A"} for v in d["a_only"]]
        items += [{"kind": kind, "value": v, "found_by": "B"} for v in d["b_only"]]
    return items


def _review_prompt(item: dict, source_text: str, role: str) -> str:
    kind = _KIND_NAME.get(item["kind"], item["kind"])
    return (f"You are reviewing whether a candidate {kind} is genuinely present and meaningful in an email.\n"
            f"{_ROLE_INSTR.get(role, '')}\n\nCandidate {kind}: {item['value']}\n\nEmail text:\n"
            f"{(source_text or '')[:6000]}\n\nAnswer: present (does this exact value appear in the text?) "
            f"and is_real (is it a genuine {kind}, not a coincidental match?).")


def adjudicate_item(item: dict, source_text: str, backend, roles=REVIEWER_ROLES) -> dict:
    """Ask each reviewer role; majority decides if the atom is genuinely real, which fixes the verdict."""
    yes = 0
    for role in roles:
        v = backend.generate("review", _review_prompt(item, source_text, role), schema=VERDICT_SCHEMA) or {}
        if v.get("present") and v.get("is_real"):
            yes += 1
    real = yes * 2 > len(roles)                          # strict majority of roles
    if real:
        verdict = "B_missed" if item["found_by"] == "A" else "A_gap"       # the finder was right; other missed
    else:
        verdict = "A_false_positive" if item["found_by"] == "A" else "B_hallucination"
    return {"item": item, "real": real, "verdict": verdict, "votes_real": yes, "roles": list(roles)}


def adjudicate(source_text: str, ai_facts: dict, backend, roles=REVIEWER_ROLES) -> dict:
    """Full A/B pass: compare, review every disagreement with the role panel, summarise the
    feedback signals (what to improve in A vs B). Agreements are never reviewed."""
    cmp = compare(source_text, ai_facts)
    items = disagreements(cmp)
    results = [adjudicate_item(it, source_text, backend, roles) for it in items]

    def count(v):
        return sum(1 for r in results if r["verdict"] == v)

    return {"compare": cmp, "results": results, "summary": {
        "disagreements": len(items),
        "confirmed_real": sum(1 for r in results if r["real"]),
        "improve_A": count("A_false_positive") + count("A_gap"),    # tighten / extend the rules
        "improve_B": count("B_missed") + count("B_hallucination"),  # AI dropped a real one / hallucinated
    }}
