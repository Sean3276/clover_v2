"""Clover v2 — Phase 1 web-app (any MailSource -> local .eml archive).

Run from .clover_v2_github:
    python -m uvicorn app.main:app --port 8765
    open http://127.0.0.1:8765
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from clover import archive
from clover import companies as companiesmod
from clover import compose as composemod
from clover import comprehend as compmod
from clover import contacts as contactsmod
from clover import config as cfgmod
from clover import linkshares as lsmod
from clover import models as modelsmod
from clover import projects as projmod
from clover import rules as rulesmod
from clover import sender as sendermod
from clover import threads as threadmod
from clover import todo as todomod
from clover.comprehenders import get_comprehender
from clover.errors import friendly_conn_error
from clover.paths import auto_clover_root, ensure_runtime
from clover.profiles import get_profile, effective_profile, from_dict as _profile_from_dict
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

from app.dev import router as _dev_router   # developer-only control panel (/dev), separate module
app.include_router(_dev_router)

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
    cfg = cfgmod.load_config()
    configured = bool((_imap_conn(cfg) or {}).get("host")) or (_archive_dir(cfg) / "threads.jsonl").exists()
    return RedirectResponse("/threads" if configured else "/setup")   # returning user -> views; first-run -> Settings


# ------------------------------------------------------------------ Setup
@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    cfg = cfgmod.load_config()
    return templates.TemplateResponse(request, "setup.html", {
        "cfg": cfg, "error": None, "suite": SUITE,
        "has_pw": cfgmod.has_secret(cfgmod.SECRET_IMAP_PASSWORD),
        "has_smtp_pw": cfgmod.has_secret(cfgmod.SECRET_SMTP_PASSWORD),
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
               archive_path: str = Form(""),
               smtp_host: str = Form(""), smtp_port: int = Form(587), smtp_security: str = Form("starttls"),
               send_from: str = Form(""), sending_enabled: str = Form(""), smtp_password: str = Form("")):
    ensure_runtime()
    cfg = cfgmod.load_config()
    cfg["auth"]["imap"] = {"host": host, "port": port, "security": security, "user": user}
    if archive_path.strip():
        cfg["archive_path"] = archive_path.strip()
    prev_send = cfg.get("sending") or {}
    cfg["sending"] = {
        "enabled": sending_enabled.strip().lower() in ("1", "true", "on", "yes"),
        "smtp": {"host": smtp_host.strip(), "port": smtp_port, "security": smtp_security},
        "from": send_from.strip(),
        "save_to_sent": prev_send.get("save_to_sent", True),
        "sent_folder": prev_send.get("sent_folder", "Sent"),
    }
    cfgmod.save_config(cfg)
    if smtp_password:
        try:
            cfgmod.set_secret(cfgmod.SECRET_SMTP_PASSWORD, smtp_password)
        except RuntimeError:
            pass
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
        "running": running,
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
        saved_new = _status.get("session_saved", 0) > 0
        do_fetch = auto_links and not _status.get("stop")
        # Re-stitch -> fetch share-links/attachments -> comprehend -> contacts. Links BEFORE comprehend so
        # their text is read; oversize files pause for confirmation and re-comprehend the thread on confirm.
        _post_import(cfg, _archive_dir(cfg), saved_new, do_fetch)
    except Exception as e:
        _log(f"ERROR: {type(e).__name__}: {e}")
    finally:
        with _lock:
            _status["running"] = False
            _status["prep"] = None


def _run_links_inline(arch, harvest: bool = True, fetch: bool = False) -> None:
    """Run the share-link step SYNCHRONOUSLY (catalogue always; download when asked) so it finishes BEFORE
    comprehension reads attachment text. Cataloguing is cheap/offline; downloading is size-gated — oversize
    files pause as needs-confirm and re-comprehend their thread once confirmed. Guarded vs a concurrent task."""
    with _lock:
        if _linktask.get("running"):
            _log("Share-link step skipped — a link task is already running.")
            return
        _linktask.update({"running": True, "stop": False})
    try:
        if harvest:
            _log("Cataloguing share links…")
        if fetch:
            _log("Downloading share-link files before comprehension (oversize files wait for confirmation)…")
        _auto_link_task(arch, harvest, fetch)
    except Exception as e:
        _log(f"Share-link step failed: {type(e).__name__}: {e}")
    finally:
        with _lock:
            _linktask["running"] = False


def _post_import(cfg: dict, arch, saved_new: bool, do_fetch: bool) -> None:
    """The post-download auto-pipeline, in order: re-stitch threads -> fetch share-links/attachments ->
    comprehend (now sees attachment text) -> refresh contacts. With no new mail, a requested download just
    runs in the background. The ORDER is the point — link files must exist before comprehension reads them."""
    if saved_new:
        _log("Rebuilding thread index…")
        try:
            threadmod.build_threads(arch, log=_log)
        except Exception as e:
            _log(f"Thread index rebuild failed: {type(e).__name__}: {e}")
        _run_links_inline(arch, harvest=True, fetch=do_fetch)   # BEFORE comprehend
        _maybe_autorun_comprehension(cfg)
        try:                                                    # signatures + AI titles/phones -> GreenBook
            contactsmod.rebuild(arch)
        except Exception as e:
            _log(f"Contacts refresh failed: {type(e).__name__}: {e}")
    elif do_fetch:                                              # no new mail — just catch up downloads
        if _start_link_task(lambda: _auto_link_task(arch, False, True)):
            _log("Share links: downloading catalogued files in the background ('Stop links' halts it).")
        else:
            _log("Share-link task skipped — a link task is already running.")


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


def _operator(cfg: dict) -> str:
    """The mailbox owner's identity/role/aliases (comma-separated) so is_mine/direction resolve. Uses
    comprehension.operator if set, else falls back to the configured IMAP account address."""
    op = str(_comp_cfg(cfg).get("operator") or "").strip()
    if op:
        return op
    imap = ((cfg or {}).get("auth") or {}).get("imap") or {}
    return str(imap.get("user") or imap.get("username") or imap.get("email") or "").strip()


def _profile(cfg: dict):
    """Active classification profile — an operator-edited override if present, else the shipped preset."""
    return effective_profile(cfg)


def _backend_available(cfg: dict) -> bool:
    am = modelsmod.active_model(cfg)
    backend = am["backend"] if am else _comp_cfg(cfg)["backend"]
    if backend == "claude-cli":
        return bool(shutil.which("claude"))
    return True


def _comprehender(cfg: dict):
    """Build the active comprehender — from the developer model registry if set, else legacy config."""
    am = modelsmod.active_model(cfg)
    backend = am["backend"] if am else _comp_cfg(cfg)["backend"]
    model = (am.get("model") if am else _comp_cfg(cfg).get("model")) or "sonnet"
    timeout = modelsmod.get_timeout(cfg)             # clamped 30..1200
    if backend in ("claude-cli", "codex-cli"):       # CLI backends take model + timeout
        return get_comprehender(backend, model=model, timeout=timeout)
    return get_comprehender(backend)


def _record_usage(backend) -> None:
    """Attribute a run's actual token usage to the active model in the registry."""
    toks = getattr(backend, "tokens", 0)
    if not toks:
        return
    cfg = cfgmod.load_config()
    am = modelsmod.active_model(cfg)
    if am:
        modelsmod.add_usage(cfg, am["id"], toks, getattr(backend, "cost", 0.0))
        cfgmod.save_config(cfg)


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
        backend = _comprehender(cfg)
        out = compmod.run_comprehension(
            _archive_dir(cfg), backend=backend, profile=_profile(cfg), operator=_operator(cfg),
            budget_tokens=10 ** 12, log=_log,    # not token-capped (that caused the silent 1/68 stall)
            limit=int(c.get("autorun_limit", 100)),   # bounded by COUNT instead — explicit, never silent
            should_stop=lambda: _status.get("stop", False),
            allowed=lambda: _comprehension_allowed(cfg),
            concurrency=modelsmod.get_concurrency(cfg),   # parallel workers (developer-controlled in /dev)
            include_stale=False,    # autorun stays cheap: only NEW threads; stale ones go brown for a manual refresh
        )
        _record_usage(backend)
        if out["pending"]:
            _log(f"Comprehension: {out['done']} done, {out['pending']} still pending "
                 f"(autorun caps at {int(c.get('autorun_limit', 100))}/run — use the Comprehend panel for the rest).")
        else:
            _log(f"Comprehension: {out['done']} done, all caught up.")
    except Exception as e:
        _log(f"Comprehension autorun failed: {type(e).__name__}: {e}")


@app.get("/threads", response_class=HTMLResponse)
def threads_page(request: Request):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    threads = threadmod.read_threads(arch)
    by_root = compmod.latest_by_root(arch)                # link by stable root_id (thread_id changes on new msgs)
    comps = {t["thread_id"]: by_root[t["root_id"]] for t in threads if t.get("root_id") in by_root}
    _dlidx = compmod.downloaded_link_index(arch)          # read link files once for the whole list
    stale = {t["thread_id"] for t in threads
             if t.get("root_id") in by_root
             and compmod.is_stale(t, by_root[t["root_id"]], compmod.thread_attach_count(arch, t, _dlidx))}
    # per-thread share-link state (for the 🔗 fetched/pending icon)
    mid2t, path2t = {}, {}
    for t in threads:
        for mm in t.get("members", []):
            if mm.get("message_id"):
                mid2t[mm["message_id"]] = t["thread_id"]
            for loc in (mm.get("locations") or []):
                if loc.get("path"):
                    path2t[loc["path"].replace("\\", "/")] = t["thread_id"]
    linkstate, link_stats = {}, {}
    for r in lsmod.read_link_shares(arch):
        s = r.get("status", "pending")
        link_stats[s] = link_stats.get(s, 0) + 1
        tid = mid2t.get(r.get("message_id")) or path2t.get((r.get("eml") or "").replace("\\", "/"))
        if not tid:
            continue
        st = linkstate.setdefault(tid, {"total": 0, "fetched": 0, "pending": 0})
        st["total"] += 1
        if s == "downloaded":
            st["fetched"] += 1
        elif s in ("pending", "needs-auth", "needs-confirm"):
            st["pending"] += 1
    return templates.TemplateResponse(request, "threads_list.html", {
        "cfg": cfg,
        "threads": threads,
        "has_index": (arch / "threads.jsonl").exists(),
        "comps": comps, "stale": stale, "linkstate": linkstate,
        "cats": sorted({(r.get("classification") or {}).get("category") for r in comps.values()
                        if (r.get("classification") or {}).get("category")}),
        "comp_counts": {"done": len(comps), "stale": len(stale),
                        "pending": sum(1 for t in threads if t.get("root_id") not in by_root),
                        "total": len(threads)},
        "ai_ready": _backend_available(cfg),
        "projects": projmod.list_projects(arch),
        "link_stats": link_stats,
        "link_total": sum(link_stats.values()),
    })


@app.get("/todo", response_class=HTMLResponse)
def todo_page(request: Request):
    """The "Need You" inbox: the operator's own open obligations across every comprehended thread,
    ranked by urgency. The cross-thread render of the to-do list the comprehension pass computes."""
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    today = date.today().isoformat()
    items = todomod.needyou_items(arch, today)
    overdue = sum(1 for i in items if i["overdue"])
    soon = sum(1 for i in items if i["days_left"] is not None and 0 <= i["days_left"] <= 7)
    undated = sum(1 for i in items if i["days_left"] is None)
    return templates.TemplateResponse(request, "todo.html", {
        "cfg": cfg, "items": items, "today": today,
        "counts": {"total": len(items), "overdue": overdue, "soon": soon, "undated": undated},
    })


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    projs = projmod.list_projects(arch)
    merges = projmod.read_merges(arch)
    name_by_key = {p["key"]: p["name"] for p in projs}
    merged = [{"from_key": fk, "to_name": name_by_key.get(projmod._resolve_merge(tk, merges), tk)}
              for fk, tk in merges.items()]
    return templates.TemplateResponse(request, "projects.html",
                                      {"cfg": cfg, "projects": projs, "merged": merged})


@app.post("/projects/merge")
def projects_merge(from_key: str = Form(...), to_key: str = Form(...)):
    """Fold one project into another (operator). The target survives; persisted, survives re-comprehension."""
    arch = _archive_dir(cfgmod.load_config())
    by_key = {p["key"]: p for p in projmod.list_projects(arch)}
    src, dest = (from_key or "").strip(), (to_key or "").strip()
    if src == dest:
        return JSONResponse({"ok": False, "message": "That's the same project."})
    if dest not in by_key:
        return JSONResponse({"ok": False, "message": "Pick a target project to merge into."})
    ok = projmod.set_merge(arch, src, dest)
    return JSONResponse({"ok": ok, "message": f"Merged into {by_key[dest]['name']}." if ok
                         else "Couldn't merge (would create a loop)."})


@app.post("/projects/unmerge")
def projects_unmerge(from_key: str = Form(...)):
    ok = projmod.unmerge(_archive_dir(cfgmod.load_config()), (from_key or "").strip())
    return JSONResponse({"ok": ok, "message": "Un-merged." if ok else "That project wasn't merged."})


@app.get("/projects/{key}", response_class=HTMLResponse)
def project_detail(request: Request, key: str):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    p = projmod.get_project(arch, key)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    return templates.TemplateResponse(request, "project_detail.html", {
        "cfg": cfg, "project": p,
        "contacts": companiesmod.project_contacts(arch, key),
        "firms": companiesmod.project_companies(arch, key)})


@app.get("/companies")
def companies_page():
    return RedirectResponse("/contacts", status_code=308)   # folded into the company-grouped Contacts page


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    data = companiesmod.list_companies(arch)
    return templates.TemplateResponse(request, "contacts.html", {
        "cfg": cfg, "companies": data["companies"],
        "individuals": data["individuals"], "individuals_count": data["individuals_count"],
        "qaqc": companiesmod.qaqc(arch)})


def _csv_response(rows: list, header: list, filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/contacts/export.csv")
def contacts_export():
    """GreenBook → CSV (the secretary/analyst's core need): every contact with its firm + code."""
    data = companiesmod.list_companies(_archive_dir(cfgmod.load_config()))
    rows = []
    for co in data["companies"]:
        for p in co["people"]:
            rows.append([co["name"], co["code"], p.get("name", ""), p.get("position", ""),
                         p.get("phone", ""), p["email"], p.get("count", "")])
    for p in data["individuals"]:
        rows.append(["(individual)", "", p.get("name", ""), p.get("position", ""),
                     p.get("phone", ""), p["email"], p.get("count", "")])
    return _csv_response(rows, ["Company", "Code", "Name", "Position", "Phone", "Email", "Mails"], "greenbook.csv")


@app.get("/projects/{key}/people.csv")
def project_people_export(key: str):
    arch = _archive_dir(cfgmod.load_config())
    rows = [[c.get("name", ""), c.get("company", ""), c.get("position", ""), c.get("phone", ""),
             c["email"], c.get("count", "")] for c in companiesmod.project_contacts(arch, key)]
    safe = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-") or "project"
    return _csv_response(rows, ["Name", "Company", "Position", "Phone", "Email", "Mails"], f"{safe}-people.csv")


@app.post("/contacts/rebuild")
def contacts_rebuild():
    """Full deterministic pass — read each sender's own signature, derive domain firms, auto-merge typos.
    Runs in FastAPI's threadpool (plain def handler), so the event loop stays free while it reads .eml."""
    cfg = cfgmod.load_config()
    try:
        people = contactsmod.rebuild(_archive_dir(cfg))
        merged = sum(len(p.get("aliases") or []) for p in people)
        firms = len(companiesmod.list_companies(_archive_dir(cfg))["companies"])
        msg = f"{len(people)} people across {firms} companies."
        if merged:
            msg += f" {merged} duplicate address(es) auto-merged."
        return JSONResponse({"ok": True, "message": msg})
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"})


@app.post("/contacts/code")
def contacts_set_code(key: str = Form(...), code: str = Form("")):
    """Operator override of a firm's company code (e.g. GCC → 4C); blank clears it back to auto.
    Rejects a code already in use by another company (codes must be unique)."""
    arch = _archive_dir(cfgmod.load_config())
    if code.strip():
        taken = companiesmod.code_in_use(arch, code, exclude_key=key.strip())
        if taken:
            return JSONResponse({"ok": False, "taken_by": taken,
                                 "message": f"Code “{code.strip().upper()}” is not available — already used by {taken}."})
    stored = companiesmod.set_code(arch, key.strip(), code)
    return JSONResponse({"ok": True, "code": stored})


@app.post("/contacts/name")
def contacts_set_name(key: str = Form(...), name: str = Form("")):
    """Operator override of a firm's display name (fill in a domain-only firm / fix a wrong one)."""
    stored = companiesmod.set_name(_archive_dir(cfgmod.load_config()), key.strip(), name)
    return JSONResponse({"ok": True, "name": stored})


@app.post("/contacts/merge")
def contacts_merge(from_key: str = Form(...), into_code: str = Form(...)):
    """Fold one firm into another (operator) — target identified by its company code. Persisted; survives Refresh."""
    arch = _archive_dir(cfgmod.load_config())
    target = (into_code or "").strip().upper()
    dest = next((c for c in companiesmod.list_companies(arch)["companies"] if (c["code"] or "").upper() == target), None)
    if not dest:
        return JSONResponse({"ok": False, "message": f"No firm has code “{target}”. Use the code shown on the target card."})
    if dest["key"] == from_key.strip():
        return JSONResponse({"ok": False, "message": "That's the same firm."})
    ok = companiesmod.set_merge(arch, from_key.strip(), dest["key"])
    return JSONResponse({"ok": ok, "message": f"Merged into {dest['name']}." if ok else "Couldn't merge (would create a loop)."})


@app.post("/contacts/unmerge")
def contacts_unmerge(key: str = Form(...)):
    ok = companiesmod.unmerge(_archive_dir(cfgmod.load_config()), key.strip())
    return JSONResponse({"ok": ok, "message": "Un-merged." if ok else "Nothing to un-merge."})


@app.post("/threads/{thread_id}/resolve")
def resolve_thread(thread_id: str, domain: str = Form(...), category: str = Form(...),
                   rule_type: str = Form(""), rule_match: str = Form("")):
    """Operator override of a flagged classification, optionally saving a learned rule."""
    from datetime import datetime, timezone
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    domain, category = domain.strip(), category.strip()
    prof = _profile(cfg)     # reject anything outside the active taxonomy
    if domain not in prof.domain_names() or category not in prof.categories(domain):
        return JSONResponse({"ok": False, "message": f"Invalid domain/category for this profile."}, status_code=400)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    root_id = (threadmod.get_thread(arch, thread_id) or {}).get("root_id", "")
    if not compmod.resolve_comprehension(arch, thread_id, domain, category, ts, root_id=root_id):
        return JSONResponse({"ok": False, "message": "This thread isn't comprehended yet."}, status_code=404)
    msg = f"Classification set to {domain.strip()} / {category.strip()}."
    if rule_type.strip() and rule_match.strip():
        if rulesmod.add_rule(arch, rule_type, rule_match, domain.strip(), category.strip(), ts):
            msg += f" Rule added — {rule_type.strip()}: “{rule_match.strip()}” → {category.strip()}."
        else:
            msg += " (rule not added — check the value)"
    return JSONResponse({"ok": True, "message": msg})


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    cfg = cfgmod.load_config()
    return templates.TemplateResponse(request, "rules.html",
                                      {"cfg": cfg, "rules": rulesmod.read_rules(_archive_dir(cfg))})


@app.post("/rules/delete")
def rules_delete(index: int = Form(...)):
    ok = rulesmod.delete_rule(_archive_dir(cfgmod.load_config()), index)
    return JSONResponse({"ok": ok, "message": "Rule deleted." if ok else
                         "Rule not found — the list may have changed; reload."})


# ---- classification profile (taxonomy the council decides against) ---------------------------------
@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    cfg = cfgmod.load_config()
    prof = _profile(cfg)
    taxonomy = "\n".join(f"{d}: {', '.join(prof.categories(d))}" for d in prof.domain_names())
    precedence = "\n".join(f"{', '.join(r.get('if_any', []))} => {r.get('then', '')}" for r in prof.precedence)
    facets = "\n".join(f"{f}: {', '.join(prof.facet_values(f))}" for f in prof.facet_names())
    customised = bool((cfg.get("comprehension") or {}).get("profile_def"))
    return templates.TemplateResponse(request, "profile.html", {
        "cfg": cfg, "prof": prof, "taxonomy": taxonomy, "precedence": precedence, "facets": facets,
        "all_categories": prof.all_categories(), "customised": customised,
        "rules": rulesmod.read_rules(_archive_dir(cfg))})


def _parse_named_lists(text: str) -> dict:
    """Parse 'Name: a, b, c' lines into {Name: [a,b,c]} (used for taxonomy domains + facets)."""
    out: dict = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, vals = line.split(":", 1)
        vl = [v.strip() for v in vals.split(",") if v.strip()]
        if name.strip() and vl:
            out[name.strip()] = vl
    return out


@app.post("/profile/save")
def profile_save(description: str = Form(""), taxonomy: str = Form(""),
                 safety_net: str = Form(""), precedence: str = Form(""), facets: str = Form("")):
    cfg = cfgmod.load_config()
    domains = _parse_named_lists(taxonomy)
    facet_def = _parse_named_lists(facets)
    prec = []
    for line in precedence.splitlines():
        if "=>" not in line:
            continue
        kws, then = line.split("=>", 1)
        kl = [k.strip() for k in kws.split(",") if k.strip()]
        if kl and then.strip():
            prec.append({"if_any": kl, "then": then.strip()})
    pdef = {"name": _comp_cfg(cfg).get("profile", "custom"), "description": description.strip(),
            "domains": domains, "safety_net": safety_net.strip(), "precedence": prec, "facets": facet_def}
    try:
        _profile_from_dict(pdef)                          # validate (raises if no usable taxonomy)
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Couldn't save: {e}"}, status_code=400)
    cfg.setdefault("comprehension", {})["profile_def"] = pdef
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": True, "message": "Profile saved. New comprehensions use it; re-comprehend to reclassify existing threads."})


@app.post("/profile/reset")
def profile_reset():
    cfg = cfgmod.load_config()
    (cfg.get("comprehension") or {}).pop("profile_def", None)
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": True, "message": "Reset to the default profile."})


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


@app.get("/threads/link-status")     # defined BEFORE /threads/{thread_id} so it isn't captured as an id
def link_status():
    """Live link-task state + status counts + a needs-auth/dead triage list, for the Mail page to poll."""
    stats = {}
    needs_auth, dead = [], []
    for r in lsmod.read_link_shares(_archive_dir(cfgmod.load_config())):
        s = r.get("status", "pending")
        stats[s] = stats.get(s, 0) + 1
        if s == "needs-auth" and len(needs_auth) < 50:
            needs_auth.append({"url": r.get("url"), "provider": r.get("provider")})
        elif s == "dead" and len(dead) < 50:
            dead.append({"url": r.get("url"), "provider": r.get("provider")})
    return JSONResponse({"running": _linktask.get("running", False), "stats": stats,
                         "total": sum(stats.values()),
                         "current": _linktask.get("current", ""), "done": _linktask.get("done", 0),
                         "batch_total": _linktask.get("total", 0), "failed": _linktask.get("failed", 0),
                         "needs_auth": needs_auth, "dead": dead})


@app.get("/threads/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: str):
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    if not t:
        return RedirectResponse("/threads", status_code=303)
    # this thread's share links: what's already saved (offer to view) vs still pending (offer to fetch)
    members = t.get("members", [])
    mids = {m.get("message_id") for m in members}
    paths = {loc.get("path") for m in members for loc in (m.get("locations") or [])}
    tlinks = [r for r in lsmod.read_link_shares(arch)
              if r.get("message_id") in mids or r.get("eml") in paths]
    link_saved = [r for r in tlinks if r.get("status") == "downloaded" and r.get("file")]
    link_pending = sum(1 for r in tlinks if r.get("status") == "pending")
    link_needs_confirm = sum(1 for r in tlinks if r.get("status") == "needs-confirm")
    _prof = _profile(cfg)   # taxonomy for the Resolve selects
    comp = compmod.comp_for_thread(arch, t)              # match by stable root_id (survives re-stitching)
    frm = request.query_params.get("from", "")           # came from a project? offer a back-to-project link
    back = {"url": frm, "label": "← back to project"} if frm.startswith("/projects/") else None
    # render only the lightweight header list from threads.jsonl; bodies load on demand below
    return templates.TemplateResponse(request, "thread_view.html", {
        "cfg": cfg, "thread": t, "back": back,
        "comp": comp, "stale": compmod.is_stale(t, comp, compmod.thread_attach_count(arch, t)),
        "ai_ready": _backend_available(cfg),
        "link_saved": link_saved, "link_pending": link_pending,
        "link_needs_confirm": link_needs_confirm,
        "sending_enabled": _sending_enabled(cfg),
        "taxonomy": {d: _prof.categories(d) for d in _prof.domain_names()},
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
        backend = _comprehender(cfg)
        rec = compmod.comprehend_thread(arch, t, backend, _profile(cfg), operator=_operator(cfg),
                                        model=getattr(backend, "model", c.get("model", "?")))
        compmod.save_comprehension(arch, rec)
        _record_usage(backend)
        return JSONResponse({"ok": True, "message": "Comprehended.", "classification": rec["classification"]})
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"   # friendly errors are clean
        return JSONResponse({"ok": False, "message": msg})


# ---- batch comprehension (select by project / date range; runs in the background) -------------------
_comptask = {"running": False, "stop": False, "done": 0, "total": 0, "current": "", "message": "",
             "errors": 0, "last_done": "", "started_at": None, "log": []}


def _selection_ids(arch, mode: str, project_key: str, date_from: str, date_to: str):
    """Resolve a UI scope to a set of thread_ids, or None for 'everything that needs it'."""
    if mode == "project" and project_key:
        p = projmod.get_project(arch, project_key)
        return {t.get("thread_id") for t in (p or {}).get("threads", [])} if p else set()
    if mode == "duration":
        df, dt = _parse_date(date_from), _parse_date(date_to)
        ids = set()
        for t in threadmod.read_threads(arch):
            day = (t.get("end") or t.get("start") or "")[:10]
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            if (df and d < df) or (dt and d > dt):
                continue
            ids.add(t.get("thread_id"))
        return ids
    return None   # 'all'


def _start_comp_task(cfg: dict, only, redo: bool) -> bool:
    with _lock:
        if _comptask["running"]:
            return False
        _comptask.update({"running": True, "stop": False, "done": 0, "total": 0, "current": "",
                          "errors": 0, "last_done": "", "message": "", "started_at": time.time(), "log": []})

    def runner():
        try:
            def prog(done=0, total=0, current="", errors=0, last_done=""):
                _comptask.update({"done": done, "total": total, "current": current,
                                  "errors": errors, "last_done": last_done})

            def clog(line):                              # comp-specific recent log (for the live panel) + global log
                _comptask["log"].append(str(line)); del _comptask["log"][:-15]
                _log(line)

            backend = _comprehender(cfg)
            out = compmod.run_comprehension(
                _archive_dir(cfg), backend=backend, profile=_profile(cfg), operator=_operator(cfg),
                budget_tokens=10 ** 12, log=clog,    # MANUAL run: do the whole selection the user chose (Stop to halt)
                should_stop=lambda: _comptask.get("stop", False),
                allowed=lambda: _comprehension_allowed(cfg),
                concurrency=modelsmod.get_concurrency(cfg),   # parallel workers (developer-controlled in /dev)
                only=only, redo=redo, include_stale=True, progress=prog)
            _record_usage(backend)
            _comptask["tokens"] = getattr(backend, "tokens", 0)
            _comptask["message"] = (f"Comprehended {out['done']} of {out['total']} thread(s)"
                                    + (f" · {out['errors']} errored" if out.get("errors") else "")
                                    + (f" · {out['needs_review']} need review" if out.get("needs_review") else "")
                                    + (" — stopped." if _comptask.get("stop") else ".")
                                    + (f" ~{getattr(backend, 'tokens', 0):,} tokens." if getattr(backend, 'tokens', 0) else ""))
            try:
                contactsmod.rebuild(_archive_dir(cfg))   # new AI titles/phones may enrich the directory
            except Exception as e:
                _log(f"Contacts refresh after comprehension failed: {type(e).__name__}: {e}")
        except Exception as e:
            _comptask["message"] = f"{type(e).__name__}: {e}"
            _log(f"comprehend task error: {type(e).__name__}: {e}")
        finally:
            with _lock:
                _comptask["running"] = False
    threading.Thread(target=runner, daemon=True).start()
    return True


@app.post("/comprehend/run")
def comprehend_run(mode: str = Form("all"), project_key: str = Form(""),
                   date_from: str = Form(""), date_to: str = Form(""), redo: str = Form("")):
    cfg = cfgmod.load_config()
    if not _comprehension_allowed(cfg):
        return JSONResponse({"ok": False, "message": "Comprehension isn't enabled for your plan."})
    if not _backend_available(cfg):
        return JSONResponse({"ok": False, "message":
                             "AI backend not set up — install the Claude CLI "
                             "(npm i -g @anthropic-ai/claude-code) and log in."})
    arch = _archive_dir(cfg)
    do_redo = redo.strip().lower() in ("1", "true", "on", "yes")
    only = _selection_ids(arch, mode.strip(), project_key.strip(), date_from, date_to)
    todo = compmod.select_threads(arch, only=only, redo=do_redo, include_stale=True)
    if not todo:
        return JSONResponse({"ok": False, "message": "Nothing to comprehend in that selection — all up to date."})
    if not _start_comp_task(cfg, only, do_redo):
        return JSONResponse({"ok": False, "message": "A comprehension run is already in progress."})
    return JSONResponse({"ok": True, "total": len(todo),
                         "message": f"Comprehending {len(todo)} thread(s) in the background…"})


@app.get("/comprehend/status")
def comprehend_status():
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    threads = threadmod.read_threads(arch)
    by_root = compmod.latest_by_root(arch)
    done = sum(1 for t in threads if t.get("root_id") in by_root)
    _dlidx = compmod.downloaded_link_index(arch)
    stale = sum(1 for t in threads if t.get("root_id") in by_root
                and compmod.is_stale(t, by_root[t["root_id"]], compmod.thread_attach_count(arch, t, _dlidx)))
    ct = _comptask
    elapsed = (time.time() - ct["started_at"]) if ct.get("started_at") else 0
    rate = (ct.get("done", 0) / elapsed) if (elapsed > 0 and ct.get("done")) else 0   # threads/sec
    remaining = max(0, ct.get("total", 0) - ct.get("done", 0))
    return JSONResponse({
        "running": ct.get("running", False), "done": ct.get("done", 0),
        "total": ct.get("total", 0), "current": ct.get("current", ""),
        "errors": ct.get("errors", 0), "last_done": ct.get("last_done", ""),
        "rate_per_min": round(rate * 60, 1), "eta_seconds": (round(remaining / rate) if rate > 0 else None),
        "message": ct.get("message", ""), "log": ct.get("log", [])[-8:],
        "counts": {"done": done, "stale": stale,
                   "pending": sum(1 for t in threads if t.get("root_id") not in by_root),
                   "total": len(threads)}})


@app.post("/comprehend/stop")
def comprehend_stop():
    _comptask["stop"] = True
    return JSONResponse({"ok": True, "message": "Stopping after the current thread…"})


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
                                       "links": lsmod.links_for_member(arch, members[idx].get("message_id"), locs),
                                       "sending_enabled": _sending_enabled(cfg)})


_linktask = {"running": False, "stop": False, "current": "", "done": 0, "total": 0, "failed": 0, "phase": ""}


def _linkprog(done=0, total=0, current="", provider="", failed=0, **_):
    _linktask.update({"done": done, "total": total, "current": current, "failed": failed, "phase": "fetching"})


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


def _auto_link_task(arch, do_harvest=True, do_fetch=False):
    """Post-archive link automation. Cataloguing (harvest) always runs after new mail — it's cheap,
    offline and idempotent; downloading (fetch) is opt-in. Fetch runs in batches of 50 — each batch
    persists (crash-safe) and is stoppable; oversize links pause as needs-confirm."""
    if do_harvest:
        lsmod.harvest(arch, log=_log)
    if do_fetch:
        while not _linktask.get("stop"):
            r = lsmod.fetch_links(arch, limit=50, headless=True, log=_log,
                                  should_stop=lambda: _linktask.get("stop", False), progress=_linkprog)
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
            should_stop=lambda: _linktask.get("stop", False), progress=_linkprog)):
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


@app.post("/threads/{thread_id}/fetch-links")
def fetch_thread_links(thread_id: str):
    """Download just this conversation's link files (background, one link task at a time)."""
    cfg = cfgmod.load_config()
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    if not t:
        return JSONResponse({"ok": False, "message": "Conversation not found."})
    ids = {m.get("message_id") for m in t.get("members", []) if m.get("message_id")}
    if not _start_link_task(lambda: lsmod.fetch_links(
            arch, only_message_ids=ids, limit=None, headless=True, log=_log,
            should_stop=lambda: _linktask.get("stop", False))):
        return JSONResponse({"ok": False, "message": "A link task (harvest or fetch) is already running."})
    return JSONResponse({"ok": True, "message":
                         "Downloading this conversation's linked files in the background — reopen shortly."})


# ------------------------------------------------ Sending (delivery track — OFF by default, fail-closed)
def _sending_enabled(cfg) -> bool:
    return bool((cfg.get("sending") or {}).get("enabled"))


def _from_addr(cfg) -> str:
    return ((cfg.get("sending") or {}).get("from") or "").strip() \
        or (cfg.get("auth", {}).get("imap", {}).get("user") or "")


def _parse_addrs(s: str) -> list[str]:
    from email.utils import getaddresses
    return [a for _, a in getaddresses([s or ""]) if a]


def _member_eml(cfg, thread_id, idx):
    arch = _archive_dir(cfg)
    t = threadmod.get_thread(arch, thread_id)
    members = (t or {}).get("members", [])
    if not t or not (0 <= idx < len(members)):
        return None, None
    locs = members[idx].get("locations") or []
    return (arch, arch / locs[0]["path"]) if locs else (None, None)


@app.post("/threads/{thread_id}/compose")
def compose_preview(thread_id: str, idx: int = Form(...), action: str = Form(...)):
    """Prefill recipients/subject for a reply/reply-all/forward. Gated: 403 when sending is off."""
    cfg = cfgmod.load_config()
    if not _sending_enabled(cfg):
        return JSONResponse({"ok": False, "message": "Sending is turned off — enable it in Setup."}, status_code=403)
    _, eml = _member_eml(cfg, thread_id, idx)
    if not eml:
        return JSONResponse({"ok": False, "message": "Message not found."}, status_code=404)
    try:
        original = composemod._load(eml)
        me = _from_addr(cfg)
        to, cc = composemod.recipients(original, action, me)
        return JSONResponse({"ok": True, "to": ", ".join(to), "cc": ", ".join(cc),
                             "subject": composemod.subject(original, action), "from": me})
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/send")
def send_mail(thread_id: str = Form(...), idx: int = Form(...), action: str = Form(...),
              to: str = Form(""), cc: str = Form(""), body: str = Form("")):
    """Send a composed reply/reply-all/forward. HARD-GATED: returns 403 unless sending is enabled."""
    cfg = cfgmod.load_config()
    if not _sending_enabled(cfg):                       # fail-closed: cannot send while off
        return JSONResponse({"ok": False, "message": "Sending is turned off — enable it in Setup."}, status_code=403)
    _, eml = _member_eml(cfg, thread_id, idx)
    if not eml:
        return JSONResponse({"ok": False, "message": "Message not found."}, status_code=404)
    to_list, cc_list = _parse_addrs(to), _parse_addrs(cc)
    if not to_list:
        return JSONResponse({"ok": False, "message": "No recipients."}, status_code=400)
    from_addr = _from_addr(cfg)
    s = cfg.get("sending") or {}
    try:
        msg = composemod.build_message(eml, action, body_text=body, from_addr=from_addr,
                                       to=to_list, cc=cc_list, me=from_addr)
        sender = sendermod.get_sender("smtp", s.get("smtp") or {}, from_addr,
                                      cfgmod.get_secret(cfgmod.SECRET_SMTP_PASSWORD) or "", from_addr)
        mid = sender.send(msg)
    except Exception as e:
        _log(f"SEND FAILED to {to_list + cc_list}: {type(e).__name__}: {e}")
        return JSONResponse({"ok": False, "message": f"Send failed: {type(e).__name__}: {e}"}, status_code=502)
    note = ""
    if s.get("save_to_sent", True):
        try:
            with get_source("imap", cfg["auth"]["imap"], cfgmod.get_secret(cfgmod.SECRET_IMAP_PASSWORD) or "") as src:
                src.append(s.get("sent_folder", "Sent"), msg.as_bytes())
        except Exception as e:
            note = f" (couldn't save to Sent: {type(e).__name__})"
    _log(f"SENT to {to_list + cc_list}: {msg['Subject']} [{mid}]")
    return JSONResponse({"ok": True, "message": f"Sent to {len(to_list + cc_list)} recipient(s).{note}"})


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
