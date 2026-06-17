"""Path-portable runtime layout.

Runtime state (config, index pointer, logs) lives under <auto_clover>/.clover_v2
(override with $CLOVER_V2_HOME). The .eml archive itself goes to a user-defined
archive_path (default below), which can be anywhere and changed later.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CLOVER_V2_HOME"


def template_dir() -> Path:
    # clover/paths.py -> parents[1] = .clover_v2_github
    return Path(__file__).resolve().parents[1]


def auto_clover_root() -> Path:
    # parents[2] = auto_clover
    return Path(__file__).resolve().parents[2]


def runtime_dir() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    return auto_clover_root() / ".clover_v2"


def config_path() -> Path:
    return runtime_dir() / "clover_config.json"


def logs_dir() -> Path:
    return runtime_dir() / "logs"


def default_archive_path() -> Path:
    return runtime_dir() / "eml_archive"


def ensure_runtime() -> Path:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    return runtime_dir()
