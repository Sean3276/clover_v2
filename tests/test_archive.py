import json
from email.message import EmailMessage

from clover import archive as ar


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
