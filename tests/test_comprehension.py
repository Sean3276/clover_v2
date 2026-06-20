import json
from email.message import EmailMessage

from clover import comprehend as cp
from clover import threads as th
from clover.comprehenders import StubComprehender, get_comprehender, _parse_json
from clover.profiles import get_profile


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


# ---------------------------------------------------------------- helpers
def test_estimate_tokens():
    assert cp.estimate_tokens("x" * 40) == 10
    assert cp.estimate_tokens("") == 1


def test_parse_json_tolerant():
    assert _parse_json('```json\n{"a":1}\n```') == {"a": 1}
    assert _parse_json('here you go: {"a": 2} thanks') == {"a": 2}


# ---------------------------------------------------------------- pipeline
def test_comprehend_thread_builds_full_record(tmp_path):
    t = _one_thread(tmp_path)
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile())
    assert rec["comprehension"] and rec["abstract"] and rec["summary"]
    assert len(rec["event"]) <= 30
    assert rec["classification"]["domain"] in ("Project", "Corporate")
    assert rec["classification"]["category"] in get_profile().all_categories()
    assert rec["method"] == "whole"
    assert rec["profile"] == "construction"


def test_facts_verified_against_source(tmp_path):
    t = _one_thread(tmp_path, body_a="discussion regarding EOT-05 submission")
    stub = StubComprehender(responses={"distill": {
        "abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": ["EOT-05", "GHOST-99"],
                  "dates": [], "amounts": []}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert rec["facts"]["refs"] == ["EOT-05"]                 # present kept
    assert any("GHOST-99" in d for d in rec["verified"]["dropped_facts"])  # absent dropped
    assert rec["verified"]["facts_ok"] is False


def test_facts_strip_annotations_weekday_and_dedup(tmp_path):
    t = _one_thread(tmp_path, body_a="Tender on 14 March 2025 for S$878,000 re COB ID confirmed")
    stub = StubComprehender(responses={"distill": {
        "abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "COB ID — interior fit-out", "parties": [], "refs": [],
                  "dates": ["Fri 14 March 2025 — submission deadline", "14 March 2025"],
                  "amounts": ["S$878,000 (excl GST)"]}}})
    r = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert r["facts"]["dates"] == ["14 March 2025"]        # weekday + annotation stripped, deduped
    assert r["facts"]["amounts"] == ["S$878,000"]          # trailing annotation stripped
    assert r["facts"]["project"] == "COB ID"               # annotation stripped
    assert r["verified"]["facts_ok"] is True               # all grounded -> no warning


def test_amount_digit_fallback(tmp_path):
    t = _one_thread(tmp_path, body_a="the contract sum is S$1,250,000 total")
    stub = StubComprehender(responses={"distill": {
        "abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": [], "dates": [],
                  "amounts": ["1250000"]}}})        # reformatted (no commas/currency) -> digit match
    r = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert r["facts"]["amounts"] == ["1250000"]


def test_event_tag_truncated_to_30(tmp_path):
    t = _one_thread(tmp_path)
    stub = StubComprehender(responses={"distill": {
        "abstract": "a", "summary": "s", "event": "x" * 80, "facts": {}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert len(rec["event"]) == 30


def test_classification_unanimous_when_confident(tmp_path):
    t = _one_thread(tmp_path)
    stub = StubComprehender(responses={"classify": {
        "domain": "Project", "category": "Quality", "confidence": 0.95, "dispute": False}})
    c = cp.comprehend_thread(tmp_path, t, stub, get_profile())["classification"]
    assert c["council"] == "small" and c["members"] == 5 and c["consensus"] == "unanimous" and c["category"] == "Quality"


def test_classification_dispute_escalates_and_precedence_referees(tmp_path):
    t = _one_thread(tmp_path, body_a="please process the EOT claim and interim payment")
    stub = StubComprehender(responses={
        "classify": {"domain": "Project", "category": "Operation", "confidence": 0.4, "dispute": True},
        "classify_full": {"domain": "Project", "category": "Operation", "confidence": 0.75,
                          "dissent": "could be commercial"}})
    c = cp.comprehend_thread(tmp_path, t, stub, get_profile())["classification"]
    assert c["council"] == "full" and c["members"] == 10
    assert c["category"] == "Commercial"               # EOT/payment -> precedence referee
    assert c["consensus"] == "split-resolved"


def test_precedence_uses_word_boundaries(tmp_path):
    p = get_profile()
    assert cp._precedence(p, "the claimant disagreed strongly") is None     # 'claimant' != 'claim'
    assert cp._precedence(p, "we will file a claim tomorrow") == "Commercial"
    assert cp._precedence(p, "lunch at Costa cafe") is None                  # 'Costa' != 'cost'
    assert cp._precedence(p, "the cost is too high") == "Commercial"
    assert cp._precedence(p, "a near miss was reported") == "Safety"


def test_classification_invalid_domain_category_pair_asks(tmp_path):
    t = _one_thread(tmp_path, body_a="ambiguous note, no keywords")
    stub = StubComprehender(responses={
        "classify": {"domain": "Project", "category": "Operation", "confidence": 0.4, "dispute": True},
        # full council returns a Corporate-only category under a Project domain -> invalid pair
        "classify_full": {"domain": "Project", "category": "Engineering & Operation",
                          "confidence": 0.85, "dissent": ""}})
    c = cp.comprehend_thread(tmp_path, t, stub, get_profile())["classification"]
    assert c["consensus"] == "asked"                # invalid (domain, category) -> surfaced, not shipped


def test_low_confidence_dispute_asks_operator(tmp_path):
    t = _one_thread(tmp_path, body_a="ambiguous note with no clear signal")
    stub = StubComprehender(responses={
        "classify": {"domain": "Project", "category": "Operation", "confidence": 0.3, "dispute": True},
        "classify_full": {"domain": "Project", "category": "Operation", "confidence": 0.3, "dissent": "unclear"}})
    c = cp.comprehend_thread(tmp_path, t, stub, get_profile())["classification"]
    assert c["consensus"] == "asked"                   # genuine doubt surfaced


# ---------------------------------------------------------------- QAQC gate
def test_qaqc_passes_clean_single_attempt(tmp_path):
    rec = cp.comprehend_thread(tmp_path, _one_thread(tmp_path), StubComprehender(), get_profile())
    assert rec["qaqc"]["passed"] is True and rec["qaqc"]["needs_review"] is False and rec["qaqc"]["attempts"] == 1


def test_qaqc_failure_retries_once_then_flags(tmp_path):
    stub = StubComprehender(responses={"qa": {"passed": False, "faithfulness": 0.4,
                                              "completeness": 0.5, "issues": ["omitted the deadline"]}})
    rec = cp.comprehend_thread(tmp_path, _one_thread(tmp_path), stub, get_profile())
    assert rec["qaqc"]["needs_review"] is True and rec["qaqc"]["attempts"] == 2
    assert rec["qaqc"]["issues"] == ["omitted the deadline"]
    assert stub.calls.count("comprehend") == 2         # re-comprehended once on failure


def test_qaqc_fails_when_a_fact_is_ungrounded(tmp_path):
    t = _one_thread(tmp_path, body_a="a note with no references at all")
    stub = StubComprehender(responses={"distill": {"abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": ["EOT-99"], "dates": [], "amounts": []}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())   # EOT-99 not in source -> dropped
    assert rec["verified"]["facts_ok"] is False and rec["qaqc"]["needs_review"] is True


def test_floor_backfills_dropped_ref(tmp_path):
    # the AI drops a reference that's plainly in the source -> the deterministic floor backfills it
    t = _one_thread(tmp_path, body_a="Please action RFI-12 today.")
    stub = StubComprehender(responses={"distill": {"abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": [], "dates": [], "amounts": []}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert "RFI-12" in rec["facts"]["refs"]
    assert rec["verified"]["backfilled"].get("refs") == ["RFI-12"]


def test_floor_does_not_duplicate_amount_in_other_format(tmp_path):
    # AI captured the amount value (no currency); floor must NOT re-add it as a duplicate
    t = _one_thread(tmp_path, body_a="The claim is SGD 1,250,000 in total.")
    stub = StubComprehender(responses={"distill": {"abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": [], "dates": [], "amounts": ["1250000"]}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert rec["facts"]["amounts"] == ["1250000"]          # not duplicated as 'SGD 1250000'


def test_attachment_content_reaches_the_floor(tmp_path):
    # a reference lives ONLY in an xlsx attachment; the AI sees nothing -> floor backfills it from the attachment
    import io
    import json as _json
    import openpyxl
    from email.message import EmailMessage
    m = EmailMessage()
    m["Message-ID"] = "<1>"; m["From"] = "a@x.com"; m["Subject"] = "S"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content("Please see the attached schedule.")
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(["Item", "Ref"]); ws.append(["Door", "RFI-99"])
    buf = io.BytesIO(); wb.save(buf)
    m.add_attachment(buf.getvalue(), maintype="application",
                     subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="schedule.xlsx")
    (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp_path / "INBOX" / "1.eml").write_bytes(m.as_bytes())
    (tmp_path / "_index.jsonl").write_text(_json.dumps(
        {"id": "1", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml", "date": m["Date"], "from": "a@x.com"}) + "\n",
        encoding="utf-8")
    th.build_threads(tmp_path, log=lambda *_: None)
    t = th.read_threads(tmp_path)[0]
    stub = StubComprehender(responses={"distill": {"abstract": "a", "summary": "s", "event": "e",
        "facts": {"project": "", "parties": [], "refs": [], "dates": [], "amounts": []}}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    assert "RFI-99" in rec["facts"]["refs"]                 # the ref was inside the attachment


def test_distill_verification_flags_unfaithful_abstract(tmp_path):
    # step 8 (ii)-(iv) vs (i): a drifting abstract flags the task even when the comprehension itself passes
    stub = StubComprehender(responses={"verify_distill": {
        "passed": False, "abstract_ok": False, "summary_ok": True, "event_ok": True,
        "issues": ["abstract states something the comprehension does not support"]}})
    rec = cp.comprehend_thread(tmp_path, _one_thread(tmp_path), stub, get_profile())
    assert rec["verified"]["abstract_ok"] is False
    assert rec["qaqc"]["distill_passed"] is False and rec["qaqc"]["needs_review"] is True


def test_task_complete_only_when_all_layers_verify(tmp_path):
    # a clean task records the per-layer verification and is NOT flagged
    rec = cp.comprehend_thread(tmp_path, _one_thread(tmp_path), StubComprehender(), get_profile())
    assert rec["verified"]["abstract_ok"] and rec["verified"]["summary_ok"] and rec["verified"]["event_ok"]
    assert rec["qaqc"]["distill_passed"] is True and rec["qaqc"]["needs_review"] is False


# ---------------------------------------------------------------- fact verification hardening
def test_verify_rejects_fabricated_amount_spanning_two_numbers():
    out, dropped = cp._verify_facts({"amounts": ["1002"]}, "Call me at 6512 3456 then ref 100200300")
    assert out["amounts"] == [] and any(d.startswith("amounts:1002") for d in dropped)


def test_verify_accepts_real_amount_despite_comma_formatting():
    out, _ = cp._verify_facts({"amounts": ["878000"]}, "the sum of S$878,000 is now due")
    assert out["amounts"] == ["878000"]            # digits equal the real source number 878,000


def test_verify_party_uses_word_boundary():
    out, _ = cp._verify_facts({"parties": ["Sun"]}, "the sunshine project team met")
    assert out["parties"] == []                    # 'Sun' is not a whole word inside 'sunshine'


# ---------------------------------------------------------------- learned rules + resolve
def test_rule_classifies_directly_without_council(tmp_path):
    from clover import rules
    t = _one_thread(tmp_path, body_a="please process the retention sum release")
    rules.add_rule(tmp_path, "keyword", "retention sum", "Project", "Commercial")
    stub = StubComprehender()
    c = cp.comprehend_thread(tmp_path, t, stub, get_profile())["classification"]
    assert c["consensus"] == "rule" and c["council"] == "rule" and c["category"] == "Commercial"
    assert "classify" not in stub.calls                # AI council was skipped by the rule


def test_resolve_overrides_classification_and_clears_flag(tmp_path):
    t = _one_thread(tmp_path)
    stub = StubComprehender(responses={
        "classify": {"domain": "Project", "category": "Operation", "confidence": 0.3, "dispute": True},
        "classify_full": {"domain": "Project", "category": "Operation", "confidence": 0.3, "dissent": "x"}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile())
    cp.save_comprehension(tmp_path, rec)
    assert rec["classification"]["consensus"] == "asked"
    assert cp.resolve_comprehension(tmp_path, t["thread_id"], "Project", "Quality", "2026-06-19T00:00:00Z")
    got = cp.get_comprehension(tmp_path, t["thread_id"])["classification"]
    assert got["domain"] == "Project" and got["category"] == "Quality" and got["consensus"] == "resolved"


# ---------------------------------------------------------------- runner
def test_run_idempotent(tmp_path):
    _one_thread(tmp_path)
    out1 = cp.run_comprehension(tmp_path, backend=StubComprehender(), log=lambda *_: None)
    out2 = cp.run_comprehension(tmp_path, backend=StubComprehender(), log=lambda *_: None)
    assert out1["done"] == 1 and out2["done"] == 0     # second run skips done thread
    assert len(cp.read_comprehensions(tmp_path)) == 1


def test_run_policy_gate_blocks(tmp_path):
    _one_thread(tmp_path)
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(),
                               allowed=lambda: False, log=lambda *_: None)
    assert out["done"] == 0 and out["blocked"] is True


def test_run_budget_stops_after_first(tmp_path):
    # two unrelated single-message threads; tiny budget => only the first runs
    _eml(tmp_path, "INBOX", "1", "a@x", text="one")
    _eml(tmp_path, "INBOX", "2", "b@x", text="two")
    th.build_threads(tmp_path, log=lambda *_: None)
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), budget_tokens=1, log=lambda *_: None)
    assert out["done"] == 1 and out["pending"] == 1


def test_run_concurrent_processes_all(tmp_path):
    # several independent single-message threads, comprehended in parallel — nothing lost, counts intact
    for i in range(8):
        _eml(tmp_path, "INBOX", str(i), f"u{i}@x", text=f"msg {i}")
    th.build_threads(tmp_path, log=lambda *_: None)
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), concurrency=4, log=lambda *_: None)
    assert out["done"] == 8 and out["errors"] == 0
    assert len(cp.read_comprehensions(tmp_path)) == 8        # every result appended under concurrency


def test_registry_get():
    assert isinstance(get_comprehender("stub"), StubComprehender)


# ---------------------------------------------------------------- web wiring
def test_comprehend_route_and_views(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    t = _one_thread(tmp_path)
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path),
           "comprehension": {"backend": "stub", "profile": "construction"}}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(m, "_comprehender", lambda c: StubComprehender())
    monkeypatch.setattr(m, "_backend_available", lambda c: True)
    client = TestClient(m.app)
    tid = t["thread_id"]

    assert "Comprehend" in client.get(f"/threads/{tid}").text          # button before
    r = client.post(f"/threads/{tid}/comprehend")
    assert r.json()["ok"] is True
    assert r.json()["classification"]["domain"] in ("Project", "Corporate")

    assert "Stub one-liner" in client.get(f"/threads/{tid}").text      # panel after
    assert "cbadge" in client.get("/threads").text                     # badge on the list


def test_comprehend_route_blocks_when_backend_missing(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    t = _one_thread(tmp_path)
    monkeypatch.setattr(m.cfgmod, "load_config",
                        lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    monkeypatch.setattr(m, "_backend_available", lambda c: False)
    r = TestClient(m.app).post(f"/threads/{t['thread_id']}/comprehend")
    assert r.json()["ok"] is False and "Claude CLI" in r.json()["message"]


# ---------------------------------------------------------------- staleness + batch
def _add_msg(tmp, key, mid, irt, text="more"):
    _eml(tmp, "INBOX", key, mid, irt=irt, text=text)
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


def test_record_stores_source_and_is_stale_on_new_message(tmp_path):
    t = _one_thread(tmp_path)
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile())
    cp.save_comprehension(tmp_path, rec)
    assert rec["source"]["n"] == t["n"]
    assert cp.is_stale(t, rec) is False
    t2 = _add_msg(tmp_path, "3", "c@x", "b@x")          # a new message arrives in the thread
    assert cp.is_stale(t2, rec) is True


def test_is_stale_legacy_record_without_source_is_not_stale(tmp_path):
    t = _one_thread(tmp_path)
    assert cp.is_stale(t, {"thread_id": t["thread_id"]}) is False   # no source -> can't tell -> not stale
    assert cp.is_stale(t, None) is False


def test_select_threads_pending_stale_redo_only(tmp_path):
    t = _one_thread(tmp_path)
    assert len(cp.select_threads(tmp_path)) == 1                    # pending
    cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile())
    assert cp.select_threads(tmp_path) == []                        # nothing needs it
    t2 = _add_msg(tmp_path, "3", "c@x", "b@x")                      # -> stale
    assert [s["thread_id"] for s in cp.select_threads(tmp_path)] == [t2["thread_id"]]
    assert cp.select_threads(tmp_path, include_stale=False) == []   # stale excluded when asked
    assert len(cp.select_threads(tmp_path, redo=True)) == 1         # redo forces it
    assert cp.select_threads(tmp_path, only=set()) == []            # empty selection


def test_run_comprehension_only_progress_and_redo(tmp_path):
    t = _one_thread(tmp_path)
    calls = []
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile(),
                               only={t["thread_id"]}, progress=lambda **k: calls.append(k))
    assert out["done"] == 1 and out["total"] == 1 and out["pending"] == 0
    assert any(c.get("total") == 1 for c in calls)                  # progress was reported
    assert cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile())["total"] == 0
    assert cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile(), redo=True)["done"] == 1


def test_batch_comprehend_routes_and_brown_clover(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import time
    import app.main as m
    _one_thread(tmp_path)
    cfg = {"auth": {"imap": {}}, "archive_path": str(tmp_path),
           "comprehension": {"backend": "stub", "profile": "construction"}}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    client = TestClient(m.app)

    r = client.post("/comprehend/run", data={"mode": "all"})
    assert r.json()["ok"] is True and r.json()["total"] == 1
    for _ in range(100):
        s = client.get("/comprehend/status").json()
        if not s["running"]:
            break
        time.sleep(0.05)
    assert s["counts"]["done"] == 1 and s["counts"]["pending"] == 0

    _add_msg(tmp_path, "3", "c@x", "b@x")                           # make it stale
    body = client.get("/threads").text
    assert "leaf stale" in body                                     # brown clover renders
    assert client.get("/comprehend/status").json()["counts"]["stale"] == 1
    assert client.post("/comprehend/stop").json()["ok"] is True


def test_comprehend_run_empty_selection(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _one_thread(tmp_path)
    cfg = {"auth": {"imap": {}}, "archive_path": str(tmp_path),
           "comprehension": {"backend": "stub", "profile": "construction"}}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    r = TestClient(m.app).post("/comprehend/run", data={"mode": "project", "project_key": "nope"})
    assert r.json()["ok"] is False and "Nothing to comprehend" in r.json()["message"]


# ---------------------------------------------------------------- mail-row icons (attachment / link)
def test_thread_attachment_and_mail_icons(tmp_path, monkeypatch):
    import json as _json
    from email.message import EmailMessage
    from starlette.testclient import TestClient
    import app.main as m

    msg = EmailMessage()
    msg["Message-ID"] = "<att1>"; msg["From"] = "a@x.com"; msg["To"] = "b@x.com"
    msg["Subject"] = "Report"; msg["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1", maintype="application", subtype="pdf", filename="r.pdf")
    (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp_path / "INBOX" / "1.eml").write_bytes(msg.as_bytes())
    (tmp_path / "_index.jsonl").write_text(_json.dumps(
        {"id": "att1", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml",
         "date": msg["Date"], "from": "a@x.com", "subject": "Report"}) + "\n", encoding="utf-8")
    th.build_threads(tmp_path, log=lambda *_: None)
    t = th.read_threads(tmp_path)[0]
    assert t["has_attach"] is True                       # multipart/mixed detected

    (tmp_path / "link_shares.jsonl").write_text(_json.dumps(
        {"message_id": "att1", "eml": "INBOX/1.eml", "url": "http://x", "provider": "p",
         "status": "pending", "file": None}) + "\n", encoding="utf-8")
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    body = TestClient(m.app).get("/threads").text
    assert "📎" in body and "🔗" in body and "still to fetch" in body


def test_review_badge_tooltip_explains_action(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    t = _one_thread(tmp_path)
    cp.save_comprehension(tmp_path, {"thread_id": t["thread_id"], "root_id": t["root_id"],
                                     "subject": "s", "summary": "x", "source": {"n": t["n"], "end": t["end"]},
                                     "classification": {"domain": "Project", "category": "Commercial",
                                                        "consensus": "asked"},
                                     "facts": {}})
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path),
                                                          "comprehension": {"profile": "construction"}})
    body = TestClient(m.app).get("/threads").text
    assert "Resolve / reclassify" in body and "council was split" in body


# ---------------------------------------------------------------- budget / limit / progress (P0)
def test_large_attachment_does_not_budget_stall(tmp_path):
    import json as _json
    from email.message import EmailMessage
    def mk(key, mid, attach_mb=0):
        m = EmailMessage(); m["Message-ID"] = f"<{mid}>"; m["From"] = "a@x.com"
        m["Subject"] = "Hi"; m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"; m.set_content("body")
        if attach_mb:
            m.add_attachment(b"x" * (attach_mb * 1024 * 1024), maintype="application",
                             subtype="octet-stream", filename="big.bin")
        (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
        (tmp_path / "INBOX" / f"{key}.eml").write_bytes(m.as_bytes())
        with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
            f.write(_json.dumps({"id": mid, "folder": "INBOX", "key": key,
                                 "path": f"INBOX/{key}.eml", "date": m["Date"], "from": "a@x.com"}) + "\n")
    mk("1", "m1", attach_mb=3); mk("2", "m2"); mk("3", "m3")          # 3MB attachment on the first
    th.build_threads(tmp_path, log=lambda *_: None)
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile())  # default 200k budget
    assert out["done"] == 3 and out["errors"] == 0                   # NOT stalled after 1 by the attachment


def test_run_comprehension_limit_caps_count_and_reports_backlog(tmp_path):
    for i in range(5):
        _eml(tmp_path, "INBOX", str(i), f"m{i}", text="hi")
    th.build_threads(tmp_path, log=lambda *_: None)
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile(), limit=2)
    assert out["done"] == 2 and out["total"] == 2 and out["pending"] == 3 and out["backlog"] == 5


def test_run_comprehension_progress_reports_errors_and_heartbeat(tmp_path):
    _one_thread(tmp_path)
    seen = []
    cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile(),
                         progress=lambda **k: seen.append(k))
    assert seen and "errors" in seen[-1] and "last_done" in seen[-1]


def test_mail_list_has_filter_chips_and_status(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    t = _one_thread(tmp_path)
    cp.save_comprehension(tmp_path, {"thread_id": t["thread_id"], "root_id": t["root_id"], "subject": "s",
                                     "summary": "x", "source": {"n": t["n"], "end": t["end"]},
                                     "classification": {"domain": "Project", "category": "Safety", "consensus": "unanimous"},
                                     "facts": {}})
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path),
                                                          "comprehension": {"profile": "construction"}})
    body = TestClient(m.app).get("/threads").text
    assert 'class="chip"' in body and "Needs review" in body and 'data-status="comprehended"' in body and ">Safety<" in body


# ---------------------------------------------------------------- faceted tags (P1#7)
def test_clean_tags_validates_against_vocab():
    out = cp._clean_tags(["Discipline: M&E", "Artifact: RFI", "Made Up: Nonsense", "Discipline: m&e"], get_profile())
    assert out == ["Discipline: M&E", "Artifact: RFI"]      # invalid dropped, case-normalised, deduped


def test_clean_tags_empty_without_facets():
    from clover.profiles import Profile
    p = Profile(name="x", description="", domains={"D": ["A"]}, safety_net="A")
    assert cp._clean_tags(["Anything: Goes"], p) == []       # no facets defined -> no tags


def test_comprehend_thread_extracts_validated_tags(tmp_path):
    t = _one_thread(tmp_path)
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=False)
    assert rec["tags"] == ["Discipline: M&E", "Artifact: RFI"]   # stub's bogus 'Made Up: Nonsense' dropped


def test_thread_view_and_mail_show_tags(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    t = _one_thread(tmp_path)
    cp.save_comprehension(tmp_path, {"thread_id": t["thread_id"], "root_id": t["root_id"], "subject": "s",
                                     "summary": "x", "source": {"n": t["n"], "end": t["end"]}, "facts": {},
                                     "tags": ["Artifact: RFI"],
                                     "classification": {"domain": "Project", "category": "Operation",
                                                        "consensus": "unanimous"}})
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path),
                                                          "comprehension": {"profile": "construction"}})
    c = TestClient(m.app)
    assert "artifact: rfi" in c.get("/threads").text                         # searchable in the Mail filter
    assert "🏷 Artifact: RFI" in c.get("/threads/" + t["thread_id"]).text     # badge on the thread
