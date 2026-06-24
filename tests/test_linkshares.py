import json
from email.message import EmailMessage

import pytest

from clover import linkshares as ls


@pytest.fixture(autouse=True)
def _stub_malware_scan(monkeypatch):
    """fetch_links now scans every download; stub it CLEAN for the link-logic tests so they stay fast and
    deterministic (the scan itself is covered in test_malware.py + the quarantine test below)."""
    from clover import malware
    monkeypatch.setattr(malware, "scan_file",
                        lambda p, **k: {"scanned": True, "clean": True, "threat": None, "scanner": "stub", "note": ""})


def _eml(tmp, folder, key, mid, body):
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"
    m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    m["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    m.set_content(body)
    rel = f"{folder}/{key}.eml"
    (tmp / folder).mkdir(parents=True, exist_ok=True)
    (tmp / rel).write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "folder": folder, "key": key, "path": rel,
                            "date": m["Date"], "from": "a@x.com", "subject": "Hi", "size": 1}) + "\n")


def test_detect_links_all_providers():
    text = ("see https://acme.sharepoint.com/:b:/x/abc , "
            "https://drive.google.com/file/d/ID/view , "
            "https://www.dropbox.com/s/xyz/file.pdf?dl=0 , "
            "https://we.tl/t-abc , https://app.box.com/s/zzz . plain text")
    provs = {p for p, _ in ls.detect_links(text)}
    assert provs == {"SharePoint/OneDrive", "Google Drive", "Dropbox", "WeTransfer", "Box"}


def test_detect_links_trims_and_dedups():
    text = "doc https://www.dropbox.com/s/x/f.pdf) again https://www.dropbox.com/s/x/f.pdf)"
    assert ls.detect_links(text) == [("Dropbox", "https://www.dropbox.com/s/x/f.pdf")]


def test_detect_no_false_positive_on_plain():
    assert ls.detect_links("just a normal email, no links, visit example.com maybe") == []


def test_harvest_catalogs_and_is_idempotent(tmp_path):
    _eml(tmp_path, "INBOX", "1", "a@x", "please use https://www.dropbox.com/s/x/f.pdf?dl=0 thanks")
    _eml(tmp_path, "INBOX", "2", "b@x", "no links in this one")
    s1 = ls.harvest(tmp_path, log=lambda *_: None)
    assert s1["added"] == 1 and s1["messages"] == 1 and s1["by_provider"] == {"Dropbox": 1}
    recs = ls.read_link_shares(tmp_path)
    assert len(recs) == 1
    assert recs[0]["provider"] == "Dropbox" and recs[0]["status"] == "pending"
    assert recs[0]["message_id"] == "a@x" and recs[0]["file"] is None
    s2 = ls.harvest(tmp_path, log=lambda *_: None)          # idempotent on (message_id, url)
    assert s2["added"] == 0
    assert ls.links_for_message(tmp_path, "a@x")[0]["url"].endswith("f.pdf?dl=0")
    assert ls.links_for_message(tmp_path, "b@x") == []


def test_direct_url_transforms():
    assert "dl=1" in ls._direct_url("https://www.dropbox.com/s/x/f.pdf?dl=0", "Dropbox")
    assert ls._direct_url("https://drive.google.com/file/d/ABCDEFGHIJ/view", "Google Drive") \
        == "https://drive.google.com/uc?export=download&id=ABCDEFGHIJ"
    assert ls._direct_url("https://acme.sharepoint.com/:b:/x/abc", "SharePoint/OneDrive") is None


def test_fetch_links_updates_status_and_saves(tmp_path):
    _eml(tmp_path, "INBOX", "1", "a@x",
         "ok https://www.dropbox.com/s/x/ok.pdf and dead https://drive.google.com/file/d/DEADDEADDED/view")
    _eml(tmp_path, "INBOX", "2", "b@x", "gated https://acme.sharepoint.com/:b:/x/abc")
    ls.harvest(tmp_path, log=lambda *_: None)

    def fake(url, provider):
        if "ok.pdf" in url:
            return ("downloaded", "ok.pdf", b"PDFBYTES")
        if "drive.google" in url:
            return ("dead", None, None)
        return ("needs-auth", None, None)

    s = ls.fetch_links(tmp_path, fetcher=fake, log=lambda *_: None)
    assert (s["downloaded"], s["dead"], s["needs_auth"], s["remaining"]) == (1, 1, 1, 0)
    dl = [r for r in ls.read_link_shares(tmp_path) if r["status"] == "downloaded"][0]
    assert dl["file"] and (tmp_path / dl["file"]).read_bytes() == b"PDFBYTES"
    assert ls.fetch_links(tmp_path, fetcher=fake, log=lambda *_: None)["downloaded"] == 0   # idempotent


def test_fetch_links_quarantines_infected_download(tmp_path, monkeypatch):
    # a malicious download is DELETED, flagged 'infected', and never kept for comprehension
    from clover import malware
    _eml(tmp_path, "INBOX", "1", "a@x", "evil https://www.dropbox.com/s/x/evil.exe?dl=0")
    ls.harvest(tmp_path, log=lambda *_: None)
    monkeypatch.setattr(malware, "scan_file", lambda p, **k: {
        "scanned": True, "clean": False, "threat": "Virus:DOS/EICAR_Test_File", "scanner": "defender", "note": ""})
    s = ls.fetch_links(tmp_path, fetcher=lambda u, pr: ("downloaded", "evil.exe", b"MZbytes"), log=lambda *_: None)
    assert s["downloaded"] == 0 and s["infected"] == 1
    rec = ls.read_link_shares(tmp_path)[0]
    assert rec["status"] == "infected" and rec["file"] is None and "EICAR" in rec.get("note", "")
    assert not list(tmp_path.rglob("evil.exe"))                    # the malware is gone from disk


def test_fetch_links_unverified_when_scan_cannot_complete(tmp_path, monkeypatch):
    # a download that could NOT be malware-scanned is kept but flagged 'unverified' (not 'downloaded'), so
    # the comprehender (which reads only status=='downloaded') never reads unverified content
    from clover import malware
    _eml(tmp_path, "INBOX", "1", "a@x", "doc https://www.dropbox.com/s/x/big.zip?dl=0")
    ls.harvest(tmp_path, log=lambda *_: None)
    monkeypatch.setattr(malware, "scan_file", lambda p, **k: {
        "scanned": False, "clean": None, "threat": None, "scanner": "none", "note": "scan timed out"})
    s = ls.fetch_links(tmp_path, fetcher=lambda u, pr: ("downloaded", "big.zip", b"PK"), log=lambda *_: None)
    assert s["downloaded"] == 0 and s["infected"] == 0
    rec = ls.read_link_shares(tmp_path)[0]
    assert rec["status"] == "unverified" and rec["file"] and "timed out" in rec.get("note", "")


def test_fetch_links_marks_clean_download_scanned(tmp_path, monkeypatch):
    from clover import malware
    _eml(tmp_path, "INBOX", "1", "a@x", "ok https://www.dropbox.com/s/x/ok.pdf?dl=0")
    ls.harvest(tmp_path, log=lambda *_: None)
    monkeypatch.setattr(malware, "scan_file", lambda p, **k: {
        "scanned": True, "clean": True, "threat": None, "scanner": "defender", "note": ""})
    s = ls.fetch_links(tmp_path, fetcher=lambda u, pr: ("downloaded", "ok.pdf", b"PDF"), log=lambda *_: None)
    assert s["downloaded"] == 1 and s["infected"] == 0
    rec = [r for r in ls.read_link_shares(tmp_path) if r["status"] == "downloaded"][0]
    assert rec.get("scanned") is True and (tmp_path / rec["file"]).read_bytes() == b"PDF"


def test_fetch_links_respects_limit(tmp_path):
    for i in range(3):
        _eml(tmp_path, "INBOX", str(i), f"m{i}@x", f"https://www.dropbox.com/s/x/f{i}.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    s = ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "f.pdf", b"x"), limit=2, log=lambda *_: None)
    assert s["downloaded"] == 2 and s["remaining"] == 1


def test_fetch_links_no_overwrite_same_message_same_name(tmp_path):
    # two DIFFERENT links in ONE message resolving to the same filename must NOT clobber each other
    _eml(tmp_path, "INBOX", "1", "m@x",
         "a https://www.dropbox.com/s/x/a.pdf b https://www.dropbox.com/s/y/b.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    seq = iter([b"FIRST", b"SECOND"])
    s = ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "report.pdf", next(seq)),
                       log=lambda *_: None)
    assert s["downloaded"] == 2
    dl = [r for r in ls.read_link_shares(tmp_path) if r["status"] == "downloaded"]
    files = {r["file"] for r in dl}
    assert len(files) == 2                                  # two distinct paths
    assert sorted((tmp_path / f).read_bytes() for f in files) == [b"FIRST", b"SECOND"]  # both preserved


def test_empty_download_is_not_marked_downloaded(tmp_path):
    _eml(tmp_path, "INBOX", "1", "m@x", "x https://www.dropbox.com/s/x/a.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    s = ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "a.pdf", b""), log=lambda *_: None)
    assert s["downloaded"] == 0                             # empty bytes != success
    rec = ls.read_link_shares(tmp_path)[0]
    assert rec["status"] == "error" and rec["file"] is None  # retryable, not a false success


def test_direct_url_dropbox_dl_not_last_param():
    from urllib.parse import urlparse, parse_qsl
    out = ls._direct_url("https://www.dropbox.com/s/abc/file.pdf?dl=0&extra=1", "Dropbox")
    parsed = urlparse(out)
    q = dict(parse_qsl(parsed.query))
    assert q.get("dl") == "1" and q.get("extra") == "1"     # dl flipped, other param kept
    assert parsed.path == "/s/abc/file.pdf" and "?" not in parsed.query  # well-formed


def test_filename_from_preserves_unicode_and_extension():
    hdr = {"content-disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"}
    assert ls._filename_from(hdr, "https://x/y") == "报告.pdf"           # percent-encoded CJK
    assert ls._filename_from({"content-disposition": 'attachment; filename="合同.docx"'},
                             "https://x/y") == "合同.docx"                # literal CJK
    a = ls._filename_from({"content-disposition": 'attachment; filename="报告.pdf"'}, "u")
    b = ls._filename_from({"content-disposition": 'attachment; filename="方案.pdf"'}, "u")
    assert a != b and a.endswith(".pdf") and b.endswith(".pdf")          # distinct, extension kept


def test_safe_filename_unicode_reserved_and_fallback():
    from clover.safe_name import safe_filename
    assert safe_filename("报告.pdf") == "报告.pdf"
    assert safe_filename("名称") == "名称"                                # no extension preserved
    assert safe_filename('a/b:c*.pdf') == "a-b-c.pdf"                    # path seps / reserved -> dash
    assert safe_filename("") == "download"                              # empty -> fallback


def test_fetch_links_url_dedup_across_messages(tmp_path):
    # same share link in two different emails -> downloaded once, reused for the other (no 2nd transfer)
    _eml(tmp_path, "INBOX", "1", "m1@x", "doc https://www.dropbox.com/s/SAME/f.pdf")
    _eml(tmp_path, "INBOX", "2", "m2@x", "doc https://www.dropbox.com/s/SAME/f.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    calls = []

    def fake(u, p):
        calls.append(u)
        return ("downloaded", "f.pdf", b"DATA")

    s = ls.fetch_links(tmp_path, fetcher=fake, log=lambda *_: None)
    assert s["downloaded"] == 1 and s["reused"] == 1            # one real fetch, one reuse
    assert len(calls) == 1                                       # the transfer happened exactly once
    recs = ls.read_link_shares(tmp_path)
    files = {r["file"] for r in recs if r["status"] == "downloaded"}
    assert len(recs) == 2 and len(files) == 1                    # both records point to the same file


def test_fetch_links_dedup_dead_and_auth_across_messages(tmp_path):
    # a dead link and a gated link each appear in two emails -> resolved once, reused for the duplicate
    links = ("https://drive.google.com/file/d/DEADDEADDED/view "
             "https://acme.sharepoint.com/:b:/x/gated")
    _eml(tmp_path, "INBOX", "1", "m1@x", links)
    _eml(tmp_path, "INBOX", "2", "m2@x", links)
    ls.harvest(tmp_path, log=lambda *_: None)
    calls = []

    def fake(u, p):
        calls.append(u)
        return ("dead", None, None) if "DEAD" in u else ("needs-auth", None, None)

    s = ls.fetch_links(tmp_path, fetcher=fake, log=lambda *_: None)
    assert len(calls) == 2                              # each unique url fetched once, not 4 times
    assert s["dead"] == 2 and s["needs_auth"] == 2     # yet all 4 records get a final status
    assert sum(1 for r in ls.read_link_shares(tmp_path) if r["status"] == "pending") == 0


def _emld(tmp, key, mid, body, date):
    """Like _eml but with a caller-chosen Date (drives the index date used for range-filtering)."""
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"; m["From"] = "a@x.com"; m["To"] = "b@x.com"; m["Subject"] = "Hi"
    if date:
        m["Date"] = date
    m.set_content(body)
    rel = f"INBOX/{key}.eml"
    (tmp / "INBOX").mkdir(parents=True, exist_ok=True)
    (tmp / rel).write_bytes(m.as_bytes())
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": mid, "folder": "INBOX", "key": key, "path": rel,
                            "date": date, "from": "a@x.com", "subject": "Hi", "size": 1}) + "\n")


def test_fetch_links_date_range_filter(tmp_path):
    # only links whose email falls inside [date_from, date_to] are fetched
    _emld(tmp_path, "1", "jan@x", "https://www.dropbox.com/s/x/jan.pdf", "Thu, 01 Jan 2026 00:00:00 +0000")
    _emld(tmp_path, "2", "jun@x", "https://www.dropbox.com/s/x/jun.pdf", "Mon, 15 Jun 2026 00:00:00 +0000")
    _emld(tmp_path, "3", "dec@x", "https://www.dropbox.com/s/x/dec.pdf", "Tue, 15 Dec 2026 00:00:00 +0000")
    ls.harvest(tmp_path, log=lambda *_: None)
    got = []
    fake = lambda u, p: (got.append(u), ("downloaded", "f.pdf", b"x"))[1]
    s = ls.fetch_links(tmp_path, fetcher=fake, date_from="2026-06-01", date_to="2026-06-30", log=lambda *_: None)
    assert s["downloaded"] == 1
    assert got == ["https://www.dropbox.com/s/x/jun.pdf"]   # Jan + Dec links skipped


def test_fetch_links_provider_filter(tmp_path):
    # only the chosen provider(s) get fetched; others stay pending
    _emld(tmp_path, "1", "m1@x", "https://www.dropbox.com/s/x/a.pdf", "Thu, 01 Jan 2026 00:00:00 +0000")
    _emld(tmp_path, "2", "m2@x", "https://drive.google.com/file/d/ABCDEFGHIJ/view", "Thu, 01 Jan 2026 00:00:00 +0000")
    ls.harvest(tmp_path, log=lambda *_: None)
    got = []
    fake = lambda u, p: (got.append(p), ("downloaded", "f.pdf", b"x"))[1]
    s = ls.fetch_links(tmp_path, fetcher=fake, providers=["Dropbox"], log=lambda *_: None)
    assert s["downloaded"] == 1 and got == ["Dropbox"]          # Google Drive link not fetched
    assert sum(1 for r in ls.read_link_shares(tmp_path) if r["status"] == "pending") == 1


def test_fetch_links_remaining_is_scoped(tmp_path):
    # 'remaining' must reflect the SCOPED pending count — else _auto_link_task's auto loop (which breaks on
    # remaining<=0) never terminates when a pre-existing backlog of other-message pending links exists.
    _eml(tmp_path, "INBOX", "1", "in@x", "https://www.dropbox.com/s/a/in.pdf")    # this import's message
    _eml(tmp_path, "INBOX", "2", "old@x", "https://www.dropbox.com/s/b/old.pdf")  # pre-existing backlog
    ls.harvest(tmp_path, log=lambda *_: None)
    s = ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "f.pdf", b"x"),
                       only_message_ids={"in@x"}, log=lambda *_: None)
    assert s["downloaded"] == 1
    assert s["remaining"] == 0       # the scoped link is done; the out-of-scope backlog must NOT keep it >0
    assert any(r["message_id"] == "old@x" and r["status"] == "pending"   # backlog left untouched for a manual run
               for r in ls.read_link_shares(tmp_path))


def test_harvest_scopes_to_only_message_ids(tmp_path):
    # incremental harvest: scan ONLY the just-imported messages, not the whole archive every run
    _eml(tmp_path, "INBOX", "1", "a@x", "doc https://www.dropbox.com/s/x/a.pdf")
    _eml(tmp_path, "INBOX", "2", "b@x", "doc https://www.dropbox.com/s/y/b.pdf")
    s = ls.harvest(tmp_path, only_message_ids={"a@x"}, log=lambda *_: None)
    assert s["added"] == 1                                  # message b never scanned
    assert {r["message_id"] for r in ls.read_link_shares(tmp_path)} == {"a@x"}


def test_auto_link_task_scopes_to_imported_ids(tmp_path, monkeypatch):
    # the post-import pipeline must pass the imported message-ids down to BOTH harvest and fetch
    import app.main as m
    cap = {}
    monkeypatch.setattr(m.lsmod, "harvest", lambda arch, **kw: (cap.__setitem__("h", kw), {"added": 0})[1])
    monkeypatch.setattr(m.lsmod, "fetch_links", lambda arch, **kw: (cap.__setitem__("f", kw), {"remaining": 0})[1])
    m._auto_link_task(tmp_path, do_harvest=True, do_fetch=True, only_message_ids={"a@x", "b@x"})
    assert cap["h"]["only_message_ids"] == {"a@x", "b@x"}
    assert cap["f"]["only_message_ids"] == {"a@x", "b@x"}


def test_fetch_links_undated_excluded_when_date_filter_active(tmp_path):
    # an email with no parseable date must NOT match a dated query (you asked for a range)
    _emld(tmp_path, "1", "nd@x", "https://www.dropbox.com/s/x/nd.pdf", "")
    ls.harvest(tmp_path, log=lambda *_: None)
    s = ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "f.pdf", b"x"),
                       date_from="2026-01-01", log=lambda *_: None)
    assert s["downloaded"] == 0
    assert ls.read_link_shares(tmp_path)[0]["status"] == "pending"   # left for an unfiltered run


def test_fetch_links_oversize_needs_confirm_then_confirm(tmp_path):
    _eml(tmp_path, "INBOX", "1", "m@x", "big https://www.dropbox.com/s/x/big.zip")
    ls.harvest(tmp_path, log=lambda *_: None)

    def fake(u, p, lb):                                          # honors the size limit it's given
        if lb is not None:
            return ("oversize", None, 5_000_000_000)            # ~5 GB while a cap is in force
        return ("downloaded", "big.zip", b"BIGDATA")            # confirmed -> no cap -> full download

    s = ls.fetch_links(tmp_path, fetcher=fake, confirm_over_mb=1024, log=lambda *_: None)
    assert s["needs_confirm"] == 1 and s["downloaded"] == 0
    rec = ls.read_link_shares(tmp_path)[0]
    assert rec["status"] == "needs-confirm" and rec["size"] == 5_000_000_000
    assert not (tmp_path / "_linkfiles").exists()               # nothing kept on disk

    ls.mark_confirmed(tmp_path, "m@x", rec["url"])              # user clicks "download anyway"
    rec2 = ls.read_link_shares(tmp_path)[0]
    assert rec2["status"] == "pending" and rec2["confirmed"] is True
    s2 = ls.fetch_links(tmp_path, fetcher=fake, confirm_over_mb=1024, log=lambda *_: None)
    assert s2["downloaded"] == 1                                 # cap bypassed for the confirmed link
    rec3 = ls.read_link_shares(tmp_path)[0]
    assert rec3["status"] == "downloaded" and (tmp_path / rec3["file"]).read_bytes() == b"BIGDATA"


def test_read_link_shares_cache_invalidates_and_is_immutable(tmp_path):
    _eml(tmp_path, "INBOX", "1", "a@x", "x https://www.dropbox.com/s/x/a.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    r1 = ls.read_link_shares(tmp_path)
    assert ls.read_link_shares(tmp_path) is r1            # cached (same object) while file unchanged
    url = r1[0]["url"]
    ls._update_records(tmp_path, {("a@x", url): {"status": "dead"}})
    r2 = ls.read_link_shares(tmp_path)
    assert r2 is not r1 and r2[0]["status"] == "dead"     # re-read after the write (mtime changed)
    assert r1[0]["status"] == "pending"                  # _update_records did NOT mutate the cached rows


def test_links_for_member_matches_by_path_for_headerless(tmp_path):
    # a headerless email's harvested record keys on the index id ('uid_<key>'), but its thread member
    # id is the 'path::<rel>' fallback — links_for_member must still surface it (matched by .eml path).
    _eml(tmp_path, "INBOX", "9", "uid_9", "x https://www.dropbox.com/s/x/h.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    rec = ls.read_link_shares(tmp_path)[0]
    assert rec["message_id"] == "uid_9" and rec["eml"] == "INBOX/9.eml"
    assert ls.links_for_message(tmp_path, "path::INBOX/9.eml") == []                 # id-only lookup misses
    got = ls.links_for_member(tmp_path, "path::INBOX/9.eml", [{"path": "INBOX/9.eml"}])
    assert len(got) == 1 and got[0]["url"].endswith("h.pdf")                         # path fallback finds it


def test_fetch_links_reports_progress(tmp_path):
    for i in range(3):
        _eml(tmp_path, "INBOX", str(i), f"m{i}@x", f"https://www.dropbox.com/s/x/f{i}.pdf")
    ls.harvest(tmp_path, log=lambda *_: None)
    seen = []
    ls.fetch_links(tmp_path, fetcher=lambda u, p: ("downloaded", "f.pdf", b"x"),
                   log=lambda *_: None, progress=lambda **k: seen.append(k))
    assert len(seen) == 3 and "current" in seen[0] and seen[0]["total"] == 3


def test_link_status_route_exposes_triage(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _eml(tmp_path, "INBOX", "1", "a@x", "https://drive.google.com/file/d/DEAD/view")
    _eml(tmp_path, "INBOX", "2", "b@x", "https://contoso.sharepoint.com/:b:/x/abc")
    ls.harvest(tmp_path, log=lambda *_: None)
    ls.fetch_links(tmp_path, log=lambda *_: None,
                   fetcher=lambda u, p: ("dead", None, None) if "drive" in u else ("needs-auth", None, None))
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    s = TestClient(m.app).get("/threads/link-status").json()
    assert s["stats"].get("dead") == 1 and s["stats"].get("needs-auth") == 1
    assert len(s["needs_auth"]) == 1 and len(s["dead"]) == 1


def test_fetch_links_route_forwards_filters(tmp_path, monkeypatch):
    # the Fetch-files button must forward date range + chosen providers to fetch_links
    import threading
    from starlette.testclient import TestClient
    import app.main as m
    m._linktask["running"] = False                                   # clean slate
    captured, done = {}, threading.Event()
    monkeypatch.setattr(m.lsmod, "fetch_links",
                        lambda arch, **kw: (captured.update(kw), done.set(), {"remaining": 0})[2])
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    r = TestClient(m.app).post("/threads/fetch-links",
                               data={"date_from": "2026-06-01", "date_to": "2026-06-30",
                                     "providers": "Dropbox, Google Drive"})
    assert r.json()["ok"] is True and done.wait(2)
    assert captured["date_from"] == "2026-06-01" and captured["date_to"] == "2026-06-30"
    assert captured["providers"] == ["Dropbox", "Google Drive"]      # comma-split + trimmed


def test_fetch_links_route_blank_filters_become_none(tmp_path, monkeypatch):
    # empty fields = "fetch everything" -> filters passed as None, not "" / [""]
    import threading
    from starlette.testclient import TestClient
    import app.main as m
    m._linktask["running"] = False
    captured, done = {}, threading.Event()
    monkeypatch.setattr(m.lsmod, "fetch_links",
                        lambda arch, **kw: (captured.update(kw), done.set(), {"remaining": 0})[2])
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: {"auth": {"imap": {}}, "archive_path": str(tmp_path)})
    TestClient(m.app).post("/threads/fetch-links", data={"date_from": "", "date_to": "", "providers": ""})
    assert done.wait(2)
    assert captured["date_from"] is None and captured["date_to"] is None and captured["providers"] is None
