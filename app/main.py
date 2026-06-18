"""Clover v2 — Phase 1 web-app (any MailSource -> local .eml archive).

Run from .clover_v2_github:
    python -m uvicorn app.main:app --port 8765
    open http://127.0.0.1:8765
"""
from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from clover import archive
from clover import config as cfgmod
from clover import threads as threadmod
from clover.errors import friendly_conn_error
from clover.paths import auto_clover_root, ensure_runtime, runtime_dir
from clover.sources import SUITE, get_source

_THREAD_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
               'img-src data:; style-src \'unsafe-inline\'; font-src data:">')

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="Clover v2 — Phase 1")

_lock = threading.Lock()
# live run state — per-folder progress drives the dashboard
_status = {"running": False, "started_at": None, "folders": {}, "log": [], "manifest": None,
           "stop": False, "attempt": 0, "session_saved": 0, "prep": None}


def _log(line: str) -> None:
    with _lock:
        _status["log"].append(str(line))
        if len(_status["log"]) > 1000:            # bound memory on long auto-resume runs
            del _status["log"][:len(_status["log"]) - 1000]


def _progress(p: dict) -> None:
    with _lock:
        _status["folders"][p["folder"]] = p


def _prep(p: dict | None) -> None:
    with _lock:
        _status["prep"] = p


def _imap_conn(cfg: dict) -> dict:
    return cfg["auth"].get("imap", {})


def _list_folders(cfg: dict, pw: str, attempts: int = 3):
    """List folders, retrying a few times so a transient DNS/connection blip doesn't dump the
    user to an error screen. Returns (folders, error_string_or_None)."""
    last = None
    for i in range(attempts):
        try:
            with get_source("imap", _imap_conn(cfg), pw) as src:
                return src.folders(), None
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(1.2)
    return [], friendly_conn_error(last)


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
        folders, folder_err = _list_folders(cfg, pw)
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


def _run_archive_bg(folders: list[str], limit: int | None, filters: dict):
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
            m = archive.run_archive(cfg, pw, log=_log, limit_per_folder=limit, progress=_progress,
                                    should_stop=lambda: _status.get("stop", False),
                                    filters=filters, prep=_prep)
            with _lock:
                _status["manifest"] = m
                _status["session_saved"] += m.get("saved", 0)
            return m

        archive.run_until_complete(            # auto-resume is always on (rides out flaky links)
            run_once, auto_resume=True, log=_log, sleep=time.sleep,
            should_stop=lambda: _status.get("stop", False),
        )
        if _status.get("session_saved", 0) > 0:    # new mail landed -> refresh the thread index once
            _log("Rebuilding thread index…")
            try:
                threadmod.build_threads(_archive_dir(cfg), log=_log)
            except Exception as e:
                _log(f"Thread index rebuild failed: {type(e).__name__}: {e}")
    except Exception as e:
        _log(f"ERROR: {type(e).__name__}: {e}")
    finally:
        with _lock:
            _status["running"] = False
            _status["prep"] = None


def _parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


@app.post("/archive/run")
def archive_run(folders: list[str] = Form(default=[]), limit: int = Form(0),
                date_from: str = Form(""), date_to: str = Form(""),
                size_mode: str = Form("all"), size_mb: float = Form(0.0), top_n: int = Form(0)):
    df, dt = _parse_date(date_from), _parse_date(date_to)
    size_min = int(size_mb * 1024 * 1024) if (size_mode == "min" and size_mb and size_mb > 0) else None
    topn = top_n if (size_mode == "top" and top_n and top_n > 0) else None
    if df and dt and df > dt:
        return JSONResponse({"ok": False, "message": "‘From’ date is after ‘To’ date."})
    filters = {"date_from": df, "date_to": dt, "size_min": size_min, "top_n": topn}
    with _lock:
        if _status["running"]:
            return JSONResponse({"ok": False, "message": "An archive run is already in progress."})
        if not folders:
            return JSONResponse({"ok": False, "message": "Select at least one folder."})
        _status.update(running=True, started_at=time.time(), folders={}, log=[], manifest=None,
                       stop=False, attempt=0, session_saved=0, prep=None)
    threading.Thread(target=_run_archive_bg, args=(folders, limit or None, filters), daemon=True).start()
    bits = []
    if df or dt:
        bits.append(f"{df or '…'}→{dt or '…'}")
    if size_min:
        bits.append(f"≥{size_mb:g} MB")
    if topn:
        bits.append(f"largest {topn}")
    suffix = (" · " + ", ".join(bits)) if bits else ""
    return JSONResponse({"ok": True, "message": f"Archiving {len(folders)} folder(s){suffix}"})


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
            "prep": _status["prep"],
        })


# ------------------------------------------------------------------ Threads (Phase 2)
def _archive_dir(cfg: dict) -> Path:
    p = Path(cfg.get("archive_path") or ".")
    if not p.is_absolute():
        p = auto_clover_root() / p
    return p


@app.get("/threads", response_class=HTMLResponse)
def threads_page(request: Request):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    return templates.TemplateResponse(request, "threads_list.html", {
        "cfg": cfg,
        "threads": threadmod.read_threads(arch),
        "has_index": (arch / "threads.jsonl").exists(),
    })


@app.post("/threads/rebuild")
def threads_rebuild():
    cfg = cfgmod.load_config()
    try:
        s = threadmod.build_threads(_archive_dir(cfg), log=_log)
        return JSONResponse({"ok": True, "message":
                             f"{s['threads']} threads ({s['multi']} multi · {s['singletons']} single) "
                             f"from {s['messages']} messages"})
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"})


@app.get("/threads/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    if not t:
        return RedirectResponse("/threads", status_code=303)
    blocks = threadmod.stitch_thread(arch, t)
    for b in blocks:                          # wrap HTML bodies for the sandboxed iframe (block remote content)
        if b.get("body_html"):
            b["srcdoc"] = _THREAD_CSP + b["body_html"]
    return templates.TemplateResponse(request, "thread_view.html", {
        "cfg": cfg, "thread": t, "blocks": blocks,
    })
