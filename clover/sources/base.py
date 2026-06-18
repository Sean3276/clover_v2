"""MailSource — the interface every ingestion provider implements.

The archiver iterates: for each folder -> select(folder) -> message_keys() -> fetch_raw(key).
A 'key' is a stable per-message id within a folder (IMAP uid; provider message-id elsewhere);
'validity' is a per-folder token that changes when keys are no longer stable (IMAP UIDVALIDITY;
'' where keys are globally stable). The archiver dedupes on (folder, validity, key).

Connection lifecycle is managed via the context manager (open/close). test() is standalone.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class MailSource(ABC):
    kind: str = "base"

    def __init__(self, conn: dict, password: str):
        self.conn = conn or {}
        self.password = password

    # context-managed connection (folders/select/keys/fetch require an open connection)
    def __enter__(self) -> "MailSource":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:  # pragma: no cover - overridden where needed
        pass

    def close(self) -> None:  # pragma: no cover - overridden where needed
        pass

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Standalone connectivity/auth check (manages its own connection)."""

    @abstractmethod
    def folders(self) -> list[dict]:
        """[{name, messages|None}] for the open connection."""

    @abstractmethod
    def select(self, folder: str) -> str:
        """Select a folder; return its validity token (may be '')."""

    @abstractmethod
    def message_keys(self) -> list[str]:
        """Stable per-message keys in the currently-selected folder."""

    @abstractmethod
    def fetch_raw(self, key: str) -> bytes | None:
        """Raw RFC822 bytes for a key in the selected folder, or None."""

    # --- connection recovery (overridable) ----------------------------------
    def force_close(self) -> None:
        """Forcibly drop the connection to unblock an in-flight read. Default: close()."""
        self.close()

    def reconnect(self, folder: str | None = None) -> None:
        """Re-establish the connection after a stuck/failed fetch; re-select a folder."""
        try:
            self.close()
        except Exception:
            pass
        self.open()
        if folder:
            self.select(folder)

    # --- optional selection filters (overridable; base = no server-side support) -------------
    def search(self, *, date_from=None, date_to=None, size_min=None) -> list[str] | None:
        """Server-side filtered keys for the SELECTED folder, or None if unsupported — in which
        case the caller falls back to message_keys() + message_meta() client-side filtering.
        date_from/date_to are datetime.date (inclusive); size_min is bytes."""
        return None

    def message_meta(self, keys: list[str], progress=None) -> dict:
        """{key: {'date': datetime|None, 'size': int}} for the SELECTED folder — metadata only,
        no message bodies. Used for client-side date/size filtering and top-N sizing.
        progress(done, total) is called as batches complete (drives the prep % bar). The caller
        may raise from progress() to cancel mid-scan, so implementations must call it between
        batches and let that exception propagate (do not swallow it)."""
        raise NotImplementedError(f"{self.kind} source does not support metadata fetch")
