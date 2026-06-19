from clover import rules


def test_add_match_delete_and_last_wins(tmp_path):
    assert rules.add_rule(tmp_path, "keyword", "retention sum", "Project", "Commercial")
    assert rules.add_rule(tmp_path, "sender", "@acme.com", "Corporate", "Account & Finance")
    assert rules.add_rule(tmp_path, "project", "Marina Bridge", "Project", "Quality")
    assert len(rules.read_rules(tmp_path)) == 3

    assert rules.match(tmp_path, text="please confirm the RETENTION SUM")["category"] == "Commercial"
    assert rules.match(tmp_path, senders=["Bob <bob@acme.com>"])["category"] == "Account & Finance"
    assert rules.match(tmp_path, project="marina bridge")["category"] == "Quality"   # case-insensitive
    assert rules.match(tmp_path, text="nothing relevant here") is None

    rules.add_rule(tmp_path, "keyword", "retention sum", "Corporate", "Commercial")   # newer, same keyword
    assert rules.match(tmp_path, text="the retention sum")["domain"] == "Corporate"   # last-match-wins

    assert rules.delete_rule(tmp_path, 0) and len(rules.read_rules(tmp_path)) == 3


def test_invalid_inputs_rejected(tmp_path):
    assert rules.add_rule(tmp_path, "keyword", "", "D", "C") is False     # empty match
    assert rules.add_rule(tmp_path, "bogus", "x", "D", "C") is False      # bad type
    assert rules.delete_rule(tmp_path, 9) is False                        # out of range
