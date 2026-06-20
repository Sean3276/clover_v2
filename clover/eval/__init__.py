"""Clover evaluation harness — the measurable comprehension reliability standard.

Implements the deterministic side of CLOVER_V2_COMPREHENSION_SPEC: the T1 recall-floor
extractors here, plus (to come) the gold store, scorer, and run_eval. All deterministic;
no AI lives in this package — it MEASURES the AI, it does not call it.
"""
