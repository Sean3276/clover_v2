"""Pipeline-progress visibility: after the download ring hits 100%, the later phases (re-stitch threads,
fetch links, comprehend, refresh Rolodex) must report their own progress to /archive/status so the UI can
show a stepper + ETA instead of a misleading 'done' ring (punch-list points 3,4,6,8)."""
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    import app.main as m
    m._clear_phase() if hasattr(m, "_clear_phase") else m._status.__setitem__("phase", None)
    return m, TestClient(m.app)


def test_status_exposes_phase_default_none(client):
    m, c = client
    assert c.get("/archive/status").json()["phase"] is None      # idle -> no active phase


def test_set_phase_populates_shape_and_step(client):
    m, c = client
    m._set_phase("comprehend", done=3, total=10, current="RFI 22")
    ph = c.get("/archive/status").json()["phase"]
    assert ph["key"] == "comprehend" and ph["step"] == 4 and ph["of"] == 5   # 4th of 5 pipeline phases
    assert ph["done"] == 3 and ph["total"] == 10 and ph["current"] == "RFI 22"
    assert ph["label"] == "Comprehending threads" and ph["indeterminate"] is False


def test_set_phase_indeterminate_for_open_ended_stages(client):
    m, c = client
    m._set_phase("threads", indeterminate=True)                  # rebuild has no countable total
    ph = c.get("/archive/status").json()["phase"]
    assert ph["key"] == "threads" and ph["step"] == 2 and ph["indeterminate"] is True


def test_clear_phase_resets_to_none(client):
    m, c = client
    m._set_phase("contacts", indeterminate=True)
    m._clear_phase()
    assert c.get("/archive/status").json()["phase"] is None      # run finished -> phase cleared


def test_status_exposes_skipped_phases(client):
    m, c = client
    m._status["skipped_phases"] = []
    assert c.get("/archive/status").json()["skipped"] == []
    m._skip_phase("comprehend")
    assert "comprehend" in c.get("/archive/status").json()["skipped"]   # so the stepper shows it skipped, not ✓


def test_phase_results_record_real_outcomes(client):
    # the stepper must show the TRUTH: ok | partial | failed | skipped per phase (not a fake ✓ from phase order)
    m, c = client
    m._status["phase_results"] = {}
    m._set_phase("threads", indeterminate=True)            # active -> 'running', NOT a premature ✓
    m._fail_phase("comprehend", "boom")
    m._phase_result("links", "partial", done=3, total=10, errors=1)
    m._skip_phase("contacts")
    pr = c.get("/archive/status").json()["phase_results"]
    assert pr["threads"]["state"] == "running"
    assert pr["comprehend"]["state"] == "failed" and "boom" in pr["comprehend"]["reason"]
    assert pr["links"]["state"] == "partial" and pr["links"]["done"] == 3 and pr["links"]["errors"] == 1
    assert pr["contacts"]["state"] == "skipped"


def test_finalize_run_builds_honest_last_run_summary(client):
    m, c = client
    m._status["phase_results"] = {}
    m._status["session_saved"] = 140
    m._phase_result("comprehend", "partial", done=100, total=140, errors=12)
    m._finalize_run()
    lr = c.get("/archive/status").json()["last_run"]
    assert lr["imported"] == 140 and lr["comprehended"] == 100 and lr["comprehend_total"] == 140
    assert lr["comprehend_errors"] == 12


def test_autorun_off_marks_comprehend_skipped(client):
    # comprehension disabled -> the comprehend phase is recorded skipped (must NOT render as a completed ✓ step)
    m, c = client
    m._status["skipped_phases"] = []
    m._maybe_autorun_comprehension({"comprehension": {"autorun": False}})
    assert "comprehend" in m._status["skipped_phases"]


# ---- honest status: an ACTIVE/resultless phase must never paint a fake ✓ (the "done but not done" bug) ----

def test_set_phase_is_running_not_ok_until_completion(client):
    # a started phase is 'running', NOT 'ok' — only an explicit completion may mark 'ok' (no premature ✓)
    m, c = client
    m._status["phase_results"] = {}
    m._set_phase("download", indeterminate=True)
    assert c.get("/archive/status").json()["phase_results"]["download"]["state"] == "running"


def test_empty_import_skips_all_downstream_even_with_fetch_off(client, tmp_path):
    # no new mail + auto-links OFF must NOT leave threads/links/comprehend/contacts resultless (they render as ✓)
    m, c = client
    m._status["phase_results"] = {}
    m._status["skipped_phases"] = []
    m._post_import({}, tmp_path, [], False)
    sk = set(c.get("/archive/status").json()["skipped"])
    assert {"threads", "links", "comprehend", "contacts"} <= sk


def test_mark_active_phase_failed_records_failure(client):
    # a crash mid-phase must flip that phase to 'failed', not leave its optimistic 'running'/'ok'
    m, c = client
    m._status["phase_results"] = {}
    m._set_phase("download", indeterminate=True)
    m._mark_active_phase_failed(RuntimeError("imap blew up"))
    pr = c.get("/archive/status").json()["phase_results"]["download"]
    assert pr["state"] == "failed" and "imap blew up" in pr["reason"]


def test_links_outcome_partial_when_files_unfetched(client, tmp_path, monkeypatch):
    # links phase is 'partial' (not ok ✓) when scoped links remain pending / needs-auth / dead
    m, c = client
    recs = [{"message_id": "a", "status": "downloaded"},
            {"message_id": "a", "status": "needs-auth"},
            {"message_id": "b", "status": "pending"}]
    monkeypatch.setattr(m.lsmod, "read_link_shares", lambda _a: recs)
    state, done, total, errors, _r = m._links_phase_outcome(tmp_path, {"a", "b"}, True)
    assert state == "partial" and done == 1 and total == 3 and errors >= 1


def test_links_outcome_ok_when_all_resolved(client, tmp_path, monkeypatch):
    m, c = client
    recs = [{"message_id": "a", "status": "downloaded"}, {"message_id": "b", "status": "reused"}]
    monkeypatch.setattr(m.lsmod, "read_link_shares", lambda _a: recs)
    state, done, total, errors, _r = m._links_phase_outcome(tmp_path, {"a", "b"}, True)
    assert state == "ok" and done == 2 and total == 2 and errors == 0


def test_links_outcome_skipped_when_not_fetched(client, tmp_path):
    # catalogue-only (fetch off) must read as 'skipped', not a download ✓
    m, c = client
    state, *_ = m._links_phase_outcome(tmp_path, {"a"}, False)
    assert state == "skipped"
