"""Phase 3 — comprehension pipeline (the first AI phase).

Pure & stateless: comprehend_thread(archive, thread, backend, profile) -> record. Reads Phase-2
threads + the .eml archive; writes <archive>/comprehension.jsonl (idempotent by thread_id).
Whole-thread by default; iterative-refine for giants. Classification via a profile-driven 2-tier
council with a deterministic precedence referee. Facts are verified against the source.
"""
from __future__ import annotations

import concurrent.futures
import threading
import json
import re
import time
from html import unescape
from pathlib import Path

from . import rules as rulesmod
from . import comprehend_prompts as cprompts
from .comprehenders import Comprehender, Stopped
from .profiles import Profile, get_profile
from . import attachments as attmod
from .threads import get_attachment, read_threads, render_message

_TAG = re.compile(r"(?s)<[^>]+>")
_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS = re.compile(r"\s+")
_ANNOT = re.compile(r"\s+(?:[—–;(]|-\s).*$")   # trailing model annotation: " — desc", " (desc)", " - desc"
_WEEKDAY = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*[,.\s]+", re.I)   # leading "Wed " on a date
_NUM = re.compile(r"\d[\d,]*\.?\d*")
_LEDGER = re.compile(r"<<<LEDGER>>>(.*?)(?:<<<END>>>|$)", re.S)   # comprehend/refine open-items tail
_MTAG = re.compile(r"[Mm]\d+")                                    # message position tag, e.g. M3

# Decomposed distill (best-of-art prompts, see comprehend_prompts.py). The single mega "distill" call
# was split: a FACTS pass (every fact + contact, each [Mn]-cited) and a SUMMARY pass (the narrative
# layer). Schemas mirror each prompt's documented output shape.
_FACTS_SCHEMA = {"project": [{"value": "str", "cite": "str"}],
                 "facts": [{"field": "party|ref|date|amount", "value": "str", "cite": "str"}],
                 "contacts": [{"name": "str", "position": "str", "company": "str",
                               "phone": "str", "email": "str", "cite": "str"}]}
_SUMMARY_SCHEMA = {"abstract": "str", "summary": "str", "event": "str (<=30 chars)",
                   "tags": ['"Facet: Value" strings, only from the listed facet vocabularies']}
_CLASSIFY_SCHEMA = {"domain": "str", "category": "str", "confidence": "number 0..1",
                    "dispute": "bool", "dissent": "str", "votes": "str"}
# qa_semantic emits the flat gate keys (passed/faithfulness/completeness/issues) plus per-lens detail;
# only the flat keys gate the task.
_QA_SCHEMA = {"passed": "bool", "faithfulness": "number 0..1", "completeness": "number 0..1",
              "more_minors": "bool", "lenses": "object", "issues": ["str"]}
# step-8 (ii)-(iv) vs (i): verify the distilled abstract / one-liner / event tag against the comprehension
_DISTILL_QA_SCHEMA = {"abstract_ok": "bool", "summary_ok": "bool", "event_ok": "bool",
                      "passed": "bool",
                      "issues": [{"target": "str", "type": "str", "evidence": "str", "detail": "str"}]}
# the per-thread to-do extraction (Phase-4 feeds off this) — one object per actionable item
_ACTIONS_SCHEMA = {"actions": [{
    "action": "str (imperative — what must be done)", "about": "str (short context, e.g. 'door contract')",
    "owner": "str (who must act, or 'unclear')", "counterparty": "str",
    "direction": "inbound|outbound|internal|unknown", "is_mine": "bool|null",
    "due_raw": "str (deadline verbatim, '' if none)", "refs": ["str"], "status": "open|done|blocked|superseded",
    "priority": "high|normal|low", "source": "str — the [Mn] tag(s)", "quote": "str (verbatim proof)",
    "confidence": "high|review", "false_positive_suspected": "bool", "implied": "bool",
    "owner_history": [{"owner": "str", "source": "str"}]}]}
_SMALL_COUNCIL, _FULL_COUNCIL = 5, 10


# ---------------------------------------------------------------- helpers
def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _block_text(block: dict) -> str:
    if block.get("body_text"):
        return block["body_text"]
    h = block.get("body_html") or ""
    h = _STYLE.sub(" ", h)
    h = _TAG.sub(" ", h)
    return _WS.sub(" ", unescape(h)).strip()


def _attachment_text(archive, location: dict, atts: list) -> str:
    """Extract text from a message's attachments so their content is comprehended too — LOUD on
    anything unread (image/OCR-needed, unsupported, parse error), never silently skipped."""
    import os
    import tempfile
    parts = []
    for a in atts:
        name = a.get("name") or "attachment"
        if a.get("img"):
            parts.append(f"[attachment NOT read: {name} — image, needs OCR (not enabled)]")
            continue
        try:
            got = get_attachment(archive, location, a.get("i", 0))
        except Exception:
            got = None
        if not got:
            continue
        fname, _ctype, data = got
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=Path(fname or name).suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data or b"")
            r = attmod.extract_attachment(tmp)
        except Exception as e:
            r = {"ok": False, "text": "", "note": f"{type(e).__name__}"}
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if r.get("ok") and r.get("text"):
            parts.append(f"[attachment: {fname or name}]\n{r['text']}")
        else:
            parts.append(f"[attachment NOT read: {fname or name} — {r.get('note', '')}]")
    return "\n\n".join(parts)


_LINK_FILE_MAX_BYTES = 25 * 1024 * 1024            # don't try to inline >25MB shared files (archives/media)
# AI prompt budget per message: the model sees at most this many chars (~12k tokens) per call, so a single
# giant attachment can't blow the per-call timeout. The DETERMINISTIC FLOOR still reads the full, uncapped
# text (see floor_src in _build_once), so no-miss stays architectural — not dependent on stuffing everything
# into one slow AI call. Lowered from 120k after live giant-attachment threads timed out at 300s/call.
_AI_MAX_CHARS = 48_000
_AI_ATTACH_CHARS = 8_000                           # per-attachment / per-shared-file slice the AI sees
_ATTACH_MARK = re.compile(r"(\n\n\[(?:attachment|shared file): [^\]]*\]\n)")


def _downloaded_files_by_mid(archive) -> dict:
    """{message_id: [downloaded share-link file paths]} — read once per thread so a shared SharePoint/
    Drive/Dropbox file's CONTENT (not just the link) is comprehended, like an attachment."""
    out: dict = {}
    try:
        from .linkshares import read_link_shares
    except Exception:
        return out
    for r in read_link_shares(archive):
        if r.get("status") == "downloaded" and r.get("file") and r.get("message_id"):
            out.setdefault(r["message_id"], []).append(r["file"])
    return out


def _link_file_text(archive, files) -> str:
    """Extract text from a message's downloaded share-link files — same loud-on-unread discipline as
    email attachments; oversize files are flagged, not loaded."""
    parts = []
    for rel in files or []:
        path = Path(archive) / rel
        name = Path(rel).name
        try:
            if not path.exists():
                continue
            if path.stat().st_size > _LINK_FILE_MAX_BYTES:
                parts.append(f"[shared file NOT read: {name} — {path.stat().st_size // (1024 * 1024)}MB, too large to inline]")
                continue
            r = attmod.extract_attachment(path)
        except Exception as e:
            r = {"ok": False, "text": "", "note": type(e).__name__}
        if r.get("ok") and r.get("text"):
            parts.append(f"[shared file: {name}]\n{r['text']}")
        else:
            parts.append(f"[shared file NOT read: {name} — {r.get('note', '')}]")
    return "\n\n".join(parts)


def _cap_msg(text: str, limit: int, attach_limit: int = _AI_ATTACH_CHARS) -> str:
    """Bound one message FOR THE AI: cap each attachment / shared-file section to attach_limit, then the
    whole message to limit — so a single giant attachment (or several) can't blow the per-call AI budget
    or time the call out. The deterministic floor still reads the FULL, uncapped text (floor_src below)."""
    parts = _ATTACH_MARK.split(text)                 # [head+body, mark1, body1, mark2, body2, ...]
    out = [parts[0]]
    i = 1
    while i < len(parts):
        out.append(parts[i])                         # the "[attachment: name]" / "[shared file: name]" marker
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if len(body) > attach_limit:
            body = body[:attach_limit] + "\n[… attachment truncated for the AI; the floor reads it in full …]"
        out.append(body)
        i += 2
    capped = "".join(out)
    if len(capped) > limit:
        capped = capped[:limit] + "\n[… message truncated to fit the comprehension budget …]"
    return capped


def _thread_messages(archive, thread: dict) -> list[str]:
    link_idx = _downloaded_files_by_mid(archive)       # downloaded share-link files, by message-id (read once)
    out = []
    for m in thread.get("members", []):
        locs = m.get("locations") or []
        if not locs:
            continue
        try:
            b = render_message(archive, locs[0])
        except Exception:
            continue
        # [Mn] position tag (contiguous over messages actually rendered) so the actions pass can
        # CITE which message each obligation came from. Still ONE header line, so the body-only
        # floor_src (which strips the first line) is unaffected.
        tag = f"[M{len(out) + 1}]"
        text = f"{tag} From: {b.get('from','')} | Date: {b.get('date','')}\n{_block_text(b)}"
        att = _attachment_text(archive, locs[0], b.get("attachments") or [])
        if att:
            text += "\n\n" + att
        link_text = _link_file_text(archive, link_idx.get(m.get("message_id")) or [])   # shared-link file content
        if link_text:
            text += "\n\n" + link_text
        out.append(text)
    return out


def _msg_date_map(archive, thread: dict) -> dict:
    """{"M1": "YYYY-MM-DD", ...} aligned with the [Mn] tags _thread_messages assigns (same numbering,
    same render-skip), so a relative deadline citing [Mn] can be resolved against that message's sent
    date. A message with no parseable date maps to ''."""
    from email.utils import parsedate_to_datetime
    out, i = {}, 0
    for m in thread.get("members", []):
        locs = m.get("locations") or []
        if not locs:
            continue
        try:
            b = render_message(archive, locs[0])
        except Exception:
            continue
        i += 1
        try:
            dt = parsedate_to_datetime(b.get("date") or "")
            out[f"M{i}"] = dt.date().isoformat() if dt else ""
        except Exception:
            out[f"M{i}"] = ""
    return out


# ---------------------------------------------------------------- data layer
def comprehension_path(archive) -> Path:
    return Path(archive) / "comprehension.jsonl"


def read_comprehensions(archive) -> list[dict]:
    p = comprehension_path(archive)
    out = []
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    return out


def comprehended_ids(archive) -> set:
    return {r.get("thread_id") for r in read_comprehensions(archive) if r.get("thread_id")}


def latest_by_thread(archive) -> dict:
    """{thread_id: latest record} — last write wins, matching get_comprehension."""
    out = {}
    for r in read_comprehensions(archive):
        if r.get("thread_id"):
            out[r["thread_id"]] = r
    return out


def latest_by_root(archive) -> dict:
    """{root_id: latest record}. root_id is the thread's STABLE identity — thread_id is a hash of the
    member message-ids and so changes whenever a message is added, which would orphan the comprehension.
    Linking by root_id lets a re-stitched thread find its prior comprehension (and be judged stale)."""
    out = {}
    for r in read_comprehensions(archive):
        if r.get("root_id"):
            out[r["root_id"]] = r
    return out


def comp_for_thread(archive, thread: dict) -> dict | None:
    """Latest comprehension for a thread, matched by stable root_id (falls back to thread_id for
    legacy records that predate root linkage)."""
    rec = latest_by_root(archive).get(thread.get("root_id"))
    return rec if rec else get_comprehension(archive, thread.get("thread_id"))


def downloaded_link_index(archive) -> tuple[dict, dict]:
    """Built once for many threads: ({message_id: count}, {eml_path: count}) of DOWNLOADED share-link
    files. Lets staleness notice when an attachment lands (its message-id or .eml path gains a file)."""
    by_mid, by_path = {}, {}
    try:
        from .linkshares import read_link_shares
    except Exception:
        return by_mid, by_path
    for r in read_link_shares(archive):
        if r.get("status") != "downloaded" or not r.get("file"):
            continue
        if r.get("message_id"):
            by_mid[r["message_id"]] = by_mid.get(r["message_id"], 0) + 1
        if r.get("eml"):
            p = str(r["eml"]).replace("\\", "/")
            by_path[p] = by_path.get(p, 0) + 1
    return by_mid, by_path


def thread_attach_count(archive, thread: dict, index: tuple[dict, dict] | None = None) -> int:
    """How many downloaded share-link files belong to this thread's messages. Pass `index` (from
    downloaded_link_index) when iterating many threads; omit it for a single thread."""
    by_mid, by_path = index if index is not None else downloaded_link_index(archive)
    n = 0
    for m in (thread.get("members") or []):
        n += by_mid.get(m.get("message_id"), 0)
        for loc in (m.get("locations") or []):
            n += by_path.get(str(loc.get("path") or "").replace("\\", "/"), 0)
    return n


def thread_sig(thread: dict, attach: int = 0) -> dict:
    """The thread's identity for staleness: message count + latest-message date + downloaded-attachment
    count (so a share-link file that arrives after comprehension re-comprehends the thread)."""
    return {"n": thread.get("n"), "end": thread.get("end") or "", "attach": int(attach)}


def is_stale(thread: dict, rec: dict | None, attach: int = 0) -> bool:
    """True if the thread changed since it was comprehended — a newer message arrived, the count grew,
    or a share-link attachment was downloaded. Legacy records with no signature can't be judged (no nag)."""
    if not rec:
        return False
    src = rec.get("source")
    if not src:
        return False
    return ((src.get("n") != thread.get("n"))
            or ((src.get("end") or "") != (thread.get("end") or ""))
            or (int(src.get("attach") or 0) != int(attach)))


def get_comprehension(archive, thread_id: str) -> dict | None:
    found = None
    for r in read_comprehensions(archive):       # latest record wins (re-comprehend supersedes)
        if r.get("thread_id") == thread_id:
            found = r
    return found


def _append(archive, record: dict, ts: float | None = None) -> None:
    record = dict(record)
    record["ts"] = ts if ts is not None else time.time()
    with comprehension_path(archive).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_comprehension(archive, record: dict) -> None:
    _append(archive, record)


def resolve_comprehension(archive, thread_id: str, domain: str, category: str, ts: str = "",
                          root_id: str = "") -> bool:
    """Operator override of a flagged thread's classification: set domain/category, consensus=resolved,
    clear needs_review. Rewrites the latest record for that thread in place. Returns False if absent.
    Matches by thread_id, falling back to the stable root_id (a re-stitched thread has a new thread_id)."""
    recs = read_comprehensions(archive)
    idx = None
    for i, r in enumerate(recs):
        if r.get("thread_id") == thread_id or (root_id and r.get("root_id") == root_id):
            idx = i                                  # latest matching record
    if idx is None:
        return False
    c = recs[idx].setdefault("classification", {})
    c["domain"], c["category"], c["consensus"] = domain, category, "resolved"
    if isinstance(recs[idx].get("qaqc"), dict):
        recs[idx]["qaqc"]["needs_review"] = False
    recs[idx]["resolved_ts"] = ts
    p = comprehension_path(archive)
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return True


def estimate_thread_tokens(archive, thread: dict) -> int:
    """Cheap pre-call estimate from .eml byte size (no body read). Each message is capped at 200 KB so a
    huge ATTACHMENT (base64 in the .eml) can't inflate the estimate to hundreds of millions of tokens and
    trip the budget after one thread — only the text actually sent to the model matters here."""
    total = 0
    for m in thread.get("members", []):
        for loc in (m.get("locations") or [])[:1]:
            try:
                total += min((Path(archive) / loc["path"]).stat().st_size, 200_000)
            except Exception:
                pass
    return max(1, total // 4)


# ---------------------------------------------------------------- prompts + prompt helpers
# The comprehend / refine / distill_facts / distill_summary / actions / qa / distill_qa prompts now
# live in comprehend_prompts.py (best-of-art, adversarially scored >9). These helpers assemble the
# runtime inputs those prompts consume.
def _facet_vocab(profile: Profile | None) -> str:
    """The 'Facet: value, value' vocabulary the distill_summary prompt picks tags from."""
    if not (profile and profile.facets):
        return "(no facets defined)"
    return "\n".join(f"{f}: {', '.join(profile.facet_values(f))}" for f in profile.facet_names())


def _ref_examples(profile: Profile | None) -> str:
    """The profile's example ref identifiers for the facts prompt (industry-agnostic: construction is
    only the seed). Falls back to a domain-spanning list so a profile with none set is still generic."""
    ex = list(getattr(profile, "ref_examples", None) or [])
    if ex:
        return ", ".join(ex)
    return "RFI, invoice no., case/matter no., ticket/PR no., PO no., clause/section no."


def _candidate_sheet(cands: list) -> str:
    """The deterministic action-floor candidates, as the JSON list the actions prompt expects."""
    return json.dumps(cands or [], ensure_ascii=False)


def _split_ledger(text: str) -> tuple[str, str]:
    """Split a comprehension into (prose, ledger_json). The comprehend/refine prompts append an
    open-items ledger delimited by <<<LEDGER>>> … (<<<END>>>). Returns ('', '') safely when absent."""
    text = text or ""
    m = _LEDGER.search(text)
    if not m:
        return text.strip(), ""
    return text[:m.start()].strip(), m.group(1).strip()


def _strip_current_state(delta: str) -> str:
    """Remove a refine chunk's trailing 'CURRENT STATE' id-list (a per-chunk live snapshot). The final
    ledger is authoritative, so these intermediate snapshots are dropped at the stitch."""
    out = []
    for line in (delta or "").splitlines():
        if line.strip().upper().startswith("CURRENT STATE"):
            break
        out.append(line)
    return "\n".join(out).strip()


def _classify_prompt(profile: Profile, comprehension, full: bool, members: int):
    taxonomy = "; ".join(f"{d}: {', '.join(profile.categories(d))}" for d in profile.domain_names())
    extra = ("\nThis is a DISPUTED case — weigh the costliest-if-wrong reading; a commercial / "
             "contractual matter must never be filed as routine info." if full else "")
    return (f"Convene a panel of {members} independent classifiers. Each member INDEPENDENTLY reads the "
            "thread and assigns a DOMAIN, then a CATEGORY within it, by MEANING (not keywords).\n"
            f"Taxonomy — {taxonomy}\n"
            f"High-stakes safety-net category: {profile.safety_net}.{extra}\n"
            f"Then report the PANEL result across the {members} members: the majority domain + category, "
            "a one-line `votes` summary of the split, confidence 0..1, dispute=true if the panel is "
            "genuinely split, and a one-line dissent for any strong minority view.\n\n"
            "COMPREHENSION:\n" + comprehension)


# ---------------------------------------------------------------- council + verification
def _precedence(profile: Profile, text: str) -> str | None:
    """First precedence rule whose keyword appears as a whole word/phrase wins. Word-boundary
    matching (not substring) so 'claim' doesn't fire on 'claimant', nor 'cost' on 'Costa'."""
    low = (text or "").lower()
    for rule in profile.precedence:
        for kw in rule.get("if_any", []):
            kw = kw.strip().lower()
            if kw and re.search(r"\b" + re.escape(kw) + r"\b", low):
                return rule.get("then")
    return None


def _classify(backend: Comprehender, profile: Profile, comprehension: str, thread_text: str) -> dict:
    small = backend.generate("classify", _classify_prompt(profile, comprehension, False, _SMALL_COUNCIL),
                             schema=_CLASSIFY_SCHEMA) or {}
    domain = small.get("domain") or profile.domain_names()[0]
    category = small.get("category") or ""
    conf = _f(small.get("confidence"))
    dispute = bool(small.get("dispute")) or conf < 0.6 or category not in profile.categories(domain)
    if not dispute:
        return {"domain": domain, "category": category, "confidence": conf,
                "council": "small", "members": _SMALL_COUNCIL, "consensus": "unanimous",
                "dissent": "", "votes": str(small.get("votes", ""))}
    # escalate to the full (larger) council
    full = backend.generate("classify_full", _classify_prompt(profile, comprehension, True, _FULL_COUNCIL),
                            schema=_CLASSIFY_SCHEMA) or {}
    domain = full.get("domain") or domain
    category = full.get("category") or category
    conf = _f(full.get("confidence"), conf)
    consensus = "majority"
    ref = _precedence(profile, thread_text)          # deterministic referee breaks ties by rule
    if ref and ref != category:
        category, consensus = ref, "split-resolved"
    if conf < 0.5 or category not in profile.categories(domain):
        consensus = "asked"                          # genuine doubt / invalid pair -> surface to operator
    return {"domain": domain, "category": category, "confidence": conf,
            "council": "full", "members": _FULL_COUNCIL, "consensus": consensus,
            "dissent": str(full.get("dissent", "")), "votes": str(full.get("votes", ""))}


def _norm(s) -> str:
    return _WS.sub(" ", str(s).lower()).strip()


def _verify_facts(facts: dict, thread_text: str) -> tuple[dict, list]:
    """Keep only facts grounded in the thread. The model often annotates a value ('14 Mar 2025 —
    deadline'); verify the BARE core (annotation stripped) appears, with a digit-match fallback for
    amounts (currency/comma formatting varies). Stores the cleaned core. Returns (facts, dropped)."""
    src = _norm(thread_text)
    src_nums = {re.sub(r"\D", "", n) for n in _NUM.findall(src)}   # source numbers, digits-only, per token
    src_nums.discard("")
    facts = dict(facts or {})
    dropped = []

    def verify(value, numeric=False):
        raw = str(value).strip()
        core = _WEEKDAY.sub("", _ANNOT.sub("", raw)).strip() or raw
        c = _norm(core)
        # ASCII: whole-word match (so 'Sun' != 'sunshine'). CJK has no word boundaries, so \b would
        # wrongly drop a value present verbatim inside a character run — use containment there.
        if c and (re.search(r"\b" + re.escape(c) + r"\b", src) if c.isascii() else c in src):
            return core
        if numeric:
            nums = [re.sub(r"\D", "", n) for n in _NUM.findall(raw)]
            nums = [n for n in nums if len(n) >= 3]      # a meaningful number, not a lone digit
            if nums and all(n in src_nums for n in nums):  # each must EQUAL a real source number (not span two)
                return core
        return None

    for key in ("refs", "dates", "amounts", "parties"):
        keep = []
        for v in (facts.get(key) or []):
            r = verify(v, numeric=(key == "amounts"))
            if r is not None and r not in keep:         # de-dup (bare + annotated collapse to one)
                keep.append(r)
            elif r is None and v:
                dropped.append(f"{key}:{v}")
        facts[key] = keep
    proj = facts.get("project") or ""
    pr = verify(proj) if proj else None
    if proj and pr is None:
        dropped.append(f"project:{proj}")
        facts["project"] = ""
    elif pr:
        facts["project"] = pr
    return facts, dropped


def _refine(backend, thread, msgs, max_chars):
    """Comprehend a thread too long for one pass, CARRYING a structured open-items ledger forward so
    early items can't be lost (the prose-regen trap). First chunk: a full `comprehend` (prose + ledger
    tail). Each later chunk: `refine` with the prior prose + ledger + next messages -> delta prose +
    the full updated ledger. The final comprehension is the concatenated prose with the last ledger
    re-attached, so the downstream QA / distill passes still see the open-items ledger."""
    subject = thread.get("subject", "")
    chunks, cur, size = [], [], 0
    for msg in msgs:
        if cur and size + len(msg) > max_chars:
            chunks.append(cur); cur, size = [], 0
        cur.append(msg); size += len(msg)
    if cur:
        chunks.append(cur)
    if not chunks:
        return ""
    first = backend.generate("comprehend", cprompts.comprehend(subject, "\n\n----\n\n".join(chunks[0])))
    running, ledger = _split_ledger(str(first))
    for ch in chunks[1:]:
        out = backend.generate("comprehend_refine",
                               cprompts.refine(subject, running, ledger, "\n\n----\n\n".join(ch)))
        delta, ledger = _split_ledger(str(out))
        # drop each chunk's "CURRENT STATE" id-list (it's a per-chunk snapshot) — the re-attached final
        # ledger is the authoritative live set, so stale intermediate snapshots must not pile up.
        running = (running + "\n\n" + _strip_current_state(delta)).strip()
    return running + "\n\n<<<LEDGER>>>\n" + ledger + "\n<<<END>>>" if ledger else running


def _clean_contacts(raw) -> list[dict]:
    """Normalize the model's contacts list to {name, position, company, phone, email}; drop empties.
    Carries the [Mn] `cite` through when the facts-registrar provides one (traceability), but never
    adds the key when absent — so legacy/stub records stay byte-identical."""
    out = []
    for c in (raw or []):
        if not isinstance(c, dict):
            continue
        rec = {k: str(c.get(k) or "").strip() for k in ("name", "position", "company", "phone", "email")}
        if rec["name"] or rec["email"]:
            cite = str(c.get("cite") or "").strip()
            if cite:
                rec["cite"] = cite
            out.append(rec)
    return out


def _facts_from_registrar(raw) -> tuple[dict, list, list]:
    """Convert the facts-registrar output ({project[], facts[](field/value/cite), contacts[]}) into
    (a) the BARE-value facts dict the floor / scorer / views consume, (b) a citation trail
    [{field, value, cite}] that preserves the [Mn] each fact came from, and (c) the contacts list.
    Keeps the project as a single string (first named) for rule/view compatibility; any extra project
    is preserved in the citation trail so it is never silently dropped."""
    facts = {"project": "", "parties": [], "refs": [], "dates": [], "amounts": []}
    sources: list = []
    field_map = {"party": "parties", "ref": "refs", "date": "dates", "amount": "amounts"}
    if not isinstance(raw, dict):
        return facts, sources, []
    projs = [str(p.get("value") or "").strip() for p in (raw.get("project") or [])
             if isinstance(p, dict) and str(p.get("value") or "").strip()]
    facts["project"] = projs[0] if projs else ""
    for p in (raw.get("project") or []):
        if isinstance(p, dict) and str(p.get("value") or "").strip():
            sources.append({"field": "project", "value": str(p["value"]).strip(),
                            "cite": str(p.get("cite") or "").strip()})
    for f in (raw.get("facts") or []):
        if not isinstance(f, dict):
            continue
        fld = field_map.get(str(f.get("field") or "").strip().lower())
        val = str(f.get("value") or "").strip()
        if not fld or not val:
            continue
        if val not in facts[fld]:
            facts[fld].append(val)
        sources.append({"field": str(f.get("field")).strip().lower(), "value": val,
                        "cite": str(f.get("cite") or "").strip()})
    return facts, sources, (raw.get("contacts") or [])


def _msg_tags(value) -> list[str]:
    """Parse [Mn] message tags out of any source field (a string '[M2][M5]' or a list), normalised to
    ['M2','M5']. Robust to either convention the prompts emit."""
    s = " ".join(str(x) for x in value) if isinstance(value, (list, tuple)) else str(value or "")
    return [t.upper() for t in _MTAG.findall(s)]


_EV_STOP = {"the", "a", "an", "after", "before", "from", "prior", "to", "following", "of", "day",
            "days", "week", "weeks", "month", "months", "business", "working", "within", "net"}


def _event_date(comprehension: str, anchor: str) -> str:
    """Resolve a named-event deadline anchor ('the trial', 'discharge', 'the award') to a date stated
    elsewhere in the comprehension: find a line mentioning the anchor's key word and read the date
    co-located there. '' if none — the caller falls back to the send-date / pending."""
    from .eval.extractors import extract_dates
    words = [w for w in re.findall(r"[a-z一-鿿]{2,}", (anchor or "").lower()) if w not in _EV_STOP]
    if not words:
        return ""
    anchored = False
    for line in (comprehension or "").splitlines():
        low = line.lower()
        if any(w in low for w in words):
            anchored = True
            ds = extract_dates(line)
            if ds:
                return ds[0]                              # date co-located with the anchor word
    # anchor mentioned but its date is on another line: if the whole thread has exactly ONE date,
    # it is unambiguously the anchor's date; more than one -> can't disambiguate -> leave pending.
    if anchored:
        all_dates = extract_dates(comprehension or "")
        if len(all_dates) == 1:
            return all_dates[0]
    return ""


# deterministic sensitivity cues -> class (legal/regulated domains: never handle these as ordinary)
_SENSITIVITY = {
    "without prejudice": "without-prejudice", "调解": "without-prejudice",
    "privileged": "privileged", "legally privileged": "privileged", "attorney-client": "privileged",
    "solicitor-client": "privileged", "work product": "privileged", "litigation privilege": "privileged",
    "strictly confidential": "confidential", "private and confidential": "confidential",
    "do not disclose": "confidential", "保密": "confidential", "不得披露": "confidential",
    "medical record": "personal-data", "patient": "personal-data", "diagnosis": "personal-data",
    "date of birth": "personal-data", "nric": "personal-data", "passport no": "personal-data",
    "national id": "personal-data", "身份证": "personal-data", "病历": "personal-data",
    "dob": "personal-data", "mrn": "personal-data", "accession": "personal-data",
    "member id": "personal-data", "policy no": "personal-data", "medical record no": "personal-data",
    "phi": "personal-data", "protected health information": "personal-data", "hipaa": "personal-data",
    "prior authorization": "personal-data", "prior auth": "personal-data", "subscriber id": "personal-data",
    "explanation of benefits": "personal-data",
}


def _sensitivity(text: str) -> list[str]:
    """Deterministic sensitivity classes detected in the source (privilege / without-prejudice /
    confidential / personal-data) so downstream handling can gate or mark them. Awareness, not redaction.
    ASCII cues match on whole words/phrases (so 'patient' doesn't fire inside 'outpatient', nor 'phi'
    inside 'morphine') — consistent with _precedence/_verify_facts. CJK cues have no word boundaries,
    so \\b would never match; use containment for them."""
    low = (text or "").lower()
    out = []
    for cue, cls in _SENSITIVITY.items():
        hit = re.search(r"\b" + re.escape(cue) + r"\b", low) if cue.isascii() else cue in low
        if hit and cls not in out:
            out.append(cls)
    return out


_NOYEAR_DMY = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\b")     # 14 March
_NOYEAR_MDY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\b")     # March 14


def _resolve_no_year(due_raw: str, anchor_iso: str) -> str:
    """A month+day deadline with NO year ('by 14 March') -> ISO, inferring the year from the citing
    message's send-date (anchor_iso). '' if no month-day or no anchor."""
    if not anchor_iso:
        return ""
    from .eval.extractors import _MON, _iso
    y = anchor_iso[:4]
    for rx, dgrp, mgrp in ((_NOYEAR_DMY, 1, 2), (_NOYEAR_MDY, 2, 1)):
        m = rx.search(due_raw or "")
        if m:
            mo = _MON.get(m.group(mgrp).lower())
            if mo:
                iso = _iso(y, mo, m.group(dgrp))
                if iso:
                    return iso
    return ""


def _tok_in(tok: str, s: str) -> bool:
    """Whole-token containment (so 'ann@x.com' does NOT match inside 'joann@x.com'). Boundaries are
    non email/word chars, so emails/domains match exactly, not as substrings."""
    return re.search(r"(?<![\w.@-])" + re.escape(tok) + r"(?![\w.@-])", s) is not None


def _parse_ledger(comprehension: str) -> list[dict]:
    """Parse the <<<LEDGER>>> tail into structured open-items (queryable: status/supersedes/owner_history),
    handling the comprehend shape {"open_items":[...]} and the refine array shape. [] if absent/invalid."""
    _, blob = _split_ledger(comprehension)
    if not blob:
        return []
    data = None
    for candidate in (blob, (re.search(r"[\[{].*[\]}]", blob, re.S) or [None])[0] if re.search(r"[\[{].*[\]}]", blob, re.S) else None):
        if candidate:
            try:
                data = json.loads(candidate); break
            except Exception:
                continue
    if isinstance(data, dict):
        data = data.get("open_items") or data.get("items") or []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _infer_operator(thread: dict) -> str:
    """Fallback operator identity when none is configured: the most frequent sender across the thread
    (the mailbox owner is usually the dominant participant). Low-confidence; a configured operator wins."""
    from collections import Counter
    c = Counter()
    for m in thread.get("members", []):
        em = re.search(r"[\w.+-]+@[\w.-]+", (m.get("from") or "").lower())
        if em:
            c[em.group(0)] += 1
    return c.most_common(1)[0][0] if c else ""


_PII_MASK = [
    (re.compile(r"\bMRN[:#\s-]*\d{3,}\b", re.I), "[MRN]"),
    (re.compile(r"\b(?:DOB|date of birth)[:\s]*[\d/.\- ]{6,12}", re.I), "[DOB]"),
    (re.compile(r"\b[STFG]\d{7}[A-Z]\b"), "[NRIC]"),                          # SG NRIC/FIN
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\b(?:member|subscriber|policy|accession)\s*(?:id|no\.?|#)?[:\s#-]*[A-Z0-9]{4,}\b", re.I), "[ID]"),
    (re.compile(r"\b\+?\d[\d().\s-]{7,}\d\b"), "[PHONE]"),
]


def _minimize(text: str) -> str:
    """Mask pattern-detectable identifiers (MRN/DOB/NRIC/email/member-id/phone) — 'minimum necessary'
    redaction for the human-facing layers of a thread classified sensitive (PHI / privileged)."""
    for rx, rep in _PII_MASK:
        text = rx.sub(rep, text or "")
    return text


def _clean_actions(raw, date_map: dict | None = None, operator: str = "",
                   comprehension: str = "", source_text: str = "", holidays=()) -> list[dict]:
    """Normalize the model's to-do items to a stable shape and resolve their deadline deterministically:
    an absolute date in due_raw -> ISO due_canonical; a RELATIVE deadline (Net-30, 'within 14 days') is
    resolved against the SOURCE message's sent-date — business/working-day phrasings skip weekends but
    stay due_pending (an estimate, not a confident date — holidays unapplied). When the operator identity
    matches the owner/counterparty by WHOLE token (and unambiguously), is_mine/direction resolve
    deterministically. Each action's `quote` is verified to appear in the comprehension and its [Mn]
    cites are range-checked; failures drop the bad cite and downgrade confidence to 'review'. Items with
    no action text drop."""
    from .eval.extractors import extract_dates
    from .eval import deadlines as _dl
    date_map = date_map or {}
    valid_tags = set(date_map)
    # a quote may be copied from the comprehension OR (verbatim) from the source thread — verify against
    # both, so a legit implied/cross-message action quoting the original isn't mislabeled a fabrication.
    comp_norm = (_norm(comprehension) + " " + _norm(source_text)).strip()
    # operator identities/roles/aliases are comma/semicolon/newline separated so multi-word roles
    # ('the Contractor') and short CJK aliases ('我方') stay intact as whole match tokens.
    optokens = [t.strip() for t in re.split(r"[,;\n]+", (operator or "").lower()) if len(t.strip()) >= 2]
    out = []
    for a in (raw or []):
        if not isinstance(a, dict):
            continue
        action = str(a.get("action") or "").strip()
        if not action:
            continue
        owner = str(a.get("owner") or "").strip() or "unclear"
        counterparty = str(a.get("counterparty") or "").strip()
        confidence = str(a.get("confidence") or "review").strip().lower() or "review"
        # cite range-check: drop any [Mn] beyond the real message universe; downgrade if we dropped one
        raw_tags = _msg_tags(a.get("source"))
        source = [t for t in raw_tags if t in valid_tags] if valid_tags else raw_tags
        if valid_tags and raw_tags and len(source) < len(raw_tags):
            confidence = "review"
        # quote verification: each proving snippet must appear in the comprehension. Implied/cross-message
        # actions carry per-leg snippets joined by '||' — verify each leg, not the contiguous join.
        quote = str(a.get("quote") or "").strip()
        legs = [lg.strip() for lg in quote.split("||") if lg.strip()]
        quote_unverified = bool(legs) and bool(comp_norm) and any(_norm(lg) not in comp_norm for lg in legs)
        if quote_unverified:
            confidence = "review"
        msg_anchor = next((date_map.get(t) for t in source if date_map.get(t)), "")
        recurrence = _dl.recurrence(action + " " + str(a.get("due_raw") or ""))   # standing/cadence duty
        due_raw = str(a.get("due_raw") or "").strip()
        iso = extract_dates(due_raw)
        rel = _dl.find_relative_deadlines(due_raw)
        due_basis = ""
        if iso:                                           # an absolute date in due_raw wins
            due_canonical, due_pending = iso[0], False
        elif rel:                                         # relative deadline
            r0 = rel[0]
            if r0.get("kind") == "relative":              # anchored to a NAMED external event
                ev = _event_date(comprehension, r0.get("anchor", ""))
                if ev:                                    # the event's date is stated in-thread -> use it
                    resolved = _dl.resolve(r0, ev)
                    due_canonical, due_pending = (resolved or ""), (resolved is None)
                    if resolved:
                        due_basis = "event-anchored (verify the anchor date)"
                else:                                     # NEVER anchor a statutory clock to the email date
                    due_canonical, due_pending = "", True
                    due_basis = f"anchor date not in thread — verify ({r0.get('anchor') or 'event'})"
            elif r0.get("kind") == "net":                 # Net-N runs from the INVOICE date, not the email
                ev = _event_date(comprehension, "invoice receipt dated issued")
                if ev:
                    resolved = _dl.resolve(r0, ev)
                    due_canonical, due_pending = (resolved or ""), (resolved is None)
                    if resolved:
                        due_basis = "Net-term from invoice date (verify)"
                else:
                    due_canonical, due_pending = "", True
                    due_basis = "Net-term — confirm invoice date"
            else:                                         # within -> now/receipt -> source msg date
                resolved = _dl.resolve(r0, msg_anchor, holidays) if msg_anchor else None
                due_canonical = resolved or ""
                if resolved and r0.get("unit") == "businessdays":
                    if holidays:                          # a holiday calendar IS configured -> confident
                        due_pending, due_basis = False, "business-day (holiday calendar applied)"
                    else:                                 # weekend-aware estimate; holidays unapplied
                        due_pending, due_basis = True, "business-day estimate (holidays not applied — verify)"
                else:
                    due_pending = resolved is None
        else:                                             # no N+unit date: try no-year month-day, then colloquial
            ny = _resolve_no_year(due_raw, msg_anchor)
            col = _dl.resolve_colloquial(due_raw, msg_anchor) if not ny else None
            if ny:
                due_canonical, due_pending, due_basis = ny, False, "year inferred from message date"
            elif col:
                due_canonical, due_pending, due_basis = col, False, "resolved vs message date"
            else:
                due_canonical, due_pending = "", False
        is_mine = a.get("is_mine")
        if not isinstance(is_mine, bool):
            is_mine = None
        direction = str(a.get("direction") or "").strip().lower()
        if optokens:                                      # deterministic operator-side resolution
            ol, cl = owner.lower(), counterparty.lower()
            owner_match = any(_tok_in(t, ol) for t in optokens)
            cp_match = any(_tok_in(t, cl) for t in optokens)
            if owner_match and not cp_match:
                is_mine, direction = True, "inbound"      # the operator owes it
            elif cp_match and not owner_match:
                is_mine, direction = False, "outbound"    # a counterparty owes it
            # both or neither -> ambiguous: leave the AI's value untouched
        # a deadline left pending BECAUSE its anchor date is missing is a time-bar / deemed-admission
        # risk — escalate so the operator is pushed to find the anchor, not left with a quiet pending.
        priority = str(a.get("priority") or "normal").strip().lower() or "normal"
        if due_pending and ("anchor" in due_basis or "confirm invoice" in due_basis) and priority != "high":
            priority = "high"
        status = str(a.get("status") or "open").strip().lower() or "open"
        if status not in {"open", "done", "blocked", "superseded", "partial"}:
            status = "open"                               # clamp to the closed set
        if status in {"done", "superseded"} and not source:   # a terminal call must name its cause [Mn]
            confidence = "review"
        owner_history = []
        for h in (a.get("owner_history") or []):
            if isinstance(h, dict) and str(h.get("owner") or "").strip():
                hsrc = [t for t in _msg_tags(h.get("source")) if not valid_tags or t in valid_tags]
                owner_history.append({"owner": str(h["owner"]).strip(), "source": ",".join(hsrc)})
        out.append({
            "action": action,
            "about": str(a.get("about") or "").strip(),
            "owner": owner,
            "counterparty": counterparty,
            "direction": direction,
            "is_mine": is_mine,
            "is_mine_confidence": "high",     # downgraded to 'low' by _build_once when operator was inferred
            "due_raw": due_raw,
            "due_canonical": due_canonical,
            "due_pending": due_pending,
            "due_basis": due_basis,
            "recurrence": recurrence,
            "refs": [str(r).strip() for r in (a.get("refs") or []) if str(r).strip()],
            "status": status,
            "priority": priority,
            "source": source,
            "quote": quote,
            "quote_unverified": quote_unverified,
            "confidence": confidence,
            "false_positive_suspected": bool(a.get("false_positive_suspected")),
            "implied": bool(a.get("implied")),
            "owner_history": owner_history,
        })
    return out


def _uncovered_candidates(candidates, actions, cue_set=frozenset({"request", "obligation", "waiver"})) -> list[str]:
    """No-miss backstop for the to-do list (parity with the facts floor backfill): strong obligation
    sentences the deterministic floor surfaced that NO emitted action covers. Conservative — a candidate
    is 'covered' if any of its distinctive tokens (a ref, a >=5-char word, or a CJK run) appears in the
    actions' text. Recall-first: surfaced for one-click triage, never silently dropped."""
    from .eval.extractors import extract_refs
    hay = " ".join(str(a.get(k) or "") for a in (actions or [])
                   for k in ("action", "about", "quote")).lower()
    hay += " " + " ".join(str(r) for a in (actions or []) for r in (a.get("refs") or [])).lower()
    out = []
    for c in (candidates or []):
        if not (set(c.get("cues") or []) & cue_set):
            continue
        text = str(c.get("text") or "")
        refs = {r.lower() for r in extract_refs(text)}
        words = set(re.findall(r"[a-z]{5,}", text.lower())) | set(re.findall(r"[一-鿿]{2,}", text))
        if not (refs or words):
            continue
        # 'covered' needs a shared REF or >=2 shared distinctive words — one incidental word is not
        # enough (that would silently hide a real miss). Recall-first: when unsure, surface it.
        covered = any(r in hay for r in refs) or sum(1 for w in words if w in hay) >= 2
        if not covered:
            out.append(text)
    return out[:10]


def _action_haystack(actions) -> str:
    """The text an action covers — its own words/about/quote plus its refs — lowercased, for coverage checks."""
    hay = " ".join(str(a.get(k) or "") for a in (actions or []) for k in ("action", "about", "quote")).lower()
    return hay + " " + " ".join(str(r) for a in (actions or []) for r in (a.get("refs") or [])).lower()


def _unreconciled_open_items(open_items, actions) -> list[str]:
    """Open ledger items (the cross-chunk no-miss record the REFINE pass maintains) that NO emitted action
    covers — a silent miss the ledger exists to prevent. Matched on a shared ref or >=2 distinctive tokens;
    items already done/closed/superseded never flag. Recall-first: surfaced so the chain doesn't die in JSON."""
    from .eval.extractors import extract_refs
    hay = _action_haystack(actions)
    out = []
    for it in (open_items or []):
        if str(it.get("status") or "open").lower() in ("done", "closed", "resolved", "superseded", "cancelled"):
            continue
        matter = str(it.get("matter") or it.get("item") or it.get("desc") or it.get("title") or "").strip()
        oid = str(it.get("id") or "").strip()
        refs = {r.lower() for r in extract_refs(matter + " " + oid)}
        words = set(re.findall(r"[a-z]{5,}", matter.lower())) | set(re.findall(r"[一-鿿]{2,}", matter))
        if not (refs or words):
            continue
        covered = any(r in hay for r in refs) or sum(1 for w in words if w in hay) >= 2
        if not covered:
            out.append(matter or oid)
    return out[:10]


def _implied_action(text: str) -> dict:
    """Turn an uncovered implied-floor obligation into a provisional REVIEW action — the no-miss net, not
    just a triage hint, so every floor-caught duty is a dismissible to-do. Carries the floor's paired date."""
    due = ""
    if " — by " in text:
        text, due = text.split(" — by ", 1)
        due = due.strip()
    iso = due if re.match(r"^\d{4}-\d\d-\d\d$", due) else ""
    return {"action": text.strip(), "about": "implied obligation — review", "owner": "", "counterparty": "",
            "direction": "unknown", "is_mine": None, "due_raw": due, "due_canonical": iso,
            "due_pending": False, "due_basis": "implied-floor" if iso else "",
            "recurrence": "", "refs": [], "status": "open", "priority": "normal",
            "source": "", "quote": "", "quote_unverified": False, "confidence": "review",
            "false_positive_suspected": False, "implied": True, "owner_history": []}


def _clean_tags(raw, profile: Profile | None) -> list[str]:
    """Keep only AI-emitted tags that match the profile's facet vocabulary (deterministic verification).
    Accepts 'Facet: Value' strings or {facet,value}; returns canonical 'Facet: Value', deduped."""
    if not profile or not profile.facets:
        return []
    valid = {(f.lower(), v.lower()): f"{f}: {v}"
             for f in profile.facet_names() for v in profile.facet_values(f)}
    out, seen = [], set()
    for t in (raw or []):
        if isinstance(t, dict):
            fac, val = str(t.get("facet") or "").strip(), str(t.get("value") or "").strip()
        else:
            parts = str(t).split(":", 1)
            fac, val = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")
        canon = valid.get((fac.lower(), val.lower()))
        if canon and canon not in seen:
            out.append(canon); seen.add(canon)
    return out


# ---------------------------------------------------------------- pipeline
def _issue_str(i) -> str:
    """Render a QA issue (a flat string from qa_semantic, or a {target,type,evidence,detail} object
    from distill_qa) into one readable line for the merged needs-review list."""
    if isinstance(i, dict):
        head = " ".join(x for x in (i.get("target"), i.get("type")) if x)
        body = i.get("detail") or i.get("problem") or ""
        ev = i.get("evidence") or i.get("cite") or ""
        return " — ".join(x for x in (head, str(body).strip(), str(ev).strip()) if x).strip(" —")
    return str(i)


def _build_once(archive, thread, backend, profile, max_chars, model, operator: str = ""):
    operator_inferred = not operator                     # configured operator vs dominant-sender guess
    operator = operator or _infer_operator(thread)       # out-of-the-box is_mine: dominant-sender fallback
    raw_msgs = _thread_messages(archive, thread)
    input_truncated = any(len(m) > max_chars for m in raw_msgs)   # a very large attachment won't fit the AI budget
    msgs = [_cap_msg(m, max_chars) for m in raw_msgs]             # bound each message so no AI call overflows / times out
    full = "\n\n----\n\n".join(msgs)
    if len(msgs) <= 1 or len(full) <= max_chars:
        method = "whole"
        comprehension = backend.generate("comprehend", cprompts.comprehend(thread.get("subject", ""), full[:max_chars]))
    else:
        method = "refine"
        comprehension = _refine(backend, thread, msgs, max_chars)
    comprehension = (comprehension if isinstance(comprehension, str) else str(comprehension)).strip()

    # FACTS pass (decomposed from the old mega-distill) — every fact + contact, each [Mn]-cited.
    # ref examples are profile-driven so the facts prompt stays industry-agnostic (construction = seed).
    fraw = backend.generate("distill_facts", cprompts.distill_facts(comprehension, _ref_examples(profile)),
                            schema=_FACTS_SCHEMA) or {}
    facts, fact_sources, contacts_raw = _facts_from_registrar(fraw)
    facts, dropped = _verify_facts(facts, full)
    # range-check fact citations against the real [Mn] universe (drop out-of-range tags, like actions)
    date_map = _msg_date_map(archive, thread)
    _valid = set(date_map)
    for _s in fact_sources:
        _s["cite"] = ",".join(t for t in _msg_tags(_s.get("cite", "")) if not _valid or t in _valid)
    # Deterministic floor backfill (recall fix): add every catchable ref/date/amount the AI dropped
    # but a rule finds in the SOURCE. Body-only (strip each message's [Mn] From:/Date: header line) so
    # the email's own send-date is never injected as a content fact. Backfilled atoms are grounded by
    # construction (they came from the source) and recorded in verified.backfilled.
    from .eval import scorer as _scorer
    floor_src = "\n\n".join(m.split("\n", 1)[1] if "\n" in m else m for m in raw_msgs)   # UNcapped: floor reads all
    floor = _scorer.crosscheck_floor(floor_src, facts)
    backfilled = {}
    _have_amt_digits = {re.sub(r"\D", "", str(a)) for a in facts.get("amounts", [])}
    for _k in ("refs", "dates", "amounts"):
        _miss = floor[_k]["missed"]
        if _k == "amounts":          # don't duplicate an amount the AI already has in another format
            _miss = [m for m in _miss if re.sub(r"\D", "", m) not in _have_amt_digits]
        if _miss:
            facts.setdefault(_k, []).extend(_miss)
            backfilled[_k] = _miss
    from .eval.extractors import extract_percents as _extpct
    facts["percents"] = _extpct(floor_src)                       # rates/retention/discounts/equity (floor)

    # SUMMARY pass (decomposed) — the narrative layer + facet tags, grounded in the comprehension.
    sraw = backend.generate("distill_summary", cprompts.distill_summary(comprehension, _facet_vocab(profile)),
                            schema=_SUMMARY_SCHEMA) or {}

    # ACTIONS pass — the per-thread to-do extraction. Deterministic action-floor candidates (strong
    # obligation cues over BODY-only source) are fed in as a no-silent-miss checklist; the AI turns the
    # thread into itemized cited actions, then deadlines are resolved deterministically in _clean_actions.
    from .eval import action_floor as _afloor
    action_cands = _afloor.action_candidates(floor_src)
    implied_cands = _afloor.implied_candidates(floor_src)        # cross-message implied-duty floor
    # feed BOTH strong AND implied candidates into the AI's no-miss checklist (implied was previously inert)
    araw = backend.generate("actions",
                            cprompts.actions(comprehension, _candidate_sheet(action_cands + implied_cands), operator),
                            schema=_ACTIONS_SCHEMA) or {}
    actions = _clean_actions(araw.get("actions") if isinstance(araw, dict) else None,
                             date_map, operator, comprehension, full, getattr(profile, "holidays", ()))
    # coverage is measured against the AI's OWN actions first, so the deterministic backfill below can't
    # mask a real miss. Each floor tier that the AI dropped becomes load-bearing, not an inert count.
    strong_uncovered = _uncovered_candidates(action_cands, actions)
    soft_uncovered = _uncovered_candidates(_afloor.soft_candidates(floor_src), actions, frozenset({"soft"}))
    implied_uncovered = _uncovered_candidates(implied_cands, actions, frozenset({"implied"}))
    open_items = _parse_ledger(comprehension)                    # structured cross-chunk ledger
    open_items_unreconciled = _unreconciled_open_items(open_items, actions)
    implied = [c["text"] for c in implied_cands]
    # BACKFILL: an implied duty the AI dropped becomes a provisional review to-do (reaches a human as a
    # dismissible row, not just a hint). Done before minimization so a sensitive thread's row is masked too.
    for _txt in implied_uncovered:
        actions.append(_implied_action(_txt))
    # who-owes-whom rests on the operator identity; when that was GUESSED (dominant sender, not configured)
    # the is_mine arrow is a guess, not an assertion — mark it low-confidence so the UI never over-claims.
    if operator_inferred:
        for _a in actions:
            if _a.get("is_mine") is not None:
                _a["is_mine_confidence"] = "low"

    # PHI / privilege MINIMIZATION: if the thread is sensitive, mask pattern-identifiers in the
    # human-facing layers (abstract/summary/actions/contacts) — 'minimum necessary', awareness->protection.
    sensitivity = _sensitivity(full)
    minimized = bool(set(sensitivity) & {"personal-data", "privileged"})
    abstract = str(sraw.get("abstract") or "").strip()
    summary = str(sraw.get("summary") or "").strip()
    contacts = _clean_contacts(contacts_raw)
    if minimized:
        abstract, summary = _minimize(abstract), _minimize(summary)
        for _a in actions:
            _a["action"], _a["quote"], _a["about"] = _minimize(_a["action"]), _minimize(_a["quote"]), _minimize(_a["about"])
        for _c in contacts:
            for _k in ("name", "phone", "email"):
                _c[_k] = _minimize(_c.get(_k, ""))

    ruled = rulesmod.match(archive, text=full, project=facts.get("project", ""),
                           senders=[m.get("from", "") for m in thread.get("members", [])])
    if ruled:                                            # a learned rule wins deterministically — no AI council
        classification = {"domain": ruled.get("domain", ""), "category": ruled.get("category", ""),
                          "confidence": 1.0, "council": "rule", "members": 0,
                          "consensus": "rule", "dissent": "", "votes": ""}
    else:
        classification = _classify(backend, profile, comprehension, full[:max_chars])
    rec = {
        "thread_id": thread.get("thread_id"), "root_id": thread.get("root_id"),
        "subject": thread.get("subject"),
        "source": thread_sig(thread, thread_attach_count(archive, thread)),   # state at comprehension (staleness)
        "comprehension": comprehension,
        "abstract": abstract,
        "summary": summary,
        "event": str(sraw.get("event") or "").strip()[:30],
        "facts": facts,
        "fact_sources": fact_sources,                    # [{field, value, cite}] — [Mn] traceability
        "contacts": contacts,
        "tags": _clean_tags(sraw.get("tags"), profile),
        "actions": actions,
        "open_items": open_items,                         # structured ledger (status/supersedes/owner_history)
        "classification": classification,
        "method": method, "model": model, "profile": profile.name,
        "verified": {"facts_ok": not dropped, "dropped_facts": dropped,
                     "grounded": bool(comprehension), "backfilled": backfilled,
                     "action_candidates": len(action_cands),
                     "action_floor_uncovered": strong_uncovered,
                     "action_floor_soft_uncovered": soft_uncovered,
                     "implied_candidates": implied,
                     "implied_uncovered": implied_uncovered,
                     "open_items_unreconciled": open_items_unreconciled,
                     "operator": operator, "operator_inferred": operator_inferred,
                     "input_truncated": input_truncated,
                     "sensitivity": sensitivity, "minimized": minimized},
    }
    return rec, full


def comprehend_thread(archive, thread: dict, backend: Comprehender, profile: Profile,
                      *, max_chars: int = _AI_MAX_CHARS, model: str = "?", qaqc: bool = True,
                      operator: str = "") -> dict:
    """Build the record, then run the FULL task verification before the task counts as COMPLETE
    (Phase-3 spec step 8). Two gates: (a) the comprehension (i) is checked for faithfulness +
    completeness vs the source — re-comprehend ONCE on failure; and (b) the distilled abstract /
    one-liner / event tag (ii)-(iv) are each verified against the comprehension (i). The deterministic
    fact-check must also pass. Any failure -> `needs_review` (never silently shipped)."""
    rec, full = _build_once(archive, thread, backend, profile, max_chars, model, operator)
    if not qaqc:
        return rec

    # (a) comprehension (i) vs raw source — independent adversarial SEMANTIC review (atom recall is the
    # floor's job, not re-checked here); faithfulness + completeness; re-comprehend once on failure.
    comp = None
    for attempt in (1, 2):
        qa = backend.generate("qa", cprompts.qa_semantic(rec["comprehension"], full[:max_chars]), schema=_QA_SCHEMA) or {}
        passed = bool(qa.get("passed")) and rec["verified"]["facts_ok"]
        issues = [_issue_str(i) for i in (qa.get("issues") or [])][:6]
        if passed or attempt == 2:
            comp = {"passed": passed, "faithfulness": _f(qa.get("faithfulness")),
                    "completeness": _f(qa.get("completeness")), "issues": issues, "attempts": attempt}
            break
        try:
            rec, full = _build_once(archive, thread, backend, profile, max_chars, model, operator)   # one retry
        except Exception as e:                 # retry failed -> keep the first attempt, flag for review
            comp = {"passed": False, "faithfulness": _f(qa.get("faithfulness")),
                    "completeness": _f(qa.get("completeness")),
                    "issues": issues + [f"retry failed: {type(e).__name__}"], "attempts": attempt}
            break

    # (b) distilled (ii)-(iv) vs the comprehension (i) — the step-8 check the task must also pass
    dq = backend.generate("verify_distill",
                          cprompts.distill_qa(rec["comprehension"], rec["abstract"], rec["summary"],
                                              rec["event"]), schema=_DISTILL_QA_SCHEMA) or {}
    # FAIL CLOSED: a missing/empty distill-QA payload (backend error, unparseable {} ) must NOT score as
    # a pass — absent verification is not verification. Defaults are False, so silence routes to review.
    rec["verified"].update({"abstract_ok": bool(dq.get("abstract_ok", False)),
                            "summary_ok": bool(dq.get("summary_ok", False)),
                            "event_ok": bool(dq.get("event_ok", False))})
    distill_passed = bool(dq.get("passed", False)) and all(
        rec["verified"][k] for k in ("abstract_ok", "summary_ok", "event_ok"))

    # the TASK is COMPLETE only when BOTH the comprehension and every distilled layer verify; detected
    # sensitive content (privilege / without-prejudice / PHI) ALSO gates to human review (not inert).
    sens = rec["verified"].get("sensitivity") or []
    uncovered = rec["verified"].get("action_floor_uncovered") or []
    implied_unc = rec["verified"].get("implied_uncovered") or []
    orphans = rec["verified"].get("open_items_unreconciled") or []
    unverified = [a for a in (rec.get("actions") or []) if a.get("quote_unverified")]
    truncated = bool(rec["verified"].get("input_truncated"))
    issues = comp["issues"] + [_issue_str(i) for i in (dq.get("issues") or [])]
    if truncated:
        issues = issues + ["a very large attachment was truncated for the AI — facts still extracted by the floor; review"]
    if sens:
        issues = issues + [f"sensitive content ({', '.join(sens)}) — route to human review"]
    if uncovered:
        issues = issues + [f"{len(uncovered)} floor obligation(s) not covered by an action — review"]
    if implied_unc:
        issues = issues + [f"{len(implied_unc)} implied obligation(s) the floor caught — added as review to-dos"]
    if orphans:
        issues = issues + [f"{len(orphans)} open ledger item(s) with no matching action — review"]
    if unverified:
        issues = issues + [f"{len(unverified)} action(s) with an unverifiable quote — possible fabrication"]
    rec["qaqc"] = {**comp, "distill_passed": distill_passed, "issues": issues[:8],
                   "needs_review": (not comp["passed"]) or (not distill_passed) or truncated
                   or bool(sens) or bool(uncovered) or bool(implied_unc) or bool(orphans) or bool(unverified)}
    return rec


def select_threads(archive, *, only=None, redo=False, include_stale=True) -> list[dict]:
    """Which threads a run would process: never-comprehended (always), stale (when include_stale),
    and everything (when redo). `only` restricts to a set of thread_ids (e.g. one project / date range)."""
    done = latest_by_root(archive)               # link by stable root_id (thread_id changes on new msgs)
    threads = read_threads(archive)
    if only is not None:
        only = set(only)
        threads = [t for t in threads if t.get("thread_id") in only]
    idx = downloaded_link_index(archive) if include_stale else ({}, {})   # read link files once
    todo = []
    for t in threads:
        rec = done.get(t.get("root_id"))
        if rec is None or redo or (include_stale and is_stale(t, rec, thread_attach_count(archive, t, idx))):
            todo.append(t)
    return todo


def run_comprehension(archive, *, backend: Comprehender, profile: Profile | None = None,
                      budget_tokens: int = 200_000, log=print,
                      should_stop=lambda: False, allowed=lambda: True,
                      only=None, redo: bool = False, include_stale: bool = True,
                      limit: int | None = None, concurrency: int = 1, operator: str = "",
                      progress=lambda **k: None) -> dict:
    """Comprehend the threads that need it within a token-estimate budget. Idempotent, resumable.
    Processes never-comprehended threads, plus stale ones (changed since comprehension) when
    `include_stale`, plus everything when `redo`. `only` restricts to a set of thread_ids (one
    project, a date range). `allowed()` is the policy gate (subscription/tier plugs in here later)."""
    archive = Path(archive)
    profile = profile or get_profile()
    if not allowed():
        log("Comprehension blocked by policy gate.")
        return {"done": 0, "pending": 0, "spent": 0, "total": 0, "blocked": True}
    backend.should_stop = should_stop                # so a Stop press aborts in-flight AI calls + kills the tree
    backlog = select_threads(archive, only=only, redo=redo, include_stale=include_stale)
    todo = backlog[:limit] if limit else backlog
    total = len(todo)
    spent = done = errors = needs = 0
    last_done = ""

    def _record(t, est, rec, err):
        """Fold one thread's result into the run totals + log. Always called in THIS thread."""
        nonlocal spent, done, errors, needs, last_done
        if err is not None:
            errors += 1
            log(f"  ! {t.get('thread_id')}: {type(err).__name__}: {err}")
            return
        _append(archive, rec)
        spent += est
        done += 1
        last_done = t.get("subject") or t.get("thread_id") or ""
        c = rec["classification"]
        nr = rec.get("qaqc", {}).get("needs_review")
        needs += 1 if nr else 0
        log(f"  ✓ {t.get('thread_id')} [{t.get('n')} msgs] -> {c['domain']}/{c['category']} ({c['consensus']})"
            + (" ⚠ NEEDS-REVIEW" if nr else ""))

    def _one(t):
        return comprehend_thread(archive, t, backend, profile, model=getattr(backend, "model", "?"),
                                 operator=operator)

    if max(1, int(concurrency or 1)) <= 1:
        for t in todo:                                  # sequential (default; unchanged behaviour)
            if should_stop():
                break
            progress(done=done, total=total, current=(t.get("subject") or t.get("thread_id") or ""),
                     errors=errors, last_done=last_done)
            est = estimate_thread_tokens(archive, t)
            if done > 0 and spent + est > budget_tokens:
                log(f"Budget reached (~{spent} est. tokens) — stopping; re-run to continue.")
                break
            try:
                _record(t, est, _one(t), None)
            except Stopped:
                break                                   # operator pressed Stop — halt, don't count as error
            except Exception as e:
                _record(t, est, None, e)
    else:                                               # concurrent — per-thread work is independent + I/O-bound
        work, acc = [], 0                               # budget pre-bounded up-front (always allow the first)
        for t in todo:
            est = estimate_thread_tokens(archive, t)
            if work and acc + est > budget_tokens:
                log(f"Budget reached (~{acc} est. tokens) — stopping; re-run to continue.")
                break
            work.append((t, est)); acc += est

        inflight: dict = {}                             # thread_id -> label: what the workers are ON right now
        inflight_lock = threading.Lock()
        def _busy() -> str:                             # readable snapshot of live per-worker activity
            with inflight_lock:
                vals = list(inflight.values())
            return " · ".join(vals[:4]) + (f" +{len(vals) - 4}" if len(vals) > 4 else "")

        def _job(item):
            t, est = item
            if should_stop():
                return None
            tid = t.get("thread_id")
            with inflight_lock:
                inflight[tid] = (t.get("subject") or tid or "")[:60]
            progress(done=done, total=total, current=_busy(), errors=errors, last_done=last_done)
            try:
                return (t, est, _one(t), None)
            except Stopped:
                return None                             # aborted by Stop — not an error
            except Exception as e:                      # report per-thread; keep the rest going
                return (t, est, None, e)
            finally:
                with inflight_lock:
                    inflight.pop(tid, None)

        progress(done=0, total=total, current="", errors=0, last_done="")
        # Results are folded in THIS thread as they complete, so the counters + _append need no lock;
        # only the backend's token tally is touched by workers (made thread-safe in the comprehender).
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(concurrency)) as ex:
            for fut in concurrent.futures.as_completed([ex.submit(_job, it) for it in work]):
                r = fut.result()
                if r is None:
                    continue
                _record(*r)
                progress(done=done, total=total, current=_busy(), errors=errors, last_done=last_done)

    progress(done=done, total=total, current="", errors=errors, last_done=last_done)
    return {"done": done, "pending": len(backlog) - done, "total": total, "backlog": len(backlog),
            "errors": errors, "needs_review": needs, "spent": spent, "last_done": last_done,
            "tokens": getattr(backend, "tokens", 0), "cost": getattr(backend, "cost", 0.0), "blocked": False}
