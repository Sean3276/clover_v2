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
_WITHIN = re.compile(r"\bwithin\s+(\d{1,3})\s+(business day|working day|day|week|month)s?"
                     r"(?:\s+(?:of|from|after)\s+([^.,;:\n]{2,40}))?\b", re.I)
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
        anc = (m.group(3) or "").strip()
        # 'within N days of <service/order/the hearing>' = an EXTERNAL anchor -> treat as event-relative
        # (statutory-safe); a bare 'within N days' (or 'of receipt/now') runs from receipt/now.
        external = bool(anc) and "receipt" not in anc.lower() and anc.lower() not in ("now", "today")
        out.append({"raw": m.group(0), "kind": ("relative" if external else "within"),
                    "n": int(m.group(1)), "unit": _unit(m.group(2)),
                    "direction": "after", "anchor": anc or "now/receipt"})
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


def _add_business_days(d: date, n: int, holidays=()) -> date:
    """Add n business days (weekends + any configured holidays skipped)."""
    hol = set(holidays or ())
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5 and d.isoformat() not in hol:    # Mon-Fri and not a holiday
            remaining -= 1
    return d


_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
             "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}
_WEEKDAY_RE = re.compile(r"\b(next\s+|this\s+|by\s+|on\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_NTH_RE = re.compile(r"\b(?:by\s+|on\s+|the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.I)
_RECUR = re.compile(r"\b(daily|every day|weekly|every week|fortnightly|biweekly|monthly|every month|"
                    r"quarterly|every quarter|annually|yearly|every year)\b", re.I)
_RECUR_CN = ["每天", "每日", "每周", "每月", "每季", "每年", "按月", "按季"]


def recurrence(text: str) -> str:
    """A cadence label ('daily/weekly/monthly/quarterly/annual') if the text states a recurring duty, else ''."""
    t = (text or "").lower()
    m = _RECUR.search(t)
    if m:
        w = m.group(1)
        return ("weekly" if "week" in w or "fortnight" in w or "biweek" in w else
                "monthly" if "month" in w else "quarterly" if "quarter" in w else
                "annual" if ("year" in w or "annual" in w) else "daily")
    for c in _RECUR_CN:
        if c in (text or ""):
            return ("weekly" if c in ("每周",) else "monthly" if c in ("每月", "按月") else
                    "quarterly" if c in ("每季", "按季") else "annual" if c == "每年" else "daily")
    return ""


def resolve_colloquial(due_raw: str, anchor) -> str | None:
    """Resolve colloquial deadlines against the citing message's date (anchor): EOD/COB/today, tomorrow,
    a weekday ('by Friday'/'next Friday'), or a bare day-of-month ('by the 20th'). None if not colloquial."""
    if not anchor:
        return None
    if isinstance(anchor, str):
        try:
            anchor = date.fromisoformat(anchor[:10])
        except ValueError:
            return None
    low = (due_raw or "").lower()
    if any(k in low for k in ("eod", "cob", "end of day", "by close", "today", "end of business")):
        return anchor.isoformat()
    if "tomorrow" in low:
        return (anchor + timedelta(days=1)).isoformat()
    mw = _WEEKDAY_RE.search(low)
    if mw:
        target = _WEEKDAYS[mw.group(2)]
        ahead = (target - anchor.weekday()) % 7          # 0 = the anchor day itself
        if (mw.group(1) or "").strip() == "next":         # 'next <weekday>' = the FOLLOWING week
            ahead += 7
        return (anchor + timedelta(days=ahead)).isoformat()
    mn = _NTH_RE.search(low)
    if mn:
        day = int(mn.group(1))
        if 1 <= day <= 31:
            import calendar as _cal
            mo, yr = anchor.month, anchor.year
            if day < anchor.day:                          # already past this month -> next month
                mo += 1
                if mo > 12:
                    mo, yr = 1, yr + 1
            day = min(day, _cal.monthrange(yr, mo)[1])
            return date(yr, mo, day).isoformat()
    return None


def resolve(rel: dict, anchor, holidays=()) -> str | None:
    """Compute the ISO due date for a relative deadline given an anchor date (ISO str or date),
    or None when no anchor / not computable. ``holidays`` (ISO strings) are skipped for business days."""
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
        return _add_business_days(anchor, n, holidays).isoformat()
    if unit == "weeks":
        return (anchor + timedelta(weeks=n)).isoformat()
    if unit == "months":
        return _add_months(anchor, n).isoformat()
    return None


def with_due(rel: dict, anchor=None) -> dict:
    """rel + due_canonical (when anchor known) + pending flag (resolve later when the anchor lands)."""
    iso = resolve(rel, anchor) if anchor else None
    return {**rel, "due_canonical": iso, "pending": iso is None}
