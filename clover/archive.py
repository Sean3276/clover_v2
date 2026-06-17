"""Phase 1 orchestrator — archive every email from a MailSource to local .eml files.

Source-agnostic: works against any clover.sources.MailSource (IMAP today; Gmail/Graph/…
later). Saves raw RFC822 (legal-grade, embedded attachments included) at
<archive>/<folder>/<message-id>.eml and records each message incrementally in
<archive>/_index.jsonl. Resumable: a (folder, validity, key) already in the index is skipped.

NOTE: .eml contains EMBEDDED attachments in full, but NOT link-shared files — later phase.
"""
from __future__ import annotations

import email
import hashlib
import json
import re
import sys
from email import policy
from pathlib import Path

from .safe_name import safe_name
from .sources import get_source


# ---------------------------------------------------------------- pure helpers
def eml_filename(message_id: str | None, key: str) -> str:
    """<safe message-id>.eml, falling back to uid_<key>.eml when the id is absent/unsafe."""
    mid = (message_id or "").strip().strip("<>").strip()
    base = safe_name(mid, maxlen=120) if mid else ""
    if not base or base == "untitled":
        base = f"uid_{key}"
    return f"{base}.eml"


def message_id_of(raw: bytes) -> str:
    obj = email.message_from_bytes(raw, policy=policy.default)
    return (obj.get("Message-ID") or "").strip().strip("<>").strip()


def folder_subpath(folder: str) -> Path:
    """Map an IMAP folder (possibly hierarchical) to a nested, sanitized dir path."""
    segs = [safe_name(p, maxlen=80) for p in re.split(r"[/\\]", folder) if p.strip()]
    if not segs:
        segs = [safe_name(folder, maxlen=80)]
    return Path(*segs)


def read_index(archive_path: Path) -> list[dict]:
    p = archive_path / "_index.jsonl"
    rows: list[dict] = []
    if p.exists():
        with p.open(encoding="utf-8") as fh:        # stream — don't load whole file at once
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def existing_keys(archive_path: Path) -> set[tuple]:
    return {
        (r.get("folder"), str(r.get("validity", "")), str(r.get("key")))
        for r in read_index(archive_path)
    }


def index_summary(archive_path: Path) -> dict:
    """Per-folder counts + unique real Message-IDs (cross-folder linkage)."""
    rows = read_index(archive_path)
    per_folder: dict[str, int] = {}
    ids: set[str] = set()
    for r in rows:
        fol = r.get("folder", "?")
        per_folder[fol] = per_folder.get(fol, 0) + 1
        rid = str(r.get("id", ""))
        if rid and not rid.startswith("uid_"):  # exclude synthetic ids
            ids.add(rid)
    return {"total": len(rows), "unique_ids": len(ids), "per_folder": per_folder}


# ---------------------------------------------------------------- orchestrator
def run_archive(cfg: dict, password: str, log=print, limit_per_folder: int | None = None) -> dict:
    kind = cfg.get("source_kind", "imap")
    conn = cfg["auth"].get(kind) or cfg["auth"].get("imap", {})
    folders = cfg.get("folders") or ["INBOX"]
    dest = Path(cfg.get("archive_path") or "").expanduser()
    if not str(dest) or str(dest) == ".":
        raise ValueError("archive_path is not set")
    if not dest.is_absolute():                       # anchor relative paths so resume is CWD-independent
        from .paths import auto_clover_root
        dest = auto_clover_root() / dest
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    done = existing_keys(dest)
    manifest = {"folders": {}, "saved": 0, "skipped": 0, "errors": 0, "archive_path": str(dest)}

    idx_fh = open(dest / "_index.jsonl", "a", encoding="utf-8")  # incremental, resumable
    try:
        with get_source(kind, conn, password) as src:
            for fol in folders:
                saved = skipped = errors = 0
                fol_dir = dest / folder_subpath(fol)
                try:
                    validity = str(src.select(fol))
                    keys = src.message_keys()
                    new = [k for k in keys if (fol, validity, str(k)) not in done]
                    skipped = len(keys) - len(new)
                    if limit_per_folder:
                        new = new[:limit_per_folder]
                    log(f"[{fol}] {len(keys)} total · {len(new)} to archive · {skipped} already done")
                    fol_dir.mkdir(parents=True, exist_ok=True)
                    for key in new:
                        try:
                            raw = src.fetch_raw(key)
                            if not raw:
                                errors += 1
                                log(f"  ! [{fol}] key {key}: empty fetch")
                                continue
                            sha = hashlib.sha256(raw).hexdigest()
                            obj = email.message_from_bytes(raw, policy=policy.default)
                            mid = (obj.get("Message-ID") or "").strip().strip("<>").strip()
                            path = fol_dir / eml_filename(mid, key)
                            if path.exists():
                                try:
                                    same = hashlib.sha256(path.read_bytes()).hexdigest() == sha
                                except Exception:
                                    same = False
                                if not same:  # genuine different message, same derived name
                                    path = path.with_name(f"{path.stem}_{key}.eml")
                            path.write_bytes(raw)
                            row = {
                                "id": mid or f"uid_{key}", "folder": fol, "key": key, "validity": validity,
                                "from": str(obj.get("From", "")), "subject": str(obj.get("Subject", "")),
                                "date": str(obj.get("Date", "")),
                                "path": str(path.relative_to(dest)).replace("\\", "/"),
                                "size": len(raw), "sha256": sha,
                            }
                            idx_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                            idx_fh.flush()
                            done.add((fol, validity, str(key)))
                            saved += 1
                            if saved % 25 == 0:
                                log(f"  [{fol}] {saved} saved…")
                        except Exception as e:
                            errors += 1
                            log(f"  ! [{fol}] key {key}: {type(e).__name__}: {e}")
                except Exception as e:
                    log(f"! folder {fol}: {type(e).__name__}: {e}")
                manifest["folders"][fol] = {"saved": saved, "skipped": skipped, "errors": errors}
                manifest["saved"] += saved
                manifest["skipped"] += skipped
                manifest["errors"] += errors
                log(f"[{fol}] done — {saved} saved, {skipped} already-archived, {errors} errors")
    finally:
        idx_fh.close()

    log(f"FINISHED — {manifest['saved']} saved, {manifest['skipped']} skipped, {manifest['errors']} errors")
    return manifest


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from .config import load_config, get_secret, SECRET_IMAP_PASSWORD
    cfg = load_config()
    pw = get_secret(SECRET_IMAP_PASSWORD) or ""
    if not pw:
        print("No IMAP password stored. Use the web UI Setup page first.")
    else:
        run_archive(cfg, pw)
