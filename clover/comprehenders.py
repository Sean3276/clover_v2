"""Comprehender — the pluggable AI backend the Phase-3 pipeline calls.

Mirrors the MailSource pattern: one interface, swappable implementations. StubComprehender is
deterministic (tests / offline build); ClaudeCliComprehender shells out to the local Claude Code
CLI (your subscription, no API key). Ollama / hosted / API can be added later without touching
the pipeline.
"""
from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod

_REGISTRY: dict = {}


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
    raise ValueError("model did not return valid JSON")


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

    def __init__(self, model: str = "sonnet", timeout: int = 180):
        self.model = model
        self.timeout = timeout
        self.tokens = 0           # actual tokens consumed (from the CLI usage envelope)
        self.cost = 0.0           # USD, if the CLI reports it
        self._lock = threading.Lock()   # token/cost accrual is concurrency-safe

    def generate(self, task: str, prompt: str, schema: dict | None = None):
        import shutil
        import subprocess
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Claude CLI not found — install with: npm i -g @anthropic-ai/claude-code")
        p = prompt
        if schema:
            p += ("\n\nReturn ONLY valid JSON (no prose, no code fence) matching this shape:\n"
                  + json.dumps(schema))
        # prompt goes on STDIN (not argv) — thread text easily exceeds the OS command-line limit;
        # force UTF-8 so mixed English/Chinese content round-trips
        proc = subprocess.run(
            [exe, "-p", "--model", self.model, "--output-format", "json"],
            input=p, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
        try:
            env = json.loads(proc.stdout)        # CLI envelope: {"result": "...", "usage": {...}, ...}
            text = env.get("result", proc.stdout)
            u = env.get("usage") or {}
            with self._lock:
                self.tokens += sum(int(v) for k, v in u.items()
                                   if isinstance(v, (int, float)) and "token" in k.lower())
                self.cost += float(env.get("total_cost_usd") or 0)
        except Exception:
            text = proc.stdout
        return _parse_json(text) if schema else text
