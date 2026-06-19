from clover import comprehend as cp
from clover import projects


def _comp(tmp, tid, project, category="Commercial"):
    cp.save_comprehension(tmp, {"thread_id": tid, "subject": "S " + tid, "summary": "sum " + tid,
                                "facts": {"project": project},
                                "classification": {"domain": "Project", "category": category}})


def test_list_projects_groups_normalizes_and_sorts(tmp_path):
    _comp(tmp_path, "t1", "Marina Bridge")
    _comp(tmp_path, "t2", " marina  bridge ")       # same project, different case/spacing
    _comp(tmp_path, "t3", "Tower X", "Safety")
    _comp(tmp_path, "t4", "")                        # no project -> excluded
    ps = projects.list_projects(tmp_path)
    assert len(ps) == 2
    assert ps[0]["count"] == 2 and ps[0]["name"] == "Marina Bridge"   # merged + busiest first
    assert {p["name"] for p in ps} == {"Marina Bridge", "Tower X"}
    detail = projects.get_project(tmp_path, ps[0]["key"])
    assert detail and len(detail["threads"]) == 2
    assert projects.get_project(tmp_path, "nonexistent") is None
