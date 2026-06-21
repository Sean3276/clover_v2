"""P2: downloaded share-link files' CONTENT is fed into comprehension (not just the link). The shared
SharePoint/Drive/Dropbox file is read like an attachment, keyed to its message; oversize files are skipped."""
from clover import comprehend as cp
from clover import linkshares as ls


def test_downloaded_files_indexed_by_message(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "read_link_shares", lambda _a: [
        {"status": "downloaded", "file": "_linkfiles/a/contract.txt", "message_id": "m1"},
        {"status": "downloaded", "file": "_linkfiles/a/notes.txt", "message_id": "m1"},
        {"status": "pending", "file": None, "message_id": "m1"},          # not downloaded -> ignored
        {"status": "downloaded", "file": "_linkfiles/b/spec.txt", "message_id": "m2"},
    ])
    idx = cp._downloaded_files_by_mid(tmp_path)
    assert idx == {"m1": ["_linkfiles/a/contract.txt", "_linkfiles/a/notes.txt"], "m2": ["_linkfiles/b/spec.txt"]}


def test_link_file_text_extracted(tmp_path):
    d = tmp_path / "_linkfiles"; d.mkdir()
    (d / "contract.txt").write_text("Shared contract — retention payment of S$50,000 due 30 June 2026.", encoding="utf-8")
    txt = cp._link_file_text(tmp_path, ["_linkfiles/contract.txt"])
    assert "[shared file: contract.txt]" in txt and "retention payment of S$50,000" in txt


def test_link_file_oversize_is_flagged_not_loaded(tmp_path):
    d = tmp_path / "_linkfiles"; d.mkdir()
    big = d / "huge.bin"
    with big.open("wb") as f:                                            # 26 MB sparse-ish, over the 25 MB cap
        f.seek(26 * 1024 * 1024); f.write(b"\0")
    out = cp._link_file_text(tmp_path, ["_linkfiles/huge.bin"])
    assert "too large to inline" in out and "huge.bin" in out


def test_thread_messages_appends_link_file_content(tmp_path, monkeypatch):
    from email.message import EmailMessage
    from clover import threads as th
    import json
    # one message thread
    m = EmailMessage()
    m["Message-ID"] = "<m1@x>"; m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"; m.set_content("see the shared contract")
    (tmp_path / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp_path / "INBOX/1.eml").write_bytes(m.as_bytes())
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "m1@x", "folder": "INBOX", "key": "1", "path": "INBOX/1.eml",
                            "date": m["Date"], "from": "a@x.com", "subject": "Hi", "size": 1}) + "\n")
    th.build_threads(tmp_path, log=lambda *_: None)
    thread = th.read_threads(tmp_path)[0]
    # a downloaded link file tied to that message
    (tmp_path / "_linkfiles").mkdir(exist_ok=True)
    (tmp_path / "_linkfiles/contract.txt").write_text("Contract body: pay by 30 June.", encoding="utf-8")
    monkeypatch.setattr(ls, "read_link_shares", lambda _a: [
        {"status": "downloaded", "file": "_linkfiles/contract.txt", "message_id": "m1@x"}])
    msgs = cp._thread_messages(tmp_path, thread)
    assert any("Contract body: pay by 30 June." in block for block in msgs)   # link-file content reached comprehension
