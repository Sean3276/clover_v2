"""Clover v2 — Phase 1 web-app (any MailSource -> local .eml archive).

Run from .clover_v2_github:
    python -m uvicorn app.main:app --port 8765
    open http://127.0.0.1:8765
"""
from __future__ import annotations

import shutil
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from clover import archive
from clover import comprehend as compmod
from clover import config as cfgmod
from clover import linkshares as lsmod
from clover import threads as threadmod
from clover.comprehenders import get_comprehender
from clover.errors import friendly_conn_error
from clover.paths import auto_clover_root, ensure_runtime, runtime_dir
from clover.profiles import get_profile
from clover.sources import SUITE, get_source

_THREAD_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
               'img-src data:; style-src \'unsafe-inline\'; font-src data:">')
# fit oversized inline images/tables to the reader width (emails embed full-resolution screenshots in
# fixed-width tables, so use !important to beat inline width= and cap the table/image to the container)
_THREAD_STYLE = ("<style>html{overflow-x:hidden}body{margin:0;padding:10px;background:#fff;color:#111;"
                 "font:14px/1.5 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;overflow-wrap:break-word}"
                 "img{max-width:100%!important;height:auto!important}"
                 "table{max-width:100%!important}td,th{max-width:100%}pre{white-space:pre-wrap}</style>")

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


def _run_archive_bg(folders: list[str], limit: int | None, filters: dict, auto_links: bool = False):
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
            _maybe_autorun_comprehension(cfg)       # comprehend the new threads (budget-capped)
        if auto_links and not _status.get("stop"):  # opt-in: catalogue + download share links after archiving
            if _start_link_task(lambda: _auto_link_task(_archive_dir(cfg))):
                _log("Auto share-links: cataloguing + downloading in the background "
                     "(oversize files wait for confirmation in Threads; 'Stop links' halts it).")
            else:
                _log("Auto share-links skipped — a link task is already running.")
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
                size_mode: str = Form("all"), size_mb: float = Form(0.0), top_n: int = Form(0),
                auto_links: str = Form("")):
    auto = auto_links.strip().lower() in ("1", "true", "on", "yes")
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
    threading.Thread(target=_run_archive_bg, args=(folders, limit or None, filters, auto), daemon=True).start()
    bits = []
    if df or dt:
        bits.append(f"{df or '…'}→{dt or '…'}")
    if size_min:
        bits.append(f"≥{size_mb:g} MB")
    if topn:
        bits.append(f"largest {topn}")
    suffix = (" · " + ", ".join(bits)) if bits else ""
    return JSONResponse({"ok": True, "message": f"Archiving {len(folders)} folder(s){suffix}"})


@app.post("/archive/reconcile")
def archive_reconcile():
    cfg = cfgmod.load_config()
    rep = archive.reconcile(_archive_dir(cfg))
    server = {}                                   # best-effort: server message count per folder
    pw = cfgmod.get_secret(cfgmod.SECRET_IMAP_PASSWORD)
    if pw and _imap_conn(cfg).get("host"):
        try:
            with get_source("imap", _imap_conn(cfg), pw) as src:
                for f in src.folders():
                    if f.get("messages") is not None:
                        server[f["name"]] = f["messages"]
        except Exception:
            server = {}
    rep["server"] = server
    return JSONResponse(rep)


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


# --- Phase 3 comprehension helpers (backend + policy gate are the future plug-in/subscription seams) ---
def _comp_cfg(cfg: dict) -> dict:
    c = dict(cfgmod.default_config()["comprehension"])
    c.update(cfg.get("comprehension") or {})
    return c


def _backend_available(cfg: dict) -> bool:
    if _comp_cfg(cfg)["backend"] == "claude-cli":
        return bool(shutil.which("claude"))
    return True


def _comprehender(cfg: dict):
    c = _comp_cfg(cfg)
    if c["backend"] == "claude-cli":
        return get_comprehender("claude-cli", model=c.get("model", "sonnet"))
    return get_comprehender(c["backend"])


def _comprehension_allowed(cfg: dict) -> bool:
    return True   # policy gate seam — subscription/tier entitlement check plugs in here later


def _maybe_autorun_comprehension(cfg: dict) -> None:
    c = _comp_cfg(cfg)
    if not c.get("autorun"):
        return
    if not _comprehension_allowed(cfg):
        _log("Comprehension autorun skipped (policy gate).")
        return
    if not _backend_available(cfg):
        _log("Comprehension autorun skipped — AI backend not set up "
             "(install Claude CLI: npm i -g @anthropic-ai/claude-code).")
        return
    _log("Comprehending new threads…")
    try:
        out = compmod.run_comprehension(
            _archive_dir(cfg), backend=_comprehender(cfg), profile=get_profile(c.get("profile")),
            budget_tokens=int(c.get("budget_tokens", 200000)), log=_log,
            should_stop=lambda: _status.get("stop", False),
            allowed=lambda: _comprehension_allowed(cfg),
        )
        _log(f"Comprehension: {out['done']} done, {out['pending']} pending.")
    except Exception as e:
        _log(f"Comprehension autorun failed: {type(e).__name__}: {e}")


@app.get("/threads", response_class=HTMLResponse)
def threads_page(request: Request):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    comps = {r["thread_id"]: r for r in compmod.read_comprehensions(arch) if r.get("thread_id")}
    link_stats = {}
    for r in lsmod.read_link_shares(arch):
        s = r.get("status", "pending")
        link_stats[s] = link_stats.get(s, 0) + 1
    return templates.TemplateResponse(request, "threads_list.html", {
        "cfg": cfg,
        "threads": threadmod.read_threads(arch),
        "has_index": (arch / "threads.jsonl").exists(),
        "comps": comps,
        "link_stats": link_stats,
        "link_total": sum(link_stats.values()),
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


def _wrap_srcdoc(body_html: str) -> str:
    """Clean standards-mode document for the sandboxed iframe (DOCTYPE first avoids quirks mode)."""
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">' + _THREAD_CSP + _THREAD_STYLE
            + "</head><body>" + body_html + "</body></html>")


@app.get("/threads/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    if not t:
        return RedirectResponse("/threads", status_code=303)
    # render only the lightweight header list from threads.jsonl; bodies load on demand below
    return templates.TemplateResponse(request, "thread_view.html", {
        "cfg": cfg, "thread": t,
        "comp": compmod.get_comprehension(arch, thread_id),
        "ai_ready": _backend_available(cfg),
    })


@app.post("/threads/{thread_id}/comprehend")
def thread_comprehend(thread_id: str):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    if not _comprehension_allowed(cfg):
        return JSONResponse({"ok": False, "message": "Comprehension isn't enabled for your plan."})
    if not _backend_available(cfg):
        return JSONResponse({"ok": False, "message":
                             "AI backend not set up — install the Claude CLI "
                             "(npm i -g @anthropic-ai/claude-code) and log in."})
    t = threadmod.get_thread(arch, thread_id)
    if not t:
        return JSONResponse({"ok": False, "message": "Thread not found."})
    c = _comp_cfg(cfg)
    try:
        rec = compmod.comprehend_thread(arch, t, _comprehender(cfg), get_profile(c.get("profile")),
                                        model=c.get("model", "?"))
        compmod.save_comprehension(arch, rec)
        return JSONResponse({"ok": True, "message": "Comprehended.", "classification": rec["classification"]})
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"})


@app.get("/threads/{thread_id}/msg/{idx}", response_class=HTMLResponse)
def thread_message(request: Request, thread_id: str, idx: int):
    """Render a single thread message on demand (so long threads stay light)."""
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    members = (t or {}).get("members", [])
    if not t or idx < 0 or idx >= len(members):
        return HTMLResponse('<p class="msg err" style="display:block">Message not found.</p>', status_code=404)
    locs = members[idx].get("locations") or []
    if not locs:
        return HTMLResponse('<p class="note dim">(no file for this message)</p>')
    try:
        block = threadmod.render_message(arch, locs[0])
    except Exception as e:
        return HTMLResponse(f'<p class="msg err" style="display:block">Couldn\'t render: {type(e).__name__}</p>')
    srcdoc = _wrap_srcdoc(block["body_html"]) if block.get("body_html") else None
    return templates.TemplateResponse(request, "thread_msg.html",
                                      {"block": block, "srcdoc": srcdoc, "tid": thread_id, "idx": idx,
                                       "links": lsmod.links_for_member(arch, members[idx].get("message_id"), locs)})


_linktask = {"running": False, "stop": False}   # ONE link task (harvest OR fetch) at a time


def _start_link_task(target) -> bool:
    with _lock:                  # atomic check-and-set (mirrors archive_run's discipline)
        if _linktask["running"]:
            return False
        _linktask["running"] = True
        _linktask["stop"] = False

    def runner():
        try:
            target()
        except Exception as e:
            _log(f"link task error: {type(e).__name__}: {e}")
        finally:
            with _lock:
                _linktask["running"] = False
    threading.Thread(target=runner, daemon=True).start()
    return True


def _auto_link_task(arch):
    """Post-archive automation: catalogue links, then download all pending in batches of 50 — each
    batch persists (crash-safe) and is stoppable; oversize links pause as needs-confirm."""
    lsmod.harvest(arch, log=_log)
    while not _linktask.get("stop"):
        r = lsmod.fetch_links(arch, limit=50, headless=True, log=_log,
                              should_stop=lambda: _linktask.get("stop", False))
        if r.get("remaining", 0) <= 0:
            break


@app.post("/threads/harvest-links")
def harvest_links():
    cfg = cfgmod.load_config()                   # background so it never blocks the request
    if not _start_link_task(lambda: lsmod.harvest(_archive_dir(cfg), log=_log)):
        return JSONResponse({"ok": False, "message": "A link task (harvest or fetch) is already running."})
    return JSONResponse({"ok": True, "message":
                         "Harvesting share links in the background — reopen Threads shortly to see the catalogue."})


@app.post("/threads/fetch-links")
def fetch_links_route(limit: int = Form(100)):
    cfg = cfgmod.load_config()
    if not _start_link_task(lambda: lsmod.fetch_links(
            _archive_dir(cfg), limit=limit, headless=True, log=_log,
            should_stop=lambda: _linktask.get("stop", False))):
        return JSONResponse({"ok": False, "message": "A link task (harvest or fetch) is already running."})
    return JSONResponse({"ok": True, "message":
                         "Fetching link files in the background (headless browser). "
                         "Reopen a thread to see statuses update."})


@app.post("/threads/stop-links")
def stop_links():
    _linktask["stop"] = True     # honored between links in fetch_links; finishes the in-flight one
    return JSONResponse({"ok": True, "message": "Stopping after the current link…"})


@app.post("/threads/confirm-link")
def confirm_link(message_id: str = Form(...), url: str = Form(...)):
    """User OK'd a large (multi-GB) link in the viewer — re-queue it past the size gate."""
    lsmod.mark_confirmed(_archive_dir(cfgmod.load_config()), message_id, url)
    return JSONResponse({"ok": True, "message": "Marked for download — click 'Fetch files' to fetch it."})


@app.get("/linkfile/{rel:path}")
def linkfile(rel: str):
    base = _archive_dir(cfgmod.load_config()).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or "_linkfiles" not in target.parts or not target.is_file():
        return Response("Not found", status_code=404)
    from urllib.parse import quote
    return Response(content=target.read_bytes(), media_type="application/octet-stream",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(target.name)}"})


@app.get("/threads/{thread_id}/msg/{idx}/att/{n}")
def thread_attachment(thread_id: str, idx: int, n: int):
    """Serve one attachment extracted from a member .eml (inline so images/PDFs open; saveable)."""
    cfg = cfgmod.load_config()
    t = threadmod.get_thread(_archive_dir(cfg), thread_id)
    members = (t or {}).get("members", [])
    if not t or not (0 <= idx < len(members)):
        return Response("Not found", status_code=404)
    locs = members[idx].get("locations") or []
    if not locs:
        return Response("Not found", status_code=404)
    try:
        got = threadmod.get_attachment(_archive_dir(cfg), locs[0], n)
    except Exception:
        got = None
    if not got:
        return Response("Not found", status_code=404)
    from urllib.parse import quote
    name, ctype, data = got
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
