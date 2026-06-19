"""Phase 1 orchestrator — archive every email from a MailSource to local .eml files.

Source-agnostic: works against any clover.sources.MailSource (IMAP today; Gmail/Graph/…
later). Saves raw RFC822 (legal-grade, embedded attachments included) at
<archive>/<folder>/<message-id>.eml and records each message incrementally in
<archive>/_index.jsonl. Resumable: a (folder, validity, key) already in the index is skipped.

Resilient: a hung fetch is force-timed-out; a dropped connection is reconnected with backoff;
if it cannot be re-established the run STOPS cleanly (re-run resumes) rather than cascading.

NOTE: .eml contains EMBEDDED attachments in full; link-shared files (Dropbox/Drive/etc.) are
captured separately by clover.linkshares (link_shares.jsonl + <archive>/_linkfiles/).
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


class _PrepStopped(Exception):
    """Raised inside the prep/scan phase when the user requests Stop, so a long metadata
    fetch can abort promptly instead of running to completion before the next stop check."""


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


def reconcile(archive_path) -> dict:
    """Local integrity check: every indexed message has its .eml on disk, and no .eml on disk is
    missing from the index. Returns per-folder counts + the missing/orphan lists (capped)."""
    dest = Path(archive_path)
    indexed: dict[str, str] = {}                  # relative path -> folder
    per_folder: dict[str, dict] = {}
    for r in read_index(dest):
        p = (r.get("path") or "").replace("\\", "/")
        if not p:
            continue
        fol = r.get("folder", "?")
        indexed[p] = fol
        per_folder.setdefault(fol, {"indexed": 0, "on_disk": 0})["indexed"] += 1
    disk: set[str] = set()
    if dest.exists():
        for f in dest.rglob("*.eml"):
            if "_linkfiles" in f.parts:               # downloaded link files aren't archive messages
                continue
            disk.add(str(f.relative_to(dest)).replace("\\", "/"))
    idx_paths = set(indexed)
    missing = sorted(idx_paths - disk)            # indexed but the file is gone
    orphans = sorted(disk - idx_paths)            # file on disk but not in the index
    for rel in disk:
        fol = indexed.get(rel) or rel.split("/")[0]
        per_folder.setdefault(fol, {"indexed": 0, "on_disk": 0})["on_disk"] += 1
    return {
        "per_folder": per_folder,
        "total_indexed": len(idx_paths), "total_on_disk": len(disk),
        "missing": missing[:50], "missing_count": len(missing),
        "orphans": orphans[:50], "orphans_count": len(orphans),
        "ok": not missing and not orphans,
    }


# ---------------------------------------------------------------- selection filters
def _warn_unmeasured(base, meta, log) -> None:
    """A filtered archive can only keep messages whose date/size we could read. Surface any whose
    metadata the server didn't return (e.g. a garbled FETCH line) instead of dropping them silently."""
    missing = sum(1 for k in base if k not in meta)
    if missing:
        log(f"  filter: {missing} message(s) had unreadable metadata — excluded from the filter")


def _meta_matches(meta: dict, date_from, date_to, size_min) -> bool:
    """True if a message's metadata passes the date/size-threshold filters (client-side).

    A message with no parseable date is EXCLUDED whenever a date bound is set (it can't be
    placed in the range). Dates compare by calendar day; size_min is a >= byte threshold."""
    d = meta.get("date")
    if (date_from or date_to):
        if d is None:
            return False
        day = d.date()
        if date_from and day < date_from:
            return False
        if date_to and day > date_to:
            return False
    if size_min and meta.get("size", 0) < size_min:
        return False
    return True


def _select_keys(src, folder, filters, log, on_prep, should_stop=lambda: False) -> list[str]:
    """Keys to archive for the SELECTED folder after applying date/size filters.

    Strategy: no filters -> all keys. top-N -> size every (date-narrowed) message client-side and
    rank. Otherwise -> try server-side UID SEARCH (date + size threshold); on failure/unsupported,
    fall back to client-side metadata filtering. on_prep(stage, done, total) drives the prep UI.

    should_stop() is polled before each network round-trip and between metadata batches; if it
    fires, _PrepStopped is raised so a long scan aborts promptly (the caller treats it as a
    user-stop, not a connection failure).
    """
    filters = filters or {}
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    size_min = filters.get("size_min")
    top_n = filters.get("top_n")

    if not (date_from or date_to or size_min or top_n):
        return src.message_keys()

    def _meta_progress(d, t):                 # cancellation point between metadata batches
        if should_stop():
            raise _PrepStopped()
        on_prep("meta", d, t)

    # --- top-N by size: the server can't rank, so size every candidate client-side ---
    if top_n:
        if should_stop():
            raise _PrepStopped()
        base, server_dated = None, False
        if date_from or date_to:                       # narrow by date on the server if we can
            try:
                base = src.search(date_from=date_from, date_to=date_to, size_min=None)
                server_dated = base is not None        # server already applied the date window
            except Exception:
                base = None
        if base is None:
            base = src.message_keys()
        meta = src.message_meta(base, progress=_meta_progress) if base else {}
        _warn_unmeasured(base, meta, log)
        # keep only sizable messages; trust the server's date filter when it ran, else apply it here
        kept = [k for k in base
                if k in meta and (server_dated or _meta_matches(meta[k], date_from, date_to, None))]
        kept.sort(key=lambda k: meta[k].get("size", 0), reverse=True)
        log(f"  filter: largest {min(top_n, len(kept))} of {len(base)} message(s) selected")
        return kept[:top_n]

    # --- date and/or size threshold: server search first, client fallback ---
    keys = None
    try:
        if should_stop():
            raise _PrepStopped()
        on_prep("search", 0, 0)
        keys = src.search(date_from=date_from, date_to=date_to, size_min=size_min)
    except _PrepStopped:
        raise                                          # a user-stop must not be swallowed below
    except Exception as e:
        log(f"  filter: server search errored ({type(e).__name__}) — using client-side filter")
        keys = None
    if keys is not None:
        log(f"  filter: server-side search matched {len(keys)} message(s)")
        return keys

    if should_stop():
        raise _PrepStopped()
    base = src.message_keys()
    meta = src.message_meta(base, progress=_meta_progress) if base else {}
    _warn_unmeasured(base, meta, log)
    kept = [k for k in base if k in meta and _meta_matches(meta[k], date_from, date_to, size_min)]
    log(f"  filter: client-side matched {len(kept)} of {len(base)} message(s)")
    return kept


# ---------------------------------------------------------------- orchestrator
def run_archive(cfg: dict, password: str, log=print, limit_per_folder: int | None = None,
                source=None, fetch_timeout: int = _FETCH_TIMEOUT, progress=None,
                should_stop=lambda: False, filters: dict | None = None, prep=None) -> dict:
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
    manifest = {"folders": {}, "saved": 0, "skipped": 0, "errors": 0, "aborted": False, "stopped": False, "archive_path": str(dest)}

    idx_fh = open(dest / "_index.jsonl", "a", encoding="utf-8")  # incremental, resumable
    try:
        with (source or get_source(kind, conn, password)) as src:
            for fol in folders:
                saved = skipped = errors = 0
                aborted = False
                stopped = False
                fol_dir = dest / folder_subpath(fol)

                def on_prep(stage, done=0, total=0):
                    if prep:
                        prep({"folder": fol, "stage": stage, "done": done, "total": total})

                # --- establish the folder; recover ONCE if the connection dropped at the boundary ---
                validity = keys = None
                for attempt in range(2):
                    try:
                        validity = str(src.select(fol))
                        keys = _select_keys(src, fol, filters, log, on_prep, should_stop)
                        break
                    except _PrepStopped:                 # user hit Stop during the scan/prep phase
                        stopped = True
                        break
                    except Exception as e:
                        log(f"  ! [{fol}] open failed: {type(e).__name__}: {e}")
                        if attempt == 0 and _recover(src, fol, log):
                            continue
                        break
                if prep:
                    prep(None)                       # selection/prep phase done — clear the prep UI
                if stopped:
                    manifest["stopped"] = True
                    log("Stopped by user.")
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
                    if should_stop():            # user hit Stop — break promptly, mid-attempt
                        stopped = True
                        break
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
                if stopped:
                    manifest["stopped"] = True
                    log("Stopped by user.")
                    break
                if aborted:
                    manifest["aborted"] = True
                    break
    finally:
        idx_fh.close()

    verb = ("STOPPED BY USER" if manifest["stopped"]
            else "STOPPED (re-run to resume)" if manifest["aborted"] else "FINISHED")
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
        if last.get("stopped"):           # user stopped mid-attempt — do not retry
            break
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
