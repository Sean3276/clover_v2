"""Bug (screenshot): `claude -p` timed out after 180s and the raw TimeoutExpired traceback was shown
to the user. The CLI timeout must be configurable with a sane default, and a timeout must surface as a
friendly, actionable message — never a raw Python exception string."""
import shutil
import subprocess

import pytest

from clover.comprehenders import ClaudeCliComprehender


def test_default_timeout_is_generous():
    # the decomposed pipeline makes several cold-start CLI calls; 180s was too tight
    assert ClaudeCliComprehender().timeout >= 300


def test_timeout_raises_friendly_error(monkeypatch):
    c = ClaudeCliComprehender(model="sonnet", timeout=5)
    monkeypatch.setattr(shutil, "which", lambda _name: "claude")
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5)
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        c.generate("comprehend", "some prompt")
    msg = str(ei.value)
    assert "timed out" in msg.lower()              # human-readable
    assert "comprehend" in msg                     # names the step that stalled
    assert "180 seconds" not in msg                # not the raw subprocess string
    assert not isinstance(ei.value, subprocess.TimeoutExpired)   # wrapped, not leaked


def test_comprehender_factory_passes_configured_timeout():
    import app.main as m
    cfg = {"comprehension": {"backend": "claude-cli", "model": "sonnet", "timeout_seconds": 420}}
    b = m._comprehender(cfg)
    assert b.timeout == 420


def test_set_timeout_clamps():
    from clover import models as mod
    cfg = {}
    assert mod.set_timeout(cfg, 99999) == 1200          # upper clamp
    assert mod.set_timeout(cfg, 5) == 30                # lower clamp
    assert mod.set_timeout(cfg, "x") == 300             # garbage -> default
    assert cfg["comprehension"]["timeout_seconds"] == 300


def test_dev_timeout_route_persists(monkeypatch, tmp_path):
    import clover.config as cfgmod
    from clover.paths import config_path
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    from starlette.testclient import TestClient
    import app.main as m
    r = TestClient(m.app).post("/dev/timeout", data={"seconds": 600})
    assert r.json()["ok"] and r.json()["timeout_seconds"] == 600
    assert (cfgmod.load_config().get("comprehension") or {}).get("timeout_seconds") == 600
    assert config_path()  # path resolves under the temp home
