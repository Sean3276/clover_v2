"""run_eval — score the comprehended corpus against the gold standard + the deterministic floor.

Pure measurement over on-disk data (CLOVER_V2_COMPREHENSION_SPEC §6). For every comprehended
thread it runs the §2a floor cross-check (hard misses on the catchable classes — no gold needed),
and for threads with CONFIRMED gold it scores recall + the AND-gated zero-miss headline. `run()`
also appends an auditable leaderboard row. No AI lives here — it MEASURES the AI.

Run:  python -m clover.eval.run_eval <archive_path>
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..comprehend import _thread_messages, read_comprehensions
from ..threads import read_threads
from . import gold as goldmod
from . import scorer

_FLOOR = ("refs", "dates", "amounts")


def _source_text(archive, thread) -> str:
    """Body-only source for the floor — drop each message's `From:/Date:` header line so the
    email's own send-date is not mistaken for a content date the AI 'missed'."""
    bodies = []
    for m in _thread_messages(archive, thread):
        bodies.append(m.split("\n", 1)[1] if "\n" in m else m)
    return "\n\n----\n\n".join(bodies)


def _rate(d: dict) -> float:
    return round(d["covered"] / d["required"], 4) if d["required"] else 1.0


def evaluate(archive) -> dict:
    """Pure: {summary, details}. Floor over every comprehended thread; recall over confirmed-gold threads."""
    archive = Path(archive)
    threads = {t.get("thread_id"): t for t in read_threads(archive)}
    comps: dict = {}
    for c in read_comprehensions(archive):              # last record per thread wins
        if c.get("thread_id"):
            comps[c["thread_id"]] = c
    gold = goldmod.read_gold(archive)

    floor = {k: {"required": 0, "covered": 0} for k in _FLOOR}
    floor_hard = 0
    gold_recall: dict = {}
    gold_threads = zero_miss = 0
    details = []

    for tid, c in comps.items():
        t = threads.get(tid)
        if not t:
            continue
        facts = c.get("facts") or {}
        cc = scorer.crosscheck_floor(_source_text(archive, t), facts)
        floor_hard += cc["hard_misses"]
        for k in _FLOOR:
            floor[k]["required"] += cc[k]["required"]
            floor[k]["covered"] += cc[k]["covered"]
        if cc["hard_misses"]:
            details.append({"thread_id": tid,
                            "floor_missed": {k: cc[k]["missed"] for k in _FLOOR if cc[k]["missed"]}})

        g = gold.get(tid)
        if g and g.get("confirmed"):
            gold_threads += 1
            s = scorer.score_against_gold(g.get("atoms") or {}, facts)
            if s.get("zero_miss"):
                zero_miss += 1
            for k, v in s.items():
                if k == "zero_miss":
                    continue
                gold_recall.setdefault(k, {"required": 0, "covered": 0})
                gold_recall[k]["required"] += v["required"]
                gold_recall[k]["covered"] += v["covered"]

    return {"summary": {
        "comprehended": len(comps),
        "floor": {"hard_misses": floor_hard,
                  **{k: {**floor[k], "recall": _rate(floor[k])} for k in _FLOOR}},
        "gold_threads": gold_threads,
        "no_miss_rate": (round(zero_miss / gold_threads, 4) if gold_threads else None),
        "gold_recall": {k: _rate(v) for k, v in gold_recall.items()},
    }, "details": details}


def leaderboard_path(archive) -> Path:
    return Path(archive) / "eval_leaderboard.jsonl"


def run(archive, *, model_id: str = "", gold_version: str = "", ts: float | None = None) -> dict:
    """evaluate() + append an auditable leaderboard row."""
    res = evaluate(archive)
    row = {"ts": ts if ts is not None else time.time(), "model_id": model_id,
           "gold_version": gold_version, **res["summary"]}
    with leaderboard_path(archive).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return res


if __name__ == "__main__":          # pragma: no cover
    import sys
    arch = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(run(arch)["summary"], indent=2, ensure_ascii=False))
