"""Local Ollama backend: free, private, unlimited (no daily/rate cap). Mirrors the CLI comprehenders'
contract — generate(task, prompt, schema) returns text, or a parsed dict when a schema is given — but
talks HTTP to a local Ollama server and uses format=json so structured passes return valid JSON."""
import json
import pytest

from clover import comprehenders as cm
from clover import models


def _fake_stream(lines):
    class FakeResp:
        def __iter__(self):
            return iter(lines)
        def close(self):
            pass
    return FakeResp()


def test_ollama_is_a_known_backend():
    assert "ollama" in models._BACKENDS


def test_parse_json_repairs_truncated_output():
    # a local model that hits its token cap mid-JSON must still yield the complete items, not a hard error
    out = cm._parse_json('{"facts": ["a", "b", "c')
    assert out["facts"][:2] == ["a", "b"]
    out2 = cm._parse_json('{"domain": "construction", "items": [{"k": 1}, {"k":')
    assert out2["domain"] == "construction" and out2["items"][0]["k"] == 1


def test_ollama_parses_json_counts_tokens_and_constrains_format(monkeypatch):
    lines = [json.dumps({"response": json.dumps({"domain": "construction"}), "done": False}).encode(),
             json.dumps({"done": True, "prompt_eval_count": 1200, "eval_count": 40}).encode()]
    cap = {}

    def fake_urlopen(req, timeout=None):
        cap["body"] = json.loads(req.data.decode("utf-8"))
        cap["url"] = req.full_url
        return _fake_stream(lines)

    monkeypatch.setattr(cm.urllib.request, "urlopen", fake_urlopen)
    c = cm.get_comprehender("ollama", model="qwen3.6:35b-a3b", timeout=60)
    out = c.generate("classify", "Classify this email.", schema={"domain": "str"})
    assert out == {"domain": "construction"}            # parsed dict for a schema call
    assert c.tokens == 1240                             # prompt_eval_count + eval_count
    assert cap["body"]["model"] == "qwen3.6:35b-a3b"
    assert cap["body"]["format"] == "json"              # token-level valid-JSON for structured passes
    assert cap["body"]["think"] is False                # thinking off: a CoT model would return an empty response
    assert cap["body"]["options"]["temperature"] == 0   # extraction, not creativity
    assert "/api/generate" in cap["url"]


def test_ollama_free_text_when_no_schema(monkeypatch):
    lines = [json.dumps({"response": "OK", "done": False}).encode(),
             json.dumps({"done": True, "eval_count": 1}).encode()]
    cap = {}

    def fake_urlopen(req, timeout=None):
        cap["body"] = json.loads(req.data.decode("utf-8"))
        return _fake_stream(lines)

    monkeypatch.setattr(cm.urllib.request, "urlopen", fake_urlopen)
    c = cm.get_comprehender("ollama")
    assert c.generate("smoke", "Say OK") == "OK"        # raw text, no parsing
    assert "format" not in cap["body"]                  # no schema -> no JSON grammar


def test_ollama_down_gives_clear_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cm.urllib.request, "urlopen", boom)
    c = cm.get_comprehender("ollama")
    with pytest.raises(RuntimeError, match="Ollama"):
        c.generate("smoke", "hi")


def test_ollama_honors_stop(monkeypatch):
    # a Stop press aborts an in-flight local call mid-stream (not counted as an error)
    lines = [json.dumps({"response": "partial", "done": False}).encode()] * 5
    monkeypatch.setattr(cm.urllib.request, "urlopen", lambda req, timeout=None: _fake_stream(lines))
    c = cm.get_comprehender("ollama")
    c.should_stop = lambda: True
    with pytest.raises(cm.Stopped):
        c.generate("smoke", "hi")
