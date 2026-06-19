from email.message import EmailMessage

from clover import compose


def _orig(tmp_path, frm="alice@x.com", to="me@x.com, bob@x.com", cc="carol@x.com",
          subject="Project", body="Hello there", mid="<orig@x>", atts=()):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Message-ID"] = mid
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content(body)
    for name, data in atts:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    p = tmp_path / "o.eml"
    p.write_bytes(m.as_bytes())
    return p


def test_reply_recipients_subject_threading(tmp_path):
    p = _orig(tmp_path)
    msg = compose.build_message(p, "reply", body_text="thanks", from_addr="me@x.com")
    assert msg["To"] == "alice@x.com" and not msg["Cc"]
    assert msg["Subject"] == "Re: Project"
    assert msg["In-Reply-To"] == "<orig@x>" and "<orig@x>" in msg["References"]
    body = msg.get_content()
    assert "thanks" in body and "> Hello there" in body


def test_reply_all_excludes_me_and_ccs_others(tmp_path):
    p = _orig(tmp_path, to="me@x.com, bob@x.com", cc="carol@x.com")
    msg = compose.build_message(p, "reply-all", body_text="hi", from_addr="me@x.com", me="me@x.com")
    assert msg["To"] == "alice@x.com"
    cc = msg["Cc"]
    assert "bob@x.com" in cc and "carol@x.com" in cc and "me@x.com" not in cc


def test_forward_carries_attachments_no_threading(tmp_path):
    p = _orig(tmp_path, atts=[("report.pdf", b"PDFDATA")])
    msg = compose.build_message(p, "forward", body_text="fyi", from_addr="me@x.com", to=["dave@x.com"])
    assert msg["To"] == "dave@x.com" and msg["Subject"] == "Fwd: Project"
    assert "In-Reply-To" not in msg                       # forwards don't thread to the original
    assert [a.get_filename() for a in msg.iter_attachments()] == ["report.pdf"]
    assert "Forwarded message" in msg.get_body(preferencelist=("plain",)).get_content()


def test_subject_prefix_not_doubled(tmp_path):
    p = _orig(tmp_path, subject="Re: Project")
    assert compose.build_message(p, "reply", body_text="x", from_addr="me@x.com")["Subject"] == "Re: Project"
    p2 = _orig(tmp_path, subject="Fwd: Project")
    assert compose.build_message(p2, "forward", body_text="x", from_addr="me@x.com", to=["d@x.com"])["Subject"] == "Fwd: Project"


def test_unknown_action_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        compose.build_message(_orig(tmp_path), "bogus", body_text="x", from_addr="me@x.com")
