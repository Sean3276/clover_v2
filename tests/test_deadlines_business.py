"""Bug (post-fix review #1, 13+ personas): business/working-day deadlines were resolved as plain
calendar days, then emitted as a confident ISO — off by every intervening weekend. The resolver must
count business days (weekends skipped) for business/working/工作日 phrasings; plain days stay calendar."""
from clover.eval.deadlines import find_relative_deadlines, resolve


def test_working_days_skip_weekends():
    rels = find_relative_deadlines("within 14 working days")
    assert rels and rels[0]["unit"] == "businessdays"
    assert resolve(rels[0], "2025-03-03") == "2025-03-21"   # Mon + 14 business days (skips 2 weekends)


def test_business_days_skip_weekends_relative_anchor():
    rels = find_relative_deadlines("5 business days after service")
    assert resolve(rels[0], "2025-03-03") == "2025-03-10"   # Mon + 5 bd = next Mon


def test_plain_days_stay_calendar():
    rels = find_relative_deadlines("within 14 days")
    assert resolve(rels[0], "2025-03-03") == "2025-03-17"   # plain calendar, unchanged


def test_cn_working_days_are_business():
    rels = find_relative_deadlines("请在14个工作日内回复")
    assert rels and rels[0]["unit"] == "businessdays"
    assert resolve(rels[0], "2025-03-03") == "2025-03-21"
