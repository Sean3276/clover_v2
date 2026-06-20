from clover.eval import deadlines as dl


def test_finds_relative_forms():
    rels = dl.find_relative_deadlines(
        "Payment Net-30. Submit within 14 days. Reply 7 days after receipt. File 2 weeks before the hearing.")
    kinds = {(r["kind"], r["n"], r["unit"], r["direction"]) for r in rels}
    assert ("net", 30, "days", "after") in kinds
    assert ("within", 14, "days", "after") in kinds
    assert ("relative", 7, "days", "after") in kinds
    assert ("relative", 2, "weeks", "before") in kinds


def test_resolve_with_anchor():
    assert dl.resolve({"n": 30, "unit": "days", "direction": "after"}, "2026-01-01") == "2026-01-31"
    assert dl.resolve({"n": 2, "unit": "weeks", "direction": "before"}, "2026-01-15") == "2026-01-01"
    assert dl.resolve({"n": 1, "unit": "months", "direction": "after"}, "2026-01-31") == "2026-02-28"  # clamps
    assert dl.resolve({"n": 5, "unit": "days", "direction": "after"}, None) is None


def test_with_due_pending_then_resolved():
    rel = {"kind": "relative", "n": 7, "unit": "days", "direction": "after", "anchor": "receipt"}
    assert dl.with_due(rel)["pending"] is True and dl.with_due(rel)["due_canonical"] is None
    assert dl.with_due(rel, "2026-03-01")["due_canonical"] == "2026-03-08"


def test_chinese_relative_forms():
    rels = dl.find_relative_deadlines("请在30天内回复，并在收到后5个工作日内确认。")
    kinds = {(r["kind"], r["n"], r["unit"]) for r in rels}
    assert ("within", 30, "days") in kinds
    assert ("relative", 5, "businessdays") in kinds        # 工作日 now resolves as business days (weekends skipped)
