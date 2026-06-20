"""Relative / external-context DEADLINE resolver (deterministic, no AI).

20-user review P0 #4: time-relative obligations ("Net-30", "14 days after service", "2 weeks before
the hearing", "within 30 days") are the biggest silent-miss class — today they land as raw strings.
This detects them, tags magnitude + unit + direction + anchor event, and computes a concrete ISO due
date when the anchor date is known; otherwise marks it pending (resolve once the anchor lands).
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_NET = re.compile(r"\bnet[\s-]?(\d{1,3})\b", re.I)
_WITHIN = re.compile(r"\bwithin\s+(\d{1,3})\s+(business day|working day|day|week|month)s?\b", re.I)
_REL = re.compile(r"\b(\d{1,3})\s+(business day|working day|day|week|month)s?\s+"
                  r"(after|before|from|prior to|following)\s+([^.,;:\n]{2,40})", re.I)
_CN_WITHIN = re.compile(r"(\d{1,3})\s*个?\s*(工作日|天|日|周|月)\s*内")
_CN_AFTER = re.compile(r"(?:收到后|之后)\s*(\d{1,3})\s*个?\s*(工作日|天|日|周|月)")


def _unit(raw: str) -> str:
    raw = raw.lower()
    if "business" in raw or "working" in raw or raw == "工作日":
        return "businessdays"          # counted skipping weekends (holidays still need a calendar)
    if "week" in raw or raw == "周":
        return "weeks"
    if "month" in raw or raw == "月":
        return "months"
    return "days"      # day / 天 / 日


def find_relative_deadlines(text: str) -> list[dict]:
    """Detect relative/external-context deadlines. Returns [{raw, kind, n, unit, direction, anchor}]."""
    t = text or ""
    out = []
    for m in _NET.finditer(t):
        out.append({"raw": m.group(0), "kind": "net", "n": int(m.group(1)), "unit": "days",
                    "direction": "after", "anchor": "invoice/receipt"})
    for m in _WITHIN.finditer(t):
        out.append({"raw": m.group(0), "kind": "within", "n": int(m.group(1)), "unit": _unit(m.group(2)),
                    "direction": "after", "anchor": "now/receipt"})
    for m in _REL.finditer(t):
        d = m.group(3).lower()
        out.append({"raw": m.group(0).strip(), "kind": "relative", "n": int(m.group(1)),
                    "unit": _unit(m.group(2)), "direction": "before" if d in ("before", "prior to") else "after",
                    "anchor": m.group(4).strip()})
    for m in _CN_WITHIN.finditer(t):
        out.append({"raw": m.group(0), "kind": "within", "n": int(m.group(1)), "unit": _unit(m.group(2)),
                    "direction": "after", "anchor": "现在/收到"})
    for m in _CN_AFTER.finditer(t):
        out.append({"raw": m.group(0), "kind": "relative", "n": int(m.group(1)), "unit": _unit(m.group(2)),
                    "direction": "after", "anchor": "收到"})
    seen, uniq = set(), []
    for d in out:
        if d["raw"] not in seen:
            seen.add(d["raw"])
            uniq.append(d)
    return uniq


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _add_business_days(d: date, n: int) -> date:
    """Add n business days (weekends skipped). Holidays are NOT applied — callers flag that caveat."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:            # Mon-Fri
            remaining -= 1
    return d


def resolve(rel: dict, anchor) -> str | None:
    """Compute the ISO due date for a relative deadline given an anchor date (ISO str or date),
    or None when no anchor / not computable."""
    if not anchor:
        return None
    if isinstance(anchor, str):
        try:
            anchor = date.fromisoformat(anchor[:10])
        except ValueError:
            return None
    n = int(rel.get("n", 0)) * (-1 if rel.get("direction") == "before" else 1)
    unit = rel.get("unit", "days")
    if unit == "days":
        return (anchor + timedelta(days=n)).isoformat()
    if unit == "businessdays":
        return _add_business_days(anchor, n).isoformat()
    if unit == "weeks":
        return (anchor + timedelta(weeks=n)).isoformat()
    if unit == "months":
        return _add_months(anchor, n).isoformat()
    return None


def with_due(rel: dict, anchor=None) -> dict:
    """rel + due_canonical (when anchor known) + pending flag (resolve later when the anchor lands)."""
    iso = resolve(rel, anchor) if anchor else None
    return {**rel, "due_canonical": iso, "pending": iso is None}
