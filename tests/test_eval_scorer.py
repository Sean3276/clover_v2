from clover.eval import gold as goldmod
from clover.eval import scorer


def test_crosscheck_flags_dropped_atom():
    source = "Action RFI-12 by 14 Mar 2025; budget SGD 1,000 and USD 500."
    ai = {"refs": ["RFI-12"], "dates": ["14 March 2025"], "amounts": ["SGD 1,000"]}  # dropped USD 500
    cc = scorer.crosscheck_floor(source, ai)
    assert cc["refs"]["missed"] == []
    assert cc["dates"]["missed"] == []                 # '14 March 2025' canonicalises to the source date
    assert cc["amounts"]["missed"] == ["USD 500"]      # the dropped amount is a hard miss
    assert cc["hard_misses"] == 1


def test_crosscheck_clean_when_ai_captures_all():
    cc = scorer.crosscheck_floor("NCR-7 raised on 2025-01-02.",
                                 {"refs": ["NCR-7"], "dates": ["2025-01-02"], "amounts": []})
    assert cc["hard_misses"] == 0


def test_score_against_gold_recall_and_zero_miss():
    gold_atoms = {"refs": ["RFI-12", "NCR-7"], "dates": ["2025-03-14"], "amounts": ["SGD 1000"]}
    ai = {"refs": ["RFI-12"], "dates": ["14 Mar 2025"], "amounts": ["SGD 1,000"]}   # missing NCR-7
    s = scorer.score_against_gold(gold_atoms, ai)
    assert s["refs"]["recall"] == 0.5 and s["refs"]["missed"] == ["NCR-7"]
    assert s["dates"]["recall"] == 1.0 and s["amounts"]["recall"] == 1.0            # canonical match
    assert s["zero_miss"] is False


def test_score_zero_miss_true_when_complete():
    s = scorer.score_against_gold({"refs": ["RFI-1"], "dates": ["2025-01-01"]},
                                  {"refs": ["RFI-1"], "dates": ["1 Jan 2025"]})
    assert s["zero_miss"] is True


def test_gold_store_roundtrip_and_bootstrap(tmp_path):
    rec = goldmod.bootstrap_record("t1", "RFI-9 due 2025-05-01 for $20.")
    assert rec["confirmed"] is False                                                # candidate, not gold
    assert rec["atoms"]["refs"] == ["RFI-9"] and rec["atoms"]["amounts"] == ["USD 20"]
    assert rec["atoms"]["actions"] == []                                            # human adds these
    goldmod.write_gold(tmp_path, rec)
    rec2 = dict(rec); rec2["confirmed"] = True                                      # re-confirm supersedes
    goldmod.write_gold(tmp_path, rec2)
    back = goldmod.read_gold(tmp_path)
    assert back["t1"]["confirmed"] is True and back["t1"]["atoms"]["refs"] == ["RFI-9"]
