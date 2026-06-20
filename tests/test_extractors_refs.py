"""Round-1 review #1 (10/20 personas): the ref floor only matched UPPERCASE prefixes, so lowercase /
word-form / #-numbered refs bypassed the no-miss floor entirely. Add a high-precision noun-anchored
pattern (case-insensitive) covering common business ref nouns across domains."""
from clover.eval.extractors import extract_refs


def test_lowercase_and_word_form_refs():
    out = extract_refs("Please action rfi 12, PO #4471 and invoice no. 88; also vo-9")
    assert "RFI-12" in out and "PO-4471" in out and "INVOICE-88" in out and "VO-9" in out


def test_uppercase_prefix_refs_still_work():
    out = extract_refs("see RFI-12 and NCR 07 and MCI-018")
    assert "RFI-12" in out and "NCR-7" in out and "MCI-18" in out


def test_legal_clause_and_ticket_forms():
    out = extract_refs("under clause 14 and ticket 441")
    assert "CLAUSE-14" in out and "TICKET-441" in out


def test_noun_anchored_precision_no_overmatch():
    # bare verbs/prepositions + numbers must NOT become refs
    out = extract_refs("we have 3 items to do, and as 2 noted, go 5 steps")
    assert out == []


def test_action_floor_scheduling_and_revision_verbs_strong():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("Let's reschedule the interview to Tuesday.")          # scheduling verb
    assert action_candidates("Needs another round on the hero copy.")               # creative-revision
    assert action_candidates("请修改第三版方案。")                                    # CN revise
