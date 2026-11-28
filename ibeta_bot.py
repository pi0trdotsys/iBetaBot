#!/usr/bin/env python3

import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -------------------------------
# LOAD ENVIRONMENT VARIABLES
# -------------------------------
load_dotenv()  # expects .env file in same directory

# Apple's official developer releases feed. It replaced scraping ipsw.dev, which started
# answering GitHub's datacenter IPs with a Cloudflare challenge (HTTP 403) - and whose
# markup had already changed twice. A feed has a stable, documented shape and no bot wall.
FEED_URL = "https://developer.apple.com/news/releases/rss/releases.rss"
DETAILS_URL = "https://developer.apple.com/news/releases/"
# Releases older than this are never announced - stops a fresh install, a lost state
# file or a source switch from replaying weeks of stale history as "new".
MAX_AGE_DAYS = 30
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("TELEGRAM_TOKEN and CHAT_ID must be set in .env")

# File for storing the last state (in the same directory as script)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibeta_last_state.txt")
HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibeta_heartbeat_state.txt")
# Tracks whether we've already alerted about an ONGOING parse failure, kept
# separate from STATE_FILE so a failure never clobbers the last known real
# release (that used to cause a spurious "new release" re-announcement of an
# already-known version the moment parsing recovered).
PARSE_ERROR_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibeta_parse_error_flag.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ibeta_bot")

# Shared HTTP session with retry/backoff for transient network errors
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))
session.mount("http://", HTTPAdapter(max_retries=retries))


# -------------------------------
# STATE MANAGEMENT
# -------------------------------
def get_last_state():
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w") as f:
            f.write("")  # create empty file
        return ""
    with open(STATE_FILE, "r") as f:
        return f.read().strip()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        f.write(state)


def get_last_release_keys():
    """STATE_FILE holds the full set of currently-known release keys, one per
    line (sorted), instead of just the single most-recent one. This means a
    genuinely new release is never missed just because it isn't first in the
    page's own ordering, and lets us report only what's actually new."""
    raw = get_last_state()
    if not raw:
        return set()
    # Older state files stored "<release> (<build>) | <date>"; keep only the
    # identity part so they still compare equal to today's date-less keys.
    return {line.split(" | ")[0].strip() for line in raw.split("\n") if line.strip()}


def save_release_keys(keys):
    save_state("\n".join(sorted(keys)))


def get_last_heartbeat_date():
    if not os.path.exists(HEARTBEAT_FILE):
        return ""
    with open(HEARTBEAT_FILE, "r") as f:
        return f.read().strip()


def save_heartbeat_date(date_str):
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(date_str)


def get_parse_error_flag():
    if not os.path.exists(PARSE_ERROR_FLAG_FILE):
        return False
    with open(PARSE_ERROR_FLAG_FILE, "r") as f:
        return f.read().strip() == "1"


def set_parse_error_flag(active: bool):
    with open(PARSE_ERROR_FLAG_FILE, "w") as f:
        f.write("1" if active else "")


# -------------------------------
# TELEGRAM NOTIFICATIONS
# -------------------------------
def send_telegram(message):
    """Returns True if Telegram confirmed delivery, False otherwise."""
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = session.post(endpoint, json=payload, timeout=10)
        if r.ok:
            logger.info("Telegram message sent (status %s)", r.status_code)
            return True
        logger.error("Telegram API rejected message: status %s, body %s", r.status_code, r.text)
        return False
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


# -------------------------------
# HEARTBEAT
# -------------------------------
def send_heartbeat_if_due():
    """Sends one 'still alive' ping per UTC calendar day, independent of
    whether a new release was found, so silence never gets mistaken for
    a dead bot."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if get_last_heartbeat_date() == today:
        return

    tracked = get_last_release_keys()

    # A heartbeat that cheerfully says "fine" while the scraper reads nothing
    # is worse than no heartbeat: that is exactly how a markup change went
    # unnoticed for two months. The one-off alert is easy to miss, so the daily
    # ping carries the warning for as long as parsing keeps failing.
    if get_parse_error_flag():
        lines = [
            "⚠️ <b>iBetaBot heartbeat</b> — the bot is running, but the scraper is "
            "<b>failing</b>: the releases feed cannot be read, so new builds are NOT being detected."
        ]
    else:
        lines = ["✅ <b>iBetaBot heartbeat</b> — bot is running fine."]
    if tracked:
        lines.append(f"Tracking <b>{len(tracked)}</b> known release(s).")
    lines.append(f"Checked: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if send_telegram("\n".join(lines)):
        save_heartbeat_date(today)


# -------------------------------
# MAIN FUNCTION
# -------------------------------
# Historical pattern only (the feed lists developer builds exclusively, there's
# no live public-beta source to compute an exact offset): Apple's Public Beta
# has usually landed around Developer Beta 3, but slipped to Beta 4 for iOS 26
# due to the Liquid Glass redesign, and point releases (x.y) tend to lag less
# than major (x.0) ones. Treat this as a rough expectation, not a guarantee.
PUBLIC_BETA_NOTE = (
    "Public Beta usually follows around Developer Beta 3 (sometimes Beta 4 "
    "for major .0 releases) — historical pattern, not a guarantee each cycle."
)

# Apple's conventional platform ordering, used to sort each version group
SYSTEM_ORDER = ["iOS", "iPadOS", "macOS", "tvOS", "visionOS", "audioOS", "watchOS"]

class SourceError(Exception):
    """The releases feed could not be read (bad HTTP status, not XML, no items)."""


# "iOS 27.3 beta 2 v2 (24C5049f)" -> system, version, label, build
TITLE_PATTERN = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)*)\s*(.*?)\s*\(([0-9A-Za-z]+)\)$")
PRERELEASE_LABEL_PATTERN = re.compile(r"\b(beta|RC)\b", re.IGNORECASE)
PUBDATE_PATTERN = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_feed_entries():
    """Fetches the feed and returns [(title, pubDate)] for every item. Raises
    SourceError when what came back is not a usable feed, with a reason that is
    safe to put in an alert (a datacenter block, an HTML error page, ...)."""
    response = session.get(FEED_URL, headers={"User-Agent": "iBetaBot"}, timeout=10)
    logger.info("Fetched %s: HTTP %s, %d bytes", FEED_URL, response.status_code, len(response.content))
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        raise SourceError(f"response is not valid XML ({e})") from None
    items = root.findall("./channel/item")
    if not items:
        raise SourceError("the feed contains no items")
    return [(item.findtext("title", "").strip(), item.findtext("pubDate", "").strip()) for item in items]


def parse_releases(entries, today=None):
    """Turns feed entries into (system, version, label, build, date_text) tuples
    for beta/RC builds of Apple's operating systems only: final releases, Xcode,
    TestFlight, App Store Connect etc. are not what this bot announces. Entries
    older than MAX_AGE_DAYS are dropped (see the constant)."""
    today = today or datetime.now(timezone.utc).date()
    releases = []
    for title, pub_date in entries:
        title_match = TITLE_PATTERN.match(title)
        if not title_match:
            continue
        system, version, label, build = title_match.groups()
        if system not in SYSTEM_ORDER or not PRERELEASE_LABEL_PATTERN.search(label):
            continue

        date_text = pub_date
        date_match = PUBDATE_PATTERN.search(pub_date)
        if date_match and date_match.group(2).title() in MONTHS:
            day, month, year = int(date_match.group(1)), MONTHS.index(date_match.group(2).title()) + 1, int(date_match.group(3))
            if (today - date(year, month, day)).days > MAX_AGE_DAYS:
                continue
            date_text = f"{MONTHS[month - 1]} {day}, {year}"
        releases.append((system, version, label, build, date_text))
    return releases


def _version_sort_key(version):
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def _system_sort_key(system):
    try:
        return SYSTEM_ORDER.index(system)
    except ValueError:
        return len(SYSTEM_ORDER)


def release_key(system, version, suffix, build, date_text=""):
    """Canonical identity string for one release, used both as the persisted
    state entry and as the set-membership key for detecting what's new.

    The date is accepted (so callers can pass a whole parsed tuple) but is NOT
    part of the identity: system + version + label + build already pins down a
    release, and the date format has already changed between sources ("August 10, 2026" vs
    "Nov 19, 2026"), which would otherwise re-announce every known release."""
    full_version = f"{system.strip()} {version.strip()}"
    if suffix.strip():
        full_version += f" {suffix.strip()}"
    return f"{full_version} ({build.strip()})"


def build_release_message(matches):
    """Groups the parsed releases by OS version (e.g. all 27.0 builds
    together, all 26.6 builds together) instead of the feed's own
    order, so related platforms read as one block. Expects only the NEWLY
    detected matches - not the full always-repeated feed listing."""
    groups = {}
    for system, version, suffix, build, date_text in matches:
        groups.setdefault(version.strip(), []).append(
            (system.strip(), suffix.strip(), build.strip(), date_text.strip())
        )

    sections = []
    for version in sorted(groups, key=_version_sort_key, reverse=True):
        items = sorted(groups[version], key=lambda item: _system_sort_key(item[0]))
        lines = [f"<b>{html.escape(version)}</b>"]
        for system, suffix, build, date_text in items:
            label = html.escape(system)
            if suffix:
                label += f" {html.escape(suffix)}"
            lines.append(f"🔹 {label} — <code>{html.escape(build)}</code> ({html.escape(date_text)})")
        sections.append("\n".join(lines))

    header = "🆕 <b>New beta release detected!</b>" if len(matches) == 1 else "🆕 <b>New beta releases detected!</b>"
    return (
        f"{header}\n\n"
        f"ℹ️ <i>{html.escape(PUBLIC_BETA_NOTE)}</i>\n\n"
        + "\n\n".join(sections)
        + f"\n\n🌐 <a href=\"{DETAILS_URL}\">Details</a>"
    )


def read_feed_with_one_retry():
    """Returns (entries, reason). A single bad response can be a transient blip
    (CDN hiccup, mid-deploy), so it is retried once before being reported as
    broken. Network-level errors keep their old behaviour: logged, run skipped."""
    reason = None
    for attempt in (1, 2):
        try:
            return load_feed_entries(), None
        except SourceError as e:
            reason = str(e)
            logger.warning("Feed unusable on attempt %d: %s", attempt, reason)
            if attempt == 1:
                time.sleep(5)
    return None, reason


def run():
    last_keys = get_last_release_keys()

    try:
        entries, failure_reason = read_feed_with_one_retry()
    except requests.RequestException as e:
        logger.error("Failed to fetch %s: %s", FEED_URL, e)
        return

    if entries is None:
        logger.error("Releases feed unusable: %s", failure_reason)
        # Alert once, not on every cron run, until the feed is readable again.
        # Tracked separately from STATE_FILE so a failure never clobbers the
        # last known real releases (previously caused a spurious "new release"
        # re-announcement the moment parsing recovered - same version as before).
        if not get_parse_error_flag():
            alert = (
                "⚠️ <b>iBetaBot: can't read Apple's releases feed.</b>\n"
                f"Reason: {html.escape(failure_reason)}\n"
                "New builds are NOT being detected until this is fixed.\n"
                f"🌐 <a href=\"{FEED_URL}\">{FEED_URL}</a>"
            )
            if send_telegram(alert):
                set_parse_error_flag(True)
        send_heartbeat_if_due()
        return

    # The feed was readable - clear any stale failure flag so a FUTURE failure
    # alerts again instead of staying silent forever.
    set_parse_error_flag(False)

    # An empty list here is NOT an error: between beta cycles the feed can
    # legitimately hold no beta/RC at all.
    matches = parse_releases(entries)

    # Compare the FULL current set of releases against the full stored set, so
    # only entries that are actually new get reported - never a replay of the
    # whole listing.
    current_keys = {release_key(*m) for m in matches}
    new_keys = current_keys - last_keys

    if new_keys:
        new_matches = [m for m in matches if release_key(*m) in new_keys]
        message = build_release_message(new_matches)

        if send_telegram(message):
            # Union, not replacement: the feed drops superseded builds, and one
            # that blips out of a response and back must not be announced twice.
            save_release_keys(last_keys | current_keys)
            logger.info("%d new release(s) saved to state", len(new_keys))
        else:
            logger.error("Telegram send failed – state NOT updated, will retry next run")
    else:
        logger.info("No new releases – skipping notification")

    send_heartbeat_if_due()


if __name__ == "__main__":
    run()
