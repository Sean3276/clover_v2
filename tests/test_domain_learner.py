from clover.eval import domain_learner as dl


def test_learns_user_ref_conventions_by_frequency():
    texts = [
        "Please pay INV-2024-001 and INV-2024-002.",
        "PO 4500001 approved; PO 4500002 pending.",
        "Patient MRN 0012345 admitted on 2024-03-14, contact a@x.com.",
        "Re INV-2024-003 and PR#88.",
    ]
    by = {(p["prefix"], p["shape"]): p["count"] for p in dl.learn_ref_patterns(texts)}
    assert by[("INV", "A-9-9")] == 3              # learned the user's invoice format, ranked top
    assert by[("PO", "A 9")] == 2
    assert ("MRN", "A 9") in by and ("PR", "A#9") in by   # other fields' refs, no construction prefixes needed


def test_excludes_dates_years_emails_and_bare_numbers():
    assert dl.candidate_refs("on 2024-03-14 in 2024 email a@x.com total 4,500 items") == []


def test_shape_generalises():
    assert dl.shape("INV-2024-001") == "A-9-9"
    assert dl.shape("PR#123") == "A#9"
    assert dl.shape("PO 4500001") == "A 9"
