"""IMAP implementation of MailSource (the reviewed Phase-1 ingestion logic)."""
from __future__ import annotations

from contextlib import suppress

from .base import MailSource

_IMAP_TIMEOUT = 30  # seconds — bound hangs on connect/login/status


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
            return False, f"{type(e).__name__}: {e}"

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
