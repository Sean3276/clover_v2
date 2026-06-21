"""Punch-list #1 — the WIRING SEAM. The no-miss safety nets the pipeline computes (implied-obligation
floor, soft-ask floor, the open-items ledger, owner-history) must be load-bearing: gated, reconciled,
and rendered — not left inert in verified{}. (raised by Sven, Deepa, Grace, Priya, Ben, Walter)."""
from clover import comprehend as cp
from clover import threads as th
from clover.comprehenders import StubComprehender
from clover.profiles import get_profile
from email.message import EmailMessage
import json


def _eml(tmp, folder, key, mid, irt=None, text="body"):
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"
    if irt:
        m["In-Reply-To"] = f"<{irt}>"
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content(text)
    rel = f"{folder}/{key}.eml"
    (tmp / folder).mkdir(parents=True, exist_ok=True)
    (tmp / rel).write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "folder": folder, "key": key, "path": rel,
                            "date": m["Date"], "from": m["From"], "subject": "Hi", "size": 1}) + "\n")


def _one_thread(tmp, body_a="hello", body_b="reply"):
    _eml(tmp, "INBOX", "1", "a@x", text=body_a)
    _eml(tmp, "INBOX", "2", "b@x", irt="a@x", text=body_b)
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


# ---- (a)+(b) implied floor is load-bearing: backfilled as a review action AND gates --------------
def test_implied_uncovered_backfilled_and_gates(tmp_path):
    t = _one_thread(tmp_path,
                    body_a="The fire-rating cert is required before handover. Handover is on 20 June 2026.",
                    body_b="ok")
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=True)
    imp = [a for a in rec["actions"] if a.get("implied")]
    assert imp and "cert" in imp[0]["action"].lower()       # the dropped duty became a real to-do row
    assert imp[0]["confidence"] == "review"
    assert imp[0]["due_canonical"] == "2026-06-20"           # date carried from the floor pairing
    assert rec["verified"]["implied_uncovered"]              # tracked
    assert rec["qaqc"]["needs_review"] is True               # and it forces human review


def test_implied_covered_by_ai_action_not_backfilled(tmp_path):
    t = _one_thread(tmp_path,
                    body_a="The fire-rating cert is required before handover. Handover is on 20 June 2026.",
                    body_b="ok")
    stub = StubComprehender(responses={"actions": {"actions": [
        {"action": "Provide the fire-rating cert before handover", "source": ["M1"],
         "quote": "The fire-rating cert is required before handover"}]}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=True)
    assert not [a for a in rec["actions"] if a.get("implied")]     # already covered -> no duplicate
    assert not rec["verified"]["implied_uncovered"]


# ---- (c) open-items ledger <-> actions reconciliation ------------------------------------------
def test_orphan_open_item_gates_review(tmp_path):
    t = _one_thread(tmp_path, body_a="x", body_b="y")
    led = '{"open_items":[{"id":"o1","matter":"EOT-05 extension grant pending","status":"open"}]}'
    stub = StubComprehender(responses={
        "comprehend": lambda p: "Walk [M1].\n<<<LEDGER>>>\n" + led + "\n<<<END>>>"})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=True)
    assert rec["open_items"] and not [a for a in rec["actions"] if not a.get("implied")]
    assert rec["verified"]["open_items_unreconciled"]            # the open item maps to no action
    assert rec["qaqc"]["needs_review"] is True


def test_covered_open_item_not_flagged(tmp_path):
    t = _one_thread(tmp_path, body_a="x", body_b="y")
    led = '{"open_items":[{"id":"o1","matter":"EOT-05 extension grant","status":"open"}]}'
    stub = StubComprehender(responses={
        "comprehend": lambda p: "Walk [M1].\n<<<LEDGER>>>\n" + led + "\n<<<END>>>",
        "actions": {"actions": [{"action": "Chase the EOT-05 extension grant", "refs": ["EOT-5"],
                                 "source": ["M1"]}]}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=True)
    assert not rec["verified"]["open_items_unreconciled"]        # shares the EOT-5 ref -> reconciled


def test_done_open_item_never_flagged(tmp_path):
    t = _one_thread(tmp_path, body_a="x", body_b="y")
    led = '{"open_items":[{"id":"o1","matter":"EOT-05 grant","status":"done"}]}'
    stub = StubComprehender(responses={
        "comprehend": lambda p: "Walk [M1].\n<<<LEDGER>>>\n" + led + "\n<<<END>>>"})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=True)
    assert not rec["verified"]["open_items_unreconciled"]        # already done -> nothing to chase


# ---- (d) UI: owner_history chain + soft-ask disclosure are rendered ----------------------------
def test_owner_history_and_soft_uncovered_render():
    import app.main as m
    tpl = m.templates.env.get_template("thread_view.html")
    comp = {
        "model": "stub", "method": "stitched",
        "classification": {"domain": "d", "category": "c", "consensus": "agreed", "confidence": 0.9},
        "actions": [{"action": "Sign the deed", "is_mine": True, "priority": "normal", "implied": False,
                     "due_canonical": "", "due_raw": "", "due_pending": False, "recurrence": "",
                     "status": "open", "refs": [], "confidence": "high", "quote_unverified": False,
                     "source": "[M3]", "quote": "q", "about": "",
                     "owner_history": [{"owner": "carol@x.com", "source": "M1"},
                                       {"owner": "sam@x.com", "source": "M3"}]}],
        "verified": {"facts_ok": True, "implied_candidates": [],
                     "action_floor_soft_uncovered": ["please look at the leak when you get a chance"]},
    }

    class Req:
        scope = {"type": "http"}
        query_params = {}
    html = tpl.render(request=Req(), comp=comp, stale=False, ai_ready=True, back=None,
                      link_saved=[], link_pending=0, link_needs_confirm=0, taxonomy=["d"],
                      thread={"subject": "S", "n": 3, "participants": ["a"], "start": "2026-01-01",
                              "end": "2026-01-03", "thread_id": "t1"})
    assert "carol@x.com" in html and "sam@x.com" in html and "→" in html   # hand-off chain rendered
    assert "soft/indirect" in html and "leak" in html                       # soft disclosure rendered
