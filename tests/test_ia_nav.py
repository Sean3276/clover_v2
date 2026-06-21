"""#2 Information-architecture restructure: four tabs — Mail · Hub · Matters · Rolodex — with the old
Settings/Archive/Projects/GreenBook/Need-You tabs folded in. Routes/URLs are unchanged; only grouping,
labels, and sub-navs change. Mail uses adaptive onboarding (empty mailbox -> Import first)."""
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_V2_HOME", str(tmp_path))
    import app.main as m
    return TestClient(m.app)


def test_nav_is_four_tabs_and_drops_old_ones(client):
    h = client.get("/archive").text                      # /archive renders (it's the Import area under Mail)
    for tab in (">Mail</a>", ">Hub</a>", ">Matters</a>", ">Rolodex</a>"):
        assert tab in h
    assert ">Settings</a>" not in h                       # Settings tab gone (folded into Mail)
    assert ">GreenBook</a>" not in h and ">Projects</a>" not in h and ">Archive</a>" not in h


def test_empty_mailbox_redirects_to_import(client):
    r = client.get("/threads", follow_redirects=False)   # no mail yet -> onboarding sends you to Import
    assert r.status_code in (303, 307) and r.headers["location"] == "/archive"


def test_mail_subnav_groups_conversations_import_account(client):
    h = client.get("/archive").text
    assert "Conversations" in h and "Import" in h and "Account" in h   # Mail sub-nav
    assert "Import" in client.get("/setup").text                       # /setup (Account) sits under Mail too


def test_hub_subnav_groups_projects_profile_rules(client):
    h = client.get("/projects").text
    assert "Profile" in h and "Rules" in h                # Hub folds profile/taxonomy + rules in
    assert "Hub" in h


def test_matters_shows_focus(client):
    h = client.get("/todo").text
    assert "Focus" in h and "Need You" not in h           # 'Need You' retired in favour of 'Focus'


def test_rolodex_is_the_contacts_tab(client):
    assert "Rolodex" in client.get("/contacts").text
