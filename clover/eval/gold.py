"""Gold store — the human-confirmed ground truth the measurement bar is scored against.

Lives in the runtime archive (`<archive>/eval_gold.jsonl`), NEVER in the repo — gold is derived
from the user's real email content (real refs/dates/parties) and is tenant-private.

Append-only, last-record-per-thread wins (so a re-confirmation supersedes). `bootstrap_record`
proposes a candidate from the T1 floor for a human to confirm/edit — it is `confirmed=False` until
reviewed (CLOVER_V2_COMPREHENSION_SPEC §4: deterministic candidate -> human review). The candidate
is NOT gold until a human confirms it and adds the semantic atoms (action items) rules can't catch.
"""
from __future__ import annotations

import json
from pathlib import Path

from .extractors import extract_atoms


def gold_path(archive) -> Path:
    return Path(archive) / "eval_gold.jsonl"


def read_gold(archive) -> dict:
    """{thread_id: record}, last write wins."""
    p = gold_path(archive)
    out: dict = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["thread_id"]] = r
    return out


def write_gold(archive, record: dict) -> None:
    with gold_path(archive).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def bootstrap_record(thread_id: str, source_text: str, gold_version: str = "0") -> dict:
    """Propose a gold CANDIDATE from the deterministic T1 floor — for human review, not yet gold.
    `actions` starts empty: the human adds the semantic obligations the extractor cannot catch."""
    a = extract_atoms(source_text or "")
    return {
        "thread_id": thread_id,
        "confirmed": False,                 # flips True only after human review
        "gold_version": gold_version,
        "atoms": {
            "refs": a["refs"],
            "dates": a["dates"],
            "amounts": [f"{x['currency']} {x['value']}" for x in a["amounts"]],
            "emails": a["emails"],
            "actions": [],                  # human-supplied (semantic; no deterministic floor)
        },
    }
