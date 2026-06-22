import json

from clover import comprehend as cp
from clover import companies


def _threads(tmp, rows):
    with (tmp / "threads.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _contacts(tmp, rows):
    with (tmp / "contacts.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _person(email, name, company, key, **kw):
    d = {"email": email, "name": name, "position": "", "company": company, "company_key": key,
         "domain": email.split("@")[1], "phone": "", "count": kw.get("count", 1), "aliases": []}
    d.update(kw)
    return d


def _seed(tmp):
    cp.save_comprehension(tmp, {"thread_id": "t1", "subject": "Bridge", "summary": "deck",
                                "facts": {"project": "Riverside Bridge"},
                                "classification": {"domain": "Project", "category": "Design"}})
    cp.save_comprehension(tmp, {"thread_id": "t2", "subject": "Tender", "summary": "tender",
                                "facts": {"project": "Tower X"},
                                "classification": {"domain": "Project", "category": "Tender"}})
    _threads(tmp, [
        {"thread_id": "t1", "participants": ["alice@globex.com", "bob@buildco.com", "me@here.com"]},
        {"thread_id": "t2", "participants": ["alice@globex.com", "me@here.com"]},
    ])
    _contacts(tmp, [
        _person("alice@globex.com", "Alice Tan", "Globex Construction Company Ltd",
                "globexconstructioncompany", count=5),
        _person("bob@buildco.com", "Bob Lee", "BuildCo Pte Ltd", "buildco", count=3),
        _person("me@here.com", "Owner", "", "", count=9),
    ])


# ── company codes ─────────────────────────────────────────────────────────────
def test_gen_code_from_full_name():
    assert companies.gen_code("Globex Construction Company Limited (Singapore Branch)") == "GCC"
    assert companies.gen_code("Hooli Holdings Pte Ltd") == "HH"
    assert companies.gen_code("Singapore Land Authority") == "SLA"


def test_set_and_read_code_override(tmp_path):
    assert companies.set_code(tmp_path, "globex", "4C") == "4C"
    assert companies.read_codes(tmp_path)["globex"] == "4C"
    companies.set_code(tmp_path, "globex", "")            # blank clears
    assert "globex" not in companies.read_codes(tmp_path)


# ── green book ────────────────────────────────────────────────────────────────
def test_list_companies_groups_with_codes_and_projects(tmp_path):
    _seed(tmp_path)
    data = companies.list_companies(tmp_path)
    by = {c["name"]: c for c in data["companies"]}
    assert set(by) == {"Globex Construction Company Ltd", "BuildCo Pte Ltd"}
    assert data["individuals_count"] == 1               # the no-company owner
    assert by["Globex Construction Company Ltd"]["code"] == "GCC"
    assert by["Globex Construction Company Ltd"]["projects"] == ["Riverside Bridge", "Tower X"]
    assert by["BuildCo Pte Ltd"]["projects"] == ["Riverside Bridge"]


def test_company_description_roundtrip_and_in_listing(tmp_path):
    # operator-editable "what this firm does" — deterministic (entered, not AI-inferred), surfaced on the card
    _seed(tmp_path)
    companies.set_description(tmp_path, "globexconstructioncompany",
                             "Façade engineering & cladding subcontractor.")
    assert companies.read_descriptions(tmp_path)["globexconstructioncompany"] == \
        "Façade engineering & cladding subcontractor."
    by = {c["name"]: c for c in companies.list_companies(tmp_path)["companies"]}
    assert by["Globex Construction Company Ltd"]["description"] == "Façade engineering & cladding subcontractor."
    assert by["BuildCo Pte Ltd"]["description"] == ""                  # default empty when unset
    companies.set_description(tmp_path, "globexconstructioncompany", "")   # blank clears
    assert "globexconstructioncompany" not in companies.read_descriptions(tmp_path)


def test_list_companies_applies_code_override(tmp_path):
    _seed(tmp_path)
    companies.set_code(tmp_path, "globexconstructioncompany", "4C")
    by = {c["name"]: c for c in companies.list_companies(tmp_path)["companies"]}
    assert by["Globex Construction Company Ltd"]["code"] == "4C"


def test_unverified_firm_shows_domain_and_name_override(tmp_path):
    _threads(tmp_path, [])
    _contacts(tmp_path, [_person("a@alphadc.com", "Alpha One", "", "alphadc.com")])  # no company name
    f = companies.list_companies(tmp_path)["companies"][0]
    assert f["unverified"] is True and f["name"] == "alphadc.com"      # domain shown, NOT faked as a name
    companies.set_name(tmp_path, "alphadc.com", "Project Alpha Data Centre Pte Ltd")
    f2 = companies.list_companies(tmp_path)["companies"][0]
    assert f2["unverified"] is False and f2["name"] == "Project Alpha Data Centre Pte Ltd"
    companies.set_name(tmp_path, "alphadc.com", "")                    # blank clears
    assert companies.list_companies(tmp_path)["companies"][0]["unverified"] is True


def test_auto_codes_are_unique(tmp_path):
    _threads(tmp_path, [])
    _contacts(tmp_path, [
        _person("a@redeng.com", "A", "Red Engineering Pte Ltd", "redengineering"),
        _person("b@realestate.com", "B", "Real Estate Pte Ltd", "realestate"),
    ])
    codes = [c["code"] for c in companies.list_companies(tmp_path)["companies"]]
    assert len(codes) == len(set(codes))               # RE collision disambiguated


# ── per-project ───────────────────────────────────────────────────────────────
def test_project_contacts_enriched_and_sorted(tmp_path):
    _seed(tmp_path)
    people = companies.project_contacts(tmp_path, "riverside bridge")
    assert {p["email"] for p in people} == {"alice@globex.com", "bob@buildco.com", "me@here.com"}
    assert people[-1]["email"] == "me@here.com"          # no-company sorts last
    assert {p["email"]: p for p in people}["alice@globex.com"]["company"].startswith("Globex")


def test_project_companies_lists_firms_with_counts(tmp_path):
    _seed(tmp_path)
    firms = companies.project_companies(tmp_path, "riverside bridge")
    names = {f["name"] for f in firms}
    assert "Globex Construction Company Ltd" in names and "BuildCo Pte Ltd" in names
    assert all(f["count"] >= 1 for f in firms)


# ── web wiring ────────────────────────────────────────────────────────────────
def test_routes_green_book_code_and_project(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _seed(tmp_path)
    cfg = {"auth": {"imap": {}}, "archive_path": str(tmp_path),
           "comprehension": {"backend": "stub", "profile": "construction"}}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    client = TestClient(m.app)

    co = client.get("/contacts").text
    assert "Globex Construction Company Ltd" in co and "GCC" in co and "Riverside Bridge" in co
    assert client.get("/companies").status_code == 200          # redirect -> contacts (followed)
    r = client.post("/contacts/code", data={"key": "globexconstructioncompany", "code": "4C"})
    assert r.json() == {"ok": True, "code": "4C"}
    rn = client.post("/contacts/name", data={"key": "buildco", "name": "BuildCo Holdings Pte Ltd"})
    assert rn.json() == {"ok": True, "name": "BuildCo Holdings Pte Ltd"}
    assert "BuildCo Holdings Pte Ltd" in client.get("/contacts").text
    pd = client.get("/projects/riverside bridge").text
    assert "Firms involved" in pd and "bob@buildco.com" in pd


def test_code_in_use_and_route_rejects_duplicate(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _seed(tmp_path)
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    c = TestClient(m.app)
    assert c.post("/contacts/code", data={"key": "buildco", "code": "ZZ"}).json()["ok"] is True
    r = c.post("/contacts/code", data={"key": "globexconstructioncompany", "code": "ZZ"})
    assert r.json()["ok"] is False and "BuildCo" in r.json()["message"]
    assert companies.code_in_use(tmp_path, "ZZ", exclude_key="other") == "BuildCo Pte Ltd"
    assert companies.code_in_use(tmp_path, "ZZ", exclude_key="buildco") == ""   # the holder itself is excluded


def test_qaqc_flags_duplicate_and_odd_names(tmp_path):
    _threads(tmp_path, [])
    _contacts(tmp_path, [
        _person("a@x.com", "A", "for Bad Name Pte Ltd", "forbadname"),
        _person("b@northwind.com", "B", "Northwind Pte Ltd", "northwind"),
        _person("c@northwind2.com", "C", "Northwind Pte Ltd", "northwind2"),     # same display name, different key
    ])
    types = {i["type"] for i in companies.qaqc(tmp_path)}
    assert "check name" in types and "possible duplicate" in types


def test_contacts_empty_state(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    monkeypatch.setattr(m.cfgmod, "load_config",
                        lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    r = TestClient(m.app).get("/contacts")
    assert r.status_code == 200 and "No contacts yet" in r.text


def test_csv_exports(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _seed(tmp_path)
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    c = TestClient(m.app)
    r = c.get("/contacts/export.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "Company,Code,Name" in r.text and "Globex Construction Company Ltd" in r.text
    rp = c.get("/projects/riverside bridge/people.csv")
    assert rp.status_code == 200 and "bob@buildco.com" in rp.text and "Name,Company" in rp.text


def test_firm_merge_and_unmerge(tmp_path):
    _threads(tmp_path, [])
    _contacts(tmp_path, [
        _person("a@x.com", "A", "Globex Pte Ltd", "globex", count=5),
        _person("b@y.com", "B", "Globex Asia Pte Ltd", "globexasia", count=1),
    ])
    assert len(companies.list_companies(tmp_path)["companies"]) == 2
    assert companies.set_merge(tmp_path, "globexasia", "globex") is True
    book = companies.list_companies(tmp_path)["companies"]
    assert len(book) == 1 and book[0]["count"] == 2          # folded: both people in one firm
    assert companies.set_merge(tmp_path, "globex", "globexasia") is False    # cycle refused
    assert companies.unmerge(tmp_path, "globexasia") is True
    assert len(companies.list_companies(tmp_path)["companies"]) == 2


def test_merge_route_by_code(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _threads(tmp_path, [])
    _contacts(tmp_path, [_person("a@x.com", "A", "Globex Pte Ltd", "globex"),
                         _person("b@y.com", "B", "Globex Asia Pte Ltd", "globexasia")])
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    c = TestClient(m.app)
    code = {x["key"]: x["code"] for x in companies.list_companies(tmp_path)["companies"]}["globex"]
    assert c.post("/contacts/merge", data={"from_key": "globexasia", "into_code": code}).json()["ok"] is True
    assert len(companies.list_companies(tmp_path)["companies"]) == 1
    assert c.post("/contacts/merge", data={"from_key": "globex", "into_code": "NOPE"}).json()["ok"] is False
