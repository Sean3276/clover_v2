import json
import time
from email.message import EmailMessage

from clover import archive as ar
from clover.sources.base import MailSource


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
