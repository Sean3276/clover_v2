"""Phase 1 orchestrator — archive every email from a MailSource to local .eml files.

Source-agnostic: works against any clover.sources.MailSource (IMAP today; Gmail/Graph/…
later). Saves raw RFC822 (legal-grade, embedded attachments included) at
<archive>/<folder>/<message-id>.eml and records each message incrementally in
<archive>/_index.jsonl. Resumable: a (folder, validity, key) already in the index is skipped.

Resilient: a hung fetch is force-timed-out; a dropped connection is reconnected with backoff;
if it cannot be re-established the run STOPS cleanly (re-run resumes) rather than cascading.

NOTE: .eml contains EMBEDDED attachments in full, but NOT link-shared files — later phase.
"""
from __future__ import annotations

import email
import hashlib
import json
import re
import sys
import threading
import time
from email import policy
from pathlib import Path

from .safe_name import safe_name
from .sources import get_source

# hard per-message wall-clock cap: a single hung/trickling fetch can never freeze the run
_FETCH_TIMEOUT = 180        # seconds
_RECONNECT_ATTEMPTS = 3     # retries (with backoff) before giving up on a dropped connection
_MAX_CONSEC_ERRORS = 10     # consecutive failures => connection/server is dead, stop the run
_AUTO_MAX_IDLE = 30         # auto-resume: pause after this many consecutive attempts with zero progress
_AUTO_BASE_WAIT = 20        # auto-resume: base seconds to wait between retry attempts


def _fetch_guarded(src, key, timeout):
    """Fetch one message with a hard timeout.

    Returns (raw_or_None, killed). killed=True means the watchdog fired and force-closed the
    socket (so the connection must be re-established before the next fetch). A fetch that
    completes successfully keeps its bytes even if the watchdog fired at the same instant.
    """
    state = {"killed": False}

    def _kill():
        state["killed"] = True
        try:
            src.force_close()       # unblock the in-flight read (shutdown+close the socket)
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill)
    timer.start()
    raw = None
    try:
        raw = src.fetch_raw(key)
    except Exception:
        if not state["killed"]:
            raise                   # a real fetch error — let the caller handle it
        # else: the exception was caused by our own force_close()
    finally:
        timer.cancel()
        timer.join()                # ensure a started _kill()/force_close() finished before returning
    return raw, state["killed"]


def _recover(src, folder, log, attempts=_RECONNECT_ATTEMPTS):
    """Re-establish a healthy connection with backoff. Returns True if recovered, else False."""
    for i in range(attempts):
        try:
            src.reconnect(folder)
            return True
        except Exception as e:
            wait = min(2 ** i, 8)
            log(f"    …reconnect attempt {i + 1}/{attempts} failed ({type(e).__name__}); retrying in {wait}s")
            time.sleep(wait)
    return False


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
def run_archive(cfg: dict, password: str, log=print, limit_per_folder: int | None = None,
                source=None, fetch_timeout: int = _FETCH_TIMEOUT, progress=None) -> dict:
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
    manifest = {"folders": {}, "saved": 0, "skipped": 0, "errors": 0, "aborted": False, "archive_path": str(dest)}

    idx_fh = open(dest / "_index.jsonl", "a", encoding="utf-8")  # incremental, resumable
    try:
        with (source or get_source(kind, conn, password)) as src:
            for fol in folders:
                saved = skipped = errors = 0
                aborted = False
                fol_dir = dest / folder_subpath(fol)

                # --- establish the folder; recover ONCE if the connection dropped at the boundary ---
                validity = keys = None
                for attempt in range(2):
                    try:
                        validity = str(src.select(fol))
                        keys = src.message_keys()
                        break
                    except Exception as e:
                        log(f"  ! [{fol}] open failed: {type(e).__name__}: {e}")
                        if attempt == 0 and _recover(src, fol, log):
                            continue
                        break
                if keys is None:
                    log("  ! connection lost at a folder boundary — stopping; re-run to resume")
                    manifest["aborted"] = True
                    break

                new = [k for k in keys if (fol, validity, str(k)) not in done]
                skipped = len(keys) - len(new)
                if limit_per_folder:
                    new = new[:limit_per_folder]
                step = max(1, len(new) // 20)   # ~20 progress updates regardless of folder size
                log(f"[{fol}] {len(keys)} total · {len(new)} to archive · {skipped} already done")
                fol_dir.mkdir(parents=True, exist_ok=True)

                def report(done=False):
                    if progress:
                        progress({"folder": fol, "total": len(keys), "to_archive": len(new),
                                  "saved": saved, "skipped": skipped, "errors": errors, "done": done})
                report()

                consec = 0

                def recover_or_abort():
                    nonlocal aborted
                    if not _recover(src, fol, log):
                        log(f"  ! [{fol}] connection lost — stopping; {saved} saved this run kept, re-run to resume")
                        aborted = True
                        return True
                    if consec >= _MAX_CONSEC_ERRORS:
                        log(f"  ! [{fol}] {consec} consecutive failures — stopping; re-run to resume")
                        aborted = True
                        return True
                    return False

                for key in new:
                    try:
                        raw, killed = _fetch_guarded(src, key, fetch_timeout)
                        if raw is None:
                            if killed:                       # genuine timeout
                                errors += 1
                                consec += 1
                                log(f"  ! [{fol}] key {key}: fetch exceeded {fetch_timeout}s — skipped")
                                report()
                                if recover_or_abort():
                                    break
                            else:                            # empty fetch — per-message anomaly, NOT a conn failure
                                errors += 1
                                log(f"  ! [{fol}] key {key}: empty fetch")
                                report()
                            continue

                        # success — persist (keep the bytes even if the watchdog fired at the same instant)
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
                        consec = 0
                        if saved % step == 0:
                            log(f"  [{fol}] {saved}/{len(new)} saved…")
                            report()
                        if killed:   # the watchdog had force-closed the socket — heal before the next message
                            if not _recover(src, fol, log):
                                log(f"  ! [{fol}] connection lost after a late timeout — stopping; re-run to resume")
                                aborted = True
                                break
                    except Exception as e:
                        errors += 1
                        consec += 1
                        log(f"  ! [{fol}] key {key}: {type(e).__name__}: {e}")
                        report()
                        if recover_or_abort():
                            break

                report(done=True)
                manifest["folders"][fol] = {"saved": saved, "skipped": skipped, "errors": errors}
                manifest["saved"] += saved
                manifest["skipped"] += skipped
                manifest["errors"] += errors
                log(f"[{fol}] done — {saved} saved, {skipped} already-archived, {errors} errors")
                if aborted:
                    manifest["aborted"] = True
                    break
    finally:
        idx_fh.close()

    verb = "STOPPED (re-run to resume)" if manifest["aborted"] else "FINISHED"
    log(f"{verb} — {manifest['saved']} saved, {manifest['skipped']} skipped, {manifest['errors']} errors")
    return manifest


def run_until_complete(run_once, *, auto_resume=True, should_stop=lambda: False,
                       sleep=time.sleep, log=print,
                       max_idle=_AUTO_MAX_IDLE, base_wait=_AUTO_BASE_WAIT) -> dict:
    """Call run_once() (a resumable run returning a manifest) repeatedly until it completes
    without a connection-loss abort. Rides out transient drops with backoff.

    Stops when: a run completes (manifest['aborted'] is False), the user requests stop,
    auto_resume is off, or max_idle consecutive attempts make zero progress.
    Returns {'attempts': n, 'manifest': last_manifest}.
    """
    attempt = 0
    idle = 0
    last: dict = {}
    while True:
        attempt += 1
        last = run_once()
        if not last.get("aborted"):
            if attempt > 1:
                log(f"Auto-resume: complete after {attempt} attempts.")
            break
        if not auto_resume or should_stop():
            break
        idle = 0 if last.get("saved", 0) > 0 else idle + 1
        if idle >= max_idle:
            log(f"Auto-resume paused: {idle} attempts with no progress — re-run when the connection is back.")
            break
        wait = min(base_wait * (idle + 1), 180)
        log(f"Auto-resume: connection dropped — retrying in {wait}s (next attempt {attempt + 1})…")
        slept = 0
        while slept < wait and not should_stop():
            sleep(1)
            slept += 1
        if should_stop():
            log("Auto-resume: stopped by user.")
            break
    return {"attempts": attempt, "manifest": last}


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
