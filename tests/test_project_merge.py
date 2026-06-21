"""Job 3: Project MERGE — fold two projects into one (mirrors the GreenBook firm-merge). Persisted in
project_merges.json and applied when the project index is built; the merge TARGET's name wins."""
import json

import clover.projects as pj
from clover.comprehend import comprehension_path


def _write_comp(tmp, recs):
    with comprehension_path(tmp).open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def test_merge_folds_projects_target_name_wins(tmp_path):
    _write_comp(tmp_path, [
        {"thread_id": "t1", "subject": "s1", "facts": {"project": "Tower A"},
         "classification": {"category": "RFI"}},
        {"thread_id": "t2", "subject": "s2", "facts": {"project": "Tower A"},
         "classification": {"category": "RFI"}},
        {"thread_id": "t3", "subject": "s3", "facts": {"project": "Twr-A Annex"},
         "classification": {"category": "VO"}},
    ])
    assert len(pj.list_projects(tmp_path)) == 2
    target = pj.project_key("Tower A"); src = pj.project_key("Twr-A Annex")
    assert pj.set_merge(tmp_path, src, target)            # fold the annex into Tower A
    after = pj.list_projects(tmp_path)
    assert len(after) == 1
    p = after[0]
    assert p["count"] == 3 and p["name"] == "Tower A"     # target's name wins, all threads folded
    assert {t["thread_id"] for t in p["threads"]} == {"t1", "t2", "t3"}


def test_merge_refuses_self_and_cycle(tmp_path):
    assert not pj.set_merge(tmp_path, "a", "a")           # self
    assert pj.set_merge(tmp_path, "a", "b")
    assert not pj.set_merge(tmp_path, "b", "a")           # would cycle


def test_unmerge_restores(tmp_path):
    _write_comp(tmp_path, [
        {"thread_id": "t1", "subject": "s1", "facts": {"project": "Alpha"}},
        {"thread_id": "t2", "subject": "s2", "facts": {"project": "Beta"}},
    ])
    pj.set_merge(tmp_path, pj.project_key("Beta"), pj.project_key("Alpha"))
    assert len(pj.list_projects(tmp_path)) == 1
    assert pj.unmerge(tmp_path, pj.project_key("Beta"))
    assert len(pj.list_projects(tmp_path)) == 2


def test_merge_route_resolves_target_by_typed_name(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    from clover.paths import default_archive_path
    arch = default_archive_path(); arch.mkdir(parents=True, exist_ok=True)
    with comprehension_path(arch).open("w", encoding="utf-8") as f:
        f.write(json.dumps({"thread_id": "t1", "root_id": "r1", "subject": "s", "facts": {"project": "Tower A"}}) + "\n")
        f.write(json.dumps({"thread_id": "t2", "root_id": "r2", "subject": "s", "facts": {"project": "Twr Annex"}}) + "\n")
    import app.main as m
    c = TestClient(m.app)
    src = pj.project_key("Twr Annex")
    r = c.post("/projects/merge", data={"from_key": src, "into": "Tower A"}).json()    # firm-style: type the target name
    assert r["ok"] and "Tower A" in r["message"]
    assert len(pj.list_projects(arch)) == 1
    bad = c.post("/projects/merge", data={"from_key": src, "into": "Nonexistent"}).json()
    assert bad["ok"] is False


def test_get_project_resolves_merged_key(tmp_path):
    _write_comp(tmp_path, [
        {"thread_id": "t1", "subject": "s1", "facts": {"project": "Alpha"}},
        {"thread_id": "t2", "subject": "s2", "facts": {"project": "Beta"}},
    ])
    pj.set_merge(tmp_path, pj.project_key("Beta"), pj.project_key("Alpha"))
    # an old link to the merged-away project still resolves to the canonical one
    p = pj.get_project(tmp_path, pj.project_key("Beta"))
    assert p and p["key"] == pj.project_key("Alpha") and p["count"] == 2
