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
