"""Project index — group comprehended threads by their extracted project name (`facts.project`).

Deterministic, read-only over comprehension.jsonl. Threads with no project (or not yet comprehended)
simply don't appear. The grouping key normalizes spelling/case so minor variants merge; the displayed
name is the most common original spelling.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .comprehend import read_comprehensions

_WS = re.compile(r"\s+")
_MERGES = "project_merges.json"


def project_key(name: str) -> str:
    """Canonical grouping key for a project name (case/space/punctuation-insensitive)."""
    return _WS.sub(" ", (name or "").strip()).strip(" .,-_").casefold()


# ── operator project merges (fold A into B) — mirrors the GreenBook firm-merge ──────────────
def merges_path(archive_path) -> Path:
    return Path(archive_path) / _MERGES


def read_merges(archive_path) -> dict:
    p = merges_path(archive_path)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_merge(key: str, merges: dict) -> str:
    """Follow a merge chain to its canonical key (cycle-safe)."""
    seen = set()
    while key in merges and key not in seen:
        seen.add(key)
        key = merges[key]
    return key


def _write_merges(archive_path, merges: dict) -> None:
    tmp = merges_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merges, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(merges_path(archive_path))


def set_merge(archive_path, from_key: str, to_key: str) -> bool:
    """Fold project `from_key` into `to_key` (the surviving target). Refuses self/cycles."""
    from_key, to_key = (from_key or "").strip(), (to_key or "").strip()
    merges = read_merges(archive_path)
    if not from_key or not to_key or from_key == to_key:
        return False
    if _resolve_merge(to_key, merges) == from_key:          # would create a cycle
        return False
    merges[from_key] = to_key
    _write_merges(archive_path, merges)
    return True


def unmerge(archive_path, from_key: str) -> bool:
    merges = read_merges(archive_path)
    if from_key not in merges:
        return False
    merges.pop(from_key, None)
    _write_merges(archive_path, merges)
    return True


def list_projects(archive_path) -> list[dict]:
    """[{key, name, count, categories, threads:[{thread_id, subject, summary, category}]}], busiest first."""
    merges = read_merges(archive_path)
    groups: dict[str, dict] = {}
    spellings: dict[str, dict] = {}        # all spellings seen under the canonical key
    native: dict[str, dict] = {}           # spellings whose OWN key == canonical (the merge target's own)
    for c in read_comprehensions(archive_path):
        proj = ((c.get("facts") or {}).get("project") or "").strip()
        rk = project_key(proj)
        if not rk:
            continue
        k = _resolve_merge(rk, merges)     # fold merged-away projects into their target
        g = groups.setdefault(k, {"key": k, "count": 0, "categories": {}, "threads": []})
        cat = (c.get("classification") or {}).get("category") or ""
        g["count"] += 1
        g["threads"].append({"thread_id": c.get("thread_id"), "subject": c.get("subject"),
                             "summary": c.get("summary"), "category": cat})
        if cat:
            g["categories"][cat] = g["categories"].get(cat, 0) + 1
        spellings.setdefault(k, {})[proj] = spellings.setdefault(k, {}).get(proj, 0) + 1
        if rk == k:                        # this spelling belongs to the surviving target itself
            native.setdefault(k, {})[proj] = native.setdefault(k, {}).get(proj, 0) + 1

    out = []
    for k, g in groups.items():
        pool = native.get(k) or spellings[k]    # the merge target's own name wins; else most common spelling
        name = max(pool.items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0]))[0]   # common, then short, stable
        out.append({"key": k, "name": name, "count": g["count"],
                    "categories": sorted(g["categories"], key=lambda c: -g["categories"][c]),
                    "threads": g["threads"]})
    out.sort(key=lambda p: (-p["count"], p["name"].casefold()))
    return out


def get_project(archive_path, key: str) -> dict | None:
    key = _resolve_merge(key, read_merges(archive_path))   # an old/merged-away link resolves to the target
    for p in list_projects(archive_path):
        if p["key"] == key:
            return p
    return None
