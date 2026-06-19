"""Project index — group comprehended threads by their extracted project name (`facts.project`).

Deterministic, read-only over comprehension.jsonl. Threads with no project (or not yet comprehended)
simply don't appear. The grouping key normalizes spelling/case so minor variants merge; the displayed
name is the most common original spelling.
"""
from __future__ import annotations

import re

from .comprehend import read_comprehensions

_WS = re.compile(r"\s+")


def project_key(name: str) -> str:
    """Canonical grouping key for a project name (case/space/punctuation-insensitive)."""
    return _WS.sub(" ", (name or "").strip()).strip(" .,-_").casefold()


def list_projects(archive_path) -> list[dict]:
    """[{key, name, count, categories, threads:[{thread_id, subject, summary, category}]}], busiest first."""
    groups: dict[str, dict] = {}
    spellings: dict[str, dict] = {}
    for c in read_comprehensions(archive_path):
        proj = ((c.get("facts") or {}).get("project") or "").strip()
        k = project_key(proj)
        if not k:
            continue
        g = groups.setdefault(k, {"key": k, "count": 0, "categories": {}, "threads": []})
        cat = (c.get("classification") or {}).get("category") or ""
        g["count"] += 1
        g["threads"].append({"thread_id": c.get("thread_id"), "subject": c.get("subject"),
                             "summary": c.get("summary"), "category": cat})
        if cat:
            g["categories"][cat] = g["categories"].get(cat, 0) + 1
        sp = spellings.setdefault(k, {})
        sp[proj] = sp.get(proj, 0) + 1

    out = []
    for k, g in groups.items():
        name = max(spellings[k].items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0]))[0]   # common, then short, stable
        out.append({"key": k, "name": name, "count": g["count"],
                    "categories": sorted(g["categories"], key=lambda c: -g["categories"][c]),
                    "threads": g["threads"]})
    out.sort(key=lambda p: (-p["count"], p["name"].casefold()))
    return out


def get_project(archive_path, key: str) -> dict | None:
    for p in list_projects(archive_path):
        if p["key"] == key:
            return p
    return None
