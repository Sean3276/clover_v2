"""Comprehender — the pluggable AI backend the Phase-3 pipeline calls.

Mirrors the MailSource pattern: one interface, swappable implementations. StubComprehender is
deterministic (tests / offline build); ClaudeCliComprehender shells out to the local Claude Code
CLI (your subscription, no API key). Ollama / hosted / API can be added later without touching
the pipeline.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

_REGISTRY: dict = {}


class Stopped(Exception):
    """Raised when an AI call is aborted because the operator pressed Stop (not an error)."""


def _kill_tree(pid: int) -> None:
    """Kill a process AND all its children — so a hung CLI (which spawns its own workers) never orphans.
    Windows: taskkill /T /F (whole tree). POSIX: process-group kill, falling back to a plain kill."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _exec(cmd: list[str], stdin_text: str, timeout: int, should_stop=None):
    """Run `cmd`, feeding stdin_text, returning (returncode, stdout, stderr). Unlike subprocess.run, this
    enforces the timeout AND a live stop signal by killing the whole PROCESS TREE — so a wedged CLI is
    actually terminated (no zombie claude.exe) and a Stop press aborts in-flight calls promptly.
    Raises TimeoutError on timeout, Stopped when should_stop() turns true."""
    if should_stop and should_stop():        # already stopping — abort BEFORE spawning, so a thread mid-way
        raise Stopped()                      # through its passes halts at the very next call (bounds stop latency)
    kw = {}
    if sys.platform != "win32":
        kw["start_new_session"] = True                 # own process group, so _kill_tree gets the children
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", **kw)
    res: dict = {}

    def _communicate():
        try:
            res["out"], res["err"] = proc.communicate(input=stdin_text)
        except Exception as e:                          # pragma: no cover - defensive
            res["exc"] = e

    worker = threading.Thread(target=_communicate, daemon=True)
    worker.start()
    start = time.monotonic()
    while True:
        worker.join(0.4)
        if not worker.is_alive():
            break
        if should_stop and should_stop():
            _kill_tree(proc.pid); worker.join(5)
            raise Stopped()
        if time.monotonic() - start > timeout:
            _kill_tree(proc.pid); worker.join(5)
            raise TimeoutError()
    if "exc" in res:
        raise res["exc"]
    return proc.returncode, res.get("out", "") or "", res.get("err", "") or ""


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def get_comprehender(name: str, **kwargs) -> "Comprehender":
    if name not in _REGISTRY:
        raise ValueError(f"unknown comprehender '{name}' (have: {', '.join(_REGISTRY)})")
    return _REGISTRY[name](**kwargs)


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction from a model response (handles code fences / surrounding prose)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)        # first {...} block
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    repaired = _repair_truncated_json(t)         # salvage a JSON object a token cap cut off mid-output
    if repaired is not None:
        return repaired
    raise ValueError("model did not return valid JSON")


def _repair_truncated_json(text: str):
    """Best-effort recovery of a JSON object truncated mid-output (a local model hitting its token cap):
    start at the first '{', drop a dangling partial string/comma, then close the still-open [ and { in
    order. Returns the parsed dict, or None if it still won't parse. The completed items survive; only the
    cut-off tail is lost — far better than failing the whole pass (the deterministic floor backfills the rest)."""
    i = text.find("{")
    if i < 0:
        return None
    s = text[i:]
    # walk back from the end to each delimiter; at the first cut that closes into valid JSON, take it
    cuts = sorted({len(s)} | {m.end() for m in re.finditer(r'[,}\]"]', s)}, reverse=True)
    for end in cuts:
        cand = s[:end].rstrip().rstrip(",:").rstrip()
        if not cand:
            continue
        stack, instr, esc = [], False, False
        for ch in cand:                          # string-aware bracket scan
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]" and stack:
                stack.pop()
        if instr:                                # this cut lands inside a string — try an earlier delimiter
            continue
        closed = cand + "".join("}" if c == "{" else "]" for c in reversed(stack))
        try:
            return json.loads(closed)
        except Exception:
            continue
    return None


def _sum_token_fields(obj) -> int:
    """Sum every int/float under a key containing 'token' (recursively) — backend-agnostic usage scrape."""
    t = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and "token" in k.lower():
                t += int(v)
            else:
                t += _sum_token_fields(v)
    elif isinstance(obj, list):
        for v in obj:
            t += _sum_token_fields(v)
    return t


def _codex_tokens(stdout: str) -> int:
    """Best-effort token total from codex `--json` JSONL events. Takes the MAX single-event sum (codex
    reports cumulative usage), so it never double-counts. 0 when no usage is reported."""
    best = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            best = max(best, _sum_token_fields(json.loads(line)))
        except Exception:
            continue
    return best


def _codex_text_from_stream(stdout: str) -> str:
    """Fallback when the -o sink is empty: pull the last assistant message text out of the JSONL stream."""
    texts = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        for key in ("last_agent_message", "message", "text", "content", "response"):
            v = ev.get(key)
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())
    return texts[-1] if texts else (stdout or "").strip()


class Comprehender(ABC):
    name = "base"

    @abstractmethod
    def generate(self, task: str, prompt: str, schema: dict | None = None):
        """Run one AI step. Returns text, or a parsed dict when `schema` is given.
        `task` is a short tag (comprehend/distill/classify/…) for routing/telemetry."""


@register
class StubComprehender(Comprehender):
    """Deterministic backend for tests and offline builds. Optional `responses` maps a task to a
    value (or a callable(prompt)->value) so tests can drive specific pipeline behaviour."""

    name = "stub"

    def __init__(self, responses: dict | None = None, model: str = "stub"):
        self.responses = responses or {}
        self.calls: list = []
        self.model = model
        self.tokens = 0
        self.cost = 0.0
        self._lock = threading.Lock()                    # token/call accrual is concurrency-safe

    def generate(self, task: str, prompt: str, schema: dict | None = None):
        with self._lock:
            self.calls.append(task)
            self.tokens += max(1, len(prompt) // 4)       # rough, so usage accrual is testable
        if task in self.responses:
            r = self.responses[task]
            return r(prompt) if callable(r) else r
        if task in ("comprehend", "comprehend_refine"):
            return "Stub comprehension of the thread."
        if task == "distill_facts":
            return {"project": [], "facts": [], "contacts": []}
        if task == "distill_summary":
            return {"abstract": "Stub abstract.", "summary": "Stub one-liner.", "event": "stub event",
                    "tags": ["Discipline: M&E", "Artifact: RFI", "Made Up: Nonsense"]}
        if task in ("classify", "classify_full"):
            return {"domain": "Project", "category": "Commercial", "confidence": 0.9,
                    "dispute": False, "dissent": ""}
        if task == "qa":
            return {"passed": True, "faithfulness": 1.0, "completeness": 1.0, "issues": []}
        if task == "verify_distill":
            return {"passed": True, "abstract_ok": True, "summary_ok": True, "event_ok": True, "issues": []}
        if task == "actions":
            return {"actions": []}
        return {} if schema else ""


@register
class ClaudeCliComprehender(Comprehender):
    """Local Claude Code CLI (`claude -p … --output-format json`). Uses your Claude subscription —
    no API key. Requires: npm i -g @anthropic-ai/claude-code (then `claude` login once)."""

    name = "claude-cli"

    def __init__(self, model: str = "sonnet", timeout: int = 300):
        self.model = model
        self.timeout = timeout
        self.tokens = 0           # actual tokens consumed (from the CLI usage envelope)
        self.cost = 0.0           # USD, if the CLI reports it
        self.should_stop = None   # set by run_comprehension so a Stop press aborts in-flight calls + kills the tree
        self._lock = threading.Lock()   # token/cost accrual is concurrency-safe

    def generate(self, task: str, prompt: str, schema: dict | None = None):
        import shutil
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Claude CLI not found — install with: npm i -g @anthropic-ai/claude-code")
        p = prompt
        if schema:
            p += ("\n\nReturn ONLY valid JSON (no prose, no code fence) matching this shape:\n"
                  + json.dumps(schema))
        # prompt goes on STDIN (not argv) — thread text easily exceeds the OS command-line limit; force
        # UTF-8 so mixed English/Chinese round-trips. _exec kills the whole tree on timeout/stop (no zombies).
        # `--tools ""` disables ALL tools: this is a pure text->text LLM call, so untrusted EMAIL CONTENT
        # can't hijack the agent into using Bash/Read/WebFetch on the machine (security) and can't send it
        # off on multi-turn agentic tangents that hang the call (the real cause of the comprehension stalls).
        try:
            rc, out, err = _exec([exe, "-p", "--model", self.model, "--output-format", "json", "--tools", ""],
                                 p, self.timeout, self.should_stop)
        except TimeoutError:
            raise RuntimeError(
                f"AI '{task}' step timed out after {self.timeout}s — the thread may be large or the model "
                f"slow. Try again, or in /dev pick a faster model or raise the AI timeout."
            ) from None
        if rc != 0:
            raise RuntimeError(f"claude CLI failed ({rc}): {err.strip()[:200]}")
        try:
            env = json.loads(out)                # CLI envelope: {"result": "...", "usage": {...}, ...}
            text = env.get("result", out)
            u = env.get("usage") or {}
            with self._lock:
                self.tokens += sum(int(v) for k, v in u.items()
                                   if isinstance(v, (int, float)) and "token" in k.lower())
                self.cost += float(env.get("total_cost_usd") or 0)
        except Exception:
            text = out
        return _parse_json(text) if schema else text


def _find_codex():
    """Resolve the codex binary, preferring the UP-TO-DATE VS Code/Cursor ChatGPT-extension build over the
    often-stale `~/.codex/.sandbox-bin` (that old build fails on gpt-5.5 with a bare 'exit 1'). Order:
    newest extension binary -> PATH -> sandbox-bin fallback."""
    import glob
    import shutil
    home = os.path.expanduser("~")
    cands = []
    for editor in (".vscode", ".vscode-server", ".vscode-insiders", ".cursor", ".windsurf"):
        cands += glob.glob(os.path.join(home, editor, "extensions", "openai.chatgpt-*", "bin", "*", "codex*"))
    cands = [c for c in cands if os.path.basename(c).lower() in ("codex", "codex.exe") and os.path.isfile(c)]

    def _ok_platform(c):                            # the extension ships ALL platforms in bin/<plat>/ — pick ours
        sub = os.path.basename(os.path.dirname(c)).lower()
        if sys.platform == "win32":
            return "win" in sub and c.lower().endswith(".exe")
        if sys.platform == "darwin":
            return any(k in sub for k in ("apple", "darwin", "mac"))
        return "linux" in sub
    cands = [c for c in cands if _ok_platform(c)]
    if cands:
        cands.sort(key=os.path.getmtime)            # newest install wins (robust vs version-string sorting)
        return cands[-1]
    onpath = shutil.which("codex")
    if onpath:
        return onpath
    fb = os.path.join(home, ".codex", ".sandbox-bin", "codex.exe" if sys.platform == "win32" else "codex")
    return fb if os.path.isfile(fb) else None


@register
class CodexCliComprehender(Comprehender):
    """Local Codex CLI (`codex exec … --json`). Plugs GPT-5.x via the user's Codex/ChatGPT auth — no API
    key. Each call uses a UNIQUE temp output file (-o) so concurrent comprehensions never collide on it.
    Local-only, like the Claude backend; swappable from the /dev model registry."""

    name = "codex-cli"

    def __init__(self, model: str = "gpt-5.5", timeout: int = 300):
        # codex model ids are lowercase; it 400s on 'GPT-5.5' ("not supported with a ChatGPT account").
        # The /dev registry can hold any casing, so normalize here.
        self.model = (model or "gpt-5.5").strip().lower()
        self.timeout = timeout
        self.tokens = 0
        self.cost = 0.0
        self.should_stop = None   # set by run_comprehension so a Stop press aborts in-flight calls + kills the tree
        self._exe = None          # resolved codex binary (cached)
        self._lock = threading.Lock()

    def generate(self, task: str, prompt: str, schema: dict | None = None):
        import tempfile
        if not self._exe:
            self._exe = _find_codex()
        exe = self._exe
        if not exe:
            raise RuntimeError("Codex CLI not found — install the OpenAI Codex/ChatGPT CLI and sign in.")
        p = prompt
        if schema:
            p += ("\n\nReturn ONLY valid JSON (no prose, no code fence) matching this shape:\n"
                  + json.dumps(schema))
        fd, outpath = tempfile.mkstemp(prefix="clover_codex_", suffix=".txt")   # unique per concurrent call
        os.close(fd)
        # read-only sandbox + ephemeral session; low reasoning effort (cheaper/faster for transcription work);
        # prompt on STDIN (the trailing '-'); the final message lands in `outpath`.
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
               "-m", self.model, "-c", "model_reasoning_effort=low", "-o", outpath, "--json", "-"]
        try:
            try:
                rc, out, err = _exec(cmd, p, self.timeout, self.should_stop)   # kills the tree on timeout/stop
            except TimeoutError:
                raise RuntimeError(
                    f"AI '{task}' step timed out after {self.timeout}s — the thread may be large or the "
                    f"model slow. Try again, or in /dev pick a faster model or raise the AI timeout."
                ) from None
            if rc != 0:
                detail = (err.strip() or out.strip() or "(no error output)")[:200]
                raise RuntimeError(f"codex CLI failed ({rc}) using {os.path.basename(exe)}: {detail}")
            try:
                with open(outpath, encoding="utf-8") as fh:
                    text = fh.read().strip()
            except OSError:
                text = ""
            if not text:                              # the -o sink was empty — recover from the JSONL stream
                text = _codex_text_from_stream(out)
            with self._lock:
                self.tokens += _codex_tokens(out)
        finally:
            try:
                os.remove(outpath)
            except OSError:
                pass
        return _parse_json(text) if schema else text


@register
class OllamaComprehender(Comprehender):
    """Local Ollama model over HTTP (default http://localhost:11434) — FREE, PRIVATE (email never leaves
    the machine) and UNLIMITED (no daily/rate cap, so a backlog just grinds in the background). Structured
    passes use Ollama's format=json for guaranteed-valid JSON — a big reliability win for smaller local
    models. Pairs best with an MoE model such as qwen3.6:35b-a3b: ~35B-class quality at ~3B-active speed,
    which suits a big-RAM / small-GPU machine. Swappable from the /dev model registry like the CLI backends."""

    name = "ollama"

    def __init__(self, model: str = "qwen3.6:35b-a3b", timeout: int = 300,
                 host: str | None = None, num_ctx: int = 16384, num_predict: int = 3072):
        self.model = model
        self.timeout = timeout
        host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").strip().rstrip("/")
        self.host = host if host.startswith("http") else "http://" + host
        self.num_ctx = int(num_ctx)           # kept in the fast envelope — big contexts crater small-VRAM inference
        self.num_predict = int(num_predict)   # CAP the output: without it a local model rambles to num_ctx and times out
        # AI prompt budget (chars) — must FIT num_ctx, so the prompt is never silently truncated and inference
        # stays fast. ~1.5 chars/token is CJK-safe. The deterministic floor still reads the FULL, uncapped text.
        self.max_chars = max(8000, int((self.num_ctx - self.num_predict) * 1.5))
        self.tokens = 0
        self.cost = 0.0           # local inference = no $ cost (kept for interface parity)
        self.should_stop = None   # set by run_comprehension so a Stop aborts the in-flight stream
        self._lock = threading.Lock()

    def generate(self, task: str, prompt: str, schema: dict | None = None):
        p = prompt
        if schema:
            p += ("\n\nReturn ONLY valid JSON (no prose, no code fence) matching this shape:\n"
                  + json.dumps(schema))
        payload = {
            "model": self.model, "prompt": p, "stream": True,
            # think:false — a "thinking" model (qwen3.x) otherwise routes its output into the reasoning channel
            # and returns an EMPTY response, which breaks format=json. These are extraction passes; no CoT needed.
            "think": False,
            # temperature 0 = deterministic extraction; num_predict caps the output. Do NOT pin
            # repeat_penalty=1.0 — greedy (temp 0) + no penalty makes a local model loop and bloat the JSON
            # until it truncates mid-object; Ollama's default penalty breaks the loop so the JSON completes.
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.num_predict},
        }
        if schema:
            payload["format"] = "json"                # token-level grammar -> always-valid JSON
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.host + "/api/generate", body,
                                     {"Content-Type": "application/json"})
        parts, pe, ec = [], 0, 0
        start = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise RuntimeError(
                f"Ollama not reachable at {self.host} — start it (`ollama serve`) or install from "
                f"ollama.com, then `ollama pull {self.model}`. ({reason})"
            ) from None
        try:
            for raw in resp:                          # NDJSON: one event per line, streamed
                if self.should_stop and self.should_stop():
                    raise Stopped()
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError()
                line = (raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw).strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("error"):
                    raise RuntimeError(f"Ollama error: {str(ev['error'])[:200]}")
                if ev.get("response"):
                    parts.append(ev["response"])
                if ev.get("done"):
                    pe = int(ev.get("prompt_eval_count") or 0)
                    ec = int(ev.get("eval_count") or 0)
        except (TimeoutError, socket.timeout):
            raise RuntimeError(
                f"AI '{task}' step timed out after {self.timeout}s — the thread may be large or the model "
                f"slow. Raise the AI timeout in /dev, or pick a smaller/faster local model."
            ) from None
        finally:
            with self._lock:
                self.tokens += pe + ec            # accrue even on abort/error (Ollama reports counts on 'done')
            try:
                resp.close()
            except Exception:
                pass
        text = "".join(parts).strip()
        return _parse_json(text) if schema else text
