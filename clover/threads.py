"""Phase 2 — deterministic thread organization.

Reads the Phase-1 `.eml` archive + `_index.jsonl`, links messages into threads by
Message-ID / References / In-Reply-To (header-only; no AI, no network), and writes
`<archive>/threads.jsonl` next to the index. Pure transform: re-running on the same archive
yields the same result. Thread *content* is never materialized — the reader stitches member
`.eml` on demand (see render_message / stitch_thread).
"""
from __future__ import annotations

import base64
import email
import hashlib
import json
import re
from datetime import timezone
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .archive import read_index

_ID_RE = re.compile(r"<[^>]+>")
_SUBJ_PREFIX = re.compile(r"^(?:\s*(?:re|fwd|fw|aw|sv|antw)\s*:\s*)+", re.I)
_CID_RE = re.compile(r'(?i)cid:([^\s"\'>)]+)')
_MAX_INLINE_IMG = 12 * 1024 * 1024          # per-image cap for base64 inlining (bytes)
_HEADER_PARSER = BytesHeaderParser(policy=policy.default)


def _img_data_uri(payload: bytes, ctype: str) -> str | None:
    """base64 data: URI for an image payload, or None if not an inlineable image."""
    ctype = (ctype or "").lower()
    if payload and ctype.startswith("image/") and len(payload) <= _MAX_INLINE_IMG:
        return f"data:{ctype};base64," + base64.b64encode(payload).decode("ascii")
    return None


def _referenced_cids(msg) -> set[str]:
    """Normalized cids referenced by <img src="cid:..."> in the message's HTML body — the same body→cid
    set that _inline_cid_images() rewrites. An image part whose cid is NOT in here is a real attachment."""
    try:
        part = msg.get_body(preferencelist=("html",))
        html = part.get_content() if part is not None else ""
    except Exception:
        html = ""
    return {c.strip().strip("<>") for c in _CID_RE.findall(html or "")}


def _is_real_attachment(part, referenced_cids=frozenset()) -> bool:
    """A genuine file attachment — not an inline/cid image (signature logo, embedded screenshot)."""
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    if disp == "inline":
        return part.get_content_maintype() != "image"  # inline PDFs/docs are real; inline images aren't
    cid = part.get("Content-ID")
    if cid and part.get_content_maintype() == "image":
        # hide it only when the body actually references this cid; an image attachment carrying a stray
        # Content-ID with no disposition (older Outlook/Apple Mail) is a real attachment, not inline.
        return cid.strip().strip("<>") not in referenced_cids
    return bool(part.get_filename())


def _real_attachments(msg) -> list:
    referenced = _referenced_cids(msg)
    return [p for p in msg.iter_attachments() if _is_real_attachment(p, referenced)]


def _inline_cid_images(msg, html: str) -> str:
    """Rewrite <img src="cid:..."> to data: URIs from the message's embedded image parts, so inline
    screenshots / photos / logos render in the sandboxed iframe (whose CSP allows only img-src data:)."""
    cmap: dict[str, str] = {}
    for part in msg.walk():
        cid = part.get("Content-ID")
        if not cid:
            continue
        uri = _img_data_uri(part.get_payload(decode=True) or b"", part.get_content_type())
        if uri:
            cmap[cid.strip().strip("<>").strip()] = uri
    if not cmap:
        return html
    return _CID_RE.sub(lambda m: cmap.get(m.group(1).strip().strip("<>"), m.group(0)), html)


# ---------------------------------------------------------------- pure helpers
def _norm_id(s: str | None) -> str:
    return (s or "").strip().strip("<>").strip().lower()


def _ids_in(value: str | None) -> list[str]:
    """All <...> message-ids in a References/In-Reply-To header, normalized."""
    return [m.strip("<>").lower() for m in _ID_RE.findall(value or "")]


def _clean_subject(s: str | None) -> str:
    return _SUBJ_PREFIX.sub("", (s or "").strip()).strip() or "(no subject)"


def _to_utc_iso(date_hdr: str | None) -> str | None:
    """RFC-2822 Date header -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC), or None if unparseable."""
    if not date_hdr:
        return None
    try:
        dt = parsedate_to_datetime(date_hdr)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:                       # naive -> assume UTC (rare; defensive)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _participants(members: list[dict]) -> list[str]:
    """Ordered-unique email addresses across every member's From + To."""
    seen: list[str] = []
    for m in members:
        for _, addr in getaddresses([m.get("from", ""), m.get("to", "")]):
            a = addr.strip().lower()
            if a and a not in seen:
                seen.append(a)
    return seen


class _DSU:
    """Union-find over message-id nodes (string keys)."""

    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        r = x
        while self.p[r] != r:
            r = self.p[r]
        while self.p[x] != r:                    # path compression
            self.p[x], x = r, self.p[x]
        return r

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ---------------------------------------------------------------- builder
def build_threads(archive_path, log=print) -> dict:
    """Link every archived message into a thread; write <archive>/threads.jsonl. Returns a summary."""
    archive = Path(archive_path)
    rows = read_index(archive)
    dsu = _DSU()
    members: dict[str, dict] = {}                # node -> member record

    for row in rows:
        rel = row.get("path")
        if not rel:
            continue
        try:
            with (archive / rel).open("rb") as fh:
                h = _HEADER_PARSER.parse(fh)
        except Exception as e:
            log(f"  ! skip unreadable {rel}: {type(e).__name__}")
            continue
        mid = _norm_id(h.get("Message-ID"))
        # a missing Message-ID can't be referenced by others and uid_ ids aren't globally unique,
        # so key such a message by its (unique) path -> guarantees it's its own node
        node = mid if mid else f"path::{rel}"
        m = members.get(node)
        if m is None:
            m = members[node] = {
                "message_id": mid or node, "date": _to_utc_iso(h.get("Date")),
                "from": str(h.get("From", "") or ""), "to": str(h.get("To", "") or ""),
                "subject": str(h.get("Subject", "") or ""), "locations": [],
            }
        loc = {"folder": row.get("folder", "?"), "path": rel}
        if loc not in m["locations"]:            # cross-folder dup -> one member, many locations
            m["locations"].append(loc)
        dsu.find(node)
        for ref in _ids_in(h.get("In-Reply-To")) + _ids_in(h.get("References")):
            dsu.union(node, ref)

    comps: dict[str, list[str]] = {}
    for node in members:
        comps.setdefault(dsu.find(node), []).append(node)

    threads: list[dict] = []
    for nodes in comps.values():
        mem = [members[n] for n in nodes]
        mem.sort(key=lambda r: (r["date"] is None, r["date"] or ""))   # chronological; undated last
        ids_sorted = sorted(members[n]["message_id"] for n in nodes)
        thread_id = hashlib.sha1("\n".join(ids_sorted).encode("utf-8")).hexdigest()[:16]
        dates = [r["date"] for r in mem if r["date"]]
        threads.append({
            "thread_id": thread_id,
            "root_id": mem[0]["message_id"],
            "subject": _clean_subject(mem[0]["subject"]),
            "n": len(mem),
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            "participants": _participants(mem),
            "members": [{"message_id": r["message_id"], "date": r["date"], "from": r["from"],
                         "subject": r["subject"], "locations": r["locations"]} for r in mem],
        })
    threads.sort(key=lambda t: (t["end"] or ""), reverse=True)          # most-recent activity first

    out = archive / "threads.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for t in threads:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    summary = {
        "threads": len(threads),
        "multi": sum(1 for t in threads if t["n"] > 1),
        "singletons": sum(1 for t in threads if t["n"] == 1),
        "messages": sum(t["n"] for t in threads),
        "path": str(out),
    }
    log(f"threads: {summary['threads']} ({summary['multi']} multi · {summary['singletons']} single) "
        f"from {summary['messages']} messages -> {out.name}")
    return summary


# ---------------------------------------------------------------- readers (for the browser)
_threads_cache: dict[str, tuple] = {}    # path -> (mtime_ns, rows); avoids re-parsing on every /msg/ open


def read_threads(archive_path) -> list[dict]:
    """Parsed threads.jsonl, cached by file mtime (callers treat the result as read-only)."""
    p = Path(archive_path) / "threads.jsonl"
    if not p.exists():
        return []
    key = str(p)
    mtime = p.stat().st_mtime_ns
    hit = _threads_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    out: list[dict] = []
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
    _threads_cache[key] = (mtime, out)
    return out


def get_thread(archive_path, thread_id: str) -> dict | None:
    for t in read_threads(archive_path):
        if t.get("thread_id") == thread_id:
            return t
    return None


def render_message(archive_path, location: dict) -> dict:
    """Read one member .eml and return a render-ready block: headers, body (html or plain),
    attachment list. No network; bodies are sanitized at render time by the sandboxed iframe."""
    base = Path(archive_path).resolve()
    target = (base / location["path"]).resolve()
    if not target.is_relative_to(base):       # defense-in-depth: never read outside the archive
        raise ValueError("message path escapes the archive")
    with target.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    body_html = body_text = None
    try:
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            body_html = part.get_content()
    except Exception:
        body_html = None
    if body_html is None:
        try:
            part = msg.get_body(preferencelist=("plain",))
            if part is not None:
                body_text = part.get_content()
        except Exception:
            body_text = None
    if body_html:
        body_html = _inline_cid_images(msg, body_html)     # show inline screenshots/photos/logos
    atts = []
    try:
        for i, part in enumerate(_real_attachments(msg)):     # exclude inline/cid images
            payload = part.get_payload(decode=True) or b""
            a = {"i": i, "name": part.get_filename() or "(unnamed)", "size": len(payload)}
            if part.get_content_maintype() == "image" and 0 < len(payload) <= _MAX_INLINE_IMG:
                a["img"] = True            # flag only — the template serves it via the /att/ URL (no base64 in the page)
            atts.append(a)
    except Exception:
        pass
    return {
        "from": str(msg.get("From", "") or ""), "to": str(msg.get("To", "") or ""),
        "date": str(msg.get("Date", "") or ""), "subject": str(msg.get("Subject", "") or ""),
        "body_html": body_html, "body_text": body_text, "attachments": atts,
        "folder": location.get("folder"),
    }


def get_attachment(archive_path, location: dict, index: int):
    """Return (filename, content_type, bytes) for the nth attachment of a member .eml, or None.
    Same path-containment guard as render_message (never read outside the archive)."""
    base = Path(archive_path).resolve()
    target = (base / location["path"]).resolve()
    if not target.is_relative_to(base):
        raise ValueError("message path escapes the archive")
    with target.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    parts = _real_attachments(msg)                            # same filter as render_message -> aligned indices
    if 0 <= index < len(parts):
        part = parts[index]
        payload = part.get_payload(decode=True) or b""
        return (part.get_filename() or f"attachment_{index}",
                part.get_content_type() or "application/octet-stream", payload)
    return None


def stitch_thread(archive_path, thread: dict) -> list[dict]:
    """Render every member of a thread in chronological order (members are already sorted)."""
    blocks = []
    for m in thread.get("members", []):
        locs = m.get("locations") or []
        if not locs or not locs[0].get("path"):
            continue
        try:
            blocks.append(render_message(archive_path, locs[0]))
        except Exception as e:
            blocks.append({"error": f"{type(e).__name__}: {e}", "date": m.get("date"),
                           "from": m.get("from", ""), "subject": m.get("subject", "")})
    return blocks
