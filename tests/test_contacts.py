import json

from clover import comprehend as cp
from clover import contacts


def _index_row(tmp, folder, key, frm, date=""):
    (tmp / folder).mkdir(parents=True, exist_ok=True)
    with (tmp / "_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": key, "folder": folder, "key": key, "date": date,
                            "path": f"{folder}/{key}.eml", "from": frm}) + "\n")


def _write_eml(tmp, folder, key, frm, body, date="Mon, 01 Jan 2024 09:00:00 +0800"):
    (tmp / folder).mkdir(parents=True, exist_ok=True)
    raw = (f"From: {frm}\r\nTo: me@here.com\r\nDate: {date}\r\n"
           f"Subject: hi\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}")
    (tmp / folder / f"{key}.eml").write_bytes(raw.encode("utf-8"))
    _index_row(tmp, folder, key, frm, date=date)


# ── signature parsing (the engine) ───────────────────────────────────────────
def test_parse_signature_reads_zone_after_signoff():
    sig = ("Hi team,\n\nPlease see attached.\n\n"
           "Best regards,\nAlice Tan\nSenior Project Manager\nNorthwind Engineering Pte Ltd\n"
           "Mobile: +65 9123 4567\nFax: +65 6000 0000\n")
    out = contacts.parse_signature(sig)
    assert out["company"] == "Northwind Engineering Pte Ltd"
    assert "Manager" in out["position"]
    assert out["phone"] == "+65 9123 4567"          # labelled mobile wins over the fax line


def test_parse_signature_ignores_quoted_reply():
    sig = ("Noted, thanks.\n\nRegards,\nBob Lee\n\n"
           "> Best regards,\n> Gary Tan\n> Hooli Holdings Pte Ltd\n> Mobile: +65 8000 1111\n")
    out = contacts.parse_signature(sig)
    assert "Hooli" not in out["company"] and out["company"] == ""   # quoted block is cut off
    assert out["phone"] == ""


def test_parse_signature_ignores_salutation_and_picks_real_signer():
    sig = ("Dear Hooli Holdings Pte Ltd,\n\nWe refer to your email.\n\n"
           "Regards,\nLim Wei\nNorthwind Builders Pte Ltd\n")
    out = contacts.parse_signature(sig)
    assert out["company"] == "Northwind Builders Pte Ltd"   # the salutation's firm (recipient) is not taken


def test_parse_signature_drops_confidentiality_footer():
    sig = ("Regards,\nChan Boon\nMitsubishi Elevator Pte Ltd\n\n"
           "This email is confidential. If received in error, please notify Mitsubishi Elevator (Singapore) Pte Ltd.\n")
    out = contacts.parse_signature(sig)
    assert out["company"] == "Mitsubishi Elevator Pte Ltd"


def test_parse_signature_rejects_long_digit_run():
    assert contacts.parse_signature("Regards,\nJo\nRef 12345678901234567890\n")["phone"] == ""


def test_parse_signature_company_allows_trailing_comma():
    out = contacts.parse_signature("Regards,\nWin\nADF Waterproof Pte Ltd,\n25 Mandai Estate\n")
    assert out["company"] == "ADF Waterproof Pte Ltd"


def test_rebuild_recovers_company_from_domain_match(tmp_path):
    # signature has the firm name but NO legal suffix ("Contoso"); the email domain confirms it
    _write_eml(tmp_path, "INBOX", "1", "James <james@contoso.com>",
               "Hi\n\nRegards,\nSam Lee\nAssociate\nContoso\nArchitecture Planning Interiors\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["james@contoso.com"]["company"] == "Contoso"


# ── company key normalisation ─────────────────────────────────────────────────
def test_company_key_normalises_case_punct_suffix():
    assert contacts.company_key("Northwind Pte. Ltd") == contacts.company_key("NORTHWIND PTE LTD") == "northwind"
    assert "globexconstruction" in contacts.company_key(
        "Globex Construction Company Limited (Singapore Branch)")


def test_company_key_collapses_company_limited_vs_ltd():
    assert contacts.company_key("Globex Construction Company Limited") == \
           contacts.company_key("Globex Construction Company Ltd")
    assert contacts.company_key("Pte Ltd") == "" and contacts.company_key("Sdn Bhd") == ""   # bare suffix


def test_norm_company_caps():
    assert contacts._norm_company_caps("MEGA BUILD CO. PTE LTD") == "Mega Build Co. Pte Ltd"
    assert contacts._norm_company_caps("HOOLI HOLDINGS PTE. LTD") == "Hooli Holdings Pte. Ltd"
    assert contacts._norm_company_caps("BCA") == "BCA"                                        # acronym kept
    assert contacts._norm_company_caps("Globex Construction Company Ltd") == \
           "Globex Construction Company Ltd"                                    # mixed kept


def test_parse_signature_recovers_wrapped_company():
    out = contacts.parse_signature("Regards,\nEve Tan\nGlobex (S)\nPte Ltd\n")
    assert "Globex" in out["company"] and "Pte Ltd" in out["company"]


def test_parse_signature_rejects_bare_legal_suffix():
    assert contacts.parse_signature("Regards,\nPte Ltd\n")["company"] == ""


def test_rebuild_merges_globex_limited_and_ltd(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "A Tan <a@globex.com>", "Hi\n\nRegards,\nA Tan\nGlobex Construction Company Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "B Lee <b@globex.com>", "Hi\n\nRegards,\nB Lee\nGlobex Construction Company Ltd\n")
    _write_eml(tmp_path, "INBOX", "3", "C Ng <c@globex.com>", "Hi\n\nRegards,\nC Ng\nGlobex Construction Company Limited\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["a@globex.com"]["company_key"] == by["c@globex.com"]["company_key"]   # one firm, not two


# ── identity = email domain ───────────────────────────────────────────────────
def test_rebuild_groups_by_domain_even_without_signature(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Alice <alice@northwind.com>",
               "Hi\n\nRegards,\nAlice Tan\nNorthwind Engineering Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Bob <bob@northwind.com>", "Hi\n\nthanks\nBob\n")   # no firm in sig
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["alice@northwind.com"]["company"] == "Northwind Engineering Pte Ltd"
    assert by["bob@northwind.com"]["company"] == "Northwind Engineering Pte Ltd"               # inherited from domain


def test_rebuild_freemail_sender_is_individual(tmp_path):
    # free-mail senders go to Individuals (no firm) — their signature company is too risky to trust
    # (often a quoted-without-> block from another firm), per the lengshijia/mindjetsg misgrouping.
    _write_eml(tmp_path, "INBOX", "1", "Carol <carol@gmail.com>",
               "Hi\n\nRegards,\nCarol\nCarol Trading Pte Ltd\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["carol@gmail.com"]["company"] == "" and by["carol@gmail.com"]["company_key"] == ""


def test_clean_company_strips_leading_junk():
    assert contacts._clean_company("for A&B Foundation Specialist Pte Ltd") == "A&B Foundation Specialist Pte Ltd"
    assert contacts._clean_company("Northwind Pte Ltd") == "Northwind Pte Ltd"


def test_rebuild_merges_near_duplicate_company_names(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Aaron J <aaron@grp.sg>", "Hi\n\nRegards,\nAaron J\nHooli Holdings Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Bee K <bee@grp.sg>", "Hi\n\nRegards,\nBee K\nHooli Holdings Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "3", "Cay L <cay@grp.sg>", "Hi\n\nRegards,\nCay L\nHooli Holding Pte Ltd\n")  # typo
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["aaron@grp.sg"]["company_key"] == by["cay@grp.sg"]["company_key"]      # merged
    assert by["cay@grp.sg"]["company"] == "Hooli Holdings Pte Ltd"                  # canonical (2 vs 1)


def test_clean_name_strips_tag_and_rejects_domain():
    assert contacts._clean_name("Dan Goh | Initech SG", "derrick@initech.com") == "Dan Goh"
    assert contacts._clean_name("Albert TANG (BCA)", "albert_tang@bca.gov.sg") == "Albert TANG"   # name == local-part is fine
    assert contacts._clean_name("VENDOR. COM", "projects@vendor.com") == ""           # domain, not a person
    assert contacts._clean_name("projects", "projects@x.com") == ""                               # role mailbox


def test_name_from_local_part():
    assert contacts._name_from_local("alex.tan@x.com") == "Alex Tan"
    assert contacts._name_from_local("projects@x.com") == ""           # role
    assert contacts._name_from_local("awp240632c@x.com") == ""         # has digits -> not a name


def test_own_signature_overrides_domain_consensus(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Bob Tan <bob@grp.sg>", "Hi\n\nRegards,\nBob Tan\nGlobex Construction Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Cathy Ng <cathy@grp.sg>", "Hi\n\nRegards,\nCathy Ng\nGlobex Construction Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "3", "Alex Tan <alex@grp.sg>",
               "Hi\n\nRegards,\nAlex Tan\nSenior Engineer\nHooli Holdings Pte Ltd\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["bob@grp.sg"]["company"] == "Globex Construction Pte Ltd"
    assert by["alex@grp.sg"]["company"] == "Hooli Holdings Pte Ltd"   # his OWN signature wins over the domain


def test_quoted_foreign_signature_falls_back_to_domain(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Bob Tan <bob@grp.sg>", "Hi\n\nRegards,\nBob Tan\nGlobex Construction Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Dave Wong <dave@grp.sg>",      # Dave's mail shows ALEX's signature
               "fyi\n\nRegards,\nAlex Tan\nHooli Holdings Pte Ltd\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["dave@grp.sg"]["company"] == "Globex Construction Pte Ltd"   # not Hooli (name != Dave)


def test_rebuild_discards_foreign_signature(tmp_path):
    # northwind.com owns "Initech Solutions Pte Ltd" (2 senders); a globex.com.my sender whose reply embeds that
    # signature must NOT be filed under Northwind — the whole foreign block is dropped.
    _write_eml(tmp_path, "INBOX", "1", "Alice <alice@northwind.com>", "Hi\n\nRegards,\nAlice\nInitech Solutions Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Bob <bob@northwind.com>", "Hi\n\nRegards,\nBob\nInitech Solutions Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "3", "Leng <leng@globex.com.my>",
               "noted\n\nRegards,\nBen Ong\nDirector\nInitech Solutions Pte Ltd\nHP: +65 9383 9173\n")
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert by["leng@globex.com.my"]["company"] == ""           # foreign Northwind signature discarded
    assert by["leng@globex.com.my"]["company_key"] == "globex.com.my"   # grouped by its own domain
    assert by["leng@globex.com.my"]["phone"] == ""             # contaminated phone dropped too
    assert by["alice@northwind.com"]["company"] == "Initech Solutions Pte Ltd"   # the real owner keeps it


def test_rebuild_misattribution_guard(tmp_path):
    # alice gives northwind.com its name; dave (same domain) only QUOTES a Hooli signature -> must stay Northwind
    _write_eml(tmp_path, "INBOX", "1", "Alice <alice@northwind.com>",
               "Hi\n\nRegards,\nAlice\nNorthwind Engineering Pte Ltd\n")
    _write_eml(tmp_path, "INBOX", "2", "Dave <dave@northwind.com>",
               "Noted.\n\nRegards,\nDave Lim\n\n> Regards,\n> Alan\n> Hooli Holdings Pte Ltd\n")
    dave = {p["email"]: p for p in contacts.rebuild(tmp_path)}["dave@northwind.com"]
    assert dave["company"] == "Northwind Engineering Pte Ltd" and "Hooli" not in dave["company"]


# ── automated senders + dedup ─────────────────────────────────────────────────
def test_rebuild_filters_automated_senders(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Zoom <no-reply@zoom.us>", "x")
    _write_eml(tmp_path, "INBOX", "2", "Drive <drive-shares-dm-noreply@google.com>", "x")
    _write_eml(tmp_path, "INBOX", "3", "Real <real@northwind.com>", "Hi\n\nthanks\nReal\n")
    emails = {p["email"] for p in contacts.rebuild(tmp_path)}
    assert emails == {"real@northwind.com"}


def test_rebuild_auto_merges_typo_address(tmp_path):
    for i in range(4):
        _index_row(tmp_path, "INBOX", f"a{i}", "John Tan <john@northwind.com>")
    _index_row(tmp_path, "INBOX", "typo", "John Tan <jon@northwind.com>")   # same name, near-miss local-part
    by = {p["email"]: p for p in contacts.rebuild(tmp_path)}
    assert "jon@northwind.com" not in by                                    # folded away
    assert by["john@northwind.com"]["count"] == 5 and "jon@northwind.com" in by["john@northwind.com"]["aliases"]


def test_rebuild_filters_bulk_and_notification_domains(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Microsoft Rewards <microsoftrewards@emailnotify.microsoft.com>", "x")
    _write_eml(tmp_path, "INBOX", "2", "Zoom <billing@zoom.us>", "x")
    _write_eml(tmp_path, "INBOX", "3", "Hays <info-sg@email.hays.com>", "x")
    _write_eml(tmp_path, "INBOX", "4", "Newsletter <news@marketing.northwind.com>", "x")
    _write_eml(tmp_path, "INBOX", "5", "Real Person <real@northwind.com>", "Hi\n\nthanks\nReal\n")
    _write_eml(tmp_path, "INBOX", "6", "Jane <jane@gmail.com>", "Hi\n\nthanks\nJane\n")   # free-mail person kept
    emails = {p["email"] for p in contacts.rebuild(tmp_path)}
    assert emails == {"real@northwind.com", "jane@gmail.com"}


def test_is_system_sender_examples():
    sys = contacts._is_system_sender
    assert sys("microsoftrewards@emailnotify.microsoft.com")
    assert sys("billing@zoom.us") and sys("x@send.relay.app") and sys("y@mail.forma.autodesk.com")
    assert sys("rewards@foo.com") and sys("newsletter@foo.com")
    assert not sys("jane@gmail.com")            # free-mail person
    assert not sys("alice@northwind.com") and not sys("info@stas.org.sg")   # real business mailboxes kept


def test_rebuild_does_not_merge_role_mailboxes(tmp_path):
    _index_row(tmp_path, "INBOX", "1", "sales@northwind.com")               # no human name
    _index_row(tmp_path, "INBOX", "2", "sales@beta.com")
    emails = {p["email"] for p in contacts.rebuild(tmp_path)}
    assert emails == {"sales@northwind.com", "sales@beta.com"}             # never merged


def test_read_contacts_returns_cache(tmp_path):
    _write_eml(tmp_path, "INBOX", "1", "Alice <alice@northwind.com>", "Hi\n\nRegards,\nAlice\nNorthwind Pte Ltd\n")
    contacts.rebuild(tmp_path)
    assert contacts.contacts_path(tmp_path).exists()
    assert {p["email"] for p in contacts.read_contacts(tmp_path)} == {"alice@northwind.com"}


# ── web wiring ────────────────────────────────────────────────────────────────
def test_contacts_page_renders_green_book(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m
    _write_eml(tmp_path, "INBOX", "1", "Alice <alice@northwind.com>",
               "Hi\n\nRegards,\nAlice Tan\nNorthwind Engineering Pte Ltd\nTel: +65 6111 2222\n")
    cfg = {"auth": {"imap": {}}, "archive_path": str(tmp_path),
           "comprehension": {"backend": "stub", "profile": "construction"}}
    monkeypatch.setattr(m.cfgmod, "load_config", lambda: dict(cfg))
    client = TestClient(m.app)
    assert client.post("/contacts/rebuild").json()["ok"] is True
    body = client.get("/contacts").text
    assert "Northwind Engineering Pte Ltd" in body and "alice@northwind.com" in body
