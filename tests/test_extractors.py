from clover.eval import extractors as ex


def test_refs_canonical_case_and_separator():
    r = ex.extract_refs("Re: RFI-12 and NCR 07, also SOI-018 / CR-59 (VO09). EOT-5 raised.")
    assert set(r) == {"RFI-12", "NCR-07", "SOI-018", "CR-59", "VO-09", "EOT-5"}


def test_refs_keep_leading_zeros_and_skip_stopwords():
    assert ex.extract_refs("doc SOI-018 per ISO9001") == ["SOI-018"]      # ISO* is stop-listed
    assert ex.extract_refs("rfi 12") == []                                # lowercase prefix = known floor gap


def test_refs_do_not_swallow_currency_amounts():
    # currency codes adjacent to numbers must NOT become refs (SGD 1,000 / USD 500)
    assert ex.extract_refs("budget SGD 1,000 and USD 500 for RFI-7") == ["RFI-7"]


def test_dates_normalised_to_iso():
    d = ex.extract_dates("Due 14 Mar 2025; logged 2025-03-14; sent 03/04/2025; dated March 5, 2025.")
    assert "2025-03-14" in d            # '14 Mar 2025' and ISO collapse to one
    assert "2025-04-03" in d            # 03/04/2025 read day-first -> 3 Apr
    assert "2025-03-05" in d            # 'March 5, 2025'
    assert d == sorted(d)               # sorted, deduped


def test_dates_reject_impossible():
    assert ex.extract_dates("2025-13-40") == []                           # month/day out of range


def test_amounts_canonical_with_multiplier():
    a = {(x["currency"], x["value"]) for x in ex.extract_amounts(
        "Claim SGD 1,234.50, variation $2m, retention USD 500, RM 1k.")}
    assert ("SGD", "1234.5") in a
    assert ("USD", "2000000") in a
    assert ("USD", "500") in a
    assert ("MYR", "1000") in a


def test_amounts_ignore_bare_numbers():
    assert ex.extract_amounts("there were 12 items across 3 floors") == []   # no currency marker


def test_emails_lowercased_sorted_deduped():
    assert ex.extract_emails("From A@X.com; cc b@y.com, again A@x.COM.") == ["a@x.com", "b@y.com"]


def test_extract_atoms_shape():
    out = ex.extract_atoms("RFI-1 due 2025-01-02 for $5 from a@x.com")
    assert set(out) == {"refs", "dates", "amounts", "emails"}
    assert out["refs"] == ["RFI-1"] and out["dates"] == ["2025-01-02"]
    assert out["amounts"] == [{"currency": "USD", "value": "5"}] and out["emails"] == ["a@x.com"]
