"""Contact directory — consolidate people across the archive, deduped by email.

Two sources merged (the operator chose AI-primary + deterministic fallback):
  - **AI:** name/position/company/phone/email the comprehender pulled from each thread (esp. signatures).
  - **Deterministic:** every From address in `_index.jsonl` (name + email, cheap, no AI, full coverage).
AI enriches with title/company/phone; deterministic guarantees everyone who ever sent mail appears.
Read-only over comprehension.jsonl + _index.jsonl.
"""
from __future__ import annotations

from email.utils import getaddresses

from .archive import read_index
from .comprehend import read_comprehensions


def consolidate(archive_path) -> list[dict]:
    """[{email, name, position, company, phone, count}] deduped by email, busiest senders first."""
    people: dict[str, dict] = {}

    def merge(email, name="", position="", company="", phone=""):
        e = (email or "").strip().lower()
        if "@" not in e:
            return None
        p = people.setdefault(e, {"email": e, "name": "", "position": "", "company": "", "phone": "", "count": 0})
        if name and len(name.strip()) > len(p["name"]):     # keep the fullest name seen
            p["name"] = name.strip()
        for key, val in (("position", position), ("company", company), ("phone", phone)):
            if val and val.strip() and not p[key]:
                p[key] = val.strip()
        return p

    # 1) AI-extracted contacts (richer fields)
    for c in read_comprehensions(archive_path):
        for k in (c.get("contacts") or []):
            merge(k.get("email"), k.get("name", ""), k.get("position", ""),
                  k.get("company", ""), k.get("phone", ""))

    # 2) deterministic: every sender in the index (name + email), counts = times they sent
    for row in read_index(archive_path):
        for name, addr in getaddresses([row.get("from", "") or ""]):
            p = merge(addr, name)
            if p is not None:
                p["count"] += 1

    out = list(people.values())
    out.sort(key=lambda p: (-p["count"], (p["name"] or p["email"]).casefold()))
    return out
