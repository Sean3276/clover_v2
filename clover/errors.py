"""Human-readable descriptions for connection/auth failures shown in the UI.

Turns a low-level exception (gaierror, timeout, SSL, auth, …) into a one-liner a non-technical
user can act on, with a short bracketed code kept for support/debugging. Provider-agnostic.
"""
from __future__ import annotations

import errno as _errno
import socket
import ssl


def friendly_conn_error(exc: Exception) -> str:
    s = str(exc).lower()
    en = getattr(exc, "errno", None)

    # name resolution failed -> almost always no internet (or a wrong/unknown server address)
    if isinstance(exc, socket.gaierror) or "getaddrinfo" in s \
            or "name or service not known" in s or "nodename nor servname" in s:
        return f"No internet connection — couldn't reach the mail server [NET-{en or 'DNS'}]"

    # network down / unreachable (cable out, Wi-Fi off, VPN dropped)
    if isinstance(exc, OSError) and en in (_errno.ENETUNREACH, _errno.ENETDOWN, 10050, 10051, 10065):
        return f"No internet connection — the network is unreachable [NET-{en}]"

    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in s:
        return "Couldn't reach the mail server in time — check your connection [TIMEOUT]"

    if isinstance(exc, ConnectionRefusedError) or "refused" in s:
        return "The mail server refused the connection — check the host and port on Setup [REFUSED]"

    if isinstance(exc, ConnectionResetError) or "reset by peer" in s or "forcibly closed" in s or "aborted" in s:
        return "The connection dropped — usually a brief network blip, try again [DROPPED]"

    if isinstance(exc, ssl.SSLError) or "ssl" in s or "certificate" in s:
        return "Secure (SSL) connection failed — check the security setting on Setup [SSL]"

    # auth failures only happen after a successful connect, so check these last
    if any(w in s for w in ("authentication", "login", "credential", "password", "invalid user")):
        return "Login failed — check your email address and password on Setup [AUTH]"

    return f"Couldn't reach the mail server [{type(exc).__name__}]"
