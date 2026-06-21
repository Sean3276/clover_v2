"""Config (non-secret JSON) + IMAP password (OS keyring). Secrets never touch JSON or logs."""
from __future__ import annotations

import json

from .paths import config_path, default_archive_path, ensure_runtime

KEYRING_SERVICE = "clover_v2"
SECRET_IMAP_PASSWORD = "imap_password"
SECRET_SMTP_PASSWORD = "smtp_password"


def default_config() -> dict:
    return {
        "auth": {"imap": {"host": "", "port": 993, "security": "ssl", "user": ""}},
        "archive_path": str(default_archive_path()),
        "folders": ["INBOX", "Sent"],
        "comprehension": {
            "backend": "claude-cli", "model": "sonnet", "profile": "construction",
            "budget_tokens": 200000, "autorun": True, "timeout_seconds": 300,
        },
        # Delivery track — OFF by default; sending is impossible until enabled (see CLOVER_V2_SENDING_SPEC).
        "sending": {
            "enabled": False,
            "smtp": {"host": "", "port": 587, "security": "starttls"},
            "from": "",
            "save_to_sent": True,
            "sent_folder": "Sent",
        },
    }


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = _merge(default_config(), loaded)   # never miss auth/imap keys
                if not cfg.get("archive_path"):
                    cfg["archive_path"] = str(default_archive_path())
                return cfg
        except Exception:
            pass
    return default_config()


def save_config(cfg: dict) -> None:
    ensure_runtime()
    config_path().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --- IMAP password via OS keyring -------------------------------------------
def set_secret(key: str, value: str) -> None:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, key, value)
    except Exception as e:  # no usable backend (e.g. headless) — surface clearly
        raise RuntimeError(f"Could not store secret in OS keyring: {e}") from e


def get_secret(key: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, key)
    except Exception:
        return None


def has_secret(key: str) -> bool:
    return bool(get_secret(key))
