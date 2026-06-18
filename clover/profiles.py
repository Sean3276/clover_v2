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

    def domain_names(self) -> list:
        return list(self.domains)

    def categories(self, domain: str) -> list:
        return list(self.domains.get(domain, []))

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
        # ordered: the first whose keyword appears in the thread wins (referee, deterministic)
        {"if_any": ["site instruction", "architect's instruction", "SOI", "MCI", "CVI",
                    "instruction to proceed", "variation order", "VO", "variation"],
         "then": "Commercial"},
        {"if_any": ["letter of award", "LOA", "purchase order", "PO", "subcontract",
                    "executed contract", "signed contract"],
         "then": "Commercial"},
        {"if_any": ["extension of time", "EOT", "interim payment", "IPC", "payment claim",
                    "claim", "cost"],
         "then": "Commercial"},
        {"if_any": ["accident", "incident", "dangerous occurrence", "near miss",
                    "permit-to-work", "permit to work"],
         "then": "Safety"},
    ],
)

PROFILES = {CONSTRUCTION.name: CONSTRUCTION}


def get_profile(name: str | None = None) -> Profile:
    return PROFILES.get(name or "construction", CONSTRUCTION)
