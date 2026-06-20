"""T1 deterministic recall-floor extractors (no AI).

The "catchable" atom classes the measurement bar anchors on — reference numbers, dates,
amounts, and email parties — pulled by pure regex/parsing. This is the recall FLOOR and a
regression seed (CLOVER_V2_COMPREHENSION_SPEC §2a/§6): it can prove the AI dropped something
a rule would catch, but it CANNOT prove completeness, and it is explicitly NOT an action-item
extractor (action items are semantic). Precision is refined against the gold set — treat output
as candidates. All output is canonicalised so the scorer can compare by value, not surface form.
"""
from __future__ import annotations

import re

# ── reference numbers: RFI-12, NCR 07, SOI-018, CR-59, VO09, EOT-5, MCI-18 ──────────
# Prefix is UPPERCASE 2–6 letters (the convention for project codes); lowercase variants
# are a known floor gap to refine against gold. Leading zeros are stripped so the same real
# reference written two ways collapses to one (MCI-018 == MCI-18, SOI-018 == SOI-18).
_REF = re.compile(r"\b([A-Z]{2,6})[-/ ]?(\d{1,6})\b")
# common letter+digit tokens that are NOT reference numbers — notably currency codes, which
# otherwise mis-read "SGD 1,000" -> "SGD-1". (Amounts are handled by extract_amounts.)
_REF_STOP = {"ISO", "COVID", "MP", "H", "CO",
             "SGD", "USD", "EUR", "GBP", "MYR", "RMB", "CNY", "RM", "US", "EU"}
# also catch a word separator between prefix and number: "TQ no. 3" / "CTR No. 5" -> TQ-3 / CTR-5
_REF_NO = re.compile(r"\b([A-Z]{2,6})\s+[Nn]o\.?\s*(\d{1,6})\b")


def extract_refs(text: str) -> list[str]:
    """Canonical ``PREFIX-N`` (uppercase prefix, separator normalised, leading zeros stripped)."""
    out: dict[str, None] = {}
    for rx in (_REF, _REF_NO):
        for m in rx.finditer(text or ""):
            prefix = m.group(1).upper()
            if prefix in _REF_STOP:
                continue
            out[f"{prefix}-{int(m.group(2))}"] = None      # leading zeros stripped: MCI-018 == MCI-18
    return list(out)


# ── dates -> ISO YYYY-MM-DD ─────────────────────────────────────────────────────────
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON.update({"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
             "august": 8, "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12})

_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})\s*[-/ ]?\s*([A-Za-z]{3,9})\.?,?\s*(\d{4})\b")   # 14 Mar 2025
_DATE_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")             # March 5, 2025
_DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")                            # 03/04/2025 (day-first)
_DATE_DOT = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")                          # 15.06.2026 (day-first)
# quoted-reply / header lines whose dates are reply machinery (the timestamp of a quoted email),
# not content the to-do list needs — their dates are skipped.
_NOISE_LINE = re.compile(r"(?i)^\s*(on\b.*\bwrote:|from:|sent:|to:|cc:|date:|subject:|-{2,}\s*original message)")


def _iso(y, mo, d) -> str | None:
    try:
        y, mo, d = int(y), int(mo), int(d)
    except (TypeError, ValueError):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def extract_dates(text: str) -> list[str]:
    """Canonical ISO dates (deduped, sorted). ``DD/MM/YYYY`` is read **day-first**. Dates on
    quoted-reply / header lines (``On … wrote:``, ``Sent:``, ``Date:`` …) are skipped — they are
    reply machinery, not content the to-do list needs."""
    out: dict[str, None] = {}

    def add(v):
        if v:
            out[v] = None

    for line in (text or "").splitlines() or [text or ""]:
        if _NOISE_LINE.match(line):
            continue
        for m in _DATE_ISO.finditer(line):
            add(_iso(m.group(1), m.group(2), m.group(3)))
        for m in _DATE_DMY.finditer(line):
            mo = _MON.get(m.group(2).lower())
            if mo:
                add(_iso(m.group(3), mo, m.group(1)))
        for m in _DATE_MDY.finditer(line):
            mo = _MON.get(m.group(1).lower())
            if mo:
                add(_iso(m.group(3), mo, m.group(2)))
        for m in _DATE_NUM.finditer(line):
            add(_iso(m.group(3), m.group(2), m.group(1)))      # day-first: D/M/Y
        for m in _DATE_DOT.finditer(line):
            add(_iso(m.group(3), m.group(2), m.group(1)))      # day-first: D.M.Y
    return sorted(out)


# ── amounts -> {currency, value} (currency-anchored to avoid catching every number) ──
_CUR = {"$": "USD", "us$": "USD", "usd": "USD", "s$": "SGD", "sgd": "SGD", "€": "EUR",
        "eur": "EUR", "£": "GBP", "gbp": "GBP", "myr": "MYR", "rm": "MYR", "rmb": "CNY",
        "cny": "CNY", "¥": "CNY"}
_AMT = re.compile(
    r"(US\$|S\$|RM|SGD|USD|EUR|GBP|MYR|RMB|CNY|[$€£¥])\s?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(million|billion|thousand|mn|bn|m|k)?\b", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def _num_str(n: float) -> str:
    return str(int(n)) if n == int(n) else ("%.2f" % n).rstrip("0").rstrip(".")


def extract_amounts(text: str) -> list[dict]:
    """Canonical ``[{currency, value}]`` — only currency-marked numbers count (a recall floor,
    not a number-grab). ``$2m`` -> ``{USD, 2000000}``; ``SGD 1,234.50`` -> ``{SGD, 1234.5}``."""
    out, seen = [], set()
    for m in _AMT.finditer(text or ""):
        cur = _CUR.get(m.group(1).lower().replace(" ", ""), m.group(1).upper())
        num = float(m.group(2).replace(",", ""))
        mult = _MULT.get((m.group(3) or "").lower())
        if mult:
            num *= mult
        val = _num_str(num)
        if (cur, val) not in seen:
            seen.add((cur, val))
            out.append({"currency": cur, "value": val})
    return out


# ── email parties ───────────────────────────────────────────────────────────────────
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_emails(text: str) -> list[str]:
    return sorted({m.group(0).lower().rstrip(".") for m in _EMAIL.finditer(text or "")})


def extract_atoms(text: str) -> dict:
    """All T1 atom classes for a blob of text — the deterministic recall floor for one source."""
    return {"refs": extract_refs(text), "dates": extract_dates(text),
            "amounts": extract_amounts(text), "emails": extract_emails(text)}
