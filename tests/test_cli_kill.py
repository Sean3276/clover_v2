"""P1: a Stop press (or a timeout) must KILL the AI subprocess tree promptly — not wait for a wedged CLI.
Verified with real child processes: a 30s sleep must be killed within seconds, not run to completion."""
import sys
import threading
import time

import pytest

from clover.comprehenders import Stopped, _exec

PY = sys.executable


def test_exec_returns_output_and_feeds_stdin():
    rc, out, err = _exec([PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"], "abc", timeout=10)
    assert rc == 0 and out.strip() == "ABC"


def test_exec_timeout_kills_promptly():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        _exec([PY, "-c", "import time; time.sleep(30)"], "", timeout=1)
    assert time.monotonic() - start < 8          # killed ~1s, NOT after the 30s sleep


def test_exec_pre_checks_stop_before_spawning():
    # already-stopping: abort instantly at the NEXT call, without launching a process (bounds stop latency
    # when calls fail fast and a thread is mid-way through its passes)
    start = time.monotonic()
    with pytest.raises(Stopped):
        _exec([PY, "-c", "import time; time.sleep(30)"], "", timeout=60, should_stop=lambda: True)
    assert time.monotonic() - start < 2


def test_exec_stop_aborts_promptly():
    flag = {"stop": False}
    threading.Timer(0.5, lambda: flag.__setitem__("stop", True)).start()
    start = time.monotonic()
    with pytest.raises(Stopped):
        _exec([PY, "-c", "import time; time.sleep(30)"], "", timeout=60, should_stop=lambda: flag["stop"])
    assert time.monotonic() - start < 8          # aborted on Stop, NOT after the 30s sleep


def test_claude_disables_tools(monkeypatch):
    """Comprehension must run claude as a PURE LLM (--tools "") so untrusted email content can't drive the
    agent into tool use (security) or multi-turn agentic hangs (the comprehension-stall bug)."""
    import shutil
    import clover.comprehenders as cmod
    captured = {}
    monkeypatch.setattr(shutil, "which", lambda _n: "claude")
    monkeypatch.setattr(cmod, "_exec", lambda cmd, stdin, timeout, should_stop=None: (captured.__setitem__("cmd", cmd), (0, '{"result":"ok"}', ""))[1])
    cmod.ClaudeCliComprehender().generate("comprehend", "hi")
    cmd = captured["cmd"]
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""   # all tools disabled


def test_run_comprehension_wires_should_stop_into_backend(tmp_path):
    import clover.comprehend as cp
    from clover.comprehenders import StubComprehender
    b = StubComprehender()
    cp.run_comprehension(tmp_path, backend=b, should_stop=lambda: False)   # no threads, just the wiring
    assert callable(b.should_stop)                                          # backend got the stop hook
