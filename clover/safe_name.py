"""Cross-platform-safe names for files/folders derived from mail data."""
from __future__ import annotations

import re
import unicodedata

_ASCII_DISALLOWED = re.compile(r"[^A-Za-z0-9 ._-]")
_MULTI_SPACE = re.compile(r"\s+")
_MULTI_DASH = re.compile(r"-{2,}")
# For real filenames: only path separators, Windows-reserved chars, and control chars are unsafe.
_FN_DISALLOWED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, maxlen: int = 120) -> str:
    """Sanitize an arbitrary string (e.g. a Message-ID or folder name) into a safe ASCII stem."""
    if not s:
        return "untitled"
    s = _ASCII_DISALLOWED.sub("-", s)
    s = _MULTI_SPACE.sub(" ", s)
    s = _MULTI_DASH.sub("-", s)
    s = s.strip(" -_.")
    if maxlen and len(s) > maxlen:
        s = s[:maxlen].rstrip(" -_.")
    return s or "untitled"


def safe_filename(name: str, maxlen: int = 120, fallback: str = "download") -> str:
    """Sanitize a real filename for cross-platform saving while PRESERVING Unicode (CJK etc.)
    and the file extension. Unlike safe_name (ASCII-only, for ids/folders), this only removes
    path separators, Windows-reserved chars and control chars — so '报告.pdf' stays '报告.pdf'."""
    if not name:
        return fallback
    name = unicodedata.normalize("NFC", name).strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:                                  # no extension
        stem, ext = name, ""
    stem = _MULTI_SPACE.sub(" ", _FN_DISALLOWED.sub("-", stem))
    stem = _MULTI_DASH.sub("-", stem).strip(" .-_")
    ext = _FN_DISALLOWED.sub("", ext).strip(" .")
    if len(ext) > 16:                            # not a plausible extension — treat as part of name
        stem, ext = (stem + "." + ext).strip(" .-_"), ""
    room = max(1, maxlen - (len(ext) + 1 if ext else 0))
    if len(stem) > room:
        stem = stem[:room].rstrip(" .-_")
    if not stem:
        stem = fallback
    return f"{stem}.{ext}" if ext else stem
