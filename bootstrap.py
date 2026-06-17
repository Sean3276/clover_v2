#!/usr/bin/env python
"""Idempotent installer: prepare the .clover_v2 runtime + seed config. Safe to re-run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from clover.paths import ensure_runtime, config_path, default_archive_path, runtime_dir  # noqa: E402


def main() -> int:
    runtime = ensure_runtime()
    cfg = config_path()
    if not cfg.exists():
        example = HERE / "clover_config.example.json"
        data = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}
        data["archive_path"] = str(default_archive_path())
        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        seeded = "seeded from example"
    else:
        seeded = "kept (already exists)"
    gi = runtime / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")

    print(f"🍀 Clover v2 runtime ready: {runtime}")
    print(f"   config: {cfg}  ({seeded})")
    print(f"   default archive path: {default_archive_path()}")
    print("\nNext:")
    print("   pip install -r requirements.txt")
    print("   python -m uvicorn app.main:app --port 8765   ->  http://127.0.0.1:8765")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
