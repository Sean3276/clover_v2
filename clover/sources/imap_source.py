"""IMAP implementation of MailSource (the reviewed Phase-1 ingestion logic)."""
from __future__ import annotations

import re
import socket
from contextlib import suppress
from datetime import datetime, timedelta

from .base import MailSource

_IMAP_TIMEOUT = 30  # seconds — bound hangs on connect/login/status
_META_CHUNK = 500   # UIDs per metadata FETCH (one round-trip per chunk)

# IMAP FETCH metadata lines look like:  b'7 (UID 42 INTERNALDATE "11-Jun-2026 01:22:12 +0000" RFC822.SIZE 4096)'
_RE_UID = re.compile(rb"UID (\d+)")
_RE_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_RE_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _parse_fetch_meta(item) -> tuple[str | None, datetime | None, int]:
    """Parse one FETCH-metadata response item -> (uid, date|None, size). Pure/testable.

    INTERNALDATE is space-padded for single-digit days (' 1-Jun-2026 …'); strip() before parsing.
    A missing/garbled INTERNALDATE yields date=None (the caller treats that as un-placeable)."""
    if isinstance(item, tuple):                  # defensive: some servers wrap in a tuple
        item = item[0]
    if not isinstance(item, (bytes, bytearray)):
        return None, None, 0
    b = bytes(item)
    mu, ms, md = _RE_UID.search(b), _RE_SIZE.search(b), _RE_DATE.search(b)
    uid = mu.group(1).decode() if mu else None
    size = int(ms.group(1)) if ms else 0
    date = None
    if md:
        with suppress(Exception):
            date = datetime.strptime(md.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
    return uid, date, size


class ImapSource(MailSource):
    kind = "imap"

    def __init__(self, conn: dict, password: str):
        super().__init__(conn, password)
        self.host = self.conn.get("host")
        self.port = self.conn.get("port")
        self.security = (self.conn.get("security") or "ssl").lower()
        self.user = self.conn.get("user")
        self._mb = None

    def _connect(self):
        import imap_tools
        if self.security == "starttls":
            # class name varies across imap-tools versions (MailBoxStartTls in 1.x; MailBoxTls older)
            cls = getattr(imap_tools, "MailBoxStartTls", None) or getattr(imap_tools, "MailBoxTls", None)
            if cls is None:
                raise RuntimeError("Installed imap-tools has no STARTTLS class — use SSL/TLS (993).")
            mb = cls(self.host, self.port or 143, timeout=_IMAP_TIMEOUT)
        else:
            mb = imap_tools.MailBox(self.host, self.port or (993 if self.security == "ssl" else 143), timeout=_IMAP_TIMEOUT)
        mb.login(self.user, self.password)
        return mb

    def _require_open(self):
        if self._mb is None:
            raise RuntimeError("source not open; use within a 'with get_source(...)' block")

    def open(self) -> None:
        self._mb = self._connect()

    def close(self) -> None:
        if self._mb is not None:
            with suppress(Exception):
                self._mb.logout()
            self._mb = None

    def force_close(self) -> None:
        """Unblock a read wedged on a hung/trickling fetch by shutting down then closing the
        socket (shutdown interrupts a blocked recv more reliably than close() alone; logout()
        would itself block on a wedged connection). The 30s socket timeout is the final backstop."""
        mb = self._mb
        if mb is None:
            return
        sock = getattr(getattr(mb, "client", None), "sock", None)
        if sock is None:
            return
        with suppress(Exception):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(Exception):
            sock.close()

    def test(self) -> tuple[bool, str]:
        try:
            mb = self._connect()
            try:
                n = len(mb.folder.list())
            finally:
                with suppress(Exception):
                    mb.logout()
            return True, f"OK — login succeeded, {n} folders visible"
        except Exception as e:
            from ..errors import friendly_conn_error
            return False, friendly_conn_error(e)

    def folders(self) -> list[dict]:
        self._require_open()
        out: list[dict] = []
        for f in self._mb.folder.list():
            cnt = None
            try:
                cnt = int(self._mb.folder.status(f.name).get("MESSAGES", 0))
            except Exception:
                cnt = None
            out.append({"name": f.name, "messages": cnt})
        return out

    def select(self, folder: str) -> str:
        """Select a folder and return its UIDVALIDITY.

        Prefer the value the server returns on SELECT (always present, no extra round-trip);
        fall back to an explicit STATUS; finally a STABLE sentinel ('na') — never '' (which the
        MailSource contract reserves for 'globally stable keys') so the resume key stays
        consistent across runs even if the server won't report UIDVALIDITY.
        """
        self._require_open()
        self._mb.folder.set(folder)
        with suppress(Exception):  # 1) from the SELECT response
            resp = self._mb.client.untagged_responses.get("UIDVALIDITY")
            if resp:
                v = resp[0]
                return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        with suppress(Exception):  # 2) explicit STATUS
            uv = str(self._mb.folder.status(folder).get("UIDVALIDITY", "") or "")
            if uv:
                return uv
        return "na"  # 3) stable fallback (consistent across runs)

    def message_keys(self) -> list[str]:
        self._require_open()
        return [str(u) for u in self._mb.uids()]

    def fetch_raw(self, key: str) -> bytes | None:
        self._require_open()
        typ, data = self._mb.client.uid("fetch", key, "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return None

    # --- selection filters --------------------------------------------------
    def search(self, *, date_from=None, date_to=None, size_min=None) -> list[str] | None:
        """Server-side UID SEARCH for the selected folder. date_from/date_to are inclusive dates;
        size_min is bytes (>= semantics). Returns None when there is nothing to search on (caller
        then archives everything)."""
        self._require_open()
        if not (date_from or date_to or size_min):
            return None
        import imap_tools
        crit: dict = {}
        if date_from:
            crit["date_gte"] = date_from                       # IMAP SINCE (inclusive)
        if date_to:
            crit["date_lt"] = date_to + timedelta(days=1)      # IMAP BEFORE is exclusive -> +1 day
        if size_min:
            crit["size_gt"] = max(0, int(size_min) - 1)        # IMAP LARGER is strict -> -1 for >=
        return [str(u) for u in self._mb.uids(imap_tools.AND(**crit))]

    def message_meta(self, keys: list[str], progress=None) -> dict:
        """{uid: {'date': datetime|None, 'size': int}} via batched FETCH (UID INTERNALDATE
        RFC822.SIZE) — headers only, no bodies. progress(done, total) fires per chunk."""
        self._require_open()
        out: dict = {}
        total = len(keys)
        if not total:
            return out
        done = 0
        for i in range(0, total, _META_CHUNK):
            chunk = [str(k) for k in keys[i:i + _META_CHUNK]]
            typ, data = self._mb.client.uid("fetch", ",".join(chunk),
                                            "(UID INTERNALDATE RFC822.SIZE)")
            if typ == "OK" and data:
                for item in data:
                    uid, date, size = _parse_fetch_meta(item)
                    if uid is not None:
                        out[uid] = {"date": date, "size": size}
            done += len(chunk)
            if progress:
                progress(done, total)
        return out
