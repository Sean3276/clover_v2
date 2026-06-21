"""F2: cross-thread "Need You" to-do inbox (Phase-4 surface).

The comprehension pass already computes a per-thread to-do list (`actions`), but it lived in no view.
This is the deterministic render of that list across every comprehended thread: flatten the operator's
OWN open obligations, annotate each with days-left/overdue, and rank by urgency. No AI — pure triage.
Recall-first: unknown-owner actions (is_mine None) are surfaced too, never silently dropped."""
from __future__ import annotations

from datetime import date

_PRIO = {"high": 0, "normal": 1, "low": 2}
_CLOSED = ("done", "superseded")


def _days_left(due: str, today: str):
    try:
        return (date.fromisoformat(due) - date.fromisoformat(today)).days
    except Exception:
        return None


def rank_actions(records: list[dict], today: str) -> list[dict]:
    """Flatten every record's actions to the operator's OPEN obligations (is_mine != False, status not
    done/superseded), each annotated with days_left/overdue, ranked: dated soonest-first (so the most
    overdue lead), then undated by priority. Each item links back to its thread for one-click drill-down."""
    items = []
    for r in records:
        subj = r.get("subject") or r.get("title") or "(no subject)"
        tid = r.get("thread_id") or r.get("root_id") or ""
        for a in (r.get("actions") or []):
            if a.get("is_mine") is False:
                continue
            if str(a.get("status") or "open").lower() in _CLOSED:
                continue
            due = a.get("due_canonical") or ""
            dl = _days_left(due, today) if due else None
            items.append({
                "thread_id": tid,
                "subject": subj,
                "action": a.get("action") or "",
                "about": a.get("about") or "",
                "due_canonical": due,
                "due_raw": a.get("due_raw") or "",
                "days_left": dl,
                "overdue": dl is not None and dl < 0,
                "priority": str(a.get("priority") or "normal").lower(),
                "status": str(a.get("status") or "open").lower(),
                "recurrence": a.get("recurrence") or "",
                "is_mine": a.get("is_mine"),
                "confidence": a.get("confidence") or "",
                "source": a.get("source") or "",
                "owner_history": a.get("owner_history") or [],
                "implied": bool(a.get("implied")),
            })

    def key(it):
        dl = it["days_left"]
        has_due = dl is not None
        return (0 if has_due else 1, dl if has_due else 0, _PRIO.get(it["priority"], 1), it["subject"])

    return sorted(items, key=key)


def needyou_items(archive, today: str) -> list[dict]:
    """The ranked Need-You inbox for an archive (latest comprehension per thread)."""
    from .comprehend import latest_by_root
    return rank_actions(list(latest_by_root(archive).values()), today)
