import json
from email.message import EmailMessage

from clover import comprehend as cp
from clover import threads as th
from clover.eval import gold as goldmod
from clover.eval import run_eval


def _archive(tmp, body):
    m = EmailMessage()
    m["Message-ID"] = "<1>"; m["From"] = "a@x.com"; m["Subject"] = "S1"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"; m.set_content(body)
    (tmp / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp / "INBOX" / "1.eml").write_bytes(m.as_bytes())
    (tmp / "_index.jsonl").write_text(json.dumps(
        {"id": "1", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml",
         "date": m["Date"], "from": "a@x.com"}) + "\n", encoding="utf-8")
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


def test_evaluate_floor_flags_dropped_atom(tmp_path):
    t = _archive(tmp_path, "Please action RFI-12 by 14 Mar 2025; budget SGD 1000 and USD 500.")
    cp.save_comprehension(tmp_path, {                               # AI dropped USD 500
        "thread_id": t["thread_id"], "classification": {"domain": "Project", "category": "Commercial"},
        "facts": {"refs": ["RFI-12"], "dates": ["14 March 2025"], "amounts": ["SGD 1,000"], "parties": []}})
    res = run_eval.evaluate(tmp_path)
    s = res["summary"]
    assert s["floor"]["hard_misses"] == 1                          # USD 500 only
    assert s["floor"]["amounts"]["recall"] < 1.0
    assert s["floor"]["dates"]["recall"] == 1.0                    # send-date header NOT counted as a miss
    assert s["no_miss_rate"] is None                              # no confirmed gold yet
    assert res["details"][0]["floor_missed"]["amounts"] == ["USD 500"]


def test_run_scores_against_confirmed_gold_and_writes_leaderboard(tmp_path):
    t = _archive(tmp_path, "RFI-12 due 14 Mar 2025; SGD 1000 and USD 500.")
    cp.save_comprehension(tmp_path, {                               # AI captured everything
        "thread_id": t["thread_id"], "classification": {"domain": "Project", "category": "Commercial"},
        "facts": {"refs": ["RFI-12"], "dates": ["14 March 2025"], "amounts": ["SGD 1000", "USD 500"], "parties": []}})
    g = goldmod.bootstrap_record(t["thread_id"], "RFI-12 due 14 Mar 2025; SGD 1000 and USD 500.")
    g["confirmed"] = True
    goldmod.write_gold(tmp_path, g)
    s = run_eval.run(tmp_path, model_id="stub", gold_version="0")["summary"]
    assert s["gold_threads"] == 1 and s["no_miss_rate"] == 1.0
    assert s["floor"]["hard_misses"] == 0
    assert run_eval.leaderboard_path(tmp_path).exists()
