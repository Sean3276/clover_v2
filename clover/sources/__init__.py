"""Pluggable mail sources. Add a provider by implementing MailSource and registering it.

The archiver (clover/archive.py) talks ONLY to the MailSource interface, so new providers
plug in without changing the archiver.
"""
from __future__ import annotations

from .base import MailSource
from .imap_source import ImapSource

# registry of implemented sources
_REGISTRY: dict[str, type[MailSource]] = {
    "imap": ImapSource,
}

# the full ingestion suite (develop one by one). "available" = implemented now.
SUITE = [
    {"id": "imap", "label": "IMAP (generic)", "protocol": "IMAP", "status": "available"},
    {"id": "gmail", "label": "Gmail / Google Workspace", "protocol": "Gmail API or IMAP+OAuth2", "status": "planned"},
    {"id": "m365", "label": "Microsoft 365 / Outlook.com", "protocol": "Microsoft Graph API", "status": "planned"},
    {"id": "exchange", "label": "Exchange (on-prem)", "protocol": "EWS", "status": "planned"},
    {"id": "yahoo", "label": "Yahoo Mail", "protocol": "IMAP (app password)", "status": "planned"},
    {"id": "icloud", "label": "iCloud Mail", "protocol": "IMAP (app-specific password)", "status": "planned"},
    {"id": "coremail", "label": "Coremail (NetEase 163/126, QQ, etc.)", "protocol": "IMAP / Coremail API", "status": "planned"},
    {"id": "zoho", "label": "Zoho Mail", "protocol": "IMAP / API", "status": "planned"},
    {"id": "fastmail", "label": "Fastmail", "protocol": "JMAP / IMAP", "status": "planned"},
    {"id": "proton", "label": "Proton Mail", "protocol": "IMAP via Proton Bridge", "status": "planned"},
    {"id": "pop3", "label": "Generic POP3", "protocol": "POP3", "status": "planned"},
    {"id": "import", "label": "Local import", "protocol": ".eml / .mbox / Maildir / .pst", "status": "planned"},
]


def get_source(kind: str, conn: dict, password: str) -> MailSource:
    kind = (kind or "imap").lower()
    impl = _REGISTRY.get(kind)
    if impl is None:
        raise NotImplementedError(
            f"source '{kind}' is not implemented yet (planned in the ingestion suite)"
        )
    return impl(conn, password)
