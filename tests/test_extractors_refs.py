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


def test_agency_edit_direction_cues_floored():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("Make the logo bigger.")
    assert action_candidates("Lose the third slide.")


def test_waiver_chase_ship_cues_floored():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("Unless filed, the claim will be struck out.")    # peremptory/default consequence
    assert action_candidates("We are still waiting on the final assets.")      # declarative soft-chase
    assert action_candidates("仍在等贵司确认。")                                 # CN chase
    assert action_candidates("Ship it.")                                       # bare imperative


def test_ap_overdue_and_agency_verbs_floored():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("The balance is now overdue.")              # AP non-imperative obligation
    assert action_candidates("Please remit the outstanding amount.")
    assert action_candidates("款项已逾期未付。")                           # CN overdue/unpaid
    assert action_candidates("Tweak the headline a touch.")              # agency creative verb
    assert action_candidates("Punch up the CTA copy.")


def test_clause_subpart_kept():
    # 'clause 14.2' must not collapse to CLAUSE-14 (the sub-part is a distinct legal/contractual ref)
    assert "CLAUSE-14.2" in extract_refs("under clause 14.2")
    assert "RFI-12" in extract_refs("RFI-12")               # plain integer refs unaffected


def test_bare_v_version_ref():
    out = extract_refs("approve v3 banner, not v2")
    assert "V-3" in out and "V-2" in out


def test_ref_alpha_suffix_kept_distinct():
    # 'VO-09A' is a distinct revision and must NOT collapse into VO-9 (silent merge of two matters)
    out = extract_refs("VO-09A supersedes VO-9; see also VO-09")
    assert "VO-9A" in out and "VO-9" in out


def test_interrogative_as_request_is_floored():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("Do you have the brand feedback yet?")        # bare question = awaited answer
    assert action_candidates("是否已批准这一版?")                            # CN interrogative
    assert action_candidates("WDYT on the punchier headline?")             # agency opinion-solicitation


def test_version_and_job_refs():
    out = extract_refs("approve version 3, not version 2; rev 4 and Job #220 and round 2")
    assert "VERSION-3" in out and "REV-4" in out and "JOB-220" in out and "ROUND-2" in out


def test_action_floor_scheduling_and_revision_verbs_strong():
    from clover.eval.action_floor import action_candidates
    assert action_candidates("Let's reschedule the interview to Tuesday.")          # scheduling verb
    assert action_candidates("Needs another round on the hero copy.")               # creative-revision
    assert action_candidates("请修改第三版方案。")                                    # CN revise
