"""Top-10 feature build (post-grind): per-industry profiles, money %, recurring obligations,
colloquial/holiday deadlines, ledger-as-data, soft-ask backstop, PHI minimization, operator default,
implied-obligation floor. One file, TDD."""
from clover import comprehend as cp
from clover import threads as th
from clover.comprehenders import StubComprehender
from clover.profiles import get_profile, PROFILES
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


# ---- F1: per-industry profiles --------------------------------------------------------------
def test_industry_profiles_ship_and_are_valid():
    for name in ("legal", "healthcare", "agency", "finance", "generic", "construction"):
        p = get_profile(name)
        assert p.name == name and p.domains
        # safety_net and every precedence target must be a real category (no invalid config)
        cats = set(p.all_categories())
        assert p.safety_net in cats
        for rule in p.precedence:
            assert rule["then"] in cats
    assert "EOT" not in " ".join(get_profile("legal").ref_examples)   # legal is not construction-flavored
    assert any("MRN" in r for r in get_profile("healthcare").ref_examples)


def test_default_profile_is_generic_not_construction():
    from clover import config as cfg
    assert cfg.default_config()["comprehension"]["profile"] == "generic"   # industry-agnostic out-of-box
    assert get_profile().name == "generic"                                 # fallback is domain-neutral
    assert get_profile(None).name == "generic" and get_profile("nope").name == "generic"
    assert get_profile("construction").name == "construction"              # still selectable


# ---- F4: money percents ---------------------------------------------------------------------
def test_percent_floor():
    from clover.eval.extractors import extract_percents
    assert extract_percents("retention 5% and 2.5 percent fee, 百分之30") == ["5%", "2.5%", "30%"]


def test_percents_surface_in_facts(tmp_path):
    t = _one_thread(tmp_path, body_a="A 2% early-payment discount applies; retention is 5%.")
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=False)
    assert "2%" in rec["facts"]["percents"] and "5%" in rec["facts"]["percents"]


# ---- F8: recurring obligations --------------------------------------------------------------
def test_recurring_obligation_tagged(tmp_path):
    t = _one_thread(tmp_path, body_a="Submit the report.", body_b="ok")
    stub = StubComprehender(responses={"actions": {"actions": [
        {"action": "Submit the monthly progress report", "source": ["M1"]},
        {"action": "Pay the invoice once", "source": ["M1"]}]}})
    acts = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False)["actions"]
    assert next(a for a in acts if "report" in a["action"])["recurrence"] == "monthly"
    assert next(a for a in acts if "once" in a["action"])["recurrence"] == ""


# ---- F9: colloquial + holiday deadlines -----------------------------------------------------
def test_colloquial_weekday_and_eod_resolve(tmp_path):
    # M1 sent Thu 01 Jan 2026; 'by Friday' -> 2026-01-02, 'EOD' -> 2026-01-01
    t = _one_thread(tmp_path, body_a="Please send it by Friday.", body_b="ok")
    stub = StubComprehender(responses={"actions": {"actions": [
        {"action": "Send by Friday", "due_raw": "by Friday", "source": ["M1"]},
        {"action": "Reply EOD", "due_raw": "EOD", "source": ["M1"]}]}})
    acts = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False)["actions"]
    assert next(a for a in acts if "Friday" in a["action"])["due_canonical"] == "2026-01-02"
    assert next(a for a in acts if "EOD" in a["action"])["due_canonical"] == "2026-01-01"


def test_holiday_calendar_makes_business_day_confident():
    from clover.eval.deadlines import find_relative_deadlines, resolve
    rels = find_relative_deadlines("within 3 working days")
    # Thu 01 Jan 2026 + 3 business days normally = Tue 06 Jan; with Fri 02 Jan a holiday -> Wed 07 Jan
    assert resolve(rels[0], "2026-01-01") == "2026-01-06"
    assert resolve(rels[0], "2026-01-01", holidays=["2026-01-02"]) == "2026-01-07"


# ---- F6: soft-ask backstop tier -------------------------------------------------------------
def test_soft_ask_backstop_surfaces_weak_ask(tmp_path):
    t = _one_thread(tmp_path, body_a="Review the latest deck.", body_b="ok")   # weak-only; stub emits no actions
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=False)
    assert any("deck" in u.lower() for u in rec["verified"]["action_floor_soft_uncovered"])


# ---- F7: ledger as structured data ----------------------------------------------------------
def test_ledger_parsed_to_open_items(tmp_path):
    t = _one_thread(tmp_path, body_a="x", body_b="y")
    led = '{"open_items":[{"id":"o1","matter":"EOT-05 grant","status":"open"}]}'
    stub = StubComprehender(responses={"comprehend": lambda p: "Walk [M1].\n<<<LEDGER>>>\n" + led + "\n<<<END>>>"})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False)
    assert rec["open_items"] and rec["open_items"][0]["id"] == "o1" and rec["open_items"][0]["status"] == "open"


# ---- F5: PHI minimization -------------------------------------------------------------------
def test_phi_minimized_in_derived_layers(tmp_path):
    t = _one_thread(tmp_path, body_a="Patient John Tan, MRN 884512 — confirm the slot.", body_b="ok")
    stub = StubComprehender(responses={
        "distill_summary": {"abstract": "Confirm slot for patient MRN 884512", "summary": "MRN 884512 pending",
                            "event": "slot", "tags": []},
        "actions": {"actions": [{"action": "Confirm slot", "quote": "Patient John Tan, MRN 884512", "source": ["M1"]}]}})
    rec = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False)
    assert rec["verified"]["minimized"] is True
    assert "884512" not in rec["abstract"] and "884512" not in rec["summary"]
    assert "884512" not in rec["actions"][0]["quote"]


# ---- F3: operator-identity default ----------------------------------------------------------
def test_operator_inferred_from_dominant_sender(tmp_path):
    t = _one_thread(tmp_path, body_a="Please review.", body_b="thanks")   # all from a@x.com (helper)
    stub = StubComprehender(responses={"actions": {"actions": [
        {"action": "Review draft", "owner": "a@x.com", "counterparty": "b@x.com", "source": ["M1"]}]}})
    a = cp.comprehend_thread(tmp_path, t, stub, get_profile(), qaqc=False)["actions"][0]
    assert a["is_mine"] is True and a["direction"] == "inbound"


# ---- F10: deterministic implied-obligation floor --------------------------------------------
def test_implied_obligation_floor(tmp_path):
    t = _one_thread(tmp_path,
                    body_a="The fire-rating cert is required before handover. Handover is on 20 June 2026.",
                    body_b="ok")
    rec = cp.comprehend_thread(tmp_path, t, StubComprehender(), get_profile(), qaqc=False)
    imp = rec["verified"]["implied_candidates"]
    assert any("cert" in c.lower() and "2026-06-20" in c for c in imp)
