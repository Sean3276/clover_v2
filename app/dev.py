"""Developer-only control panel — a SEPARATE module from the end-user app.

Mounted at /dev (not linked in the normal nav). Lets a developer register several comprehension AI
models, set a token budget per model, see tokens used / left, and switch the ACTIVE model. Gated by the
CLOVER_DEV env var (set CLOVER_DEV=0 to hide it in a locked-down deployment; default on for local use).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from clover import config as cfgmod
from clover import models as modelsmod

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def dev_enabled() -> bool:
    return os.environ.get("CLOVER_DEV", "1").strip().lower() not in ("0", "false", "off", "no")


def _guard():
    return None if dev_enabled() else JSONResponse(
        {"ok": False, "message": "Developer panel is disabled (set CLOVER_DEV=1 to enable)."}, status_code=403)


@router.get("/dev", response_class=HTMLResponse)
def dev_page(request: Request):
    if not dev_enabled():
        return HTMLResponse("<p style='font:14px sans-serif;padding:40px'>Developer panel disabled. "
                            "Set <code>CLOVER_DEV=1</code> and restart to enable.</p>", status_code=403)
    cfg = cfgmod.load_config()
    models = modelsmod.read_models(cfg)
    rows = [{**m, "left": modelsmod.token_left(m), "active": m.get("id") == modelsmod.active_id(cfg)}
            for m in models]
    return _templates.TemplateResponse(request, "dev.html", {
        "cfg": cfg, "models": rows, "active_id": modelsmod.active_id(cfg),
        "legacy": (cfg.get("comprehension") or {}).get("model", "")})


@router.post("/dev/models/save")
def dev_models_save(id: str = Form(""), label: str = Form(""), backend: str = Form("claude-cli"),
                    model: str = Form(""), token_budget: int = Form(0), enabled: str = Form("on")):
    g = _guard()
    if g:
        return g
    cfg = cfgmod.load_config()
    mid = modelsmod.upsert_model(cfg, {
        "id": id.strip(), "label": label.strip(), "backend": backend.strip(), "model": model.strip(),
        "token_budget": token_budget, "enabled": enabled.strip().lower() in ("1", "true", "on", "yes")})
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": True, "id": mid, "message": f"Saved model “{label.strip() or mid}”."})


@router.post("/dev/models/activate")
def dev_models_activate(id: str = Form(...)):
    g = _guard()
    if g:
        return g
    cfg = cfgmod.load_config()
    ok = modelsmod.set_active(cfg, id.strip())
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": ok, "message": "Active model set." if ok else "Model not found."})


@router.post("/dev/models/delete")
def dev_models_delete(id: str = Form(...)):
    g = _guard()
    if g:
        return g
    cfg = cfgmod.load_config()
    ok = modelsmod.delete_model(cfg, id.strip())
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": ok, "message": "Model deleted." if ok else "Model not found."})


@router.post("/dev/models/reset-usage")
def dev_models_reset(id: str = Form(...)):
    g = _guard()
    if g:
        return g
    cfg = cfgmod.load_config()
    modelsmod.reset_usage(cfg, id.strip())
    cfgmod.save_config(cfg)
    return JSONResponse({"ok": True, "message": "Usage reset."})
