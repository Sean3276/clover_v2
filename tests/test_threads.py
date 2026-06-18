import json
from email.message import EmailMessage
from pathlib import Path

from clover import threads as th


def _write_eml(archive: Path, folder: str, key: str, *, mid, refs=None, irt=None,
               date="Thu, 01 Jan 2026 00:00:00 +0000", frm="a@x.com", to="b@x.com",
               subject="s", html=None, text="hello", atts=()):
    m = EmailMessage()
    if mid:
        m["Message-ID"] = f"<{mid}>"
    if irt:
        m["In-Reply-To"] = f"<{irt}>"
    if refs:
        m["References"] = " ".join(f"<{r}>" for r in refs)
    m["Date"] = date
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m.set_content(text)
    if html is not None:
        m.add_alternative(html, subtype="html")
    for name, data in atts:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    raw = m.as_bytes()
    rel = f"{folder}/{key}.eml"
    (archive / folder).mkdir(parents=True, exist_ok=True)
    (archive / rel).write_bytes(raw)
    row = {"id": (mid or f"uid_{key}"), "folder": folder, "key": key,
           "path": rel, "date": date, "from": frm, "subject": subject, "size": len(raw)}
    with (archive / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return rel


# ---------------------------------------------------------------- pure helpers
def test_norm_id_and_ids_in():
    assert th._norm_id("  <A@B.com> ") == "a@b.com"
    assert th._ids_in("<a@x> <b@x>") == ["a@x", "b@x"]
    assert th._ids_in(None) == []


def test_clean_subject_strips_prefixes():
    assert th._clean_subject("Re: Fwd:  Hello") == "Hello"
    assert th._clean_subject("RE: re: AW: Status") == "Status"
    assert th._clean_subject("   ") == "(no subject)"


def test_to_utc_iso_normalizes_timezone():
    assert th._to_utc_iso("Thu, 11 Jun 2026 09:00:00 +0800") == "2026-06-11T01:00:00Z"
    assert th._to_utc_iso("garbage") is None
    assert th._to_utc_iso(None) is None


# ---------------------------------------------------------------- builder
def test_links_chain_by_references(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", date="Thu, 01 Jan 2026 00:00:00 +0000")
    _write_eml(tmp_path, "INBOX", "2", mid="b@x", irt="a@x", date="Thu, 02 Jan 2026 00:00:00 +0000")
    _write_eml(tmp_path, "INBOX", "3", mid="c@x", refs=["a@x", "b@x"], date="Thu, 03 Jan 2026 00:00:00 +0000")
    s = th.build_threads(tmp_path, log=lambda *_: None)
    threads = th.read_threads(tmp_path)
    assert s["threads"] == 1 and s["multi"] == 1
    t = threads[0]
    assert t["n"] == 3
    assert [m["message_id"] for m in t["members"]] == ["a@x", "b@x", "c@x"]   # chronological
    assert t["root_id"] == "a@x"
    assert t["start"] == "2026-01-01T00:00:00Z" and t["end"] == "2026-01-03T00:00:00Z"


def test_unrelated_messages_are_singletons(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x")
    _write_eml(tmp_path, "INBOX", "2", mid="b@x")
    s = th.build_threads(tmp_path, log=lambda *_: None)
    assert s["threads"] == 2 and s["singletons"] == 2


def test_cross_folder_duplicate_is_one_member_two_locations(tmp_path):
    _write_eml(tmp_path, "Trash", "1", mid="dup@x")
    _write_eml(tmp_path, "Sent Items", "9", mid="dup@x")
    s = th.build_threads(tmp_path, log=lambda *_: None)
    threads = th.read_threads(tmp_path)
    assert s["threads"] == 1
    t = threads[0]
    assert t["n"] == 1
    locs = {(l["folder"]) for l in t["members"][0]["locations"]}
    assert locs == {"Trash", "Sent Items"}


def test_missing_message_id_does_not_collide_across_folders(tmp_path):
    # both rows would have index id 'uid_5' (IMAP UID not globally unique) -> must NOT merge
    _write_eml(tmp_path, "Trash", "5", mid=None)
    _write_eml(tmp_path, "Sent Items", "5", mid=None)
    s = th.build_threads(tmp_path, log=lambda *_: None)
    assert s["threads"] == 2 and s["singletons"] == 2


def test_missing_message_id_reply_still_attaches(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="root@x", date="Thu, 01 Jan 2026 00:00:00 +0000")
    _write_eml(tmp_path, "INBOX", "2", mid=None, irt="root@x", date="Thu, 02 Jan 2026 00:00:00 +0000")
    s = th.build_threads(tmp_path, log=lambda *_: None)
    assert s["threads"] == 1 and th.read_threads(tmp_path)[0]["n"] == 2


def test_phantom_root_links_replies(tmp_path):
    # the original (ghost@x) was deleted/never archived; its two replies still thread together
    _write_eml(tmp_path, "INBOX", "1", mid="b@x", refs=["ghost@x"])
    _write_eml(tmp_path, "INBOX", "2", mid="c@x", refs=["ghost@x"])
    s = th.build_threads(tmp_path, log=lambda *_: None)
    t = th.read_threads(tmp_path)[0]
    assert s["threads"] == 1 and t["n"] == 2
    assert "ghost@x" not in [m["message_id"] for m in t["members"]]   # phantom not a member


def test_thread_subject_is_cleaned(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", subject="Re: Fwd: Project kickoff")
    th.build_threads(tmp_path, log=lambda *_: None)
    assert th.read_threads(tmp_path)[0]["subject"] == "Project kickoff"


def test_rebuild_is_deterministic(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", date="Thu, 01 Jan 2026 00:00:00 +0000")
    _write_eml(tmp_path, "INBOX", "2", mid="b@x", irt="a@x", date="Thu, 02 Jan 2026 00:00:00 +0000")
    th.build_threads(tmp_path, log=lambda *_: None)
    first = (tmp_path / "threads.jsonl").read_text(encoding="utf-8")
    th.build_threads(tmp_path, log=lambda *_: None)
    second = (tmp_path / "threads.jsonl").read_text(encoding="utf-8")
    assert first == second                                   # identical re-run (pure transform)


def test_participants_collected(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", frm="alice@x.com", to="bob@x.com")
    _write_eml(tmp_path, "INBOX", "2", mid="b@x", irt="a@x", frm="bob@x.com", to="alice@x.com")
    th.build_threads(tmp_path, log=lambda *_: None)
    parts = set(th.read_threads(tmp_path)[0]["participants"])
    assert parts == {"alice@x.com", "bob@x.com"}


# ---------------------------------------------------------------- reader / stitch
def test_get_thread_and_render_message(tmp_path):
    rel = _write_eml(tmp_path, "INBOX", "1", mid="a@x", subject="Hi",
                     html="<p>hello <b>world</b></p>", atts=[("report.pdf", b"PDFDATA")])
    th.build_threads(tmp_path, log=lambda *_: None)
    t = th.read_threads(tmp_path)[0]
    assert th.get_thread(tmp_path, t["thread_id"])["thread_id"] == t["thread_id"]
    assert th.get_thread(tmp_path, "nonexistent") is None
    blocks = th.stitch_thread(tmp_path, t)
    assert len(blocks) == 1
    b = blocks[0]
    assert "hello" in (b["body_html"] or "")
    assert b["attachments"] == [{"name": "report.pdf", "size": len(b"PDFDATA")}]


def test_render_message_plain_text_fallback(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", text="just plain", html=None)
    th.build_threads(tmp_path, log=lambda *_: None)
    t = th.read_threads(tmp_path)[0]
    b = th.stitch_thread(tmp_path, t)[0]
    assert b["body_html"] is None and "just plain" in b["body_text"]


def test_empty_archive(tmp_path):
    s = th.build_threads(tmp_path, log=lambda *_: None)
    assert s["threads"] == 0 and th.read_threads(tmp_path) == []


def test_render_message_rejects_path_escape(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        th.render_message(tmp_path, {"path": "../../etc/passwd"})   # containment guard


def test_render_inlines_cid_images_and_image_attachments(tmp_path):
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    m = EmailMessage()
    m["Message-ID"] = "<imgmsg@x>"
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "pic"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content("see image")
    m.add_alternative('<p>see <img src="cid:logo123"></p>', subtype="html")
    m.get_payload()[1].add_related(png, maintype="image", subtype="png", cid="<logo123>")
    m.add_attachment(png, maintype="image", subtype="png", filename="photo.png")
    rel = "INBOX/1.eml"
    (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(m.as_bytes())
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "imgmsg@x", "folder": "INBOX", "key": "1", "path": rel,
                            "date": m["Date"], "from": m["From"], "subject": m["Subject"], "size": 1}) + "\n")
    th.build_threads(tmp_path, log=lambda *_: None)
    b = th.stitch_thread(tmp_path, th.read_threads(tmp_path)[0])[0]
    assert "data:image/png;base64," in b["body_html"]      # cid rewritten to inline data URI
    assert "cid:logo123" not in b["body_html"]
    assert any(a.get("img", "").startswith("data:image/png;base64,") for a in b["attachments"])
