"""#1 Pipeline reorder: share-links/attachments are fetched BEFORE comprehension, and a share-link file
that lands later (e.g. an oversize download confirmed after the fact) re-comprehends its thread — so its
attachment text is never silently left out of comprehension."""
import json
from email.message import EmailMessage

from clover import comprehend as cp
from clover import linkshares as ls
from clover import threads as th


def _eml(tmp, key, mid, irt=None, text="body"):
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"
    if irt:
        m["In-Reply-To"] = f"<{irt}>"
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content(text)
    rel = f"INBOX/{key}.eml"
    (tmp / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp / rel).write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "folder": "INBOX", "key": key, "path": rel,
                            "date": m["Date"], "from": m["From"], "subject": "Hi", "size": 1}) + "\n")


def _thread(tmp):
    _eml(tmp, "1", "a@x", text="see attachment")
    th.build_threads(tmp, log=lambda *_: None)
    return th.read_threads(tmp)[0]


# ---- attachment-aware staleness ---------------------------------------------------------------
def test_attach_is_part_of_signature_and_staleness():
    t = {"n": 1, "end": "2026-01-01", "members": [{"message_id": "a@x"}]}
    rec = {"source": cp.thread_sig(t, attach=0)}              # comprehended with no link files yet
    assert cp.thread_sig(t, attach=2)["attach"] == 2
    assert cp.is_stale(t, rec, attach=0) is False             # nothing changed
    assert cp.is_stale(t, rec, attach=1) is True              # a share-link file landed -> re-comprehend
    assert cp.is_stale(t, rec) is False                       # backward-compatible (no attach arg)


def test_downloaded_link_index_and_count(monkeypatch):
    monkeypatch.setattr(ls, "read_link_shares", lambda _a: [
        {"status": "downloaded", "file": "f1", "message_id": "a@x"},
        {"status": "downloaded", "file": "f2", "message_id": "a@x"},
        {"status": "pending", "message_id": "a@x"},                       # not downloaded -> ignored
        {"status": "downloaded", "file": "f3", "eml": "INBOX/2.eml"},
    ])
    by_mid, by_path = cp.downloaded_link_index("arch")
    assert by_mid == {"a@x": 2} and by_path == {"INBOX/2.eml": 1}
    t = {"members": [{"message_id": "a@x"}, {"message_id": "b@x", "locations": [{"path": "INBOX/2.eml"}]}]}
    assert cp.thread_attach_count("arch", t, (by_mid, by_path)) == 3


def test_select_threads_recomprehends_when_attachment_lands(tmp_path, monkeypatch):
    t = _thread(tmp_path)
    rec = {"thread_id": t["thread_id"], "root_id": t["root_id"],
           "source": cp.thread_sig(t, attach=0)}              # comprehended before the file existed
    with cp.comprehension_path(tmp_path).open("w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    monkeypatch.setattr(ls, "read_link_shares", lambda _a: [])           # no files yet
    assert cp.select_threads(tmp_path, include_stale=True) == []         # nothing to do
    monkeypatch.setattr(ls, "read_link_shares", lambda _a:               # a file lands for this thread
                        [{"status": "downloaded", "file": "f", "message_id": "a@x"}])
    todo = cp.select_threads(tmp_path, include_stale=True)
    assert [x["thread_id"] for x in todo] == [t["thread_id"]]            # now re-comprehended


# ---- reorder: links fetched before comprehend -------------------------------------------------
def test_post_import_fetches_links_before_comprehend(tmp_path, monkeypatch):
    import app.main as m
    order = []
    monkeypatch.setattr(m.threadmod, "build_threads", lambda *a, **k: order.append("restitch"))
    monkeypatch.setattr(m, "_run_links_inline", lambda *a, **k: order.append("links"))
    monkeypatch.setattr(m, "_maybe_autorun_comprehension", lambda *a, **k: order.append("comprehend"))
    monkeypatch.setattr(m.contactsmod, "rebuild", lambda *a, **k: order.append("contacts"))
    m._post_import({}, tmp_path, ["a@x", "b@x"], do_fetch=True)          # the ids this import brought in
    assert order == ["restitch", "links", "comprehend", "contacts"]      # links BEFORE comprehend


def test_post_import_scopes_links_to_imported_ids(tmp_path, monkeypatch):
    # the link step must be scoped to the messages THIS import brought in — not the whole backlog
    import app.main as m
    cap = {}
    monkeypatch.setattr(m.threadmod, "build_threads", lambda *a, **k: None)
    monkeypatch.setattr(m, "_maybe_autorun_comprehension", lambda *a, **k: None)
    monkeypatch.setattr(m.contactsmod, "rebuild", lambda *a, **k: None)
    monkeypatch.setattr(m, "_run_links_inline", lambda arch, **k: cap.update(k))
    m._post_import({}, tmp_path, ["a@x", "b@x"], do_fetch=True)
    assert cap["only_message_ids"] == {"a@x", "b@x"} and cap["fetch"] is True


def test_post_import_no_new_mail_does_not_fetch_backlog(tmp_path, monkeypatch):
    # empty import + download-on must NOT fetch every pending link (that was the "fetch all is meaningless" bug)
    import app.main as m
    called = {"links": False}
    monkeypatch.setattr(m, "_run_links_inline", lambda *a, **k: called.__setitem__("links", True))
    monkeypatch.setattr(m, "_start_link_task", lambda *a, **k: called.__setitem__("links", True) or True)
    m._post_import({}, tmp_path, [], do_fetch=True)
    assert called["links"] is False                                       # nothing auto-fetched


def test_run_links_inline_harvests_then_fetches(tmp_path, monkeypatch):
    import app.main as m
    order = []
    monkeypatch.setattr(m.lsmod, "harvest", lambda *a, **k: order.append("harvest"))
    monkeypatch.setattr(m.lsmod, "fetch_links", lambda *a, **k: (order.append("fetch"), {"remaining": 0})[1])
    m._run_links_inline(tmp_path, harvest=True, fetch=True)
    assert order == ["harvest", "fetch"]
