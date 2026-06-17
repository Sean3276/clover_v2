import pytest

from clover.sources import SUITE, get_source
from clover.sources.base import MailSource
from clover.sources.imap_source import ImapSource


def test_imap_source_registered_and_available():
    src = get_source("imap", {"host": "h", "port": 993, "security": "ssl", "user": "u"}, "pw")
    assert isinstance(src, ImapSource) and isinstance(src, MailSource)
    assert src.host == "h" and src.port == 993


def test_unknown_source_raises():
    with pytest.raises(NotImplementedError):
        get_source("gmail", {}, "pw")


def test_suite_lists_imap_available_and_others_planned():
    by_id = {s["id"]: s for s in SUITE}
    assert by_id["imap"]["status"] == "available"
    planned = [s for s in SUITE if s["status"] == "planned"]
    assert len(planned) >= 5  # the full suite to develop one by one
    assert all({"id", "label", "protocol", "status"} <= set(s) for s in SUITE)
