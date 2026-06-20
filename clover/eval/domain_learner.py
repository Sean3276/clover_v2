"""Domain reference-pattern LEARNER (deterministic, no AI) — the industry-agnostic floor.

The 20-user review's #1 finding: the recall floor's reference patterns were construction-carved
(RFI/NCR/VO/EOT...), so every other field's refs silently dropped (PR#, INV, B/L, MRN, PO, NSF
grant, POL-/CLM-) — a "false-green" floor. The fix (and the industry-agnostic principle in the
roadmap): don't hardcode prefixes — LEARN this user's reference conventions from their own emails.

Scan a corpus, collect "reference-like" tokens, generalise each to a structural SHAPE, and rank by
frequency. The common (prefix, shape) pairs are the user's real ref formats; the operator confirms
them once and they become a per-user allowlist the floor checks first. Works for ANY field.
"""
from __future__ import annotations

import re
from collections import Counter

from .extractors import extract_dates

# a code with internal separators + alnum (INV-2024-001, 2026/QS/BC/PSAC/4018875, v1.2.3, B/L-12),
# OR an UPPERCASE 2-6 letter prefix (+ optional '#' / single space) + digits (PR#123, PO 4500001,
# MRN 0012345). The space variant requires an uppercase prefix so prose ("on 2024", "total 4")
# is not mistaken for a reference; lowercase space-separated refs are a known gap the learner refines.
_CAND = re.compile(r"\b[A-Za-z0-9]+(?:[-/#._][A-Za-z0-9]+)+\b|\b[A-Z]{2,6}#?\s?\d{1,8}\b")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def _is_refish(tok: str) -> bool:
    """A reference candidate has BOTH letters and digits, isn't a bare year, a date, or an email."""
    if not (any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok)):
        return False
    if _YEAR.match(tok) or "@" in tok:
        return False
    return not extract_dates(tok)            # exclude things that are really dates


def candidate_refs(text: str) -> list[str]:
    return [m.group(0).strip() for m in _CAND.finditer(text or "") if _is_refish(m.group(0).strip())]


def shape(token: str) -> str:
    """Structural signature: UPPER run -> A, lower run -> a, digit run -> 9, separators kept.
    'INV-2024-001' -> 'A-9-9'; 'PR#123' -> 'A#9'; 'PO 4500001' -> 'A 9'; 'v1.2.3' -> 'a9.9.9'."""
    s = re.sub(r"[A-Z]+", "A", token)
    s = re.sub(r"[a-z]+", "a", s)
    return re.sub(r"\d+", "9", s)


def _prefix(token: str) -> str:
    m = re.match(r"[A-Za-z]+", token)
    return m.group(0).upper() if m else ""


def learn_ref_patterns(texts, top: int = 20) -> list[dict]:
    """Frequency table of candidate ref (prefix, shape) pairs across the corpus, most common first.
    Returns [{prefix, shape, example, count}] for one-time operator confirmation -> per-user allowlist."""
    keyed: Counter = Counter()
    example: dict = {}
    for t in texts:
        for tok in candidate_refs(t):
            k = (_prefix(tok), shape(tok))
            keyed[k] += 1
            example.setdefault(k, tok)
    return [{"prefix": k[0], "shape": k[1], "example": example[k], "count": n}
            for k, n in keyed.most_common(top)]
