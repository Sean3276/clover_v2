import json

import pytest

from clover import profiles as pf


def test_from_to_dict_roundtrip():
    d = pf.to_dict(pf.get_profile())
    p = pf.from_dict(d)
    assert p.domain_names() == pf.get_profile().domain_names()
    assert p.safety_net == pf.get_profile().safety_net
    assert p.precedence == pf.get_profile().precedence


def test_from_dict_requires_taxonomy():
    with pytest.raises(ValueError):
        pf.from_dict({"domains": {}})


def test_from_dict_safety_net_falls_back_into_taxonomy():
    p = pf.from_dict({"domains": {"Ops": ["A", "B"]}, "safety_net": "NotThere"})
    assert p.safety_net == "A"               # invalid safety-net snaps to a real category


def test_effective_profile_uses_override_else_default():
    cfg = {"comprehension": {"profile": "construction", "profile_def": {
        "name": "construction", "domains": {"Ops": ["A", "B"]}, "safety_net": "A", "precedence": []}}}
    p = pf.effective_profile(cfg)
    assert p.domain_names() == ["Ops"] and p.categories("Ops") == ["A", "B"]
    assert pf.effective_profile({}).name == "construction"                       # no override -> default
    assert pf.effective_profile({"comprehension": {"profile_def": {"domains": {}}}}).name == "construction"  # invalid -> default


def _store_cfg(monkeypatch, m, cfg):
    store = {"cfg": cfg}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: json.loads(json.dumps(store["cfg"])))
    monkeypatch.setattr(m.cfgmod, "save_config", lambda c: store.__setitem__("cfg", c))
    return store


def test_profile_routes_show_save_reset(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    store = _store_cfg(monkeypatch, m, {"auth": {"imap": {}}, "archive_path": str(tmp_path),
                                        "comprehension": {"backend": "stub", "profile": "construction"}})
    c = TestClient(m.app)
    assert "Project: Commercial" in c.get("/profile").text          # default taxonomy is shown

    r = c.post("/profile/save", data={"description": "mine", "taxonomy": "Ops: Alpha, Beta\nAdmin: Gamma",
                                      "safety_net": "Alpha", "precedence": "vo, claim => Alpha"})
    assert r.json()["ok"] is True
    pdef = store["cfg"]["comprehension"]["profile_def"]
    assert pdef["domains"] == {"Ops": ["Alpha", "Beta"], "Admin": ["Gamma"]}
    assert pdef["precedence"] == [{"if_any": ["vo", "claim"], "then": "Alpha"}]

    body = c.get("/profile").text
    assert "Ops: Alpha, Beta" in body and "customised" in body
    assert pf.effective_profile(store["cfg"]).categories("Ops") == ["Alpha", "Beta"]

    assert c.post("/profile/save", data={"taxonomy": "", "safety_net": "", "precedence": ""}).status_code == 400
    assert c.post("/profile/reset").json()["ok"] is True
    assert "profile_def" not in store["cfg"]["comprehension"]


def test_account_under_mail_and_classification_under_hub(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    c = TestClient(m.app)
    setup = c.get("/setup").text                         # /setup is now the 'Account' page under Mail
    assert ">Mail<" in setup and "Account" in setup
    assert ">Settings<" not in setup                     # the Settings tab is retired
    hub = c.get("/projects").text                        # classification (profile + rules) folded under Hub
    assert 'href="/profile"' in hub and 'href="/rules"' in hub


def test_facets_roundtrip_and_effective():
    d = pf.to_dict(pf.get_profile())
    assert "Discipline" in d["facets"] and "Authority" in d["facets"]
    p = pf.from_dict(d)
    assert p.facet_values("Authority") == pf.get_profile().facet_values("Authority")
    cfg = {"comprehension": {"profile_def": {"domains": {"D": ["A"]}, "safety_net": "A",
                                             "facets": {"Trade": ["Mason", "Welder"]}}}}
    assert pf.effective_profile(cfg).facet_values("Trade") == ["Mason", "Welder"]


def test_profile_save_persists_facets(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    store = _store_cfg(monkeypatch, m, {"auth": {"imap": {}}, "archive_path": str(tmp_path),
                                        "comprehension": {"profile": "construction"}})
    c = TestClient(m.app)
    r = c.post("/profile/save", data={"taxonomy": "Project: Commercial, Safety", "safety_net": "Safety",
                                      "precedence": "", "facets": "Trade: Mason, Welder\nZone: A, B"})
    assert r.json()["ok"] is True
    assert store["cfg"]["comprehension"]["profile_def"]["facets"] == {"Trade": ["Mason", "Welder"], "Zone": ["A", "B"]}
    assert "Trade: Mason, Welder" in c.get("/profile").text
