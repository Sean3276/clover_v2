"""Trial bug: a thread with a very large attachment sent the entire text to the AI, blowing the prompt
and timing the call out (so the thread never comprehended). The AI input must be BOUNDED (and flagged),
while the deterministic floor still reads the FULL text so no atom is missed."""
import json
from email.message import EmailMessage

from clover import comprehend as cp
from clover import threads as th
from clover.comprehenders import StubComprehender
from clover.profiles import get_profile


def _one_msg_thread(tmp, body):
    m = EmailMessage()
    m["Message-ID"] = "<a@x>"; m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"; m.set_content(body)
    (tmp / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp / "INBOX/1.eml").write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "a@x", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml",
                            "date": m["Date"], "from": "a@x.com", "subject": "Hi", "size": 1}) + "\n")
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


def test_huge_message_capped_for_ai_but_floor_reads_all(tmp_path):
    body = "RFI-99; payment of S$1,000,000 due 31 December 2026. " + ("filler text " * 2000)   # ~24K chars
    t = _one_msg_thread(tmp_path, body)
    seen = {}
    stub = StubComprehender(responses={"comprehend": lambda p: (seen.__setitem__("len", len(p)), "Walk [M1].")[1]})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False, max_chars=5000)
    assert seen["len"] < 30000                         # comprehend prompt BOUNDED, not the full ~24K+ body
    assert rec["verified"]["input_truncated"] is True  # flagged
    refs = [str(r) for r in (rec["facts"].get("refs") or [])]
    assert any("RFI-99" in r for r in refs)            # floor read the FULL text -> extracted the ref anyway


def test_small_thread_not_truncated(tmp_path):
    t = _one_msg_thread(tmp_path, "Short note: please confirm RFI-7 by Friday.")
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=False, max_chars=120_000)
    assert rec["verified"]["input_truncated"] is False
