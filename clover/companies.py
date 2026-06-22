"""Company "green book" + per-project people — deterministic, no AI.

Builds on the contacts directory (identity already resolved to email-domain firms there):
  - `list_companies`  : people grouped by firm, each with a full name + an auto/overridable **company code**
                        and the **projects the firm worked on** (from who's on each project's threads).
  - `project_contacts`: the people on one project (enriched from the directory, sorted by company).
  - `project_companies`: the firms involved in one project — "who worked together".

Company codes are generated from the full name (Globex Construction Company → GCC) and can be
overridden by the operator (→ 4C); overrides persist in `<archive>/company_codes.json`.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from . import contacts as contactsmod
from . import projects as projmod
from . import threads as threadmod

_CODES = "company_codes.json"
_NAMES = "company_names.json"
_MERGES = "company_merges.json"
_DESCS = "company_descriptions.json"
_CODE_DROP = re.compile(r"(?i)\b(?:pte|ltd|sdn|bhd|llp|llc|inc|gmbh|corp|corporation|berhad|limited|co)\b\.?")
_CODE_STOP = {"and", "of", "the", "for", "a", "an", "&"}


def names_path(archive_path) -> Path:
    return Path(archive_path) / _NAMES


def read_names(archive_path) -> dict:
    p = names_path(archive_path)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def set_name(archive_path, key: str, name: str) -> str:
    """Operator override of a firm's display name (e.g. fill in a domain-only firm). Blank clears it."""
    names = read_names(archive_path)
    name = re.sub(r"\s+", " ", (name or "")).strip()[:80]
    if name:
        names[key] = name
    else:
        names.pop(key, None)
    tmp = names_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(names, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(names_path(archive_path))
    return name


# ── company descriptions (operator's "what this firm does" note; deterministic, never AI-inferred) ──
def descs_path(archive_path) -> Path:
    return Path(archive_path) / _DESCS


def read_descriptions(archive_path) -> dict:
    p = descs_path(archive_path)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def set_description(archive_path, key: str, description: str) -> str:
    """Operator's note on what a firm does (business / industry). Blank clears it. Returns the stored text."""
    descs = read_descriptions(archive_path)
    description = re.sub(r"\s+", " ", (description or "")).strip()[:240]
    if description:
        descs[key] = description
    else:
        descs.pop(key, None)
    tmp = descs_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(descs, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(descs_path(archive_path))
    return description


# ── firm merges (operator folds one firm into another; survives Refresh) ──────
def merges_path(archive_path) -> Path:
    return Path(archive_path) / _MERGES


def read_merges(archive_path) -> dict:
    p = merges_path(archive_path)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_merge(key: str, merges: dict) -> str:
    """Follow a merge chain to its canonical key (cycle-safe)."""
    seen = set()
    while key in merges and key not in seen:
        seen.add(key)
        key = merges[key]
    return key


def set_merge(archive_path, from_key: str, to_key: str) -> bool:
    """Fold firm `from_key` into `to_key`. Refuses self/cycles. Persists to company_merges.json."""
    from_key, to_key = (from_key or "").strip(), (to_key or "").strip()
    merges = read_merges(archive_path)
    if not from_key or not to_key or from_key == to_key:
        return False
    if _resolve_merge(to_key, merges) == from_key:          # would create a cycle
        return False
    merges[from_key] = to_key
    tmp = merges_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merges, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(merges_path(archive_path))
    return True


def unmerge(archive_path, from_key: str) -> bool:
    merges = read_merges(archive_path)
    if from_key not in merges:
        return False
    merges.pop(from_key, None)
    tmp = merges_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merges, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(merges_path(archive_path))
    return True


# ── company codes (auto + operator override) ──────────────────────────────────
def codes_path(archive_path) -> Path:
    return Path(archive_path) / _CODES


def read_codes(archive_path) -> dict:
    p = codes_path(archive_path)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def set_code(archive_path, key: str, code: str) -> str:
    """Set (or clear, if blank) the operator override for a firm's code. Returns the stored code."""
    codes = read_codes(archive_path)
    code = re.sub(r"\s+", "", (code or "")).upper()[:8]
    if code:
        codes[key] = code
    else:
        codes.pop(key, None)
    tmp = codes_path(archive_path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(codes, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(codes_path(archive_path))
    return code


def code_in_use(archive_path, code: str, exclude_key: str = "") -> str:
    """The name of the company already using `code` (other than exclude_key), or '' — for the no-duplicate
    rule: two companies must never share a code."""
    code = re.sub(r"\s+", "", (code or "")).upper()
    if not code:
        return ""
    for c in list_companies(archive_path)["companies"]:
        if c["key"] != exclude_key and (c["code"] or "").upper() == code:
            return c["name"]
    return ""


def qaqc(archive_path) -> list[dict]:
    """Deterministic quality gate for the green book — surfaces what an operator should eyeball/fix:
    duplicate codes, odd company names, and likely-duplicate firms (similar names, not auto-merged)."""
    # name-less ("name not detected") firms are excluded from QAQC entirely — each is already tagged on its card
    book = [c for c in list_companies(archive_path)["companies"] if not c["unverified"]]
    issues: list[dict] = []
    seen: dict[str, str] = {}
    for c in book:
        cu = (c["code"] or "").upper()
        if cu and cu in seen:
            issues.append({"type": "duplicate code", "detail": f"“{cu}” is on both {seen[cu]} and {c['name']}"})
        else:
            seen[cu] = c["name"]
    for c in book:
        nm = c["name"]
        if c["unverified"]:
            continue
        bad = (re.match(r"(?i)^(?:for|at|with|to|from|by|dear|re|fw|fwd)\b", nm) or "[" in nm or "]" in nm
               or len(re.sub(r"[^A-Za-z0-9]", "", nm)) < 2
               or not contactsmod.company_key(nm)                  # bare legal suffix ('Pte Ltd')
               or (" " in nm and re.fullmatch(r"[^a-z]*", nm)))    # multi-word ALL-CAPS (acronyms exempt)
        if bad:
            issues.append({"type": "check name", "detail": f"“{nm}” looks off — edit it (✎)"})
    named = [c["name"] for c in book if not c["unverified"]]
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            ka, kb = contactsmod.company_key(named[i]), contactsmod.company_key(named[j])
            if ka and kb and ka[:5] == kb[:5] and (ka == kb or contactsmod._close(ka, kb, 2)):
                issues.append({"type": "possible duplicate",
                               "detail": f"“{named[i]}” ↔ “{named[j]}” — same firm? merge via ✎"})
    return issues


def gen_code(name: str) -> str:
    """Deterministic abbreviation: initials of the significant words (legal suffixes/stopwords dropped)."""
    n = _CODE_DROP.sub(" ", name or "")
    n = re.sub(r"\(.*?\)", " ", n)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", n) if w and w.lower() not in _CODE_STOP]
    code = "".join(w[0] for w in words).upper()
    if len(code) < 2:
        flat = re.sub(r"[^A-Za-z0-9]", "", n) or re.sub(r"[^A-Za-z0-9]", "", name or "")
        code = (flat[:4] or "NA").upper()
    return code[:6]


# ── helpers ───────────────────────────────────────────────────────────────────
def _dom(eaddr: str) -> str:
    return eaddr.split("@", 1)[1].strip().lower() if "@" in eaddr else ""


def _directory(archive_path) -> list[dict]:
    return list(contactsmod.read_contacts(archive_path))


def _key_maps(directory: list[dict], merges: dict | None = None):
    """email/alias -> company_key, and domain -> company_key (for participants not in the directory).
    Keys are resolved through operator merges so 'firms involved' reflects merged firms."""
    merges = merges or {}
    by_email, by_domain = {}, {}
    for c in directory:
        k = _resolve_merge(c.get("company_key", ""), merges)
        by_email[c["email"]] = k
        for a in c.get("aliases", []):
            by_email[a] = k
        dom = c.get("domain", "")
        if dom and k and dom not in contactsmod._FREEMAIL:
            by_domain.setdefault(dom, k)
    return by_email, by_domain


def _consensus_name(group: list[dict]) -> str:
    """Fullest detected company name across the group's signatures (frequent, then longest). '' if none."""
    counts: dict[str, int] = {}
    for c in group:
        if c.get("company"):
            counts[c["company"]] = counts.get(c["company"], 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0] if counts else ""


def _participants_by_thread(archive_path) -> dict[str, list[str]]:
    return {t.get("thread_id"): (t.get("participants") or []) for t in threadmod.read_threads(archive_path)}


def _firmkey_for(email_: str, by_email: dict, by_domain: dict) -> str:
    return by_email.get(email_) or by_domain.get(_dom(email_), "")


# ── the green book ────────────────────────────────────────────────────────────
def list_companies(archive_path) -> dict:
    """{companies:[{key,name,code,count,people,projects}], individuals:int}. `projects` = the names of
    projects any of the firm's people are on (deterministic, from thread participants)."""
    arch = Path(archive_path)
    directory = _directory(arch)
    by_email, by_domain = _key_maps(directory, read_merges(arch))

    # firm -> projects worked on (who appears on each project's threads)
    parts = _participants_by_thread(arch)
    company_projects: dict[str, set] = defaultdict(set)
    for p in projmod.list_projects(arch):
        firmkeys = {fk for th in p.get("threads", [])
                    for e in parts.get(th.get("thread_id"), [])
                    if (fk := _firmkey_for(e, by_email, by_domain))}
        for fk in firmkeys:
            company_projects[fk].add(p["name"])

    merges = read_merges(arch)
    groups: dict[str, list[dict]] = defaultdict(list)
    individuals: list[dict] = []
    for c in directory:
        k = _resolve_merge(c.get("company_key", ""), merges)   # operator merges fold firms together
        if k:
            groups[k].append(c)
        else:
            individuals.append(c)

    code_ov, name_ov, desc_ov = read_codes(arch), read_names(arch), read_descriptions(arch)
    out, used_codes = [], {}
    for k, group in groups.items():
        domain = next((c.get("domain") for c in group if c.get("domain")), "") or k
        detected = _consensus_name(group)
        # name: operator override > detected signature name > UNVERIFIED (show the domain, don't fake a name)
        if name_ov.get(k):
            name, unverified = name_ov[k], False
        elif detected:
            name, unverified = detected, False
        else:
            name, unverified = domain, True
        code = code_ov.get(k) or gen_code(name if not unverified else domain.split(".")[0])
        if k not in code_ov:                                # keep auto-codes unique
            base, n = code, 2
            while used_codes.get(code) not in (None, k):
                code = f"{base}{n}"; n += 1
        used_codes[code] = k
        people = sorted(group, key=lambda c: (c.get("name") or c.get("email") or "").casefold())
        out.append({"key": k, "name": name, "code": code, "count": len(group),
                    "unverified": unverified, "domain": domain, "description": desc_ov.get(k, ""),
                    "people": people, "projects": sorted(company_projects.get(k, set()), key=str.casefold)})
    out.sort(key=lambda g: (g["unverified"], g["name"].casefold()))   # named firms first, then unverified
    individuals.sort(key=lambda c: (c.get("name") or c.get("email") or "").casefold())
    return {"companies": out, "individuals": individuals, "individuals_count": len(individuals)}


def project_contacts(archive_path, key: str) -> list[dict]:
    """Everyone on project `key`'s threads, enriched from the directory, sorted by company."""
    arch = Path(archive_path)
    proj = projmod.get_project(arch, key)
    if not proj:
        return []
    directory = {c["email"]: c for c in _directory(arch)}
    for c in list(directory.values()):
        for a in c.get("aliases", []):
            directory.setdefault(a, c)
    parts = _participants_by_thread(arch)
    out, seen = [], set()
    for th in proj.get("threads", []):
        for e in parts.get(th.get("thread_id"), []):
            if not e or e in seen:
                continue
            seen.add(e)
            out.append(directory.get(e) or contactsmod._blank(e))
    return contactsmod._sort(out)


def project_companies(archive_path, key: str) -> list[dict]:
    """Firms involved in project `key` — 'who worked together'. [{name, code, count}]."""
    arch = Path(archive_path)
    people = project_contacts(arch, key)
    book = {c["key"]: c for c in list_companies(arch)["companies"]}
    merges = read_merges(arch)
    firms: dict[str, dict] = {}
    for p in people:
        k = _resolve_merge(p.get("company_key", ""), merges)   # follow merges to the canonical firm
        if not k:
            continue
        b = book.get(k)
        f = firms.setdefault(k, {"name": (b or {}).get("name", p.get("company") or k),
                                 "code": (b or {}).get("code", ""), "count": 0})
        f["count"] += 1
    return sorted(firms.values(), key=lambda f: (-f["count"], f["name"].casefold()))
