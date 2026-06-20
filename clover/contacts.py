"""Contact directory — one cleaned record per person, deterministic, no AI.

Pipeline (all rule-based, idempotent, re-runnable):
  1. **Headers** (`_index.jsonl` From): name + email + how often they sent. Full coverage.
  2. **Own-signature parse**: from each sender's NEWEST message, take ONLY the text the sender authored
     (everything above the first quoted-reply / disclaimer boundary), then read phone / company / title from
     the signature block. Quoted history, salutations ("Dear …"), confidentiality + copyright footers are
     dropped — they were the source of the garbage in the first build.
  3. **Identity = email domain.** A person's employer is their email domain's organisation (every
     `@globex.com` person = GCC), named by the fullest consensus signature seen on that domain. A stray
     firm scraped from one quoted line is out-voted. Free-mail domains (gmail, singnet, …) are not firms.
  4. **Auto-clean** typos / duplicate addresses: fold a near-miss or low-volume address into its canonical
     sibling (same person), summing counts and keeping the alternates as `aliases`.

`rebuild()` does the full pass and caches `contacts.jsonl`; `read_contacts()` returns the cache (falling back
to the cheap header-only `consolidate()` when there's no cache). Company grouping / codes live in companies.py.
"""
from __future__ import annotations

import email
import html as _html
import json
import re
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .archive import read_index
from .comprehend import read_comprehensions

_CONTACT_PATH = "contacts.jsonl"

# domains that are mailbox providers, not employers
_FREEMAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.sg", "outlook.com", "outlook.sg",
    "live.com", "live.com.sg", "yahoo.com", "yahoo.com.sg", "yahoo.com.cn", "ymail.com",
    "icloud.com", "me.com", "msn.com", "aol.com", "protonmail.com", "proton.me", "gmx.com",
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com", "singnet.com.sg", "pacific.net.sg",
    "starhub.net.sg", "mail.com", "zoho.com",
}
# local-parts that are automated systems / bulk senders, not people
_AUTOMATED = re.compile(
    r"(?i)(?:^|[._-])(?:no-?reply|do-?not-?reply|donotreply|noreply|mailer-?daemon|postmaster|"
    r"notifications?|notify|bounce[sd]?|auto(?:mated|reply)?|system|daemon|drive-shares?|"
    r"calendar-notification|invitations?|alerts?|support-?bot|newsletters?|marketing|rewards?|"
    r"promo(?:tions?)?|updates?|subscriptions?|unsubscribe|digest|mailings?|messaging)(?:$|[._-])"
    r"|^(?:no_?reply|noreply|mailer|webmaster|donotreply)$")
# sending/notification SUBDOMAINS (leftmost label) + known bulk-email providers -> not a person's own mailbox
_BULK_LABEL = re.compile(r"(?i)(?:notify|noreply|no-reply|mailer|bounce|mktg|marketing|newsletter|email)"
                         r"|^(?:e|em|mail|mails|news|send|sends|smtp|reply|mailing|cmail)$")
_BULK_PROVIDER = re.compile(r"(?i)(?:mailchimp|sendgrid|amazonses|sparkpost\w*|mandrill\w*|mcsv|hubspot|"
                            r"salesforce|intercom|zendesk|relay\.app)|(?:^|\.)zoom\.us$")

# signature parsing
_QUOTE_LINE = re.compile(r"""(?xi)
    ^\s*>                                   # quoted line
  | ^\s*On\s.{0,200}\bwrote:\s*$            # "On <date>, <name> wrote:"
  | ^\s*-{2,}\s*original\smessage\s*-{2,}   # Outlook separator
  | ^\s*_{5,}\s*$                           # Outlook divider rule
  | ^\s*(?:from|发件人|de|von|van)\s*[:：]\s.{0,200}$   # quoted header block start
  | ^\s*(?:sent|发送时间|date)\s*[:：]\s.{0,120}$
""")
_SIGNOFF = re.compile(
    r"(?i)^\s*(?:best\s+regards|warm(?:est)?\s+regards|kind\s+regards|with\s+(?:kind\s+)?regards|"
    r"regards|rgds|thanks?(?:\s*(?:&|and)\s*regards)?|thank\s+you|sincerely|yours\s+\w+|cheers|"
    r"b\.?\s*r\.?|with\s+thanks)\b[\s,.!-]*$")
_SKIP_LINE = re.compile(
    r"(?i)\b(?:confidential|in\s+error|please\s+notify|intended\s+(?:solely\s+)?(?:for|recipient|only)|"
    r"privileged|all\s+rights\s+reserved|unsubscribe|disclaimer|do\s+not\s+(?:copy|disclose)|"
    r"this\s+(?:e-?mail|message|communication))\b|^\s*(?:dear|hi|hello|attn|to)\b|"
    r"©|&copy;|\bcopyright\b|\bsent\s+from\s+my\b")
_PHONE_LABEL = re.compile(r"(?i)\b(?:mobile|mob|hp|cell|tel|phone|direct|did|contact|m|t|p|d)\b")
_FAX = re.compile(r"(?i)\bfax\b")
_PHONE = re.compile(r"(?:(?:\+|00)\d[\d\s().-]{6,}\d|\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4})")
_TITLE = re.compile(
    r"(?i)\b(?:Senior |Snr |Junior |Jr |Chief |Deputy |Assistant |Asst |Principal |Lead |Head\s+of\s+)?"
    r"(?:Managing\s+)?(?:Project\s+|Site\s+|Sales\s+|Account\s+|General\s+|Operations\s+|Business\s+)?"
    r"(?:Manager|Director|Engineer|Executive|Officer|President|CEO|CFO|CTO|COO|Consultant|Architect|"
    r"Surveyor|Coordinator|Supervisor|Specialist|Administrator|Analyst|Partner|Associate|Designer|"
    r"Technician|Draftsman|Drafter|Foreman|Estimator|Planner|Secretary|Accountant)s?\b")
# a firm name = a phrase that ENDS in a legal suffix (anchored to end-of-line so prose like "Limited Offer" fails)
_LEGAL = (r"(?:Pte\.?\s*Ltd|Sdn\.?\s*Bhd|Co\.?\s*,?\s*Ltd|Company\s+Limited|Berhad|Limited|Ltd|LLP|LLC|"
          r"L\.?L\.?C|Inc|GmbH|Corp(?:oration)?|S\.?A\.?|N\.?V\.?|Pte)")
_COMPANY_TAIL = re.compile(
    r"(?i)([A-Z0-9][\w&.,'’()\-/ ]{1,58}?\b" + _LEGAL + r")\b\.?(?:\s*\([^)]{0,40}\))?[\s.,;:]*$")
_WS = re.compile(r"\s+")
_LEADING_JUNK = re.compile(r"(?i)^(?:(?:for|at|with|to|from|by|of|and|dear|attn|re|fw|fwd|via|c/o|c-o)\b[\s,:./-]*)+")
# trailing legal / generic-company words stripped to form the grouping key (so 'Company Limited' and
# 'Company Ltd' collapse to the SAME key — the cause of the GCC split)
_KEY_LEGAL = {"pte", "ltd", "sdn", "bhd", "llp", "llc", "inc", "gmbh", "corp", "corporation",
              "limited", "berhad", "company", "co", "plc", "pl", "sa", "nv", "bv", "ag"}
# canonical casing for legal words when tidying display capitalisation
_LEGAL_CASE = {"pte": "Pte", "ltd": "Ltd", "sdn": "Sdn", "bhd": "Bhd", "llp": "LLP", "llc": "LLC",
               "inc": "Inc", "gmbh": "GmbH", "co": "Co", "corp": "Corp", "corporation": "Corporation",
               "limited": "Limited", "berhad": "Berhad", "company": "Company", "plc": "PLC"}


def _norm_company_caps(name: str) -> str:
    """Tidy capitalisation: SHOUTED multi-word names → Title Case (MEGA BUILD CO PTE LTD → Mega Build Co
    Pte Ltd), legal words canonicalised (Pte Ltd), single short all-caps tokens kept as acronyms (ABC, GCC,
    XYZ), already-mixed names left alone."""
    if not name:
        return name
    toks = name.split()
    def alpha(t):
        return re.sub(r"[^A-Za-z]", "", t)
    name_toks = [t for t in toks if alpha(t) and alpha(t).lower() not in _KEY_LEGAL]
    single_acronym = len(name_toks) == 1 and alpha(name_toks[0]).isupper() and len(alpha(name_toks[0])) <= 6
    name_shouted = bool(name_toks) and all(alpha(t).isupper() for t in name_toks if alpha(t))
    out = []
    for t in toks:
        k = alpha(t).lower()
        if k in _LEGAL_CASE:                                   # Pte / Ltd / Co. etc.
            out.append(t.replace(alpha(t), _LEGAL_CASE[k]))
        elif single_acronym:
            out.append(t)
        elif name_shouted:
            out.append(re.sub(r"[A-Za-z]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), t))
        else:
            out.append(t)
    return " ".join(out)


def _clean_company(raw: str) -> str:
    """Tidy a parsed company name: collapse spaces, strip edge punctuation, drop leading connector words,
    standardise capitalisation. 'for A&B Foundation Specialist Pte Ltd' -> 'A&B Foundation Specialist Pte Ltd'."""
    n = _WS.sub(" ", raw or "").strip(" .,-|'’")
    n = _LEADING_JUNK.sub("", n).strip(" .,-|'’")
    return _norm_company_caps(n)


def contacts_path(archive_path) -> Path:
    return Path(archive_path) / _CONTACT_PATH


# ── email body → the sender's own authored text ───────────────────────────────
def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return _html.unescape(s)


def _message_text(eml_path: Path) -> str:
    try:
        with eml_path.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
        if part.get_content_subtype() == "html":
            text = _html_to_text(text)
        return text
    except Exception:
        return ""


def _strip_quoted(text: str) -> str:
    """Everything above the first quoted-reply / forwarded-header boundary — the sender's own text."""
    out = []
    for ln in (text or "").splitlines():
        if _QUOTE_LINE.match(ln):
            break
        out.append(ln)
    return "\n".join(out)


def _clean_marker(ln: str) -> str:
    return re.sub(r"^[>*_|\-\s]+", "", ln).strip().strip("*_").strip()


def _signature_zone(own_text: str) -> list[str]:
    """The lines that make up the signature: after the last sign-off, else the tail. Markers stripped,
    salutations / disclaimers / copyright dropped."""
    raw = [ln for ln in (own_text or "").splitlines()]
    nonblank = [(i, ln) for i, ln in enumerate(raw) if ln.strip()]
    start = 0
    for i, ln in nonblank:
        if _SIGNOFF.match(ln):
            start = i + 1                                   # zone begins after the LAST sign-off
    zone_src = raw[start:] if start else [ln for _, ln in nonblank[-10:]]
    zone = []
    for ln in zone_src:
        c = _clean_marker(ln)
        if not c or _SKIP_LINE.search(c):
            continue
        zone.append(c)
        if len(zone) >= 12:
            break
    return zone


def parse_signature(text: str) -> dict:
    """{phone, company, position} from a sender's own text. Phone is reliable; company/title best-effort
    but only from the signature zone (never from quotes, salutations or footers)."""
    out = {"phone": "", "company": "", "position": ""}
    zone = _signature_zone(_strip_quoted(text))
    if not zone:
        return out
    blob = "\n".join(zone)

    for i, ln in enumerate(zone):                           # company: first legal-suffix line in the zone
        m = _COMPANY_TAIL.search(ln)
        if not m:
            continue
        name = _clean_company(m.group(1))
        if not company_key(name) and i > 0:                 # captured only a bare 'Pte Ltd' -> prepend prev line
            m2 = _COMPANY_TAIL.search(zone[i - 1].strip(" .,-|'’") + " " + ln)
            if m2:
                name = _clean_company(m2.group(1))
        if company_key(name) and len(name) >= 3 and not name.lower().startswith(("re ", "fw ", "fwd ")):
            out["company"] = name
            break

    mt = _TITLE.search(blob)                                # title anywhere in the zone
    if mt:
        out["position"] = mt.group(0).strip()

    for ln in zone:                                         # phone: prefer a labelled, non-fax line
        if _FAX.search(ln):
            continue
        mp = _PHONE.search(ln)
        if mp:
            digits = re.sub(r"\D", "", mp.group(0))
            if 7 <= len(digits) <= 15:
                out["phone"] = mp.group(0).strip(" .-")
                if _PHONE_LABEL.search(ln):
                    break                                   # labelled phone wins; else keep first plausible
    return out


# ── company-name normalisation (shared key) ───────────────────────────────────
def company_key(name: str) -> str:
    """Canonical grouping key: drop a trailing run of legal / generic-company words, punctuation, case.
    'Company Limited' and 'Company Ltd' both collapse to the same key. '' if nothing meaningful remains
    (e.g. a bare 'Pte Ltd')."""
    if not name:
        return ""
    n = re.sub(r"\(.*?\)", " ", name)                       # drop (Singapore Branch) etc.
    words = re.findall(r"[A-Za-z0-9&]+", n.lower())
    while words and words[-1] in _KEY_LEGAL:               # strip trailing legal/company words
        words.pop()
    return "".join(words)


def _date_key(row) -> float:
    try:
        return parsedate_to_datetime(row.get("date", "")).timestamp()
    except Exception:
        return 0.0


def _domain(eaddr: str) -> str:
    return eaddr.split("@", 1)[1].strip().lower() if "@" in eaddr else ""


def _is_system_sender(eaddr: str) -> bool:
    """True for automated / bulk / notification senders (no-reply, marketing, MS Rewards, Zoom, ESPs).
    Free-mail domains are exempt so a real person on gmail/hotmail is never mistaken for a bot."""
    local, dom = (eaddr.split("@", 1) + [""])[:2]
    if _AUTOMATED.search(local):
        return True
    if dom in _FREEMAIL:
        return False
    if _BULK_PROVIDER.search(dom):
        return True
    return bool(_BULK_LABEL.search(dom.split(".", 1)[0]))   # leftmost label = a sending subdomain?


def _blank(email_, name="", count=0):
    return {"email": email_, "name": name, "position": "", "company": "", "company_key": "",
            "domain": _domain(email_), "phone": "", "count": count, "aliases": []}


# ── cheap header-only fallback (cold start; no .eml reads) ─────────────────────
def consolidate(archive_path) -> list[dict]:
    arch = Path(archive_path)
    people: dict[str, dict] = {}
    seen = set()
    for row in read_index(arch):
        rid = row.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        addrs = getaddresses([row.get("from", "") or ""])
        if not addrs:
            continue
        name, addr = addrs[0]
        e = (addr or "").strip().lower()
        if "@" not in e or _is_system_sender(e):
            continue
        p = people.setdefault(e, _blank(e))
        p["count"] += 1
        cn = _clean_name(name, e)
        if cn and len(cn) > len(p["name"]):
            p["name"] = cn
    for p in people.values():
        if not p["name"]:
            p["name"] = _name_from_local(p["email"])
    return _sort(list(people.values()))


# ── full rebuild (signatures + domain identity + auto-clean) ───────────────────
def rebuild(archive_path) -> list[dict]:
    arch = Path(archive_path)
    rows = read_index(arch)

    people: dict[str, dict] = {}
    newest: dict[str, dict] = {}
    seen = set()
    for row in rows:
        rid = row.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        addrs = getaddresses([row.get("from", "") or ""])
        if not addrs:
            continue
        name, addr = addrs[0]
        e = (addr or "").strip().lower()
        if "@" not in e or _is_system_sender(e):
            continue
        p = people.setdefault(e, _blank(e))
        p["count"] += 1
        cn = _clean_name(name, e)                          # 'Dan Goh | Initech SG' -> 'Dan Goh'
        if cn and len(cn) > len(p["name"]):
            p["name"] = cn
        if e not in newest or _date_key(row) >= _date_key(newest[e]):
            newest[e] = row

    # parse each sender's own-signature once (newest message); keep the zone + signature name
    sigs: dict[str, dict] = {}
    zones: dict[str, list] = {}
    signame: dict[str, str] = {}
    for e, row in newest.items():
        own = _strip_quoted(_message_text(arch / row.get("path", "")))
        sigs[e] = parse_signature(own)
        zones[e] = _signature_zone(own)
        signame[e] = _signature_name(zones[e])

    # OWN-SIGNATURE check: replies/forwards often embed ANOTHER person's signature (no '>' markers). Trust a
    # parsed company ONLY if the signature's name matches the sender (their own) — that lets Alex Tan →
    # Hooli even on a @globex.com address, while a quoted Ben/Carol block (name ≠ sender) is dropped.
    own_set: set = set()
    for e, sig in list(sigs.items()):
        if not sig.get("company"):
            continue
        rel = _own_signature(people[e]["name"], e, signame.get(e, ""))
        if rel is True:
            own_set.add(e)
        elif rel is False:                                 # clearly someone else's signature -> discard block
            sigs[e] = {"phone": "", "company": "", "position": ""}
            zones[e] = []
    # for ambiguous (name couldn't be compared) companies, keep the domain-ownership guard as a backstop
    owner: dict[str, dict[str, int]] = {}
    for e, sig in sigs.items():
        ck = company_key(sig.get("company", ""))
        if ck:
            owner.setdefault(ck, {})[_domain(e)] = owner.setdefault(ck, {}).get(_domain(e), 0) + 1
    for e, sig in sigs.items():
        if e in own_set:
            continue
        ck = company_key(sig.get("company", ""))
        if ck and owner[ck].get(_domain(e), 0) < max(owner[ck].values()):
            sigs[e] = {"phone": "", "company": "", "position": ""}
            zones[e] = []

    # domain → firm: consensus full name across that domain's TRUSTED signatures (corporate domains only).
    # A company names the domain only if CORROBORATED (2+ senders) or CONSISTENT with the domain — a lone
    # mismatched signature (e.g. Brand360 on @vendor.com) must NOT rename the domain.
    dom_names: dict[str, dict[str, int]] = {}
    for e, sig in sigs.items():
        dom = _domain(e)
        if dom and dom not in _FREEMAIL and sig.get("company"):
            dom_names.setdefault(dom, {})[sig["company"]] = dom_names.setdefault(dom, {}).get(sig["company"], 0) + 1
    dom_own: dict[str, set] = {}                  # companies backed by a name-verified (own) signer
    for e in own_set:
        c, dom = sigs[e].get("company"), _domain(e)
        if c and dom and dom not in _FREEMAIL:
            dom_own.setdefault(dom, set()).add(c)
    dom_firm: dict[str, str] = {}
    for dom, counts in dom_names.items():
        core = re.sub(r"[^a-z0-9]", "", dom.split(".", 1)[0].lower())
        owned = dom_own.get(dom, set())
        trusted = {nm: c for nm, c in counts.items()
                   if nm in owned or c >= 2 or _consistent(company_key(nm), core)}
        if trusted:
            dom_firm[dom] = _pick_firm_name(trusted)

    # fallback for corporate domains with no legal-suffix company: a signature line that MATCHES the domain
    # (e.g. "Contoso" on @contoso.com). Signature-confirmed — not a name fabricated from the domain.
    for e, zlines in zones.items():
        dom = _domain(e)
        if not dom or dom in _FREEMAIL or dom in dom_firm:
            continue
        hit = _company_from_domain(zlines, dom)
        if hit:
            dom_firm[dom] = hit

    for e, p in people.items():
        sig = sigs.get(e, {})
        if sig.get("phone"):
            p["phone"] = sig["phone"]
        if sig.get("position"):
            p["position"] = sig["position"]
        # name: header (cleaned) > own-signature name (cleaned) > derived from the email local-part
        if not p["name"] and e in own_set:
            p["name"] = _clean_name(signame.get(e, ""), e)
        if not p["name"]:
            p["name"] = _name_from_local(e)
        dom = p["domain"]
        if e in own_set and sig.get("company") and dom and dom not in _FREEMAIL:
            p["company"] = sig["company"]                  # the person's OWN signature wins over the domain
            p["company_key"] = company_key(sig["company"]) or dom
        elif dom and dom not in _FREEMAIL:                 # employer = domain consensus (no fabrication)
            p["company"] = dom_firm.get(dom, "")
            p["company_key"] = company_key(p["company"]) or dom
        else:                                              # free-mail / no domain -> Individual (no firm)
            p["company"] = ""
            p["company_key"] = ""

    # AI comprehension contacts can only ADD a phone/title for a person already known (never a company)
    for c in read_comprehensions(arch):
        for k in (c.get("contacts") or []):
            e = (k.get("email") or "").strip().lower()
            if e in people:
                if not people[e]["position"] and k.get("position"):
                    people[e]["position"] = str(k["position"]).strip()
                if not people[e]["phone"] and k.get("phone"):
                    people[e]["phone"] = str(k["phone"]).strip()

    out = _dedupe(list(people.values()))
    _merge_similar_companies(out)                          # fold 'Hooli Holding' into 'Hooli Holdings' etc.
    out = _sort(out)
    tmp = contacts_path(arch).with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for p in out:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    tmp.replace(contacts_path(arch))
    return out


def read_contacts(archive_path) -> list[dict]:
    p = contacts_path(archive_path)
    if p.exists():
        out = []
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        if out:
            return out
    return consolidate(archive_path)


# ── helpers ───────────────────────────────────────────────────────────────────
def _pick_firm_name(counts: dict[str, int]) -> str:
    """Fullest consensus name for a domain: prefer frequent AND long (avoid abbreviations)."""
    if not counts:
        return ""
    best = max(counts.items(), key=lambda kv: (kv[1] >= 2, len(kv[0]) >= 6, kv[1], len(kv[0])))
    return best[0]


def _name_tokens(s: str) -> set:
    return {w for w in re.sub(r"[^a-z]", " ", (s or "").lower()).split() if len(w) >= 2}


_GENERIC_NAME = re.compile(r"(?i)\b(?:team|support|dept|department|sales|admin|service|desk|group|division|"
                           r"office|hotline|enquir\w*|customer|projects?|the\s+\w+)\b|^\s*(?:your|the|dear|hi|hello)\b")


def _signature_name(zone: list) -> str:
    """The person's name line in a signature — the first top line that isn't a company / title / phone, and
    isn't a generic 'support team' / 'your projects' line."""
    for ln in zone[:4]:
        if _COMPANY_TAIL.search(ln) or _TITLE.search(ln) or _PHONE.search(ln) or _GENERIC_NAME.search(ln):
            continue
        words = [w for w in re.sub(r"[^A-Za-z ]", " ", ln).split() if len(w) >= 2]
        if 1 <= len(words) <= 4:
            return ln.strip()
    return ""


def _own_signature(sender_name: str, email_: str, sig_name: str):
    """Is the parsed signature the SENDER's own? True / False (clearly someone else's) / None (can't tell).
    This lets a person's own signature (Alex Tan → Hooli, even on a @globex.com address) be trusted,
    while a quoted/forwarded foreign signature (name ≠ sender) is rejected. Role mailboxes (no person
    name, e.g. projects@) are never 'own' — they can't be verified."""
    sig_t = _name_tokens(sig_name)
    snd_t = _name_tokens(sender_name)
    if not snd_t:
        snd_t = {t for t in _name_tokens(re.sub(r"[._-]", " ", email_.split("@")[0])) if t not in _ROLE_WORDS}
    if not sig_t or not snd_t:
        return None
    matched = snd_t & sig_t
    if len(matched) >= 2 or (matched and (len(snd_t) <= 1 or len(sig_t) <= 1)):
        return True
    return False


def _consistent(company_key_: str, domain_core: str) -> bool:
    """A signature company is 'consistent' with the email domain when their cores overlap (northwind ~ northwind.com).
    Used to trust a LONE signature for naming a domain; mismatches (brand360 on vendor.com) are not."""
    return bool(company_key_) and len(domain_core) >= 3 and (domain_core in company_key_ or company_key_ in domain_core)


_ROLE_WORDS = {"projects", "project", "sales", "admin", "info", "enquiry", "enquiries", "accounts",
               "account", "hr", "support", "finance", "contracts", "contract", "tender", "tenders",
               "purchasing", "purchase", "procurement", "general", "office", "reception", "mail",
               "service", "services", "team", "marketing", "billing", "qs", "qaqc", "draughting",
               "drafting", "design", "engineering", "cost", "commercial", "ops", "operations"}


def _clean_name(raw: str, email_: str) -> str:
    """A real person's name from a header/signature line — strip '| Company' / '(Company)' tags, and reject
    domains, role mailboxes, and name==email-local (so 'Dan Goh | Initech SG' → 'Dan Goh',
    'VENDOR.COM' → '', 'projects' → '')."""
    n = (raw or "").strip().strip("\"'")
    n = re.split(r"\s*[|/]\s*", n)[0].strip()                  # drop '| Initech SG' etc.
    n = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", n).strip()   # drop a trailing (Branch) / [..]
    low = n.lower()
    if not n or "@" in low or low in _ROLE_WORDS:
        return ""
    if re.search(r"(?:^|[.\s])(?:com|net|org|sg|my|co|io|gov|edu|biz|info)\s*$", low):
        return ""                                             # ends in a TLD ("VENDOR. COM") -> a domain
    # a real name may legitimately equal the local-part (albert_tang), so only reject the full domain
    if re.sub(r"[^a-z0-9]", "", low) == re.sub(r"[^a-z0-9]", "", email_.split("@")[1].lower()):
        return ""
    return n


def _name_from_local(email_: str) -> str:
    """Last-resort person name from the email local-part (the name IS in the email): alex.tan → Alex Tan."""
    local = email_.split("@")[0]
    if local.lower() in _ROLE_WORDS or any(ch.isdigit() for ch in local):
        return ""
    parts = [p for p in re.split(r"[._-]+", local) if p.isalpha() and len(p) >= 2]
    if not parts or len(parts) > 3 or any(p.lower() in _ROLE_WORDS for p in parts):
        return ""
    return " ".join(p.capitalize() for p in parts)


def _company_from_domain(zone_lines: list, domain: str) -> str:
    """A signature line that contains the domain's core label (e.g. 'Contoso' on @contoso.com) — the firm
    name confirmed by the domain, not fabricated from it. Returns the cleanest such line, or ''."""
    core = re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0].lower())
    if len(core) < 3:
        return ""
    best = ""
    for ln in zone_lines:
        if "@" in ln or "[" in ln or "]" in ln or _SKIP_LINE.search(ln) \
           or re.search(r"(?i)https?://|www\.|\b(?:logo|image|banner|cid)\b", ln):
            continue
        words = ln.split()
        if not words or len(words) > 4:                          # a firm name is short; prose is long
            continue
        norm = re.sub(r"[^a-z0-9]", "", ln.lower())
        first = re.sub(r"[^a-z0-9]", "", words[0].lower())
        # the line is ABOUT the firm: it starts with (or is) the domain core, and is barely longer than it
        if core in norm and len(norm) <= len(core) + 16 and (core in first or first in core or len(norm) <= len(core) + 6):
            cand = ln.strip(" .,-|·•").strip()
            if 2 <= len(cand) <= 48 and (not best or len(cand) < len(best)):
                best = cand
    return _norm_company_caps(best)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def _close(a: str, b: str, dist: int = 2) -> bool:
    if a == b or abs(len(a) - len(b)) > dist:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= dist


def _merge_similar_companies(people: list[dict]) -> None:
    """Fold near-duplicate company names into one ('Hooli Holding' → 'Hooli Holdings'). Clusters real
    (alnum, ≥6-char) company keys that share a 4-char prefix AND are within edit-distance 2; the canonical
    is the variant with the most people, named by the commonest/longest spelling. Domain-only keys are left."""
    from collections import defaultdict
    stat: dict[str, dict] = {}
    for p in people:
        k = p.get("company_key", "")
        if k and re.fullmatch(r"[a-z0-9]+", k) and len(k) >= 6:
            s = stat.setdefault(k, {"count": 0, "names": defaultdict(int)})
            s["count"] += p.get("count", 1) or 1
            if p.get("company"):
                s["names"][p["company"]] += 1
    keys = list(stat)
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            if a[:4] == b[:4] and _close(a, b, 2):
                parent[find(a)] = find(b)
    clusters: dict[str, list] = defaultdict(list)
    for k in keys:
        clusters[find(k)].append(k)
    remap, cname = {}, {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        canon = max(members, key=lambda k: stat[k]["count"])
        allnames: dict[str, int] = defaultdict(int)
        for k in members:
            for nm, c in stat[k]["names"].items():
                allnames[nm] += c
        cn = max(allnames.items(), key=lambda kv: (kv[1], len(kv[0])))[0] if allnames else canon
        for k in members:
            remap[k] = canon; cname[canon] = cn
    for p in people:
        k = p.get("company_key", "")
        if k in remap:
            p["company_key"] = remap[k]
            p["company"] = cname[remap[k]]


def _dedupe(people: list[dict]) -> list[dict]:
    """Auto-fold typo'd / duplicate addresses of the SAME person into the busier canonical record.
    Same normalised display-name AND (same domain w/ near-miss local-part, OR near-miss domain w/ same
    local-part). Role/no-name addresses are never merged (avoids sales@/admin@ collisions)."""
    people.sort(key=lambda p: -p["count"])
    dropped: set[int] = set()
    for i, a in enumerate(people):
        if id(a) in dropped:
            continue
        an = _norm_name(a["name"])
        if not an or an == a["email"].split("@")[0]:
            continue                                       # no human name -> don't risk a merge
        al, ad = a["email"].split("@", 1)
        for b in people[i + 1:]:
            if id(b) in dropped or _norm_name(b["name"]) != an:
                continue
            bl, bd = b["email"].split("@", 1)
            same = (ad == bd and _close(al, bl)) or (al == bl and _close(ad, bd))
            if not same:
                continue
            a["count"] += b["count"]                       # fold the rarer into the busier
            a.setdefault("aliases", []).append(b["email"])
            a["aliases"] += [x for x in b.get("aliases", []) if x not in a["aliases"]]
            for f in ("position", "company", "company_key", "phone"):
                if not a[f] and b[f]:
                    a[f] = b[f]
            dropped.add(id(b))
    return [p for p in people if id(p) not in dropped]


def _sort(people: list[dict]) -> list[dict]:
    people.sort(key=lambda p: (not p.get("company"), (p.get("company") or "").casefold(),
                               (p.get("name") or p.get("email") or "").casefold()))
    return people
