import json

from clover import models as mdl
from clover import comprehend as cp
from clover.comprehenders import StubComprehender


# ── registry unit ─────────────────────────────────────────────────────────────
def test_upsert_assigns_id_sets_active_and_keeps_usage():
    cfg = {}
    mid = mdl.upsert_model(cfg, {"label": "Sonnet CLI", "backend": "claude-cli", "model": "sonnet", "token_budget": 1000})
    assert mid == "sonnet-cli"
    assert mdl.active_id(cfg) == mid                         # first model becomes active
    mdl.add_usage(cfg, mid, 250)
    mdl.upsert_model(cfg, {"id": mid, "label": "Sonnet", "backend": "claude-cli", "model": "sonnet", "token_budget": 1000})
    assert mdl.read_models(cfg)[0]["tokens_used"] == 250     # edit must not wipe accrued usage


def test_token_left_and_active_fallback():
    cfg = {"comprehension": {"models": [
        {"id": "a", "backend": "stub", "model": "x", "token_budget": 1000, "tokens_used": 400, "enabled": True},
        {"id": "b", "backend": "stub", "model": "y", "token_budget": 0, "tokens_used": 999, "enabled": True}]}}
    assert mdl.token_left(mdl.read_models(cfg)[0]) == 600
    assert mdl.token_left(mdl.read_models(cfg)[1]) is None    # 0 budget = untracked
    assert mdl.active_model(cfg)["id"] == "a"                 # no active_model set -> first enabled


def test_set_active_delete_and_disabled_skipped():
    cfg = {}
    mdl.upsert_model(cfg, {"id": "a", "backend": "stub", "label": "A"})
    mdl.upsert_model(cfg, {"id": "b", "backend": "stub", "label": "B"})
    assert mdl.set_active(cfg, "b") and mdl.active_id(cfg) == "b"
    assert not mdl.set_active(cfg, "nope")
    assert mdl.delete_model(cfg, "b") and mdl.active_id(cfg) == "a"   # active falls back after delete


def test_concurrency_get_set_clamped():
    cfg = {}
    assert mdl.get_concurrency(cfg) == 1                  # default = serial
    assert mdl.set_concurrency(cfg, 5) == 5 and mdl.get_concurrency(cfg) == 5
    assert mdl.set_concurrency(cfg, 999) == 16            # clamped to max
    assert mdl.set_concurrency(cfg, 0) == 1               # clamped to min
    assert mdl.set_concurrency(cfg, "x") == 1             # non-numeric -> 1


# ── token tracking + estimate ─────────────────────────────────────────────────
def test_stub_accrues_tokens():
    s = StubComprehender()
    s.generate("comprehend", "x" * 400)
    assert s.tokens >= 100


def test_estimate_caps_attachment_bytes(tmp_path):
    (tmp_path / "INBOX").mkdir(parents=True)
    (tmp_path / "INBOX" / "1.eml").write_bytes(b"x" * 5_000_000)      # 5 MB "attachment"
    t = {"members": [{"locations": [{"path": "INBOX/1.eml"}]}]}
    assert cp.estimate_thread_tokens(tmp_path, t) == 200_000 // 4     # capped, not 1.25M


def test_run_comprehension_reports_tokens(tmp_path):
    import json as _json
    from email.message import EmailMessage
    from clover import threads as th
    m = EmailMessage(); m["Message-ID"] = "<1>"; m["From"] = "a@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"; m.set_content("hello world")
    (tmp_path / "INBOX").mkdir(parents=True); (tmp_path / "INBOX" / "1.eml").write_bytes(m.as_bytes())
    (tmp_path / "_index.jsonl").write_text(_json.dumps(
        {"id": "1", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml", "date": m["Date"], "from": "a@x.com"}) + "\n",
        encoding="utf-8")
    th.build_threads(tmp_path, log=lambda *_: None)
    from clover.profiles import get_profile
    out = cp.run_comprehension(tmp_path, backend=StubComprehender(), profile=get_profile())
    assert out["done"] == 1 and out["tokens"] > 0


# ── dev routes ────────────────────────────────────────────────────────────────
def _store(monkeypatch, m, cfg):
    store = {"cfg": cfg}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: json.loads(json.dumps(store["cfg"])))
    monkeypatch.setattr(m.cfgmod, "save_config", lambda c: store.__setitem__("cfg", c))
    return store


def test_dev_routes_crud_and_active(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    monkeypatch.setenv("CLOVER_DEV", "1")
    store = _store(monkeypatch, m, {"auth": {"imap": {}}, "archive_path": str(tmp_path), "comprehension": {}})
    c = TestClient(m.app)
    assert "Developer controls" in c.get("/dev").text
    r = c.post("/dev/models/save", data={"label": "Opus", "backend": "claude-cli", "model": "opus", "token_budget": "5000", "enabled": "on"})
    assert r.json()["ok"] is True
    mid = r.json()["id"]
    assert store["cfg"]["comprehension"]["active_model"] == mid
    assert "Opus" in c.get("/dev").text
    assert c.post("/dev/models/save", data={"label": "Sonnet", "backend": "claude-cli", "model": "sonnet"}).json()["ok"]
    assert c.post("/dev/models/activate", data={"id": "sonnet"}).json()["ok"] is True
    assert store["cfg"]["comprehension"]["active_model"] == "sonnet"
    assert c.post("/dev/models/delete", data={"id": mid}).json()["ok"] is True


def test_dev_concurrency_route(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    monkeypatch.setenv("CLOVER_DEV", "1")
    store = _store(monkeypatch, m, {"auth": {"imap": {}}, "archive_path": str(tmp_path), "comprehension": {}})
    c = TestClient(m.app)
    r = c.post("/dev/concurrency", data={"n": "4"})
    assert r.json()["ok"] is True and r.json()["concurrency"] == 4
    assert store["cfg"]["comprehension"]["concurrency"] == 4
    assert 'id="conc"' in c.get("/dev").text             # control is rendered


def test_dev_gated_off(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    monkeypatch.setenv("CLOVER_DEV", "0")
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    assert TestClient(m.app).get("/dev").status_code == 403


def test_comprehender_uses_active_model(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "_comp_cfg", lambda cfg: {"backend": "claude-cli", "model": "sonnet"})
    cfg = {"comprehension": {"models": [{"id": "y", "backend": "claude-cli", "model": "opus", "enabled": True}],
                             "active_model": "y"}}
    assert m._comprehender(cfg).model == "opus"              # active model overrides legacy
