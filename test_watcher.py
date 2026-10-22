import imaplib
import logging
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

import watcher
from watcher import Config, confirm, load_config, run_once, tick, extract_link, fetch_candidates, is_fresh, parse_mail, Mail

NETFLIX_HTML = """
<html><body>
<p>Demande de mise à jour de votre foyer.</p>
<a href="https://www.netflix.com/password?lkid=PASSWORD_CHANGE">Modifier le mot de passe</a>
<a href="https://www.netflix.com/account/update-primary-location?nftoken=FAKE&amp;g=abc&amp;lkid=UPDATE_HOUSEHOLD_REQUESTED_OTP_CTA">Oui, c'était moi</a>
</body></html>
"""


def test_extract_link_returns_household_link():
    assert extract_link(NETFLIX_HTML) == (
        "https://www.netflix.com/account/update-primary-location"
        "?nftoken=FAKE&g=abc&lkid=UPDATE_HOUSEHOLD_REQUESTED_OTP_CTA")


def test_extract_link_ignores_other_links():
    assert extract_link('<a href="https://www.netflix.com/password">x</a>') is None


def test_extract_link_none_on_empty():
    assert extract_link("") is None


@pytest.mark.parametrize("href", [
    "https://evil.example/account/update-primary-location?nftoken=x",
    "https://www.netflix.com.evil.example/account/update-primary-location",
    "https://evil.example/?next=https://www.netflix.com/account/update-primary-location",
    "http://www.netflix.com/account/update-primary-location",
    "http://localhost:8080/update-primary-location",
    "file:///etc/update-primary-location",
])
def test_extract_link_rejects_links_outside_netflix(href):
    assert extract_link(f'<a href="{href}">Oui</a>') is None


def test_extract_link_skips_spoofed_link_before_real_one():
    real = "https://netflix.com/account/update-primary-location?nftoken=x"
    html = (f'<a href="https://evil.example/update-primary-location">a</a>'
            f'<a href="{real}">b</a>')
    assert extract_link(html) == real


MAIL_DATE = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
CLEAN_LINK = ("https://www.netflix.com/account/update-primary-location"
              "?nftoken=FAKE&g=abc&lkid=UPDATE_HOUSEHOLD_REQUESTED_OTP_CTA")


def build_mail(html: str | None = NETFLIX_HTML, message_id: str | None = "<id@netflix>",
               date: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Mise à jour de votre foyer"
    msg["Date"] = date if date is not None else format_datetime(MAIL_DATE)
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg.set_content("Plain text body.")
    if html is not None:
        msg.add_alternative(html, subtype="html", cte="quoted-printable")
    return msg.as_bytes()


def test_parse_mail_qp_multipart():
    mail = parse_mail(build_mail())
    assert mail.link == CLEAN_LINK
    assert mail.key == "<id@netflix>"
    assert mail.date == MAIL_DATE


def test_parse_mail_without_message_id_uses_link_as_key():
    mail = parse_mail(build_mail(message_id=None))
    assert mail.key == mail.link == CLEAN_LINK


def test_parse_mail_text_only_has_no_link():
    mail = parse_mail(build_mail(html=None))
    assert mail.link is None


def test_parse_mail_bad_date_is_none():
    mail = parse_mail(build_mail(date="not a date"))
    assert mail.date is None


def test_parse_mail_naive_date_is_none():
    mail = parse_mail(build_mail(date="Wed, 22 Oct 2026 10:00:00"))
    assert mail.date is None


def test_is_fresh_true_at_1_min():
    mail = Mail(key="k", date=MAIL_DATE, link="x")
    assert is_fresh(mail, MAIL_DATE + timedelta(minutes=1)) is True


def test_is_fresh_false_at_16_min():
    mail = Mail(key="k", date=MAIL_DATE, link="x")
    assert is_fresh(mail, MAIL_DATE + timedelta(minutes=16)) is False


def test_is_fresh_false_when_date_missing():
    assert is_fresh(Mail(key="k", date=None, link="x"), MAIL_DATE) is False


def test_parse_mail_bogus_charset_has_no_link():
    raw = (
        "Date: " + format_datetime(MAIL_DATE) + "\r\n"
        "Message-ID: <bad@netflix>\r\n"
        'Content-Type: multipart/alternative; boundary="b"\r\n'
        "\r\n"
        "--b\r\n"
        'Content-Type: text/html; charset="bogus-xyz"\r\n'
        "\r\n"
        + NETFLIX_HTML + "\r\n"
        "--b--\r\n"
    ).encode()
    mail = parse_mail(raw)
    assert mail.link is None


def test_is_fresh_true_at_exactly_15_min():
    mail = Mail(key="k", date=MAIL_DATE, link="x")
    assert is_fresh(mail, MAIL_DATE + timedelta(minutes=15)) is True


class FakeImap:
    def __init__(self, search_data=(b"1 2",), messages=None, search_status="OK", select_status="OK"):
        self.select_status = select_status
        self.messages = messages or {}
        self.search_data = list(search_data)
        self.search_status = search_status
        self.select_calls = []
        self.search_args = None
        self.fetch_specs = []

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        return (self.select_status, [b"1"])

    def search(self, charset, *criteria):
        self.search_args = (charset, *criteria)
        return (self.search_status, self.search_data)

    def fetch(self, num, spec):
        self.fetch_specs.append(spec)
        raw = self.messages[num]
        return ("OK", [(b"%s (BODY[] {%d}" % (num, len(raw)), raw), b")"])


def test_fetch_selects_folder_readonly():
    imap = FakeImap(search_data=[b""])
    fetch_candidates(imap, "INBOX", date(2026, 10, 22))
    assert imap.select_calls == [('"INBOX"', True)]


def test_fetch_search_criteria():
    imap = FakeImap(search_data=[b""])
    fetch_candidates(imap, "INBOX", date(2026, 10, 22))
    assert imap.search_args == (None, "FROM", '"netflix"', "SINCE", "21-Oct-2026")


def test_fetch_returns_only_household_mails():
    household = build_mail(message_id="<house@netflix>")
    newsletter = build_mail(html="<p>Newsletter</p>", message_id="<news@netflix>")
    imap = FakeImap(search_data=[b"1 2"], messages={b"1": household, b"2": newsletter})
    mails = fetch_candidates(imap, "INBOX", date(2026, 10, 22))
    assert [m.key for m in mails] == ["<house@netflix>"]
    assert mails[0].link == CLEAN_LINK


def test_fetch_uses_peek():
    imap = FakeImap(search_data=[b"1 2"], messages={b"1": build_mail(), b"2": build_mail()})
    fetch_candidates(imap, "INBOX", date(2026, 10, 22))
    assert imap.fetch_specs == ["(BODY.PEEK[])", "(BODY.PEEK[])"]


def test_fetch_empty_search_returns_empty_list():
    imap = FakeImap(search_data=[b""])
    assert fetch_candidates(imap, "INBOX", date(2026, 10, 22)) == []
    assert imap.fetch_specs == []


def test_fetch_raises_when_search_fails():
    imap = FakeImap(search_data=[None], search_status="NO")
    with pytest.raises(imaplib.IMAP4.error):
        fetch_candidates(imap, "INBOX", date(2026, 10, 22))


def test_fetch_quotes_folder_with_spaces():
    imap = FakeImap(search_data=[b""])
    fetch_candidates(imap, "[Gmail]/All Mail", date(2026, 10, 22))
    assert imap.select_calls == [('"[Gmail]/All Mail"', True)]


def test_fetch_escapes_quotes_and_backslashes_in_folder():
    imap = FakeImap(search_data=[b""])
    fetch_candidates(imap, 'a"b\\c', date(2026, 10, 22))
    assert imap.select_calls == [('"a\\"b\\\\c"', True)]


def test_fetch_raises_when_select_fails():
    imap = FakeImap(select_status="NO")
    with pytest.raises(imaplib.IMAP4.error):
        fetch_candidates(imap, "Nope", date(2026, 10, 22))


def _fixture_url(name: str) -> str:
    # The marker in the query string lets the single-submit fallback apply to file:// pages.
    url = (Path(__file__).parent / "tests_fixtures" / name).resolve().as_uri()
    return f"{url}?{watcher.LINK_MARKER}"


def test_confirm_clicks_known_label(tmp_path):
    assert confirm(_fixture_url("confirm_fr.html"), tmp_path) is True


def test_confirm_falls_back_to_single_submit(tmp_path):
    assert confirm(_fixture_url("confirm_unknown.html"), tmp_path) is True


def test_confirm_fallback_refused_on_login_page(tmp_path):
    assert confirm(_fixture_url("login.html"), tmp_path) is False
    assert len(list(tmp_path.glob("*.png"))) == 1


def test_confirm_fallback_refused_without_link_marker(tmp_path):
    url = _fixture_url("confirm_unknown.html").split("?")[0]
    assert confirm(url, tmp_path) is False


def test_confirm_no_button_returns_false_and_screenshots(tmp_path):
    shots = tmp_path / "missing" / "dir"
    assert confirm(_fixture_url("no_button.html"), shots) is False
    assert len(list(shots.glob("*.png"))) == 1


def test_confirm_navigation_error_returns_false(tmp_path):
    assert confirm("file:///nonexistent/page.html", tmp_path) is False


REQUIRED_ENV = {"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p"}
NOW = MAIL_DATE + timedelta(minutes=1)
SECRET_LINK = "https://www.netflix.com/account/update-primary-location?nftoken=SECRET"


def test_load_config_defaults():
    config = load_config(REQUIRED_ENV)
    assert config.port == 993
    assert config.folder == "INBOX"


def test_load_config_lists_all_missing():
    with pytest.raises(SystemExit) as exc:
        load_config({})
    for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        assert name in str(exc.value)


def test_run_once_confirms_fresh_mail_once():
    calls = []
    attempts = {}
    mail = Mail("k", MAIL_DATE, CLEAN_LINK)
    for _ in range(2):
        run_once([mail], NOW, attempts, lambda url: calls.append(url) or True)
    assert len(calls) == 1


def test_run_once_retries_up_to_three_times():
    calls = []
    attempts = {}
    mail = Mail("k", MAIL_DATE, CLEAN_LINK)
    for _ in range(4):
        run_once([mail], NOW, attempts, lambda url: calls.append(url) or False)
    assert len(calls) == 3


def test_run_once_skips_stale_mail():
    calls = []
    mail = Mail("k", MAIL_DATE, CLEAN_LINK)
    run_once([mail], MAIL_DATE + timedelta(minutes=16), {},
             lambda url: calls.append(url) or True)
    assert calls == []


def test_tick_survives_fetch_error(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("network down")
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", boom)
    with caplog.at_level(logging.INFO):
        tick(Config("h", "u", "p"), {}, lambda url: True)
    assert "OSError" in caplog.text
    assert "network down" not in caplog.text


def test_tick_passes_imap_timeout(monkeypatch):
    seen = {}
    def fake(*args, **kwargs):
        seen.update(kwargs)
        raise OSError("stop")
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", fake)
    tick(Config("h", "u", "p"), {}, lambda url: True)
    assert seen["timeout"] == watcher.IMAP_TIMEOUT_SECONDS == 30


def test_tick_logs_imap_error_message(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise imaplib.IMAP4.error("AUTHENTICATIONFAILED bad creds")
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", boom)
    with caplog.at_level(logging.INFO):
        tick(Config("h", "u", "p"), {}, lambda url: True)
    assert "AUTHENTICATIONFAILED" in caplog.text


def test_load_config_rejects_non_integer_port():
    with pytest.raises(SystemExit, match="IMAP_PORT must be an integer"):
        load_config({**REQUIRED_ENV, "IMAP_PORT": "abc"})


def test_tick_logs_out_when_fetch_fails(monkeypatch):
    class FakeImap:
        logged_out = False
        def __init__(self, *a, **kw): pass
        def login(self, *a): pass
        def logout(self): FakeImap.logged_out = True
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(watcher, "fetch_candidates",
                        lambda *a: (_ for _ in ()).throw(imaplib.IMAP4.error("x")))
    tick(Config("h", "u", "p"), {}, lambda url: True)
    assert FakeImap.logged_out


class OkImap:
    def __init__(self, *a, **kw): pass
    def login(self, *a): pass
    def logout(self): pass


def test_tick_logs_connected_on_first_success(monkeypatch, caplog):
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", OkImap)
    monkeypatch.setattr(watcher, "fetch_candidates", lambda *a: [])
    with caplog.at_level(logging.INFO):
        assert tick(Config("h", "u", "p"), {}, lambda url: True) is True
    assert "IMAP connected" in caplog.text


def test_tick_stays_quiet_while_connected(monkeypatch, caplog):
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", OkImap)
    monkeypatch.setattr(watcher, "fetch_candidates", lambda *a: [])
    with caplog.at_level(logging.INFO):
        tick(Config("h", "u", "p"), {}, lambda url: True, was_connected=True)
    assert caplog.text == ""


def test_tick_returns_false_on_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("down")
    monkeypatch.setattr(watcher.imaplib, "IMAP4_SSL", boom)
    assert tick(Config("h", "u", "p"), {}, lambda url: True, was_connected=True) is False


@pytest.mark.parametrize("key", ["<id@netflix>", SECRET_LINK])
def test_logs_never_contain_link(caplog, key):
    mail = Mail(key, MAIL_DATE, SECRET_LINK)
    with caplog.at_level(logging.DEBUG):
        run_once([mail], NOW, {}, lambda url: True)
        run_once([mail], NOW, {}, lambda url: False)
    assert caplog.text
    assert "SECRET" not in caplog.text
    assert "nftoken" not in caplog.text
    assert "update-primary-location" not in caplog.text
