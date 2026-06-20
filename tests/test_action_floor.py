from clover.eval import action_floor as af


def test_request_with_deadline_courtesy_excluded():
    c = af.action_candidates("Thanks for the update. Please submit the report by 14 Mar 2026. We met last week.")
    texts = [x["text"] for x in c]
    assert any("submit the report" in t for t in texts)
    assert not any("Thanks for the update" in t for t in texts)   # courtesy-only excluded
    assert not any("We met last week" in t for t in texts)        # no obligation cue
    sub = next(x for x in c if "submit" in x["text"])
    assert "request" in sub["cues"] and sub["has_deadline"] is True


def test_obligation_modal_and_waiver():
    c = af.action_candidates("The contractor shall provide insurance. Failing which the deposit is forfeited.")
    cues = set().union(*[set(x["cues"]) for x in c])
    assert "obligation" in cues and "waiver" in cues


def test_chinese_request_and_deadline_courtesy_excluded():
    c = af.action_candidates("请在本周五之前提交报告。谢谢。")
    assert len(c) == 1 and "谢谢" not in c[0]["text"]
    assert "request" in c[0]["cues"] and c[0]["has_deadline"] is True   # 之前 deadline cue
