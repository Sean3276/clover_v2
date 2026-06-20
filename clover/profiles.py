"""Classification profiles — the taxonomy the council decides against.

Profile-driven by design: the taxonomy is config, not hard-coded, because different roles
classify differently (a project lead vs a GM vs a technical coordinator). One default profile
ships now; profile management (presets + custom + per-user) is a later product feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    domains: dict                      # level-1 domain -> list of level-2 categories
    safety_net: str                    # the high-stakes category (its miss is costliest)
    precedence: list = field(default_factory=list)   # ordered deterministic tie-break rules
    facets: dict = field(default_factory=dict)       # orthogonal tag vocabularies: facet -> [allowed values]

    def domain_names(self) -> list:
        return list(self.domains)

    def categories(self, domain: str) -> list:
        return list(self.domains.get(domain, []))

    def facet_names(self) -> list:
        return list(self.facets)

    def facet_values(self, facet: str) -> list:
        return list(self.facets.get(facet, []))

    def all_categories(self) -> list:
        seen = []
        for cats in self.domains.values():
            for c in cats:
                if c not in seen:
                    seen.append(c)
        return seen


# Default: construction contractor view (legacy-derived), two-level.
CONSTRUCTION = Profile(
    name="construction",
    description="Construction view: Project vs Corporate, then discipline/department.",
    domains={
        "Project": ["Commercial", "Design & Technical", "Operation", "Quality", "Safety"],
        "Corporate": ["HR & Admin", "Account & Finance", "Design & Planning",
                      "Safety", "Commercial", "Engineering & Operation"],
    },
    safety_net="Commercial",
    precedence=[
        # ordered: the first whose keyword appears in the thread wins (referee, deterministic).
        # SAFETY FIRST — a missed safety matter is the costliest, so it must beat the commercial rules
        # below (else "incident — cost of damage" would mis-file as Commercial).
        {"if_any": ["accident", "incident", "dangerous occurrence", "near miss",
                    "permit-to-work", "permit to work", "stop-work order", "fatality", "injury"],
         "then": "Safety"},
        {"if_any": ["site instruction", "architect's instruction", "SOI", "MCI", "CVI",
                    "instruction to proceed", "variation order", "VO", "variation"],
         "then": "Commercial"},
        {"if_any": ["letter of award", "LOA", "purchase order", "PO", "subcontract",
                    "executed contract", "signed contract"],
         "then": "Commercial"},
        {"if_any": ["extension of time", "EOT", "interim payment", "IPC", "payment claim",
                    "claim", "cost"],
         "then": "Commercial"},
    ],
    facets={
        # orthogonal tags the AI applies on top of Domain/Category (any combination = flexible depth).
        "Discipline": ["Architecture", "Structural", "M&E", "Civil & Structural", "Geotechnical",
                       "Façade", "Interior", "Landscape", "Authority"],
        "Element": ["Substructure", "Superstructure", "Podium", "Basement", "Roof", "Façade",
                    "Doors & Windows", "M&E Services", "Drainage", "External Works"],
        "Artifact": ["Drawing", "RFI", "Submission", "Minutes", "Quotation", "Claim", "NCR",
                     "Method Statement", "Schedule", "Report", "Certificate"],
        "Authority": ["BCA", "URA", "SCDF", "LTA", "PUB", "NEA", "NParks", "JTC"],
    },
)

PROFILES = {CONSTRUCTION.name: CONSTRUCTION}


def get_profile(name: str | None = None) -> Profile:
    return PROFILES.get(name or "construction", CONSTRUCTION)


def to_dict(p: Profile) -> dict:
    return {"name": p.name, "description": p.description,
            "domains": {k: list(v) for k, v in p.domains.items()},
            "safety_net": p.safety_net, "precedence": [dict(r) for r in p.precedence],
            "facets": {k: list(v) for k, v in p.facets.items()}}


def _clean_value_list(values) -> list:
    out, seen = [], set()
    for v in (values or []):
        v = str(v).strip()
        if v and v.lower() not in seen:
            out.append(v); seen.add(v.lower())
    return out


def from_dict(d: dict) -> Profile:
    """Build a Profile from an operator-edited dict; raises ValueError if there's no usable taxonomy."""
    domains = {}
    for dom, cats in (d.get("domains") or {}).items():
        dom = str(dom).strip()
        cl, seen = [], set()
        for c in (cats or []):
            c = str(c).strip()
            if c and c.lower() not in seen:
                cl.append(c); seen.add(c.lower())
        if dom and cl:
            domains[dom] = cl
    if not domains:
        raise ValueError("A profile needs at least one domain with categories.")
    prec = []
    for r in (d.get("precedence") or []):
        kws = [str(k).strip() for k in (r.get("if_any") or []) if str(k).strip()]
        then = str(r.get("then") or "").strip()
        if kws and then:
            prec.append({"if_any": kws, "then": then})
    facets = {}
    for fac, vals in (d.get("facets") or {}).items():
        fac = str(fac).strip()
        vl = _clean_value_list(vals)
        if fac and vl:
            facets[fac] = vl
    allcats = [c for cats in domains.values() for c in cats]
    sn = str(d.get("safety_net") or "").strip()
    if sn not in allcats:
        sn = allcats[0]
    return Profile(name=(str(d.get("name") or "").strip() or "custom"),
                   description=str(d.get("description") or "").strip(),
                   domains=domains, safety_net=sn, precedence=prec, facets=facets)


def effective_profile(cfg: dict | None) -> Profile:
    """The active profile: an operator-edited override (cfg.comprehension.profile_def) if valid, else the
    shipped preset named by cfg.comprehension.profile."""
    c = (cfg or {}).get("comprehension") or {}
    d = c.get("profile_def")
    if isinstance(d, dict) and d.get("domains"):
        try:
            return from_dict(d)
        except Exception:
            pass
    return get_profile(c.get("profile"))
