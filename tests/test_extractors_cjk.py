"""Bug: the deterministic floor was English/Latin-only, so Chinese + full-width atoms were silently
invisible — yet every comprehension prompt tells the AI the floor catches atoms. These tests pin the
CJK floor so the no-miss backstop actually covers bilingual content."""
from clover.eval.extractors import extract_dates, extract_amounts


# ── CJK dates -> ISO ──────────────────────────────────────────────────────────────────
def test_cjk_date_year_month_day():
    assert "2025-03-14" in extract_dates("请在 2025年3月14日 前提交")


def test_cjk_date_fullwidth_digits():
    assert "2025-03-14" in extract_dates("截止 ２０２５年３月１４日")


def test_cjk_date_collapses_with_iso():
    # the same date written CN and ISO must canonicalise to one value
    assert extract_dates("2025年3月14日 = 2025-03-14") == ["2025-03-14"]


# ── CJK amounts -> {currency, value} ─────────────────────────────────────────────────
def test_cjk_amount_rmb_prefix_wan_multiplier():
    assert {"currency": "CNY", "value": "800000"} in extract_amounts("合同金额 人民币80万")


def test_cjk_amount_yuan_suffix_wan():
    assert {"currency": "CNY", "value": "800000"} in extract_amounts("预算 80万元")


def test_cjk_amount_yen_symbol_with_comma():
    assert {"currency": "CNY", "value": "1200"} in extract_amounts("费用 ¥1,200")


def test_cjk_amount_yi_multiplier():
    assert {"currency": "CNY", "value": "120000000"} in extract_amounts("总额 1.2亿元")


def test_plain_number_not_grabbed_as_amount():
    # no currency marker (leading 人民币/¥ or trailing 元) -> NOT an amount (precision floor preserved)
    assert extract_amounts("会议室 80 人") == []


def test_no_phantom_amount_for_rmb_plus_ascii_multiplier():
    # bug: RMB/¥ + ASCII multiplier (m/bn) emitted BOTH the real value and a phantom bare number
    assert extract_amounts("RMB 1.2m") == [{"currency": "CNY", "value": "1200000"}]
    assert {"currency": "CNY", "value": "1.2"} not in extract_amounts("¥1.2bn")


def test_us_month_first_date_recovered():
    assert "2025-03-14" in extract_dates("filing due 3/14/2025")    # month-first recovered (was dropped)
    assert "2025-04-03" in extract_dates("meet 03/04/2025")          # ambiguous stays day-first


def test_slash_iso_date():
    assert "2025-03-14" in extract_dates("dated 2025/3/14")


def test_hkd_and_ntd_not_mislabeled_usd():
    # bug: the bare-$ branch ate HK$/NT$ and tagged them USD
    assert {"currency": "HKD", "value": "50000"} in extract_amounts("HK$50,000")
    assert {"currency": "TWD", "value": "1200000"} in extract_amounts("NT$1,200,000")
    assert {"currency": "USD", "value": "500"} in extract_amounts("US$500")   # plain US$ still USD
