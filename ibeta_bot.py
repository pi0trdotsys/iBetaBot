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
# (ibeta_ipsw.py still reads ipsw.dev, from a Mac, for the products this feed lacks.)
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
# Tracks whether we've already alerted about an ONGOING source failure, kept
# separate from STATE_FILE so a failure never clobbers the last known real
# release (that used to cause a spurious "new release" re-announcement of an
# already-known version the moment the source recovered).
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


class SourceError(Exception):
    """A release source could not be read (bad HTTP status, not the expected
    format, nothing in it). The message is safe to show in an alert."""


# -------------------------------
# WHAT WE TRACK AND HOW IT LOOKS
# -------------------------------
# Apple's releases feed carries these. Anything else it lists (Xcode, TestFlight,
# App Store Connect, ...) is not an operating system build and is ignored.
FEED_SYSTEMS = ["iOS", "iPadOS", "macOS", "tvOS", "visionOS", "watchOS"]
# The feed does NOT carry these; ibeta_ipsw.py reads them from ipsw.dev instead.
# Keeping the two lists disjoint is what stops the same build being announced
# twice, once by each source.
IPSW_SYSTEMS = ["audioOS", "HomeView Software"]
SYSTEM_ORDER = FEED_SYSTEMS + IPSW_SYSTEMS  # also the display order inside a message

SYSTEM_EMOJI = {
    "iOS": "📱",
    "iPadOS": "📲",
    "macOS": "💻",
    "tvOS": "📺",
    "visionOS": "🥽",
    "watchOS": "⌚",
    "audioOS": "🔊",
    "HomeView Software": "🏠",
}

SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Historical pattern only (the sources list developer builds exclusively, there's
# no live public-beta source to compute an exact offset): Apple's Public Beta
# has usually landed around Developer Beta 3, but slipped to Beta 4 for iOS 26
# due to the Liquid Glass redesign, and point releases (x.y) tend to lag less
# than major (x.0) ones. Treat this as a rough expectation, not a guarantee.
PUBLIC_BETA_NOTE = (
    "Public Beta usually follows around Developer Beta 3 (sometimes Beta 4 "
    "for major .0 releases) — historical pattern, not a guarantee each cycle."
)


def _esc(value):
    """Escapes text for Telegram's HTML mode. Quotes are left alone on purpose:
    Telegram only guarantees &lt; &gt; &amp; &quot; plus numeric entities, and
    html.escape would turn an apostrophe into the hex entity &#x27;."""
    return html.escape(str(value), quote=False)


def _attr(value):
    """Same, for use inside an href="..." attribute."""
    return html.escape(str(value), quote=True)


def _banner(title):
    return [SEPARATOR, title, SEPARATOR]


# -------------------------------
# STATE MANAGEMENT
# -------------------------------
# Every helper takes an optional path so a second tracker (ibeta_ipsw.py, running
# on a Mac) can keep its own, machine-local state with the very same code. The
# default is resolved at call time, so the module-level constants stay patchable.
def get_last_state(path=None):
    path = path or STATE_FILE
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("")  # create empty file
        return ""
    with open(path, "r") as f:
        return f.read().strip()


def save_state(state, path=None):
    with open(path or STATE_FILE, "w") as f:
        f.write(state)


def get_last_release_keys(path=None):
    """The state file holds the full set of known release keys, one per line
    (sorted), instead of just the single most-recent one. This means a
    genuinely new release is never missed just because it isn't first in the
    source's own ordering, and lets us report only what's actually new."""
    raw = get_last_state(path)
    if not raw:
        return set()
    # Older state files stored "<release> (<build>) | <date>"; keep only the
    # identity part so they still compare equal to today's date-less keys.
    return {line.split(" | ")[0].strip() for line in raw.split("\n") if line.strip()}


def save_release_keys(keys, path=None):
    save_state("\n".join(sorted(keys)), path)


def get_last_heartbeat_date(path=None):
    path = path or HEARTBEAT_FILE
    if not os.path.exists(path):
        return ""
    with open(path, "r") as f:
        return f.read().strip()


def save_heartbeat_date(date_str, path=None):
    with open(path or HEARTBEAT_FILE, "w") as f:
        f.write(date_str)


def get_parse_error_flag(path=None):
    path = path or PARSE_ERROR_FLAG_FILE
    if not os.path.exists(path):
        return False
    with open(path, "r") as f:
        return f.read().strip() == "1"


def set_parse_error_flag(active: bool, path=None):
    with open(path or PARSE_ERROR_FLAG_FILE, "w") as f:
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
def send_heartbeat_if_due(source_label="Apple releases feed", heartbeat_file=None, state_file=None, flag_file=None):
    """Sends one 'still alive' ping per UTC calendar day, independent of
    whether a new release was found, so silence never gets mistaken for
    a dead bot."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if get_last_heartbeat_date(heartbeat_file) == today:
        return

    tracked = get_last_release_keys(state_file)

    # A heartbeat that cheerfully says "fine" while the source reads nothing
    # is worse than no heartbeat: that is exactly how a markup change went
    # unnoticed for two months. The one-off alert is easy to miss, so the daily
    # ping carries the warning for as long as the source keeps failing.
    if get_parse_error_flag(flag_file):
        status = "⚠️ <b>Failing</b> — can't read the source, new builds are <b>NOT</b> being detected."
    else:
        status = "✅ Running fine"

    lines = _banner("💓 <b>iBETABOT</b> · heartbeat")
    lines.append(status)
    lines.append(f"📡 Source: {_esc(source_label)}")
    if tracked:
        lines.append(f"📚 Tracking <b>{len(tracked)}</b> known build(s)")
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if send_telegram("\n".join(lines)):
        save_heartbeat_date(today, heartbeat_file)


def report_source_failure(source_name, source_url, reason, flag_file=None):
    """Alerts ONCE per outage (tracked in the flag file), not on every run."""
    if get_parse_error_flag(flag_file):
        return
    alert = "\n".join(
        _banner("🚨 <b>iBETABOT</b> · source down")
        + [
            f"Can't read <b>{_esc(source_name)}</b>.",
            f"Reason: <code>{_esc(reason)}</code>",
            "New builds are <b>NOT</b> being detected until this is fixed.",
            "",
            f'🔗 <a href="{_attr(source_url)}">{_esc(source_url)}</a>',
        ]
    )
    if send_telegram(alert):
        set_parse_error_flag(True, flag_file)


# -------------------------------
# RELEASES
# -------------------------------
# "iOS 27.3 beta 2 v2 (24C5049f)" -> system, version, label, build
TITLE_PATTERN = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)*)\s*(.*?)\s*\(([0-9A-Za-z]+)\)$")
PRERELEASE_LABEL_PATTERN = re.compile(r"\b(beta|RC)\b", re.IGNORECASE)
PUBDATE_PATTERN = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})")


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
        if system not in FEED_SYSTEMS or not PRERELEASE_LABEL_PATTERN.search(label):
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


def parse_display_date(text):
    """Reads the 'Nov 19, 2026' / 'November 19, 2026' style used for display."""
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def short_date(date_text, today=None):
    """'Nov 19, 2026' -> 'Nov 19' within the current year; unreadable text is kept as is."""
    parsed = parse_display_date(date_text)
    if not parsed:
        return date_text
    today = today or datetime.now(timezone.utc).date()
    text = f"{MONTHS[parsed.month - 1]} {parsed.day}"
    return text if parsed.year == today.year else f"{text}, {parsed.year}"


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


def release_stage(label):
    """🧪 beta, 🏁 release candidate, 📦 anything else (a plain release)."""
    if re.search(r"\bbeta\b", label, re.IGNORECASE):
        return "🧪"
    if re.search(r"\bRC\b", label, re.IGNORECASE):
        return "🏁"
    return "📦"


def release_key(system, version, suffix, build, date_text=""):
    """Canonical identity string for one release, used both as the persisted
    state entry and as the set-membership key for detecting what's new.

    The date is accepted (so callers can pass a whole parsed tuple) but is NOT
    part of the identity: system + version + label + build already pins down a
    release, and the date format has already changed between sources ("August
    10, 2026" vs "Nov 19, 2026"), which would otherwise re-announce every
    known release."""
    full_version = f"{system.strip()} {version.strip()}"
    if suffix.strip():
        full_version += f" {suffix.strip()}"
    return f"{full_version} ({build.strip()})"


def build_release_message(matches, details=(DETAILS_URL, "Apple Releases"), note=PUBLIC_BETA_NOTE):
    """Groups the releases by OS version (all 27.3 builds together) instead of
    the source's own order, so related platforms read as one block. Expects only
    the NEWLY detected ones - never the full, always-repeated listing. The
    Public Beta note is only attached when a beta is actually in the message."""
    groups = {}
    for system, version, suffix, build, date_text in matches:
        groups.setdefault(version.strip(), []).append(
            (system.strip(), suffix.strip(), build.strip(), date_text.strip())
        )

    sections = []
    for version in sorted(groups, key=_version_sort_key, reverse=True):
        items = sorted(groups[version], key=lambda item: _system_sort_key(item[0]))
        stages = {release_stage(label) for _, label, _, _ in items}
        lines = [f"{stages.pop() if len(stages) == 1 else '📦'} <b>{_esc(version)}</b>"]
        for system, label, build, date_text in items:
            parts = [f"{SYSTEM_EMOJI.get(system, '🔹')} <b>{_esc(system)}</b>"]
            if label:
                parts.append(_esc(label))
            parts.append(f"<code>{_esc(build)}</code>")
            parts.append(_esc(short_date(date_text)))
            lines.append("   " + " · ".join(parts))
        sections.append("\n".join(lines))

    count = len(matches)
    summary = f"{count} new build{'s' if count != 1 else ''}"
    if len(groups) > 1:
        summary += f" · {len(groups)} versions"

    out = _banner("🍎 <b>iBETABOT</b> · new Apple builds") + [f"<i>{summary}</i>", "", "\n\n".join(sections)]
    if note and any(release_stage(label) == "🧪" for _, _, label, _, _ in matches):
        out += ["", f"<blockquote>ℹ️ {_esc(note)}</blockquote>"]
    url, label = details
    out += ["", f'🔗 <a href="{_attr(url)}">{_esc(label)}</a>']
    return "\n".join(out)


def announce_new_releases(matches, last_keys, state_file=None, details=(DETAILS_URL, "Apple Releases"), note=PUBLIC_BETA_NOTE):
    """Compares the FULL current set against the stored one and reports only what
    is actually new. State is saved only after Telegram confirmed delivery (a
    failed send is simply retried next run), and as a UNION: sources drop
    superseded builds, and one that blips out of a response and back must not be
    announced twice."""
    current_keys = {release_key(*m) for m in matches}
    new_keys = current_keys - last_keys

    if not new_keys:
        logger.info("No new releases – skipping notification")
        return

    new_matches = [m for m in matches if release_key(*m) in new_keys]
    if send_telegram(build_release_message(new_matches, details=details, note=note)):
        save_release_keys(last_keys | current_keys, state_file)
        logger.info("%d new release(s) saved to state", len(new_keys))
    else:
        logger.error("Telegram send failed – state NOT updated, will retry next run")


def read_with_one_retry(loader):
    """Returns (data, reason). A single bad response can be a transient blip
    (CDN hiccup, mid-deploy), so it is retried once before being reported as
    broken. Network-level errors are NOT caught here: callers keep their old
    behaviour of logging them and skipping the run."""
    reason = None
    for attempt in (1, 2):
        try:
            return loader(), None
        except SourceError as e:
            reason = str(e)
            logger.warning("Source unusable on attempt %d: %s", attempt, reason)
            if attempt == 1:
                time.sleep(5)
    return None, reason


# -------------------------------
# MAIN FUNCTION
# -------------------------------
def run():
    last_keys = get_last_release_keys()

    try:
        entries, failure_reason = read_with_one_retry(load_feed_entries)
    except requests.RequestException as e:
        logger.error("Failed to fetch %s: %s", FEED_URL, e)
        return

    if entries is None:
        logger.error("Releases feed unusable: %s", failure_reason)
        report_source_failure("Apple's releases feed", FEED_URL, failure_reason)
        send_heartbeat_if_due()
        return

    # The feed was readable - clear any stale failure flag so a FUTURE failure
    # alerts again instead of staying silent forever.
    set_parse_error_flag(False)

    # An empty list here is NOT an error: between beta cycles the feed can
    # legitimately hold no beta/RC at all.
    announce_new_releases(parse_releases(entries), last_keys)
    send_heartbeat_if_due()


if __name__ == "__main__":
    run()
