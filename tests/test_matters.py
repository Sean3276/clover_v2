"""Matters engine: stable item identity + a per-archive prefs store (pins, important/normal overrides,
learned focus-keywords, saved layout), the Happenings digest, the Focus assembly, and the deterministic
focus-keyword learner. (Foundation for the Matters page: Focus = what needs you, Happenings = what's going on.)"""
from clover import matters as mt


def _rec(rid, subject, summary, domain, category, end, actions=None, needs_review=False):
    return {"thread_id": "t-" + rid, "root_id": rid, "subject": subject, "summary": summary,
            "abstract": summary, "event": "", "classification": {"domain": domain, "category": category},
            "facts": {"refs": []}, "actions": actions or [], "qaqc": {"needs_review": needs_review},
            "source": {"end": end}}


# ---- stable item identity ---------------------------------------------------------------------
def test_item_key_stable_normalized_and_scoped():
    k = mt.item_key("r1", "Send the cert")
    assert k == mt.item_key("r1", "send   the  CERT")     # case/space-insensitive -> survives re-comprehension
    assert k != mt.item_key("r1", "Send the invoice")     # different text -> different item
    assert k != mt.item_key("r2", "Send the cert")        # thread-scoped


# ---- prefs store ------------------------------------------------------------------------------
def test_store_defaults_and_pin_roundtrip(tmp_path):
    s = mt.read_store(tmp_path)
    assert s["pins"] == [] and s["keywords"] == [] and s["importance"] == {}
    mt.set_pin(tmp_path, "k1", True)
    assert "k1" in mt.read_store(tmp_path)["pins"]
    mt.set_pin(tmp_path, "k1", False)
    assert "k1" not in mt.read_store(tmp_path)["pins"]


def test_layout_roundtrip(tmp_path):
    mt.set_layout(tmp_path, {"sort": "domain", "gauge": True})
    lay = mt.read_store(tmp_path)["layout"]
    assert lay["sort"] == "domain" and lay["gauge"] is True


# ---- uniform items: same shape, Focus = ONLY what the operator stars -------------------------
def test_focus_is_empty_until_starred_then_item_moves(tmp_path):
    recs = [_rec("r1", "Alpha", "did A", "Project", "RFI", "2026-06-10")]
    store = mt.read_store(tmp_path)
    assert mt.focus(recs, "2026-06-21", store) == []          # Clover never auto-fills Focus
    assert len(mt.happenings(recs, "2026-06-21", store)) == 1  # it sits in Happenings
    key = mt.items(recs, "2026-06-21", store)[0]["key"]
    mt.set_pin(tmp_path, key, True)                           # operator stars it
    store = mt.read_store(tmp_path)
    assert [i["subject"] for i in mt.focus(recs, "2026-06-21", store)] == ["Alpha"]   # now in Focus
    assert mt.happenings(recs, "2026-06-21", store) == []     # and out of Happenings


def test_happenings_uniform_items(tmp_path):
    recs = [_rec("r1", "Alpha", "did A", "Project", "RFI", "2026-06-10"),
            _rec("r2", "Beta", "did B", "Project", "VO", "2026-06-20", needs_review=True)]
    h = mt.happenings(recs, "2026-06-21", mt.read_store(tmp_path))
    assert {x["subject"] for x in h} == {"Alpha", "Beta"}
    beta = next(x for x in h if x["subject"] == "Beta")
    assert beta["summary"] == "did B" and beta["category"] == "VO" and beta["needs_review"] is True


def test_importance_override_marks_item(tmp_path):
    recs = [_rec("r1", "A", "x", "Project", "RFI", "2026-06-10")]
    key = mt.items(recs, "2026-06-21")[0]["key"]
    mt.set_importance(tmp_path, key, "high")
    assert mt.items(recs, "2026-06-21", mt.read_store(tmp_path))[0]["important"] is True


# ---- the learner SUGGESTS, never auto-adds ----------------------------------------------------
def test_my_open_obligation_is_suggested_not_auto_focused(tmp_path):
    recs = [_rec("r1", "A", "x", "Project", "RFI", "2026-06-10",
                 actions=[{"action": "Sign", "is_mine": True, "status": "open", "due_canonical": "2026-06-25"}])]
    it = mt.items(recs, "2026-06-21", mt.read_store(tmp_path))[0]
    assert it["focus"] is False and it["suggested"] is True    # surfaced for confirmation, NOT in Focus
    assert mt.focus(recs, "2026-06-21", mt.read_store(tmp_path)) == []


def test_keyword_match_suggests(tmp_path):
    mt.set_keywords(tmp_path, [{"term": "retention", "weight": 3}, {"term": "payment", "weight": 3}])
    recs = [_rec("r1", "Pay", "resolve the retention payment", "Finance", "Payment", "2026-06-10")]
    it = mt.items(recs, "2026-06-21", mt.read_store(tmp_path))[0]
    assert it["suggested"] is True                              # matches learned focus keywords


# ---- sorting + saved layout -------------------------------------------------------------------
def test_sort_items_modes():
    its = [{"subject": "A", "days_left": 5, "date": "2026-06-10", "domain": "Zeta", "category": "RFI"},
           {"subject": "B", "days_left": -2, "date": "2026-06-20", "domain": "Alpha", "category": "VO"},
           {"subject": "C", "days_left": None, "date": "2026-06-15", "domain": "Mu", "category": "RFI"}]
    assert [i["subject"] for i in mt.sort_items(its, "expiry")] == ["B", "A", "C"]    # overdue first, undated last
    assert [i["subject"] for i in mt.sort_items(its, "recency")] == ["B", "C", "A"]   # newest date first
    assert mt.sort_items(its, "domain")[0]["subject"] == "B"                          # domain 'Alpha' first


def test_focus_and_happenings_honour_saved_sort(tmp_path):
    recs = [_rec("r1", "A", "x", "Z", "RFI", "2026-06-10",
                 actions=[{"action": "x", "is_mine": True, "status": "open", "due_canonical": "2026-06-22"}]),
            _rec("r2", "B", "y", "A", "VO", "2026-06-20",
                 actions=[{"action": "y", "is_mine": True, "status": "open", "due_canonical": "2026-06-30"}])]
    h = mt.happenings(recs, "2026-06-21", mt.read_store(tmp_path))   # default = expiry: A(06-22) before B(06-30)
    assert [i["subject"] for i in h] == ["A", "B"]
    mt.set_layout(tmp_path, {"sort": "recency"})                     # newest message first: B(06-20) before A(06-10)
    h2 = mt.happenings(recs, "2026-06-21", mt.read_store(tmp_path))
    assert [i["subject"] for i in h2] == ["B", "A"]


# ---- AI themes (the AI-inferred suggestion layer) ---------------------------------------------
def test_theme_match_suggests(tmp_path):
    mt.set_themes(tmp_path, [{"label": "Payment disputes", "terms": ["payment", "retention", "invoice"]}])
    recs = [_rec("r1", "Inv", "overdue retention payment held", "Finance", "Payment", "2026-06-10")]
    it = mt.items(recs, "2026-06-21", mt.read_store(tmp_path))[0]
    assert it["suggested"] is True                              # matches an AI-inferred theme (shared terms)


def test_infer_themes_from_starred(tmp_path):
    from clover.comprehenders import StubComprehender
    recs = [_rec("r1", "A", "retention payment dispute", "Finance", "Payment", "2026-06-10"),
            _rec("r2", "B", "withheld progress claim", "Finance", "Payment", "2026-06-11")]
    for it in mt.items(recs, "2026-06-21"):
        mt.set_pin(tmp_path, it["key"], True)                  # star both -> enough signal for themes
    stub = StubComprehender(responses={"matters_themes":
            {"themes": [{"label": "Cash-flow", "terms": ["payment", "retention", "claim"]}]}})
    themes = mt.infer_themes(recs, "2026-06-21", stub, mt.read_store(tmp_path))
    assert themes and themes[0]["label"] == "Cash-flow" and "payment" in themes[0]["terms"]


def test_infer_themes_needs_two_starred(tmp_path):
    from clover.comprehenders import StubComprehender
    recs = [_rec("r1", "A", "x", "Finance", "Payment", "2026-06-10")]   # nothing starred
    assert mt.infer_themes(recs, "2026-06-21", StubComprehender(), mt.read_store(tmp_path)) == []


# ---- urgency gauge ----------------------------------------------------------------------------
def test_gauge_fill():
    assert mt.gauge_fill({"days_left": -3}) == 100               # overdue -> full
    assert mt.gauge_fill({"days_left": 0}) >= 90                 # due today -> nearly full
    assert mt.gauge_fill({"days_left": None}) == 0              # no deadline -> empty
    assert mt.gauge_fill({"days_left": 10}) > mt.gauge_fill({"days_left": 25})   # nearer = fuller
    assert mt.gauge_fill({"days_left": 99}) <= 10               # far off -> low


# ---- the deterministic focus-keyword learner --------------------------------------------------
def test_learn_from_pin_then_score(tmp_path):
    item = {"action": "Resolve the retention payment dispute", "subject": "Retention claim",
            "refs": ["RFI-12"], "domain": "Finance", "category": "Payment"}
    mt.learn_from_pin(tmp_path, item)
    kws = mt.read_store(tmp_path)["keywords"]
    terms = {k["term"] for k in kws}
    assert "rfi-12" in terms and "payment" in terms and "retention" in terms   # ref + category + salient term
    # a structured signal (ref/category) carries more weight than a loose word
    assert next(k["weight"] for k in kws if k["term"] == "payment") > \
           next(k["weight"] for k in kws if k["term"] == "retention")
    match = {"action": "Chase the retention payment", "subject": "", "refs": [], "domain": "Finance", "category": "Payment"}
    nomatch = {"action": "Schedule a site visit", "subject": "", "refs": [], "domain": "Project", "category": "Site"}
    assert mt.score_item(match, kws) > mt.score_item(nomatch, kws)


def test_user_can_edit_keywords(tmp_path):
    mt.learn_from_pin(tmp_path, {"action": "x", "category": "Payment", "refs": [], "subject": ""})
    mt.set_keywords(tmp_path, [{"term": "tender", "weight": 5}])   # operator overrides the learned set
    kws = mt.read_store(tmp_path)["keywords"]
    assert kws == [{"term": "tender", "weight": 5}]


# ---- route wiring (the learning loop end to end) ----------------------------------------------
def _seed(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    from clover.paths import default_archive_path
    from clover.comprehend import comprehension_path
    arch = default_archive_path(); arch.mkdir(parents=True, exist_ok=True)
    rec = {"thread_id": "t1", "root_id": "r1", "subject": "Retention", "summary": "retention held",
           "classification": {"domain": "Finance", "category": "Payment"}, "facts": {"refs": ["RFI-12"]},
           "actions": [{"action": "Resolve the retention payment", "is_mine": True, "status": "open",
                        "priority": "normal", "due_canonical": "2026-06-25"}],
           "qaqc": {"needs_review": True}, "source": {"end": "2026-06-01"}}
    comprehension_path(arch).write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return arch, rec


def test_pin_route_stars_into_focus_and_learns(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    arch, rec = _seed(monkeypatch, tmp_path)
    import app.main as m
    key = mt.items([rec], "2026-06-21")[0]["key"]
    assert TestClient(m.app).post("/matters/pin", data={"key": key, "on": 1}).json()["pinned"] is True
    store = mt.read_store(arch)
    assert key in store["pins"]
    assert [i["subject"] for i in mt.focus([rec], "2026-06-21", store)] == ["Retention"]   # now in Focus
    assert any(k["term"] == "payment" for k in store["keywords"])   # starring taught the learner


def test_importance_route_marks_item(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    arch, rec = _seed(monkeypatch, tmp_path)
    import app.main as m
    key = mt.items([rec], "2026-06-21")[0]["key"]
    TestClient(m.app).post("/matters/importance", data={"key": key, "level": "high"})
    assert mt.items([rec], "2026-06-21", mt.read_store(arch))[0]["important"] is True


def test_sort_items_by_importance(tmp_path):
    # new sort axis: important matters first, then by soonest expiry within each group
    its = [
        {"subject": "a", "important": False, "days_left": 1, "date": "2026-06-02"},
        {"subject": "b", "important": True, "days_left": 5, "date": "2026-06-01"},
        {"subject": "c", "important": True, "days_left": 2, "date": "2026-06-03"},
    ]
    assert [i["subject"] for i in mt.sort_items(its, "importance")] == ["c", "b", "a"]


def test_dmy_date_filter():
    from app.main import _dmy
    assert _dmy("2026-06-15") == "15 Jun 2026"            # ISO date -> dd mmm yyyy
    assert _dmy("2026-06-15T10:30:00Z") == "15 Jun 2026"  # ISO datetime -> just the date
    assert _dmy("") == "" and _dmy(None) == ""            # empty stays empty
    assert _dmy("2026-00-15") == "2026-00-15"             # month 00 must NOT wrap to 'Dec' — fall through raw
    assert _dmy("2026-13-15") == "2026-13-15"             # out-of-range month -> raw


def test_matters_firstrun_has_no_controls(monkeypatch, tmp_path):
    # no comprehension records -> the first-run card, NOT the controls/tagPop (the global JS listeners that
    # reference $("tagPop") must not be present without a guard — this is the page a brand-new user lands on)
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    from starlette.testclient import TestClient
    import app.main as m
    h = TestClient(m.app).get("/todo").text
    assert "Your matters will appear here" in h and "Import mail" in h   # first-run CTA
    assert 'id="tagPop"' not in h and 'id="fSort"' not in h             # controls absent on first run


def test_matters_dates_and_independent_focus_and_importance(monkeypatch, tmp_path):
    # 14a: an item with no extracted deadline must still show a date (its last activity), never "no date"
    # 14b: FOCUS (★ = your to-do tracking) and IMPORTANT (⚑ = importance flag) are INDEPENDENT axes — either
    #      can be set without the other; marking important must NOT add the item to Focus, and vice versa.
    import json
    from starlette.testclient import TestClient
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    from clover.paths import default_archive_path
    from clover.comprehend import comprehension_path
    arch = default_archive_path(); arch.mkdir(parents=True, exist_ok=True)
    rec = {"thread_id": "t1", "root_id": "r1", "subject": "Cladding RFI", "summary": "awaiting reply",
           "classification": {"domain": "Project", "category": "Design"}, "facts": {},
           "actions": [], "source": {"end": "2026-06-15"}}        # no dated obligation
    comprehension_path(arch).write_text(json.dumps(rec) + "\n", encoding="utf-8")
    import app.main as m
    h = TestClient(m.app).get("/todo").text
    assert "15 Jun 2026" in h and "last update" in h            # dd mmm yyyy fallback to the activity date
    assert "setImp(" in h and "cloverbtn" in h                  # star=Important + clover=Focus both present
    key = mt.item_key("r1", "awaiting reply")
    mt.set_importance(arch, key, "high")                         # mark important (star) WITHOUT focusing (clover)
    assert key not in mt.read_store(arch)["pins"]               # importance did NOT add it to Focus
    assert "star on" in TestClient(m.app).get("/todo").text     # the star shows the important (amber) state
    mt.set_pin(arch, key, True)                                 # now focus it via the clover
    store = mt.read_store(arch)
    assert key in store["pins"] and store["importance"].get(key) == "high"   # both axes held independently


def test_matters_page_renders_focus_and_happenings(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    _seed(monkeypatch, tmp_path)
    import app.main as m
    h = TestClient(m.app).get("/todo").text
    assert "Focus" in h and "Happenings" in h
    assert "Retention" in h and "retention held" in h        # subject (line 1) + summary TLDR (line 2)
    assert "Sort" in h and "urgency gauge" in h and "Save layout" in h   # the controls bar


def test_layout_route_persists(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    arch, rec = _seed(monkeypatch, tmp_path)
    import app.main as m
    r = TestClient(m.app).post("/matters/layout", data={"sort": "domain", "tags": "domain,refs", "gauge": 1})
    assert r.json()["ok"] and r.json()["sort"] == "domain"
    lay = mt.read_store(arch)["layout"]
    assert lay["sort"] == "domain" and lay["gauge"] is True and lay["tags"] == ["domain", "refs"]
