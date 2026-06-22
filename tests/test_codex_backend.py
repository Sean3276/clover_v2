"""Job 2: pluggable Codex / GPT-5.5 CLI comprehender. Mirrors the Claude CLI backend but shells out to
`codex exec … --json` with a unique temp output file per call. Local-only, no API key."""
import shutil

import pytest

import clover.comprehenders as cmod
from clover.comprehenders import CodexCliComprehender, get_comprehender


def test_registered_and_configurable():
    b = get_comprehender("codex-cli", model="gpt-5.5", timeout=420)
    assert b.name == "codex-cli" and b.model == "gpt-5.5" and b.timeout == 420
    assert CodexCliComprehender().model == "gpt-5.5"        # sensible default model


def test_codex_model_is_normalized_lowercase():
    # codex rejects 'GPT-5.5' (400: not supported with a ChatGPT account) but accepts 'gpt-5.5'.
    # The /dev registry may store any casing, so the backend must normalize before invoking codex.
    assert CodexCliComprehender(model="GPT-5.5").model == "gpt-5.5"
    assert CodexCliComprehender(model="  GPT-5.5  ").model == "gpt-5.5"


def _fake_exec_to_outfile(payload):
    def _ex(cmd, stdin_text, timeout, should_stop=None):    # mock the _exec seam (Popen+tree-kill supervisor)
        out = cmd[cmd.index("-o") + 1]                      # the unique temp sink
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload)
        return 0, '{"type":"item","usage":{"input_tokens":12,"output_tokens":8}}\n', ""
    return _ex


def test_generate_reads_last_message_file_and_accrues_tokens(monkeypatch):
    b = CodexCliComprehender(timeout=30)
    monkeypatch.setattr(cmod, "_find_codex", lambda: "codex")
    monkeypatch.setattr(cmod, "_exec", _fake_exec_to_outfile("The comprehension result."))
    assert b.generate("comprehend", "prompt") == "The comprehension result."
    assert b.tokens == 20                                   # 12 + 8 from the JSONL usage event


def test_generate_parses_json_with_schema(monkeypatch):
    b = CodexCliComprehender()
    monkeypatch.setattr(cmod, "_find_codex", lambda: "codex")
    monkeypatch.setattr(cmod, "_exec", _fake_exec_to_outfile('{"abstract": "x", "summary": "y"}'))
    out = b.generate("distill_summary", "prompt", schema={"abstract": "str"})
    assert out == {"abstract": "x", "summary": "y"}


def test_timeout_is_friendly(monkeypatch):
    b = CodexCliComprehender(timeout=5)
    monkeypatch.setattr(cmod, "_find_codex", lambda: "codex")
    def _boom(*a, **k):
        raise TimeoutError()
    monkeypatch.setattr(cmod, "_exec", _boom)
    with pytest.raises(RuntimeError) as ei:
        b.generate("comprehend", "p")
    assert "timed out" in str(ei.value).lower() and not isinstance(ei.value, TimeoutError)


def test_missing_cli_is_friendly(monkeypatch):
    b = CodexCliComprehender()
    monkeypatch.setattr(cmod, "_find_codex", lambda: None)
    with pytest.raises(RuntimeError) as ei:
        b.generate("comprehend", "p")
    assert "codex" in str(ei.value).lower()


def test_codex_selectable_in_dev_registry():
    from clover import models as m
    assert "codex-cli" in m._BACKENDS                       # offered as a backend in /dev
