"""Punch-list #4 (operator-inference confidence — most-raised bug, 9 personas) and #11a (QA gates
must fail CLOSED). The dominant-sender operator fallback must not assert who-owes-whom with unearned
confidence; an absent/empty distill-QA payload must not count as a pass."""
from clover import comprehend as cp
from clover import threads as th
from clover.comprehenders import StubComprehender
from clover.profiles import get_profile
from email.message import EmailMessage
import json


def _eml(tmp, key, mid, frm, irt=None, text="body"):
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"
    if irt:
        m["In-Reply-To"] = f"<{irt}>"
    m["From"] = frm; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content(text)
    rel = f"INBOX/{key}.eml"
    (tmp / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp / rel).write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "folder": "INBOX", "key": key, "path": rel,
                            "date": m["Date"], "from": frm, "subject": "Hi", "size": 1}) + "\n")


def _thread(tmp):
    _eml(tmp, "1", "a@x", "a@x.com", text="Please review the draft.")
    _eml(tmp, "2", "b@x", "a@x.com", irt="a@x", text="thanks")   # a@x.com dominates
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


_ACT = {"actions": {"actions": [
    {"action": "Review draft", "owner": "a@x.com", "counterparty": "b@x.com", "source": ["M1"]}]}}


# ---- #4 operator-inference confidence ---------------------------------------------------------
def test_inferred_operator_marks_is_mine_low_confidence(tmp_path):
    t = _thread(tmp_path)
    a = cp.comprehend_thread(tmp_path, t, StubComprehender(responses=_ACT), get_profile(), qaqc=False)["actions"][0]
    assert a["is_mine"] is True                      # dominant-sender guess still resolves the arrow
    assert a["is_mine_confidence"] == "low"          # but flagged as a guess, not asserted


def test_configured_operator_is_high_confidence(tmp_path):
    t = _thread(tmp_path)
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(responses=_ACT), get_profile(),
                               qaqc=False, operator="a@x.com")
    a = rec["actions"][0]
    assert a["is_mine"] is True and a["is_mine_confidence"] == "high"
    assert rec["verified"]["operator_inferred"] is False


def test_inferred_operator_flag_in_verified(tmp_path):
    t = _thread(tmp_path)
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(responses=_ACT), get_profile(), qaqc=False)
    assert rec["verified"]["operator_inferred"] is True


# ---- #11a fail-closed distill QA --------------------------------------------------------------
def test_distill_qa_fails_closed_on_empty_payload(tmp_path):
    t = _thread(tmp_path)
    stub = StubComprehender(responses={"verify_distill": {}})    # backend returned nothing usable
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=True)
    assert rec["qaqc"]["distill_passed"] is False                # absent verification != pass
    assert rec["qaqc"]["needs_review"] is True
