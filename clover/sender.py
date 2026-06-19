"""Pluggable mail senders (the delivery track). SMTP first; a Stub for tests/dry-run.

Mirrors clover.sources: the send route talks only to MailSender, so other transports plug in later.
SENDING IS GATED OFF by default at the app layer — this module just performs the transport.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def _stamp(msg: EmailMessage, from_addr: str) -> None:
    if "From" not in msg and from_addr:
        msg["From"] = from_addr
    if "Date" not in msg:
        msg["Date"] = formatdate(localtime=True)
    if "Message-ID" not in msg:
        msg["Message-ID"] = make_msgid()


class MailSender(ABC):
    def __init__(self, smtp: dict, username: str, password: str, from_addr: str):
        self.smtp = smtp or {}
        self.username = username
        self.password = password
        self.from_addr = from_addr

    @abstractmethod
    def send(self, msg: EmailMessage) -> str:
        """Send `msg`; return its Message-ID. Stamps From/Date/Message-ID if missing."""


class SmtpSender(MailSender):
    def send(self, msg: EmailMessage) -> str:
        import smtplib
        import ssl
        host = self.smtp.get("host")
        if not host:
            raise ValueError("SMTP host not configured")
        port = int(self.smtp.get("port") or 587)
        security = (self.smtp.get("security") or "starttls").lower()
        _stamp(msg, self.from_addr)
        ctx = ssl.create_default_context()
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                if security == "starttls":
                    s.starttls(context=ctx)
                    s.ehlo()
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(msg)
        return str(msg["Message-ID"])


class StubSender(MailSender):
    """Records the last message without touching the network (tests / dry-run)."""
    last: EmailMessage | None = None

    def send(self, msg: EmailMessage) -> str:
        _stamp(msg, self.from_addr)
        self.last = msg
        return str(msg["Message-ID"])


_REGISTRY: dict[str, type[MailSender]] = {"smtp": SmtpSender, "stub": StubSender}


def get_sender(kind: str, smtp: dict, username: str, password: str, from_addr: str) -> MailSender:
    impl = _REGISTRY.get((kind or "smtp").lower(), SmtpSender)
    return impl(smtp, username, password, from_addr)
