"""F2: cross-thread "Need You" to-do inbox — deterministic aggregation + triage ranking of the
operator's own open obligations across every comprehended thread."""
from clover.todo import rank_actions


def _rec(subject, actions):
    return {"thread_id": "t-" + subject, "subject": subject, "actions": actions}


def test_rank_orders_overdue_then_soonest_then_undated():
    recs = [
        _rec("A", [{"action": "soon", "is_mine": True, "due_canonical": "2026-06-25", "status": "open"}]),
        _rec("B", [{"action": "overdue", "is_mine": True, "due_canonical": "2026-06-10", "status": "open"}]),
        _rec("C", [{"action": "undated", "is_mine": True, "due_canonical": "", "status": "open"}]),
    ]
    out = rank_actions(recs, "2026-06-21")
    assert [i["action"] for i in out] == ["overdue", "soon", "undated"]
    assert out[0]["overdue"] is True and out[0]["days_left"] == -11


def test_rank_excludes_counterparty_and_completed_keeps_unknown_owner():
    recs = [_rec("A", [
        {"action": "theirs", "is_mine": False, "status": "open"},
        {"action": "done", "is_mine": True, "status": "done"},
        {"action": "mine", "is_mine": True, "status": "open"},
        {"action": "maybe", "is_mine": None, "status": "open"},   # unknown owner -> surfaced (recall-first)
    ])]
    out = [i["action"] for i in rank_actions(recs, "2026-06-21")]
    assert "theirs" not in out and "done" not in out
    assert "mine" in out and "maybe" in out


def test_rank_tiebreaks_undated_by_priority():
    recs = [_rec("A", [
        {"action": "low", "is_mine": True, "priority": "low", "status": "open"},
        {"action": "high", "is_mine": True, "priority": "high", "status": "open"},
    ])]
    assert [i["action"] for i in rank_actions(recs, "2026-06-21")] == ["high", "low"]
