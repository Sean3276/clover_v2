import json
import socket
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage

from clover import archive as ar
from clover.errors import friendly_conn_error
from clover.sources.base import MailSource
from clover.sources.imap_source import _parse_fetch_meta


def _raw(mid: str) -> bytes:
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}>"
    m["From"] = "a@b.com"
    m["Subject"] = "s"
    m["Date"] = "Thu, 11 Jun 2026 01:22:12 +0000"
    m.set_content("hi")
    return m.as_bytes()


class _FakeSource(MailSource):
    """A fake source where message '2' simulates a hung fetch until force_close()d."""

    def __init__(self, msgs):
        super().__init__({}, "")
        self.msgs = msgs
        self.reconnects = 0
        self._killed = False

    def open(self): self._killed = False
    def close(self): pass
    def test(self): return True, "ok"
    def folders(self): return [{"name": "INBOX", "messages": len(self.msgs)}]
    def select(self, folder): return "1"
    def message_keys(self): return list(self.msgs.keys())

    def fetch_raw(self, key):
        v = self.msgs[key]
        if v == "SLOW":
            for _ in range(200):                 # blocks until force_close() trips _killed
                if self._killed:
                    raise OSError("socket closed")
                time.sleep(0.02)
            return b"late"
        return v

    def force_close(self): self._killed = True

    def reconnect(self, folder=None):
        self.reconnects += 1
        self._killed = False


class _DeadSource(MailSource):
    """Connection is dead and cannot be re-established."""

    def __init__(self): super().__init__({}, "")
    def open(self): pass
    def close(self): pass
    def test(self): return True, "ok"
    def folders(self): return [{"name": "INBOX", "messages": 5}]
    def select(self, folder): return "1"
    def message_keys(self): return ["1", "2", "3", "4", "5"]
    def fetch_raw(self, key): raise OSError("connection dead")
    def force_close(self): pass
    def reconnect(self, folder=None): raise OSError("getaddrinfo failed")


def test_run_until_complete_retries_until_success():
    calls = {"n": 0}

    def run_once():
        calls["n"] += 1
        return {"aborted": True, "saved": 5} if calls["n"] < 3 else {"aborted": False, "saved": 2}

    out = ar.run_until_complete(run_once, sleep=lambda *_: None, log=lambda *_: None)
    assert out["attempts"] == 3 and out["manifest"]["aborted"] is False


def test_run_until_complete_pauses_on_no_progress():
    def run_once():
        return {"aborted": True, "saved": 0}        # never makes progress
    out = ar.run_until_complete(run_once, sleep=lambda *_: None, log=lambda *_: None, max_idle=4)
    assert out["attempts"] == 4                       # paused after max_idle zero-progress attempts


def test_run_until_complete_honors_stop():
    state = {"stop": False}

    def run_once():
        state["stop"] = True                          # user hits Stop during the first attempt
        return {"aborted": True, "saved": 1}

    out = ar.run_until_complete(run_once, should_stop=lambda: state["stop"],
                                sleep=lambda *_: None, log=lambda *_: None)
    assert out["attempts"] == 1


def test_run_until_complete_no_auto_resume_runs_once():
    calls = {"n": 0}

    def run_once():
        calls["n"] += 1
        return {"aborted": True, "saved": 0}
    out = ar.run_until_complete(run_once, auto_resume=False, sleep=lambda *_: None, log=lambda *_: None)
    assert out["attempts"] == 1


class _SelectFailSource(MailSource):
    """Connection dropped at a folder boundary: select() fails and cannot be recovered."""

    def __init__(self): super().__init__({}, "")
    def open(self): pass
    def close(self): pass
    def test(self): return True, "ok"
    def folders(self): return [{"name": "INBOX"}]
    def select(self, folder): raise OSError("getaddrinfo failed")
    def message_keys(self): return []
    def fetch_raw(self, key): return b""
    def force_close(self): pass
    def reconnect(self, folder=None): raise OSError("still down")


def test_run_archive_aborts_when_select_fails_at_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(ar.time, "sleep", lambda *a, **k: None)
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX", "Sent"], "archive_path": str(tmp_path)}
    logs = []
    manifest = ar.run_archive(cfg, "pw", log=logs.append, source=_SelectFailSource())
    assert manifest["aborted"] is True                # must NOT silently report FINISHED
    assert manifest["saved"] == 0
    assert not any("FINISHED" in line for line in logs)
    assert any("folder boundary" in line for line in logs)


def test_run_archive_aborts_when_unrecoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(ar.time, "sleep", lambda *a, **k: None)   # no backoff delay in test
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    logs = []
    manifest = ar.run_archive(cfg, "pw", log=logs.append, source=_DeadSource(), fetch_timeout=5)
    assert manifest["aborted"] is True
    assert manifest["saved"] == 0
    assert manifest["errors"] == 1          # stopped after the FIRST failure — no 2000-error cascade
    assert any("connection lost" in line for line in logs)


def test_run_archive_stops_promptly_midrun(tmp_path):
    msgs = {str(i): _raw(f"m{i}@x") for i in range(10)}
    fake = _FakeSource(msgs)
    n = {"c": 0}

    def stop():
        n["c"] += 1
        return n["c"] > 3              # let ~3 through, then request stop

    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    m = ar.run_archive(cfg, "pw", log=lambda *_: None, source=fake, should_stop=stop)
    assert m["stopped"] is True
    assert m["aborted"] is False
    assert m["saved"] < 10            # stopped before archiving all 10


def test_run_archive_skips_hung_fetch(tmp_path):
    fake = _FakeSource({"1": _raw("a@x"), "2": "SLOW", "3": _raw("b@x")})
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    logs = []
    manifest = ar.run_archive(cfg, "pw", log=logs.append, source=fake, fetch_timeout=0.4)
    assert manifest["saved"] == 2          # messages 1 and 3 archived
    assert manifest["errors"] == 1         # message 2 timed out and was skipped
    assert fake.reconnects >= 1            # reconnected after the hang
    assert len(ar.read_index(tmp_path)) == 2
    assert any("exceeded" in line for line in logs)


def test_eml_filename_from_message_id():
    assert ar.eml_filename("<abc.123@host>", "42") == "abc.123-host.eml"


def test_eml_filename_fallback_to_uid():
    assert ar.eml_filename("", "42") == "uid_42.eml"
    assert ar.eml_filename(None, "7") == "uid_7.eml"


def test_eml_filename_untitled_falls_back_to_key():
    assert ar.eml_filename("<@@@>", "5") == "uid_5.eml"


def test_message_id_of_parses_raw():
    m = EmailMessage()
    m["Message-ID"] = "<hello.world@x.com>"
    m.set_content("hi")
    assert ar.message_id_of(m.as_bytes()) == "hello.world@x.com"


def test_folder_subpath_preserves_hierarchy():
    assert str(ar.folder_subpath("Parent/Child")).replace("\\", "/") == "Parent/Child"
    assert ar.folder_subpath("A/B") != ar.folder_subpath("A-B")


def test_read_index_skips_non_dict_and_blank(tmp_path):
    (tmp_path / "_index.jsonl").write_text(
        '123\n"x"\n\n{"id":"<a>","folder":"INBOX","key":"1"}\nnot json\n', encoding="utf-8"
    )
    rows = ar.read_index(tmp_path)
    assert len(rows) == 1 and rows[0]["id"] == "<a>"


def test_index_summary_empty(tmp_path):
    assert ar.index_summary(tmp_path) == {"total": 0, "unique_ids": 0, "per_folder": {}}


def test_existing_keys_and_summary(tmp_path):
    rows = [
        {"id": "<a@x>", "folder": "INBOX", "key": "1", "validity": "100"},
        {"id": "<a@x>", "folder": "Sent", "key": "9", "validity": "200"},   # same email across folders
        {"id": "<b@x>", "folder": "INBOX", "key": "2", "validity": "100"},
        {"id": "uid_3", "folder": "INBOX", "key": "3", "validity": "100"},   # synthetic id
    ]
    with open(tmp_path / "_index.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    assert ar.existing_keys(tmp_path) == {
        ("INBOX", "100", "1"), ("Sent", "200", "9"), ("INBOX", "100", "2"), ("INBOX", "100", "3"),
    }
    s = ar.index_summary(tmp_path)
    assert s["total"] == 4
    assert s["unique_ids"] == 2          # <a@x> once; uid_3 excluded
    assert s["per_folder"] == {"INBOX": 3, "Sent": 1}


# ---------------------------------------------------------------- friendly errors
def test_friendly_error_dns_is_no_internet():
    msg = friendly_conn_error(socket.gaierror(11001, "getaddrinfo failed"))
    assert "No internet" in msg and "11001" in msg


def test_friendly_error_timeout():
    assert "in time" in friendly_conn_error(TimeoutError("timed out")).lower()


def test_friendly_error_refused():
    assert "refused" in friendly_conn_error(ConnectionRefusedError(10061, "refused")).lower()


def test_friendly_error_auth():
    msg = friendly_conn_error(Exception("b'[AUTHENTICATIONFAILED] Authentication failed.'"))
    assert "Login failed" in msg


def test_friendly_error_fallback_keeps_type():
    assert "Couldn't reach" in friendly_conn_error(ValueError("weird"))


def test_reconcile_detects_missing_and_orphans(tmp_path):
    (tmp_path / "INBOX").mkdir(parents=True)
    (tmp_path / "INBOX/a.eml").write_bytes(b"x")          # indexed + present
    (tmp_path / "INBOX/orphan.eml").write_bytes(b"y")     # present, not indexed
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "<a>", "folder": "INBOX", "key": "1", "path": "INBOX/a.eml"}) + "\n")
        f.write(json.dumps({"id": "<b>", "folder": "INBOX", "key": "2", "path": "INBOX/b.eml"}) + "\n")  # b missing
    rep = ar.reconcile(tmp_path)
    assert rep["total_indexed"] == 2 and rep["total_on_disk"] == 2
    assert rep["missing"] == ["INBOX/b.eml"] and rep["orphans"] == ["INBOX/orphan.eml"]
    assert rep["ok"] is False
    assert rep["per_folder"]["INBOX"] == {"indexed": 2, "on_disk": 2}


def test_reconcile_ignores_linkfiles(tmp_path):
    (tmp_path / "INBOX").mkdir(parents=True)
    (tmp_path / "INBOX/a.eml").write_bytes(b"x")
    (tmp_path / "_linkfiles" / "mid").mkdir(parents=True)
    (tmp_path / "_linkfiles/mid/shared.eml").write_bytes(b"y")     # a downloaded .eml link file
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "<a>", "folder": "INBOX", "key": "1", "path": "INBOX/a.eml"}) + "\n")
    rep = ar.reconcile(tmp_path)
    assert rep["ok"] is True and rep["orphans"] == [] and rep["total_on_disk"] == 1   # _linkfiles ignored


def test_reconcile_ok_when_consistent(tmp_path):
    (tmp_path / "INBOX").mkdir(parents=True)
    (tmp_path / "INBOX/a.eml").write_bytes(b"x")
    with (tmp_path / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "<a>", "folder": "INBOX", "key": "1", "path": "INBOX/a.eml"}) + "\n")
    rep = ar.reconcile(tmp_path)
    assert rep["ok"] is True and not rep["missing"] and not rep["orphans"]


# ---------------------------------------------------------------- selection filters
def test_parse_fetch_meta_typical():
    uid, dt, size = _parse_fetch_meta(b'7 (UID 42 INTERNALDATE "11-Jun-2026 01:22:12 +0000" RFC822.SIZE 4096)')
    assert uid == "42" and size == 4096
    assert (dt.year, dt.month, dt.day) == (2026, 6, 11)


def test_parse_fetch_meta_space_padded_day():
    uid, dt, size = _parse_fetch_meta(b'1 (UID 5 RFC822.SIZE 10 INTERNALDATE " 1-Jul-2026 00:00:00 +0000")')
    assert uid == "5" and size == 10 and dt.day == 1


def test_parse_fetch_meta_garbage():
    assert _parse_fetch_meta(b"garbage") == (None, None, 0)
    assert _parse_fetch_meta(12345) == (None, None, 0)        # non-bytes input


def _meta(y, mo, d, size=100):
    return {"date": datetime(y, mo, d, tzinfo=timezone.utc), "size": size}


def test_meta_matches_date_range():
    m = _meta(2026, 6, 15)
    assert ar._meta_matches(m, date(2026, 6, 1), date(2026, 6, 30), None)
    assert not ar._meta_matches(m, date(2026, 7, 1), None, None)        # before range
    assert not ar._meta_matches(m, None, date(2026, 6, 1), None)        # after range


def test_meta_matches_size_threshold():
    assert ar._meta_matches(_meta(2026, 6, 15, size=2000), None, None, 1000)
    assert not ar._meta_matches(_meta(2026, 6, 15, size=500), None, None, 1000)


def test_meta_matches_missing_date():
    assert not ar._meta_matches({"date": None, "size": 100}, date(2026, 1, 1), None, None)  # excluded
    assert ar._meta_matches({"date": None, "size": 100}, None, None, 50)                    # no date filter -> ok


class _FilterSource(MailSource):
    """Fake source for filter tests. server: None=unsupported, 'raise'=errors, or a callable."""

    def __init__(self, meta, server=None, drop_meta=()):
        super().__init__({}, "")
        self.meta = meta
        self.server = server
        self.drop_meta = set(drop_meta)        # keys present in the folder but with unreadable metadata
        self.meta_calls = 0

    def open(self): pass
    def close(self): pass
    def test(self): return True, "ok"
    def folders(self): return [{"name": "INBOX"}]
    def select(self, folder): return "1"
    def message_keys(self): return list(self.meta.keys())
    def fetch_raw(self, key): return b""

    def search(self, *, date_from=None, date_to=None, size_min=None):
        if self.server == "raise":
            raise OSError("server search unavailable")
        if self.server is None:
            return None
        return self.server(date_from, date_to, size_min)

    def message_meta(self, keys, progress=None):
        self.meta_calls += 1
        if progress:
            progress(len(keys), len(keys))
        return {k: self.meta[k] for k in keys if k in self.meta and k not in self.drop_meta}


def _noop_prep(stage, done=0, total=0): pass


def test_select_keys_no_filters_returns_all():
    src = _FilterSource({"1": _meta(2026, 1, 1), "2": _meta(2026, 2, 2)})
    keys = ar._select_keys(src, "INBOX", {}, lambda *_: None, _noop_prep)
    assert set(keys) == {"1", "2"}
    assert src.meta_calls == 0                       # no metadata fetch when nothing is filtered


def test_select_keys_uses_server_search():
    meta = {"1": _meta(2026, 1, 1), "2": _meta(2026, 6, 6), "3": _meta(2026, 12, 12)}
    src = _FilterSource(meta, server=lambda df, dt, sm: ["2"])
    f = {"date_from": date(2026, 5, 1), "date_to": date(2026, 7, 1)}
    keys = ar._select_keys(src, "INBOX", f, lambda *_: None, _noop_prep)
    assert keys == ["2"]
    assert src.meta_calls == 0                       # server did the filtering


def test_select_keys_client_fallback_when_server_fails():
    meta = {"1": _meta(2026, 1, 1), "2": _meta(2026, 6, 6), "3": _meta(2026, 12, 12)}
    src = _FilterSource(meta, server="raise")
    f = {"date_from": date(2026, 5, 1), "date_to": date(2026, 7, 1)}
    prog = []
    keys = ar._select_keys(src, "INBOX", f, lambda *_: None,
                           lambda stage, done=0, total=0: prog.append(stage))
    assert keys == ["2"]
    assert src.meta_calls == 1                        # fell back to client-side metadata filter
    assert "meta" in prog


def test_select_keys_top_n_ranks_by_size():
    meta = {"1": _meta(2026, 1, 1, size=100), "2": _meta(2026, 1, 2, size=900),
            "3": _meta(2026, 1, 3, size=500)}
    src = _FilterSource(meta)                          # server None -> size everything client-side
    keys = ar._select_keys(src, "INBOX", {"top_n": 2}, lambda *_: None, _noop_prep)
    assert keys == ["2", "3"]                          # two largest, in descending size order
    assert src.meta_calls == 1


def test_select_keys_top_n_within_date_window():
    meta = {"1": _meta(2026, 1, 1, size=900), "2": _meta(2026, 6, 6, size=500),
            "3": _meta(2026, 6, 7, size=800)}
    src = _FilterSource(meta, server=lambda df, dt, sm: ["2", "3"])   # server narrows by date
    f = {"date_from": date(2026, 5, 1), "date_to": date(2026, 7, 1), "top_n": 1}
    keys = ar._select_keys(src, "INBOX", f, lambda *_: None, _noop_prep)
    assert keys == ["3"]                               # largest within the date-narrowed set


def test_select_keys_stop_before_scan_raises():
    import pytest
    src = _FilterSource({"1": _meta(2026, 1, 1)}, server="raise")
    f = {"date_from": date(2026, 1, 1)}
    with pytest.raises(ar._PrepStopped):
        ar._select_keys(src, "INBOX", f, lambda *_: None, _noop_prep, should_stop=lambda: True)


def test_select_keys_stop_between_metadata_batches():
    import pytest
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2          # False at the two pre-checks; True once a batch reports

    src = _FilterSource({"1": _meta(2026, 1, 1)}, server="raise")   # forces client-meta path
    f = {"date_from": date(2026, 1, 1)}
    with pytest.raises(ar._PrepStopped):
        ar._select_keys(src, "INBOX", f, lambda *_: None, _noop_prep, should_stop=stop)


def test_select_keys_excludes_and_warns_unreadable_meta():
    meta = {"1": _meta(2026, 6, 1, size=100), "2": _meta(2026, 6, 2, size=100)}
    src = _FilterSource(meta, server="raise", drop_meta={"2"})   # client path; key 2 unreadable
    logs = []
    keys = ar._select_keys(src, "INBOX", {"date_from": date(2026, 6, 1)}, logs.append, _noop_prep)
    assert keys == ["1"]                                          # unmeasurable key excluded, not silently kept
    assert any("unreadable metadata" in ln for ln in logs)


def test_select_keys_topn_excludes_unreadable_meta():
    meta = {"1": _meta(2026, 1, 1, size=900), "2": _meta(2026, 1, 2, size=800)}
    src = _FilterSource(meta, drop_meta={"2"})                    # top-N sizes client-side
    keys = ar._select_keys(src, "INBOX", {"top_n": 5}, lambda *_: None, _noop_prep)
    assert keys == ["1"]                                          # unsizable key not ranked into top-N


def test_run_archive_stops_during_prep(tmp_path):
    # Stop requested while filtering -> clean user-stop, nothing fetched, no abort.
    meta = {str(i): _meta(2026, 1, i % 28 + 1) for i in range(5)}
    src = _FilterSource(meta, server="raise")
    cfg = {"auth": {"imap": {}}, "folders": ["INBOX"], "archive_path": str(tmp_path)}
    m = ar.run_archive(cfg, "pw", log=lambda *_: None, source=src,
                       filters={"date_from": date(2026, 1, 1)}, should_stop=lambda: True)
    assert m["stopped"] is True
    assert m["aborted"] is False
    assert m["saved"] == 0
