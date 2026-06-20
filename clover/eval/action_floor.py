"""Deterministic ACTION floor (no AI) — recall-first candidate-obligation surface, EN + CN.

20-user review P0 #3: the action layer was AI-recall-only, so obligations without obvious request
markers silently dropped. This is the deterministic FLOOR under the AI action pass: scan sentence by
sentence and surface every sentence carrying a STRONG obligation signal — a clear imperative/request
verb, a modal (must/shall/need), a real deadline (a date or a deadline keyword), or a waiver/
consequence cue — in English AND Chinese. It does NOT decide the final action (the AI does); it
guarantees no cued obligation is silently dropped and feeds the AI pass + the gold scorer a checklist.
Ambiguous nouns ("the update", "the review") and courtesy alone never qualify.
"""
from __future__ import annotations

import re

from .extractors import extract_dates

_SENT = re.compile(r"[^.!?;\n。！？；]+")

# clear imperative / request verbs + politeness (strong = enough to qualify on their own)
_STRONG_REQ_EN = ["please", "kindly", "could you", "can you", "request", "ensure", "make sure",
                  "confirm", "provide", "submit", "send ", "approve", "sign ", "pay ", "release ",
                  "furnish", "endorse", "expedite", "revert", "advise", "follow up", "action required",
                  "respond", "reply", "arrange", "prepare", "complete the", "fill in", "fill out",
                  # scheduling / meeting (cross-domain: recruiting, agency, facilities)
                  "reschedule", "schedule a", "schedule the", "set up a", "book a", "set a time",
                  # creative / iterative revision (agency, design, QA)
                  "revise", "rework", "redo", "amend", "another round", "another pass", "sign off",
                  "sign-off", "turn around", "circle back", "loop in", "action this"]
_STRONG_REQ_CN = ["请", "麻烦", "务必", "需要", "需", "应当", "应", "提交", "确认", "批准", "审批",
                  "回复", "提供", "安排", "签署", "盖章", "付款", "落实", "尽快",
                  "修改", "重做", "改期", "跟进", "处理"]
_MODAL = ["must ", "shall ", "need to", "needs to", "required to", "have to", "has to", "is to ",
          "are to ", "obligated", "responsible for"]
_WAIVER_EN = ["failing which", "failure to", "will be deemed", "deemed to", "or else"]
_WAIVER_CN = ["否则", "逾期", "视为放弃"]
_STRONG_DL = ["due ", "deadline", "within ", "no later than", "asap", "eod", "cob", "on or before",
              "截止", "期限", "不迟于", "天内", "日内", "工作日内"]
# weak/ambiguous (reported as cues, but NOT enough to qualify alone)
_WEAK_REQ = ["review", "update", "return ", "issue ", "proceed", "attend", "schedule", "verify", "escalate"]
_DL_WEAK = ["by ", "before ", "end of", "之前", "以前"]
_COURTESY = ["thank", "thanks", "regards", "appreciate", "fyi", "no action", "谢谢", "感谢", "辛苦"]


def _has(s_low: str, s_raw: str, en: list, cn: list = ()) -> bool:
    return any(c in s_low for c in en) or any(c in s_raw for c in cn)


def _categories(s_low: str, s_raw: str, real_deadline: bool) -> list[str]:
    cats = set()
    if _has(s_low, s_raw, _STRONG_REQ_EN, _STRONG_REQ_CN) or _has(s_low, s_raw, _WEAK_REQ):
        cats.add("request")
    if _has(s_low, s_raw, _MODAL):
        cats.add("obligation")
    if _has(s_low, s_raw, _WAIVER_EN, _WAIVER_CN):
        cats.add("waiver")
    if real_deadline or _has(s_low, s_raw, _DL_WEAK):
        cats.add("deadline")
    if _has(s_low, s_raw, _COURTESY):
        cats.add("courtesy")
    return sorted(cats)


def action_candidates(text: str) -> list[dict]:
    """Every sentence carrying a STRONG obligation signal, as a CANDIDATE action (recall-first).
    Returns [{text, cues, has_deadline}]. Ambiguous-noun and courtesy-only sentences are excluded."""
    out = []
    for m in _SENT.finditer(text or ""):
        s = m.group(0).strip()
        if len(s) < 3:
            continue
        low = s.lower()
        real_deadline = bool(extract_dates(s)) or _has(low, s, _STRONG_DL)
        strong = (_has(low, s, _STRONG_REQ_EN, _STRONG_REQ_CN)
                  or _has(low, s, _MODAL)
                  or _has(low, s, _WAIVER_EN, _WAIVER_CN)
                  or real_deadline)
        if not strong:
            continue
        cats = _categories(low, s, real_deadline)
        out.append({"text": s[:200], "cues": cats, "has_deadline": "deadline" in cats})
    return out
