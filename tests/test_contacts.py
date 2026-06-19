import json

from clover import comprehend as cp
from clover import contacts


def _index_row(tmp, folder, key, frm):
    (tmp / folder).mkdir(parents=True, exist_ok=True)
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": key, "folder": folder, "key": key,
                            "path": f"{folder}/{key}.eml", "from": frm}) + "\n")


def test_consolidate_merges_ai_and_headers_dedup_by_email(tmp_path):
    cp.save_comprehension(tmp_path, {"thread_id": "t1", "contacts": [
        {"name": "Alice Tan", "position": "PM", "company": "Acme", "phone": "+65 1234", "email": "alice@acme.com"}]})
    _index_row(tmp_path, "INBOX", "1", "Alice <alice@acme.com>")
    _index_row(tmp_path, "INBOX", "2", "Alice Tan <alice@acme.com>")
    _index_row(tmp_path, "INBOX", "3", "bob@x.com")

    out = contacts.consolidate(tmp_path)
    by = {c["email"]: c for c in out}
    a = by["alice@acme.com"]
    assert a["name"] == "Alice Tan" and a["position"] == "PM" and a["company"] == "Acme"
    assert a["phone"] == "+65 1234" and a["count"] == 2           # AI fields + header count merged
    assert by["bob@x.com"]["count"] == 1
    assert out[0]["email"] == "alice@acme.com"                    # busiest sender first
