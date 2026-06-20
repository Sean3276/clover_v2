from clover.comprehenders import StubComprehender
from clover.eval import adjudicate


def test_compare_finds_agreements_and_disagreements():
    source = "RFI-12 and NCR-7 due 5 Feb 2025."
    ai = {"refs": ["RFI-12"], "dates": [], "amounts": []}     # AI got RFI-12, missed NCR-7 + the date
    cmp = adjudicate.compare(source, ai)
    assert cmp["refs"]["agree"] == ["RFI-12"]
    assert cmp["refs"]["a_only"] == ["NCR-7"]                 # rule found, AI missed
    assert cmp["dates"]["a_only"] == ["2025-02-05"]
    assert adjudicate.disagreements(cmp) and len(adjudicate.disagreements(cmp)) == 2


def test_adjudicate_reviews_only_disagreements_with_role_panel():
    source = "RFI-12 and NCR-7 due 5 Feb 2025."
    ai = {"refs": ["RFI-12"], "dates": [], "amounts": []}
    stub = StubComprehender(responses={"review": {"present": True, "is_real": True}})
    out = adjudicate.adjudicate(source, ai, stub, roles=("strict", "domain", "skeptic"))
    assert out["summary"]["disagreements"] == 2              # RFI-12 agreed -> NOT reviewed
    assert [r["verdict"] for r in out["results"]].count("B_missed") == 2   # real atoms the AI dropped
    assert out["summary"]["improve_B"] == 2
    assert stub.calls.count("review") == 6                   # 3 roles x 2 disagreements; agreements free


def test_adjudicate_flags_ai_hallucination():
    stub = StubComprehender(responses={"review": {"present": False, "is_real": False}})
    out = adjudicate.adjudicate("Meeting notes, nothing else.",
                                {"refs": ["XX-9"], "dates": [], "amounts": []}, stub)
    assert out["results"][0]["item"]["found_by"] == "B"      # AI invented it; rule didn't find it
    assert out["results"][0]["verdict"] == "B_hallucination"
    assert out["summary"]["improve_B"] == 1


def test_adjudicate_majority_vote_rules_real():
    # 2 of 3 roles say real -> majority -> treated as a real atom the AI missed
    def vote(prompt):
        return {"present": True, "is_real": True} if "strict" not in prompt else {"present": True, "is_real": False}
    stub = StubComprehender(responses={"review": vote})
    out = adjudicate.adjudicate("PO-5 received.", {"refs": [], "dates": [], "amounts": []}, stub)
    assert out["results"][0]["verdict"] == "B_missed" and out["results"][0]["votes_real"] == 2
