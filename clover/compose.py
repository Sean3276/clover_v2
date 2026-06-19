"""Pure reply / reply-all / forward message builder (no network, no I/O beyond reading the source .eml).

Given an original `.eml`, an action, and the user's text, build a correct RFC822 EmailMessage:
recipients per action, Re:/Fwd: subject, In-Reply-To/References threading headers, a quoted original,
and (for forward) the original's attachments carried along. Deterministic except for Message-ID/Date,
which the caller/SMTP layer can stamp — tests don't assert on those.
"""
from __future__ import annotations

import email
import re
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+\n")


def _load(eml_path) -> EmailMessage:
    with Path(eml_path).open("rb") as fh:
        return email.message_from_binary_file(fh, policy=policy.default)


def _addrs(value: str) -> list[tuple[str, str]]:
    return [(n, a) for n, a in getaddresses([value or ""]) if a]


def recipients(original: EmailMessage, action: str, me: str = "") -> tuple[list[str], list[str]]:
    """(to, cc) address lists for the action. `me` is excluded from reply-all. Forward -> empty."""
    me_l = (me or "").strip().lower()
    frm = _addrs(str(original.get("From", "")))
    reply_to = _addrs(str(original.get("Reply-To", ""))) or frm
    if action == "reply":
        return [a for _, a in reply_to], []
    if action == "reply-all":
        to = [a for _, a in reply_to]
        seen = {a.lower() for a in to} | ({me_l} if me_l else set())
        cc = []
        for _, a in _addrs(str(original.get("To", ""))) + _addrs(str(original.get("Cc", ""))):
            if a.lower() not in seen:
                seen.add(a.lower())
                cc.append(a)
        return to, cc
    return [], []        # forward


def subject(original: EmailMessage, action: str) -> str:
    s = (str(original.get("Subject", "")) or "").strip()
    low = s.lower()
    if action == "forward":
        return s if low.startswith(("fwd:", "fw:")) else f"Fwd: {s}"
    return s if low.startswith("re:") else f"Re: {s}"


def _strip_html(html: str) -> str:
    text = _TAG.sub("", html or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return _WS.sub("\n", text).strip()


def _body_plain(msg: EmailMessage) -> str:
    try:
        p = msg.get_body(preferencelist=("plain",))
        if p is not None:
            return p.get_content()
        h = msg.get_body(preferencelist=("html",))
        if h is not None:
            return _strip_html(h.get_content())
    except Exception:
        pass
    return ""


def _quoted(original: EmailMessage, action: str) -> str:
    body = _body_plain(original)
    frm = str(original.get("From", ""))
    date = str(original.get("Date", ""))
    if action == "forward":
        head = ("---------- Forwarded message ----------\n"
                f"From: {frm}\nDate: {date}\n"
                f"Subject: {original.get('Subject', '')}\nTo: {original.get('To', '')}\n")
        return head + "\n" + body
    attribution = f"On {date}, {frm} wrote:" if date or frm else "Previously:"
    return attribution + "\n" + "\n".join("> " + ln for ln in body.splitlines())


def _attachments(original: EmailMessage):
    """Real file attachments of the original (skip inline/cid images — signature logos etc.)."""
    out = []
    for part in original.iter_attachments():
        disp = (part.get_content_disposition() or "").lower()
        if disp == "inline" and part.get_content_maintype() == "image":
            continue
        if part.get("Content-ID") and part.get_content_maintype() == "image" and disp != "attachment":
            continue
        if part.get_filename():
            out.append(part)
    return out


def build_message(eml_path, action: str, *, body_text: str, from_addr: str,
                  to: list[str] | None = None, cc: list[str] | None = None, me: str = "") -> EmailMessage:
    """Build the reply/reply-all/forward EmailMessage. `to`/`cc` override the auto-derived recipients
    (used by the UI after the user edits them); for forward they must be supplied."""
    if action not in ("reply", "reply-all", "forward"):
        raise ValueError(f"unknown action: {action}")
    original = _load(eml_path)
    auto_to, auto_cc = recipients(original, action, me or from_addr)
    to_list = list(to) if to is not None else auto_to
    cc_list = list(cc) if cc is not None else auto_cc

    msg = EmailMessage()
    msg["From"] = from_addr
    if to_list:
        msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject(original, action)
    oid = original.get("Message-ID")
    if action in ("reply", "reply-all") and oid:
        msg["In-Reply-To"] = str(oid)
        prior = str(original.get("References", "") or "").strip()
        msg["References"] = (prior + " " + str(oid)).strip()

    msg.set_content((body_text or "").rstrip() + "\n\n" + _quoted(original, action))

    if action == "forward":
        for part in _attachments(original):
            data = part.get_payload(decode=True) or b""
            ctype = part.get_content_type()
            maintype, _, subtype = ctype.partition("/")
            msg.add_attachment(data, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=part.get_filename())
    return msg
