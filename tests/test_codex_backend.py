"""Job 2: pluggable Codex / GPT-5.5 CLI comprehender. Mirrors the Claude CLI backend but shells out to
`codex exec … --json` with a unique temp output file per call. Local-only, no API key."""
import shutil
import subprocess

import pytest

from clover.comprehenders import CodexCliComprehender, get_comprehender


def test_registered_and_configurable():
    b = get_comprehender("codex-cli", model="gpt-5.5", timeout=420)
    assert b.name == "codex-cli" and b.model == "gpt-5.5" and b.timeout == 420
    assert CodexCliComprehender().model == "gpt-5.5"        # sensible default model


def _fake_run_to_outfile(payload):
    def _run(cmd, input=None, **k):
        out = cmd[cmd.index("-o") + 1]                      # the unique temp sink
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload)
        class P:
            returncode = 0
            stdout = '{"type":"item","usage":{"input_tokens":12,"output_tokens":8}}\n'
            stderr = ""
        return P()
    return _run


def test_generate_reads_last_message_file_and_accrues_tokens(monkeypatch):
    b = CodexCliComprehender(timeout=30)
    monkeypatch.setattr(shutil, "which", lambda _n: "codex")
    monkeypatch.setattr(subprocess, "run", _fake_run_to_outfile("The comprehension result."))
    assert b.generate("comprehend", "prompt") == "The comprehension result."
    assert b.tokens == 20                                   # 12 + 8 from the JSONL usage event


def test_generate_parses_json_with_schema(monkeypatch):
    b = CodexCliComprehender()
    monkeypatch.setattr(shutil, "which", lambda _n: "codex")
    monkeypatch.setattr(subprocess, "run", _fake_run_to_outfile('{"abstract": "x", "summary": "y"}'))
    out = b.generate("distill_summary", "prompt", schema={"abstract": "str"})
    assert out == {"abstract": "x", "summary": "y"}


def test_timeout_is_friendly(monkeypatch):
    b = CodexCliComprehender(timeout=5)
    monkeypatch.setattr(shutil, "which", lambda _n: "codex")
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=5)
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        b.generate("comprehend", "p")
    assert "timed out" in str(ei.value).lower() and not isinstance(ei.value, subprocess.TimeoutExpired)


def test_missing_cli_is_friendly(monkeypatch):
    b = CodexCliComprehender()
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError) as ei:
        b.generate("comprehend", "p")
    assert "codex" in str(ei.value).lower()


def test_codex_selectable_in_dev_registry():
    from clover import models as m
    assert "codex-cli" in m._BACKENDS                       # offered as a backend in /dev
