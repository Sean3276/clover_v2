"""Cross-platform-safe names for files/folders derived from mail data."""
from __future__ import annotations

import re

_ASCII_DISALLOWED = re.compile(r"[^A-Za-z0-9 ._-]")
_MULTI_SPACE = re.compile(r"\s+")
_MULTI_DASH = re.compile(r"-{2,}")


def safe_name(s: str, maxlen: int = 120) -> str:
    """Sanitize an arbitrary string (e.g. a Message-ID or folder name) into a safe stem."""
    if not s:
        return "untitled"
    s = _ASCII_DISALLOWED.sub("-", s)
    s = _MULTI_SPACE.sub(" ", s)
    s = _MULTI_DASH.sub("-", s)
    s = s.strip(" -_.")
    if maxlen and len(s) > maxlen:
        s = s[:maxlen].rstrip(" -_.")
    return s or "untitled"
