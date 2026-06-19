"""Learned classification rules — inspectable & deterministic. A matching rule classifies a thread
directly (no AI council), so the operator's answers reliably win. Stored in <archive>/rules.jsonl,
one rule per line. See CLOVER_V2_RULES_SPEC.
"""
from __future__ import annotations

import json
from pathlib import Path

_TYPES = ("keyword", "sender", "project")


def rules_path(archive_path) -> Path:
    return Path(archive_path) / "rules.jsonl"


def read_rules(archive_path) -> list[dict]:
    p = rules_path(archive_path)
    out: list[dict] = []
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    return out


def add_rule(archive_path, rtype: str, match: str, domain: str, category: str, ts: str = "") -> bool:
    rtype = (rtype or "").strip().lower()
    match = (match or "").strip()
    if rtype not in _TYPES or not match or not domain or not category:
        return False
    with rules_path(archive_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": rtype, "match": match, "domain": domain,
                             "category": category, "ts": ts}, ensure_ascii=False) + "\n")
    return True


def delete_rule(archive_path, index: int) -> bool:
    recs = read_rules(archive_path)
    if not (0 <= index < len(recs)):
        return False
    recs.pop(index)
    p = rules_path(archive_path)
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return True


def match(archive_path, *, text: str = "", senders=(), project: str = "") -> dict | None:
    """First matching rule (newest first → last-match-wins), or None. `senders` = raw From strings."""
    text_l = (text or "").lower()
    senders_l = [str(s).lower() for s in (senders or []) if s]
    proj_l = (project or "").strip().lower()
    for r in reversed(read_rules(archive_path)):
        t, m = r.get("type"), (r.get("match") or "").strip().lower()
        if not m:
            continue
        if t == "keyword" and m in text_l:
            return r
        if t == "sender" and any(m in s for s in senders_l):
            return r
        if t == "project" and proj_l and m == proj_l:
            return r
    return None
