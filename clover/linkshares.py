"""Phase 1 add-on — link-share harvesting (and, later, download).

Many emails reference files behind share links (SharePoint/OneDrive, Google Drive, Dropbox,
Box, WeTransfer) rather than embedding them. This module:
  - **4a (here):** detect + catalog every share link per archived message -> <archive>/link_shares.jsonl
    so nothing is silently lost (visible in the viewer; status="pending").
  - **4b/4c (next):** download public links directly, then authenticated links via a browser session;
    each record's status moves pending -> downloaded | dead | needs-auth | error.
Pure/deterministic detection; download tiers are added on top of the same record model.
"""
from __future__ import annotations

import email
import json
import re
from email import policy
from pathlib import Path

from .archive import read_index
from .safe_name import safe_name, safe_filename

# provider -> URL pattern (whole-URL match). Order matters only for labelling.
_PROVIDERS = {
    "SharePoint/OneDrive": r"https?://[^\s\"'<>)]*(?:sharepoint\.com|1drv\.ms|onedrive\.live)[^\s\"'<>)]*",
    "Google Drive": r"https?://(?:drive|docs)\.google\.com/[^\s\"'<>)]+",
    "Dropbox": r"https?://[^\s\"'<>)]*dropbox\.com/[^\s\"'<>)]+",
    "WeTransfer": r"https?://(?:we\.tl|[^\s\"'<>)]*wetransfer\.com)/[^\s\"'<>)]+",
    "Box": r"https?://[^\s\"'<>)]*box\.com/[^\s\"'<>)]+",
}
_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in _PROVIDERS.items()]


def detect_links(text: str) -> list[tuple[str, str]]:
    """[(provider, url)] for share links in the text, de-duplicated, trailing punctuation trimmed."""
    out, seen = [], set()
    for name, pat in _COMPILED:
        for m in pat.finditer(text or ""):
            url = m.group(0).rstrip(').,;>"\'')
            if url not in seen:
                seen.add(url)
                out.append((name, url))
    return out


def _body_text(eml_path: Path) -> str:
    with eml_path.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    parts = []
    for pref in ("html", "plain"):
        try:
            p = msg.get_body(preferencelist=(pref,))
            if p is not None:
                parts.append(p.get_content())
        except Exception:
            pass
    return "\n".join(parts)


def link_shares_path(archive_path) -> Path:
    return Path(archive_path) / "link_shares.jsonl"


_shares_cache: dict = {}     # path -> (sig, rows); avoids re-parsing the whole catalogue per request


def _invalidate_shares_cache(p) -> None:
    _shares_cache.pop(str(p), None)


def read_link_shares(archive_path) -> list[dict]:
    """Parsed link_shares.jsonl, cached by (mtime, size). Callers treat the result as read-only
    (the only mutator, _update_records, copies first). Our own writers invalidate explicitly, so the
    cache is correct even when two writes land within the filesystem's mtime granularity."""
    p = link_shares_path(archive_path)
    if not p.exists():
        return []
    key = str(p)
    st = p.stat()
    sig = (st.st_mtime_ns, st.st_size)
    hit = _shares_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    out = []
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
    _shares_cache[key] = (sig, out)
    return out


def links_for_message(archive_path, message_id: str) -> list[dict]:
    return [r for r in read_link_shares(archive_path) if r.get("message_id") == message_id]


def links_for_member(archive_path, message_id: str, locations=None) -> list[dict]:
    """Links for a thread member. Matches the record's message_id, and (for headerless emails whose
    member id is 'path::<rel>', which never equals the index id 'uid_<key>') also by the member's
    stored .eml path — so their harvested links still surface in the viewer."""
    paths = {(loc.get("path") or "").replace("\\", "/") for loc in (locations or []) if loc.get("path")}
    out = []
    for r in read_link_shares(archive_path):
        if r.get("message_id") == message_id or (paths and (r.get("eml") or "").replace("\\", "/") in paths):
            out.append(r)
    return out


def harvest(archive_path, log=print, only_message_ids=None) -> dict:
    """Scan archived .eml for share links; append new ones to link_shares.jsonl (idempotent on
    (message_id, url)). Returns a summary. Detection only — no downloads (that's 4b/4c). With
    `only_message_ids`, scan ONLY those messages (incremental — the post-import pipeline passes the
    ids it just imported instead of re-reading the whole store every run)."""
    dest = Path(archive_path)
    only = set(only_message_ids) if only_message_ids is not None else None
    existing = {(r.get("message_id"), r.get("url")) for r in read_link_shares(dest)}
    added, msgs, by_provider, scanned = 0, set(), {}, 0
    with link_shares_path(dest).open("a", encoding="utf-8") as fh:
        for r in read_index(dest):
            if only is not None and r.get("id") not in only:   # not part of this import — skip
                continue
            rel = r.get("path")
            if not rel:
                continue
            try:
                text = _body_text(dest / rel)
            except Exception:
                continue
            scanned += 1
            links = detect_links(text)
            if links:
                msgs.add(r.get("id"))
            for provider, url in links:
                key = (r.get("id"), url)
                if key in existing:
                    continue
                existing.add(key)
                fh.write(json.dumps({
                    "message_id": r.get("id"), "folder": r.get("folder"), "eml": rel,
                    "provider": provider, "url": url, "status": "pending", "file": None,
                }, ensure_ascii=False) + "\n")
                added += 1
                by_provider[provider] = by_provider.get(provider, 0) + 1
    _invalidate_shares_cache(link_shares_path(dest))     # we just appended — drop the stale cache
    log(f"link harvest: scanned {scanned} message(s) → {added} new share link(s) found")
    return {"added": added, "messages": len(msgs), "scanned": scanned, "by_provider": by_provider,
            "total": len(read_link_shares(dest))}


# ---------------------------------------------------------------- download (4b/4c)
def _update_records(archive_path, updates: dict) -> None:
    """Rewrite link_shares.jsonl applying {(message_id, url): {...}} status/file updates."""
    recs = [dict(r) for r in read_link_shares(archive_path)]   # copy: never mutate cached rows
    for r in recs:
        u = updates.get((r.get("message_id"), r.get("url")))
        if u:
            r.update(u)
    p = link_shares_path(archive_path)
    tmp = p.with_suffix(".jsonl.tmp")            # write-then-atomic-rename: no torn file on crash
    with tmp.open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)
    _invalidate_shares_cache(p)                           # we just rewrote — drop the stale cache


def _dest_dir(archive_path, message_id) -> Path:
    d = Path(archive_path) / "_linkfiles" / (safe_name(message_id or "unknown", maxlen=80) or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _filename_from(headers, url, fallback="download") -> str:
    from urllib.parse import unquote, urlparse
    cd = (headers or {}).get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    name = unquote(m.group(1)).strip() if m else unquote(urlparse(url).path.rsplit("/", 1)[-1])
    return safe_filename(name, maxlen=120, fallback=fallback)


def _unique_path(dest: Path, name: str) -> Path:
    """A non-colliding path in `dest` for `name` — appends ' (2)', ' (3)' before the extension
    so two files from one message never overwrite each other."""
    p = dest / name
    if not p.exists():
        return p
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    i = 2
    while True:
        cand = dest / (f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})")
        if not cand.exists():
            return cand
        i += 1


def _direct_url(url: str, provider: str) -> str | None:
    """A no-browser direct-download URL for the trivial cases, else None (use the browser)."""
    if provider == "Dropbox":
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        parts = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(parts.query) if k != "dl"]
        q.append(("dl", "1"))
        return urlunparse(parts._replace(query=urlencode(q)))
    if provider == "Google Drive":
        m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def _http_fetch(url: str, provider: str, limit_bytes: int | None = None):
    """Trivial direct download via httpx, streamed so an oversize file is caught before the whole
    transfer -> ('downloaded', filename, bytes) | ('oversize', None, size) | None."""
    direct = _direct_url(url, provider)
    if not direct:
        return None
    try:
        import httpx
        with httpx.stream("GET", direct, follow_redirects=True, timeout=30) as r:
            if r.status_code != 200 or "text/html" in r.headers.get("content-type", "").lower():
                return None
            cl = r.headers.get("content-length")
            if limit_bytes and cl and cl.isdigit() and int(cl) > limit_bytes:
                return "oversize", None, int(cl)             # known too-big up front — no transfer
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if limit_bytes and len(buf) > limit_bytes:
                    return "oversize", None, len(buf)        # streamed past the threshold — stop
            name = _filename_from(r.headers, direct)
        data = bytes(buf)
        if not data:
            return None
        head = data[:512].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")):
            return None                                      # HTML login/interstitial mislabeled as a file
        return "downloaded", name, data
    except Exception:
        return None


# Cookie/consent banners cover the download control on most providers; clear them first.
_CONSENT_SELS = [
    "button:has-text('Accept all')", "button:has-text('Accept All')",
    "button:has-text('Accept')", "button:has-text('I agree')",
    "button:has-text('Agree')", "button:has-text('Allow all')",
    "[aria-label*='Accept']", "[data-testid='accept-cookies']",
]


def _dismiss_consent(page):
    """Best-effort dismissal of a cookie/consent banner (never the download trigger itself)."""
    for sel in _CONSENT_SELS:
        try:
            page.click(sel, timeout=1500)
            return
        except Exception:
            continue


def _trigger_download(page, provider):
    """Best-effort click of the provider's Download control (selectors tuned against real links).
    Consent banners are dismissed first so they don't swallow the click."""
    _dismiss_consent(page)
    sels = {
        "Dropbox": ["[data-testid='download-button']", "button:has-text('Download')",
                    "[aria-label='Download']"],
        "Google Drive": ["[aria-label*='Download']", "div[role='button'][aria-label*='Download']",
                          "div[role='button']:has-text('Download')"],
        "WeTransfer": ["button:has-text('Download')", "a:has-text('Download')"],
        "Box": ["button:has-text('Download')", "[aria-label='Download']"],
        "SharePoint/OneDrive": ["button[name='Download']", "button:has-text('Download')",
                                "[aria-label='Download']"],
    }
    for sel in sels.get(provider, ["button:has-text('Download')"]):
        try:
            page.click(sel, timeout=5000)
            return
        except Exception:
            continue


def _playwright_fetch(url: str, provider: str, headless: bool = True, timeout: int = 60,
                      limit_bytes: int | None = None):
    """Browser download flow -> ('downloaded', name, bytes) | ('oversize', None, size)
    | ('needs-auth'|'dead', None, None). Browser providers (e.g. SharePoint folder-zips) don't
    expose a size up front, so an oversize file is detected after it streams to a temp file."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return "needs-auth", None, None              # engine not installed
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                nav = min(timeout, 30)               # cap navigation so a hanging page can't burn the whole budget
                resp = page.goto(url, timeout=nav * 1000, wait_until="domcontentloaded")
                if resp is not None and resp.status in (404, 410):
                    return "dead", None, None
                try:
                    with page.expect_download(timeout=timeout * 1000) as dl:
                        _trigger_download(page, provider)
                    d = dl.value
                    import tempfile
                    tmp = Path(tempfile.gettempdir()) / (d.suggested_filename or "download")
                    d.save_as(str(tmp))
                    size = tmp.stat().st_size
                    if not size:
                        tmp.unlink(missing_ok=True)
                        return "needs-auth", None, None  # empty download = gated/failed, retry later
                    if limit_bytes and size > limit_bytes:
                        tmp.unlink(missing_ok=True)
                        return "oversize", None, size    # too big — flag for confirmation, don't keep
                    data = tmp.read_bytes()
                    tmp.unlink(missing_ok=True)
                    return "downloaded", safe_filename(d.suggested_filename or "download", maxlen=120), data
                except Exception:
                    return "needs-auth", None, None  # gated / expired / flow changed
            finally:
                ctx.close()
                browser.close()
    except Exception:
        return "needs-auth", None, None


def _default_fetch(url, provider, headless=True, timeout=60, limit_bytes=None):
    return (_http_fetch(url, provider, limit_bytes)
            or _playwright_fetch(url, provider, headless=headless, timeout=timeout, limit_bytes=limit_bytes))


def _status_note(status: str, provider: str, url: str) -> str:
    """A plain-English reason for a non-downloaded outcome, so the user isn't left with a bare status."""
    if status == "needs-auth":
        return "Sign-in required (private link). Open it yourself, or download the file and attach it."
    if status == "dead":
        if provider == "Google Drive" and "docs.google.com" in (url or ""):
            return "Google Doc — can't auto-download. Open it signed in and use File > Download, or share it as a file."
        return "Link returned not-found / expired — the file may have been removed or the URL changed."
    if status == "error":
        return "Couldn't fetch this link (network or browser error) — it'll be retried next time."
    return ""


def mark_confirmed(archive_path, message_id: str, url: str) -> None:
    """User OK'd a large link in the UI — clear the size gate and re-queue it for the next fetch."""
    _update_records(archive_path, {(message_id, url): {"confirmed": True, "status": "pending"}})


def _email_date_iso(date_hdr: str | None) -> str:
    """RFC-2822 Date header -> 'YYYY-MM-DD' (UTC date part), or '' if unparseable/missing."""
    if not date_hdr:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(date_hdr)
    except Exception:
        return ""
    return dt.date().isoformat() if dt else ""


def fetch_links(archive_path, *, fetcher=None, limit=50, headless=True, timeout=60,
                confirm_over_mb=1024, only_message_ids=None, date_from=None, date_to=None,
                providers=None, log=print, should_stop=lambda: False,
                progress=lambda **k: None) -> dict:
    """Download up to `limit` pending links into _linkfiles/<message-id>/, updating each record's
    status (downloaded | needs-confirm | dead | needs-auth | error). Re-runnable (only touches 'pending').

    URL-dedup: the same share link is resolved once — a successful download is reused, and a
    dead/gated/oversize outcome is reused too — so duplicate links across emails never re-fetch
    (critical at scale: ~85% of a real corpus is repeated links). Size gate: a download exceeding
    `confirm_over_mb` is NOT kept — it's marked
    'needs-confirm' (with its size) so the UI can ask the user; a record flagged 'confirmed' bypasses
    the gate. `timeout` bounds the browser download wait per link (navigation capped separately).

    Selection filters (all optional, AND-combined): `date_from`/`date_to` ('YYYY-MM-DD', inclusive) keep
    only links whose source email falls in that window — an email with no parseable date is EXCLUDED while
    a date filter is active (you asked for a range), but kept when no date filter is set; `providers` (a
    list of provider names) keeps only links from those providers. Unselected links stay 'pending'."""
    import inspect
    from . import malware as malwaremod
    limit_bytes = int(confirm_over_mb * 1024 * 1024) if confirm_over_mb else None
    if fetcher:
        try:
            takes_limit = len(inspect.signature(fetcher).parameters) >= 3
        except (ValueError, TypeError):
            takes_limit = False
        fetch = (lambda u, p, lb: fetcher(u, p, lb)) if takes_limit else (lambda u, p, lb: fetcher(u, p))
    else:
        fetch = lambda u, p, lb: _default_fetch(u, p, headless, timeout, lb)

    recs = read_link_shares(archive_path)
    done_files = {r.get("url"): r.get("file")            # url -> already-downloaded file (cross-run dedup)
                  for r in recs if r.get("status") == "downloaded" and r.get("file")}
    oversize = {r.get("url"): r.get("size")              # url -> known-oversize size (don't re-pull)
                for r in recs if r.get("status") == "needs-confirm"}
    dead_urls = {r.get("url") for r in recs if r.get("status") == "dead"}        # don't re-try a known-dead link
    auth_urls = {r.get("url") for r in recs if r.get("status") == "needs-auth"}  # ...or a known-gated one
    provset = {p for p in providers} if providers else None     # None -> all providers
    mid_date = None                                              # message_id -> 'YYYY-MM-DD', built lazily
    if date_from or date_to:
        mid_date = {r.get("id"): _email_date_iso(r.get("date")) for r in read_index(archive_path)}

    def _selected(r) -> bool:
        if provset is not None and r.get("provider") not in provset:
            return False
        if mid_date is not None:                                # a date window is active
            d = mid_date.get(r.get("message_id"), "")
            if not d:                                           # undated email -> not in any dated range
                return False
            if date_from and d < date_from:
                return False
            if date_to and d > date_to:
                return False
        return True

    pending = [r for r in recs if r.get("status") == "pending"
               and (only_message_ids is None or r.get("message_id") in only_message_ids)
               and _selected(r)]
    updates = {}
    done = reused = confirm = dead = auth = infected = unscanned = 0
    batch = pending[:limit]
    for i, r in enumerate(batch):
        if should_stop():
            break
        mid, url, prov, ok = r.get("message_id"), r.get("url"), r.get("provider"), bool(r.get("confirmed"))
        progress(done=done, total=len(batch), current=str(url)[:80], provider=prov,
                 failed=dead + auth, i=i)
        if url in done_files:                            # already have this exact link — reuse, no transfer
            updates[(mid, url)] = {"status": "downloaded", "file": done_files[url]}
            reused += 1
            continue
        if not ok and url in oversize:                   # known too-big and not yet confirmed
            updates[(mid, url)] = {"status": "needs-confirm", "size": oversize[url]}
            confirm += 1
            continue
        if not ok and url in dead_urls:                  # same link already dead elsewhere — skip the re-fetch
            updates[(mid, url)] = {"status": "dead", "note": _status_note("dead", prov, url)}
            dead += 1
            continue
        if not ok and url in auth_urls:                  # same link already gated elsewhere — skip the re-fetch
            updates[(mid, url)] = {"status": "needs-auth", "note": _status_note("needs-auth", prov, url)}
            auth += 1
            continue
        try:
            status, fname, data = fetch(url, prov, None if ok else limit_bytes)
        except Exception as e:
            status, fname, data = "error", None, None
            log(f"  ! {prov} {str(url)[:60]}: {type(e).__name__}")
        if status == "downloaded" and data:
            path = _unique_path(_dest_dir(archive_path, mid), fname or "download")  # never overwrite
            path.write_bytes(data)
            rel = str(path.relative_to(Path(archive_path))).replace("\\", "/")
            scan = malwaremod.scan_file(path)                # check the untrusted download before keeping it
            if scan.get("clean") is False:                   # MALWARE / unsafe archive -> quarantine: delete, never comprehended
                try:
                    path.unlink()
                except OSError:
                    pass
                updates[(mid, url)] = {"status": "infected", "file": None,
                                       "note": f"malware: {scan.get('threat') or 'detected'} ({scan.get('scanner')})"}
                infected += 1
                log(f"  !! MALWARE in {prov} download {str(url)[:50]}: {scan.get('threat')} — deleted, not comprehended")
            elif not scan.get("scanned"):                    # could NOT verify -> keep on disk but do NOT comprehend; flag
                updates[(mid, url)] = {"status": "unverified", "file": rel, "size": len(data),
                                       "note": f"not malware-scanned: {scan.get('note') or scan.get('scanner')}"}
                unscanned += 1
                log(f"  ? could not malware-scan {prov} download {str(url)[:50]}: {scan.get('note')} — kept, NOT comprehended")
            else:                                            # verified clean
                updates[(mid, url)] = {"status": "downloaded", "file": rel, "size": len(data), "scanned": True}
                done_files[url] = rel
                done += 1
        elif status == "oversize":
            size = data if isinstance(data, int) else None
            updates[(mid, url)] = {"status": "needs-confirm", "size": size}
            oversize[url] = size
            confirm += 1
        else:
            if status == "downloaded":          # 'downloaded' with no bytes -> retryable failure, not success
                status = "error"
            updates[(mid, url)] = {"status": status, "note": _status_note(status, prov, url)}
            if status == "dead":
                dead += 1
                dead_urls.add(url)              # remember so duplicate emails skip the re-fetch
            elif status == "needs-auth":
                auth += 1
                auth_urls.add(url)
    _update_records(archive_path, updates)
    # remaining must honour the SAME scope (only_message_ids + provider/date filters) used to pick the batch —
    # otherwise a caller looping until remaining<=0 never terminates while an out-of-scope backlog stays pending.
    remaining = sum(1 for r in read_link_shares(archive_path)
                    if r.get("status") == "pending"
                    and (only_message_ids is None or r.get("message_id") in only_message_ids)
                    and _selected(r))
    log(f"link fetch: {done} downloaded ({reused} reused), {confirm} need-confirm, "
        f"{dead} dead, {auth} need-auth"
        + (f", {infected} INFECTED (quarantined)" if infected else "")
        + (f", {unscanned} UNVERIFIED (kept, not comprehended)" if unscanned else "")
        + f" · {remaining} pending")
    return {"downloaded": done, "reused": reused, "needs_confirm": confirm,
            "dead": dead, "needs_auth": auth, "infected": infected, "unscanned": unscanned,
            "remaining": remaining}
