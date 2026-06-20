"""Comprehension model registry — DEVELOPER-facing.

Lets a developer register several AI backends/models, give each a token budget, see tokens used / left,
and pick the ACTIVE model the comprehension pipeline uses. Stored in config under:
    comprehension.models      = [ {id, label, backend, model, token_budget, tokens_used, enabled}, … ]
    comprehension.active_model = "<id>"

Pure helpers over a cfg dict (the caller persists with config.save_config). If no models are registered,
`active_model` returns None and the app falls back to the legacy comprehension.backend / .model settings.
"""
from __future__ import annotations

import re

_BACKENDS = ("claude-cli", "stub")          # known comprehender backends


def _comp(cfg) -> dict:
    return (cfg or {}).get("comprehension") or {}


def read_models(cfg) -> list[dict]:
    return list(_comp(cfg).get("models") or [])


def active_id(cfg) -> str:
    return _comp(cfg).get("active_model") or ""


def active_model(cfg) -> dict | None:
    models = read_models(cfg)
    aid = active_id(cfg)
    for m in models:
        if m.get("id") == aid and m.get("enabled", True):
            return m
    for m in models:                        # fallback: first enabled
        if m.get("enabled", True):
            return m
    return None


def token_left(m: dict):
    """Remaining tokens for a model, or None when its budget is 0/unset (= unlimited / untracked)."""
    budget = int(m.get("token_budget") or 0)
    return None if budget <= 0 else max(0, budget - int(m.get("tokens_used") or 0))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "model"


def upsert_model(cfg, model: dict) -> str:
    """Add or update a model (matched by id). Assigns a unique id when missing. Returns the id."""
    comp = cfg.setdefault("comprehension", {})
    models = comp.setdefault("models", [])
    mid = (model.get("id") or "").strip()
    if not mid:
        base, mid, n = _slug(model.get("label") or model.get("model") or "model"), "", 1
        mid = base
        existing = {m.get("id") for m in models}
        while mid in existing:
            n += 1; mid = f"{base}-{n}"
    rec = {
        "id": mid,
        "label": (model.get("label") or model.get("model") or mid).strip(),
        "backend": (model.get("backend") or "claude-cli").strip(),
        "model": (model.get("model") or "").strip(),
        "token_budget": max(0, int(model.get("token_budget") or 0)),
        "tokens_used": max(0, int(model.get("tokens_used") or 0)),
        "enabled": bool(model.get("enabled", True)),
    }
    for i, m in enumerate(models):
        if m.get("id") == mid:
            rec["tokens_used"] = int(m.get("tokens_used") or 0)   # never lose accrued usage on edit
            models[i] = rec
            break
    else:
        models.append(rec)
    if not comp.get("active_model"):
        comp["active_model"] = mid
    return mid


def delete_model(cfg, model_id: str) -> bool:
    comp = cfg.setdefault("comprehension", {})
    models = comp.get("models") or []
    n = len(models)
    comp["models"] = [m for m in models if m.get("id") != model_id]
    if comp.get("active_model") == model_id:
        comp["active_model"] = next((m["id"] for m in comp["models"] if m.get("enabled", True)), "")
    return len(comp["models"]) != n


def set_active(cfg, model_id: str) -> bool:
    if any(m.get("id") == model_id for m in read_models(cfg)):
        cfg.setdefault("comprehension", {})["active_model"] = model_id
        return True
    return False


def add_usage(cfg, model_id: str, tokens: int, cost: float = 0.0) -> None:
    for m in read_models(cfg):              # same dict refs as cfg -> mutation persists
        if m.get("id") == model_id:
            m["tokens_used"] = int(m.get("tokens_used") or 0) + max(0, int(tokens or 0))
            if cost:
                m["cost_usd"] = round(float(m.get("cost_usd") or 0) + float(cost), 4)
            return


def reset_usage(cfg, model_id: str) -> None:
    for m in read_models(cfg):
        if m.get("id") == model_id:
            m["tokens_used"] = 0
            m["cost_usd"] = 0.0
            return
