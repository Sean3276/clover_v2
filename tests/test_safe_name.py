from clover.safe_name import safe_name


def test_strips_special_chars():
    assert safe_name("abc.123@host:x") == "abc.123-host-x"


def test_collapses_and_trims():
    assert safe_name("  hello   world  ") == "hello world"
    assert safe_name("--a--b--") == "a-b"


def test_keeps_dot_for_ids():
    assert safe_name("CAFEBABE.20260617@mail.example.com") == "CAFEBABE.20260617-mail.example.com"


def test_maxlen():
    assert len(safe_name("x" * 200, maxlen=120)) == 120


def test_empty():
    assert safe_name("") == "untitled"
    assert safe_name("@@@") == "untitled"
