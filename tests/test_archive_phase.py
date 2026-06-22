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


def test_autorun_off_marks_comprehend_skipped(client):
    # comprehension disabled -> the comprehend phase is recorded skipped (must NOT render as a completed ✓ step)
    m, c = client
    m._status["skipped_phases"] = []
    m._maybe_autorun_comprehension({"comprehension": {"autorun": False}})
    assert "comprehend" in m._status["skipped_phases"]
