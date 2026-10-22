import email
import email.message
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

log = logging.getLogger(__name__)
LINK_MARKER = "update-primary-location"
NETFLIX_HOSTS = frozenset({"www.netflix.com", "netflix.com"})
MAX_AGE = timedelta(minutes=15)
MAX_ATTEMPTS = 3
POLL_SECONDS = 30
GOTO_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 10_000
IMAP_TIMEOUT_SECONDS = 30
BUTTON_LABELS = (
    "Confirmer la mise à jour",
    "Confirm update",
    "Confirmar actualización",
    "Aktualisierung bestätigen",
    "Conferma aggiornamento",
    "Confirmar atualização",
)
# Explicit English abbreviations: strftime("%b") depends on the process locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class _HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def _is_household_link(href: str) -> bool:
    # Anyone can send a mail whose sender name says "Netflix": only follow
    # https links to Netflix itself, never another host, localhost or file://.
    parts = urlsplit(href)
    return (parts.scheme == "https" and parts.hostname in NETFLIX_HOSTS
            and LINK_MARKER in parts.path)


def extract_link(html: str) -> str | None:
    collector = _HrefCollector()
    collector.feed(html)
    collector.close()
    return next((href for href in collector.hrefs if _is_household_link(href)), None)


@dataclass(frozen=True)
class Mail:
    key: str
    date: datetime | None
    link: str | None


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # Naive datetimes (no zone, or "-0000") cannot be compared to an aware "now".
    return parsed if parsed.tzinfo is not None else None


def _html_link(msg: email.message.EmailMessage) -> str | None:
    html_part = msg.get_body(("html",))
    if html_part is None:
        return None
    try:
        html = html_part.get_content()
    except (LookupError, UnicodeError, ValueError):
        # Malformed body (e.g. unknown charset): treat as no link, do not abort polling.
        return None
    return extract_link(html)


def parse_mail(raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    link = _html_link(msg)
    message_id = str(msg["Message-ID"] or "").strip()
    return Mail(key=message_id or link or "", date=_parse_date(msg["Date"]), link=link)


def is_fresh(mail: Mail, now: datetime) -> bool:
    if mail.date is None:
        return False
    return now - mail.date <= MAX_AGE


def _imap_date(day: date) -> str:
    return f"{day.day}-{MONTHS[day.month - 1]}-{day.year}"


def _check(status: str, what: str) -> None:
    if status != "OK":
        raise imaplib.IMAP4.error(f"IMAP {what} failed: {status}")


def _message_bytes(data: list) -> bytes | None:
    # imaplib returns [(b"<id> (BODY[] {n}", raw), b")"]; keep only the tuple's payload.
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    return None


def fetch_candidates(imap: imaplib.IMAP4, folder: str, today: date) -> list[Mail]:
    # Read-only: never set flags, move or delete. BODY.PEEK keeps mails unread.
    # Quoted: imaplib sends the name verbatim, so "[Gmail]/All Mail" would be a BAD command.
    quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
    status, _ = imap.select(quoted, readonly=True)
    _check(status, "select")
    since = _imap_date(today - timedelta(days=1))
    status, data = imap.search(None, "FROM", '"netflix"', "SINCE", since)
    _check(status, "search")
    mails = []
    for num in data[0].split():
        status, fetched = imap.fetch(num, "(BODY.PEEK[])")
        _check(status, "fetch")
        raw = _message_bytes(fetched)
        if raw is None:
            continue
        mail = parse_mail(raw)
        if mail.link is not None:
            mails.append(mail)
    return mails


def _settle(page, timeout_ms: int) -> None:
    """Best-effort wait: real pages may never reach networkidle."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightError:
        pass


def _find_confirm_button(page):
    for label in BUTTON_LABELS:
        button = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
        if button.count() > 0:
            return button.first
    # The lone-submit fallback is only safe on the household page: an expired token
    # redirects to a login form whose "Sign in" button must never be clicked.
    if LINK_MARKER not in page.url or page.locator("input[type=password]").count() > 0:
        return None
    submit = page.locator("button[type=submit]")
    return submit if submit.count() == 1 else None


def _save_screenshot(page, screenshot_dir: Path) -> None:
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=screenshot_dir / f"{datetime.now():%Y%m%d-%H%M%S}.png",
                        full_page=True)
    except (PlaywrightError, OSError):
        pass


def confirm(url: str, screenshot_dir: Path) -> bool:
    """Open the link and click the confirm button. Never raises; never logs the URL."""
    clicked = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                    _settle(page, SETTLE_TIMEOUT_MS)
                    button = _find_confirm_button(page)
                    if button is None:
                        _save_screenshot(page, screenshot_dir)
                        return False
                    button.click()
                    clicked = True  # the link is single-use: from here on, report success
                    _settle(page, SETTLE_TIMEOUT_MS)
                    return True
                except PlaywrightError:
                    if not clicked:
                        _save_screenshot(page, screenshot_dir)
                    return clicked
            finally:
                browser.close()
    except Exception as exc:  # never raise; type only, the message may embed the URL
        log.error("confirm failed: %s", type(exc).__name__)
        return clicked


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"


def load_config(env: Mapping[str, str]) -> Config:
    required = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")
    try:
        port = int(env.get("IMAP_PORT") or 993)
    except ValueError:
        raise SystemExit("IMAP_PORT must be an integer") from None
    return Config(host=env["IMAP_HOST"], user=env["IMAP_USER"], password=env["IMAP_PASSWORD"],
                  port=port, folder=env.get("IMAP_FOLDER") or "INBOX")


def _mail_id(mail: Mail) -> str:
    # The key may be the link itself (login token): log only a short digest.
    return hashlib.sha256(mail.key.encode()).hexdigest()[:12]


def run_once(mails: list[Mail], now: datetime, attempts: dict[str, int],
             confirm_fn: Callable[[str], bool]) -> None:
    for mail in mails:
        if attempts.get(mail.key, 0) >= MAX_ATTEMPTS or not is_fresh(mail, now):
            continue
        if confirm_fn(mail.link):
            attempts[mail.key] = MAX_ATTEMPTS
            log.info("mail %s confirmed", _mail_id(mail))
        else:
            attempts[mail.key] = attempts.get(mail.key, 0) + 1
            log.warning("mail %s confirm failed (attempt %d/%d)",
                        _mail_id(mail), attempts[mail.key], MAX_ATTEMPTS)


def tick(config: Config, attempts: dict[str, int], confirm_fn: Callable[[str], bool],
         was_connected: bool = False) -> bool:
    """One poll. Returns whether IMAP succeeded; logs only on (re)connection."""
    try:
        imap = imaplib.IMAP4_SSL(config.host, config.port,
                                timeout=IMAP_TIMEOUT_SECONDS)
        try:
            imap.login(config.user, config.password)
            now = datetime.now(timezone.utc)
            mails = fetch_candidates(imap, config.folder, now.date())
        finally:
            try:
                imap.logout()
            except Exception:  # best effort
                pass
        if not was_connected:
            log.info("IMAP connected")
        run_once(mails, now, attempts, confirm_fn)
        return True
    except imaplib.IMAP4.error as exc:  # server response text (e.g. AUTHENTICATIONFAILED), no secrets
        log.error("tick failed: %s: %s", type(exc).__name__, exc)
    except Exception as exc:  # type only: messages may embed the URL or credentials
        log.error("tick failed: %s", type(exc).__name__)
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(os.environ)
    screenshots = Path("screenshots")
    screenshots.mkdir(exist_ok=True)
    log.info("watching folder %s on %s every %d s", config.folder, config.host, POLL_SECONDS)
    attempts: dict[str, int] = {}
    connected = False
    while True:
        connected = tick(config, attempts, lambda url: confirm(url, screenshots), connected)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
