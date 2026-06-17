"""Clover v2 — Phase 1 web-app (any MailSource -> local .eml archive).

Run from .clover_v2_github:
    python -m uvicorn app.main:app --port 8765
    open http://127.0.0.1:8765
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from clover import archive
from clover import config as cfgmod
from clover.paths import ensure_runtime, runtime_dir
from clover.sources import SUITE, get_source

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="Clover v2 — Phase 1")

_lock = threading.Lock()
# live run state — per-folder progress drives the dashboard
_status = {"running": False, "started_at": None, "folders": {}, "log": [], "manifest": None,
           "stop": False, "attempt": 0, "session_saved": 0}


def _log(line: str) -> None:
    with _lock:
        _status["log"].append(str(line))
        if len(_status["log"]) > 1000:            # bound memory on long auto-resume runs
            del _status["log"][:len(_status["log"]) - 1000]


def _progress(p: dict) -> None:
    with _lock:
        _status["folders"][p["folder"]] = p


def _imap_conn(cfg: dict) -> dict:
    return cfg["auth"].get("imap", {})


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/setup")


# ------------------------------------------------------------------ Setup
@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    cfg = cfgmod.load_config()
    return templates.TemplateResponse(request, "setup.html", {
        "cfg": cfg, "error": None, "suite": SUITE,
        "has_pw": cfgmod.has_secret(cfgmod.SECRET_IMAP_PASSWORD),
    })


@app.post("/setup/test-imap")
def test_imap(host: str = Form(...), port: int = Form(993), security: str = Form("ssl"),
              user: str = Form(...), password: str = Form("")):
    pw = password or cfgmod.get_secret(cfgmod.SECRET_IMAP_PASSWORD) or ""
    conn = {"host": host, "port": port, "security": security, "user": user}
    ok, msg = get_source("imap", conn, pw).test()
    return JSONResponse({"ok": ok, "message": msg})


@app.post("/setup/save")
def setup_save(request: Request, host: str = Form(...), port: int = Form(993),
               security: str = Form("ssl"), user: str = Form(...), password: str = Form(""),
               archive_path: str = Form("")):
    ensure_runtime()
    cfg = cfgmod.load_config()
    cfg["auth"]["imap"] = {"host": host, "port": port, "security": security, "user": user}
    if archive_path.strip():
        cfg["archive_path"] = archive_path.strip()
    cfgmod.save_config(cfg)
    if password:
        try:
            cfgmod.set_secret(cfgmod.SECRET_IMAP_PASSWORD, password)
        except RuntimeError as e:
            return templates.TemplateResponse(request, "setup.html", {
                "cfg": cfg, "suite": SUITE,
                "has_pw": cfgmod.has_secret(cfgmod.SECRET_IMAP_PASSWORD),
                "error": f"Settings saved, but the password could NOT be stored: {e}",
            })
    return RedirectResponse("/archive", status_code=303)


# ------------------------------------------------------------------ Archive
@app.get("/archive", response_class=HTMLResponse)
def archive_page(request: Request):
    cfg = cfgmod.load_config()
    folders, folder_err = [], None
    pw = cfgmod.get_secret(cfgmod.SECRET_IMAP_PASSWORD)
    if pw and _imap_conn(cfg).get("host"):
        try:
            with get_source("imap", _imap_conn(cfg), pw) as src:
                folders = src.folders()
        except Exception as e:
            folder_err = f"{type(e).__name__}: {e}"
    try:
        summary = archive.index_summary(Path(cfg.get("archive_path") or "."))
    except Exception:
        summary = {"total": 0, "unique_ids": 0, "per_folder": {}}
    with _lock:
        running = _status["running"]
    return templates.TemplateResponse(request, "archive.html", {
        "cfg": cfg, "folders": folders, "folder_err": folder_err,
        "selected": set(cfg.get("folders") or []), "summary": summary,
        "running": running, "runtime": str(runtime_dir()),
    })


def _run_archive_bg(folders: list[str], limit: int | None, auto_resume: bool):
    try:
        cfg = cfgmod.load_config()
        cfg["folders"] = folders
        cfgmod.save_config(cfg)
        pw = cfgmod.get_secret(cfgmod.SECRET_IMAP_PASSWORD) or ""
        if not pw:
            _log("No IMAP password stored — save credentials on Setup first.")
            return

        def run_once():
            with _lock:
                _status["attempt"] += 1
            m = archive.run_archive(cfg, pw, log=_log, limit_per_folder=limit, progress=_progress)
            with _lock:
                _status["manifest"] = m
                _status["session_saved"] += m.get("saved", 0)
            return m

        archive.run_until_complete(
            run_once, auto_resume=auto_resume, log=_log, sleep=time.sleep,
            should_stop=lambda: _status.get("stop", False),
        )
    except Exception as e:
        _log(f"ERROR: {type(e).__name__}: {e}")
    finally:
        with _lock:
            _status["running"] = False


@app.post("/archive/run")
def archive_run(folders: list[str] = Form(default=[]), limit: int = Form(0),
                auto_resume: str = Form("off")):
    ar = str(auto_resume).lower() in ("on", "true", "1", "yes")
    with _lock:
        if _status["running"]:
            return JSONResponse({"ok": False, "message": "An archive run is already in progress."})
        if not folders:
            return JSONResponse({"ok": False, "message": "Select at least one folder."})
        _status.update(running=True, started_at=time.time(), folders={}, log=[], manifest=None,
                       stop=False, attempt=0, session_saved=0)
    threading.Thread(target=_run_archive_bg, args=(folders, limit or None, ar), daemon=True).start()
    return JSONResponse({"ok": True,
                         "message": f"Archiving {len(folders)} folder(s)" + (" · auto-resume on" if ar else "")})


@app.post("/archive/stop")
def archive_stop():
    with _lock:
        _status["stop"] = True
    return JSONResponse({"ok": True, "message": "Stopping after the current attempt…"})


@app.get("/archive/status")
def archive_status():
    with _lock:
        return JSONResponse({
            "running": _status["running"],
            "started_at": _status["started_at"],
            "folders": list(_status["folders"].values()),
            "log": _status["log"][-200:],   # cap payload on long runs
            "manifest": _status["manifest"],
            "attempt": _status["attempt"],
            "session_saved": _status["session_saved"],
        })
