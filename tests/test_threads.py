import email
import email.policy
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
    assert b["attachments"] == [{"i": 0, "name": "report.pdf", "size": len(b"PDFDATA")}]


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


def test_get_attachment(tmp_path):
    import pytest
    rel = _write_eml(tmp_path, "INBOX", "1", mid="g@x", subject="Hi", atts=[("report.pdf", b"PDFDATA")])
    name, ctype, data = th.get_attachment(tmp_path, {"path": rel}, 0)
    assert name == "report.pdf" and data == b"PDFDATA"
    assert th.get_attachment(tmp_path, {"path": rel}, 9) is None         # out of range
    with pytest.raises(ValueError):
        th.get_attachment(tmp_path, {"path": "../../etc/passwd"}, 0)     # containment guard


def test_thread_routes_lazy_load(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", subject="Hi", html="<p>hello</p>")
    _write_eml(tmp_path, "INBOX", "2", mid="b@x", irt="a@x", subject="Re: Hi", text="reply")
    th.build_threads(tmp_path, log=lambda *_: None)
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    c = TestClient(m.app)
    tid = th.read_threads(tmp_path)[0]["thread_id"]

    r = c.get(f"/threads/{tid}")
    assert r.status_code == 200
    assert r.text.count('class="card tmsg"') == 2     # both headers rendered
    assert "srcdoc" not in r.text                      # bodies are NOT inlined (lazy)

    r0 = c.get(f"/threads/{tid}/msg/0")
    assert r0.status_code == 200 and "emlframe" in r0.text   # html body rendered on demand

    assert c.get(f"/threads/{tid}/msg/99").status_code == 404
    assert c.get("/threads/deadbeef/msg/0").status_code == 404


def test_confirm_link_route_and_needs_confirm_render(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    from clover import linkshares as ls
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", subject="Big",
               html='<p>file https://www.dropbox.com/s/x/big.zip here</p>')
    th.build_threads(tmp_path, log=lambda *_: None)
    ls.harvest(tmp_path, log=lambda *_: None)
    ls.fetch_links(tmp_path, fetcher=lambda u, p, lb: ("oversize", None, 3_000_000_000),
                   confirm_over_mb=1024, log=lambda *_: None)               # -> needs-confirm
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    c = TestClient(m.app)
    tid = th.read_threads(tmp_path)[0]["thread_id"]

    r = c.get(f"/threads/{tid}/msg/0")
    assert r.status_code == 200
    assert "Download anyway" in r.text and "needs-confirm" in r.text and "GB" in r.text

    rec = ls.read_link_shares(tmp_path)[0]
    resp = c.post("/threads/confirm-link", data={"message_id": rec["message_id"], "url": rec["url"]})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    rec2 = ls.read_link_shares(tmp_path)[0]
    assert rec2["status"] == "pending" and rec2["confirmed"] is True       # re-queued past the gate


def test_thread_view_link_bar_pending_then_saved(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    from clover import linkshares as ls
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", subject="Files",
               html='<p>https://www.dropbox.com/s/A/a.pdf and https://www.dropbox.com/s/B/b.pdf</p>')
    th.build_threads(tmp_path, log=lambda *_: None)
    ls.harvest(tmp_path, log=lambda *_: None)
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    c = TestClient(m.app)
    tid = th.read_threads(tmp_path)[0]["thread_id"]

    r1 = c.get(f"/threads/{tid}")                       # nothing downloaded yet -> download button, no view
    assert "Download 2 linked file(s)" in r1.text and "file(s) downloaded" not in r1.text

    ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "f.pdf", b"X"), log=lambda *_: None)
    r2 = c.get(f"/threads/{tid}")                       # both downloaded -> view appears, download button gone
    assert "file(s) downloaded" in r2.text and "/linkfile/" in r2.text
    assert "Download 2 linked file(s)" not in r2.text   # don't offer to re-fetch what's already kept


def test_link_status_endpoint_resolves_before_thread_id(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    from clover import linkshares as ls
    _write_eml(tmp_path, "INBOX", "1", mid="a@x", html='<p>https://www.dropbox.com/s/x/a.pdf</p>')
    th.build_threads(tmp_path, log=lambda *_: None)
    ls.harvest(tmp_path, log=lambda *_: None)
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    c = TestClient(m.app)
    r = c.get("/threads/link-status")                  # must NOT be captured as /threads/{thread_id}
    assert r.status_code == 200
    j = r.json()
    assert j["running"] is False and j["stats"].get("pending") == 1 and j["total"] == 1


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
    assert len(b["attachments"]) == 1                       # only the real attachment (cid image excluded)
    assert b["attachments"][0]["name"] == "photo.png"
    assert b["attachments"][0]["img"] is True            # flagged as image; served via /att/ URL, not base64


def test_image_attachment_with_stray_cid_not_in_body_is_kept(tmp_path):
    # Older Outlook/Apple Mail sometimes give a genuine image ATTACHMENT a stray Content-ID and no
    # Content-Disposition. It must NOT be hidden unless its cid is actually referenced by an
    # <img src="cid:..."> in the body. Here only `shown` is referenced; `stray` is a real attachment.
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    m = EmailMessage()
    m["Message-ID"] = "<strayimg@x>"
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "pics"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content("see image")
    m.add_alternative('<p>see <img src="cid:shown"></p>', subtype="html")
    m.add_attachment(png, maintype="image", subtype="png", filename="shown.png")
    m.add_attachment(png, maintype="image", subtype="png", filename="stray.png")
    for img, cid, fn in ((m.get_payload()[1], "<shown>", "shown.png"),
                         (m.get_payload()[2], "<stray>", "stray.png")):
        img["Content-ID"] = cid
        del img["Content-Disposition"]                    # no disposition (legacy clients)
        img.set_param("name", fn, header="Content-Type")  # filename lives in Content-Type name=

    msg = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
    names = [p.get_filename() for p in th._real_attachments(msg)]
    assert names == ["stray.png"]            # referenced cid hidden; stray-cid image kept

    rel = "INBOX/1.eml"
    (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(m.as_bytes())
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "strayimg@x", "folder": "INBOX", "key": "1", "path": rel,
                            "date": m["Date"], "from": m["From"], "subject": m["Subject"], "size": 1}) + "\n")
    th.build_threads(tmp_path, log=lambda *_: None)
    b = th.stitch_thread(tmp_path, th.read_threads(tmp_path)[0])[0]
    assert "data:image/png;base64," in b["body_html"]      # referenced cid inlined into the body
    assert "cid:shown" not in b["body_html"]
    assert [a["name"] for a in b["attachments"]] == ["stray.png"]   # only the genuine attachment


def test_inline_nonimage_attachment_kept_inline_image_dropped():
    # senders often mark a real file (PDF/doc) as Content-Disposition: inline; it must stay visible,
    # while an inline IMAGE (embedded preview/signature) is still excluded from the attachment list.
    m = EmailMessage()
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "s"
    m.set_content("body")
    m.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="invoice.pdf")
    m.add_attachment(b"PNGDATA", maintype="image", subtype="png", filename="sig.png")
    parts = m.get_payload()
    parts[1].replace_header("Content-Disposition", 'inline; filename="invoice.pdf"')
    parts[2].replace_header("Content-Disposition", 'inline; filename="sig.png"')
    names = [p.get_filename() for p in th._real_attachments(m)]
    assert names == ["invoice.pdf"]                          # inline PDF kept, inline image dropped
