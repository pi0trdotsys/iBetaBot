#!/usr/bin/env python3

import html
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -------------------------------
# LOAD ENVIRONMENT VARIABLES
# -------------------------------
load_dotenv()  # expects .env file in same directory

URL = "https://ipsw.dev/"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("TELEGRAM_TOKEN and CHAT_ID must be set in .env")

# File for storing the last state (in the same directory as script)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibeta_last_state.txt")
HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibeta_heartbeat_state.txt")

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


def get_last_heartbeat_date():
    if not os.path.exists(HEARTBEAT_FILE):
        return ""
    with open(HEARTBEAT_FILE, "r") as f:
        return f.read().strip()


def save_heartbeat_date(date_str):
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(date_str)


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

    last_state = get_last_state()
    known_release = last_state if last_state and last_state != PARSE_ERROR_STATE else None

    lines = ["✅ <b>iBetaBot heartbeat</b> — bot is running fine."]
    if known_release:
        lines.append(f"Last known version: <code>{html.escape(known_release)}</code>")
    lines.append(f"Checked: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if send_telegram("\n".join(lines)):
        save_heartbeat_date(today)


# -------------------------------
# MAIN FUNCTION
# -------------------------------
PARSE_ERROR_STATE = "PARSE_ERROR"

# Historical pattern only (ipsw.dev tracks developer betas exclusively, there's
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

RELEASE_PATTERN = re.compile(
    r'<h3 class="font-semibold text-gray-900 dark:text-gray-100">([A-Za-z]+)\s+([0-9][0-9.]*)\s*(.*?)</h3>\s*'
    r'<p class="font-mono text-sm">([0-9A-Za-z]+)</p>\s*'
    r'<p[^>]*>\s*<span class="text-sm">(.*?)</span>',
    flags=re.DOTALL
)


def fetch_html():
    headers = {"User-Agent": "Mozilla/5.0"}
    return session.get(URL, headers=headers, timeout=10).text


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


def build_release_message(matches):
    """Groups the raw regex matches by OS version (e.g. all 27.0 builds
    together, all 26.6 builds together) instead of the site's interleaved
    order, so related platforms read as one block."""
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

    return (
        "🚀 <b>New beta releases available!</b>\n\n"
        f"ℹ️ <i>{html.escape(PUBLIC_BETA_NOTE)}</i>\n\n"
        + "\n\n".join(sections)
        + f"\n\n🌐 <a href=\"{URL}\">Details</a>"
    )


def run():
    last_state = get_last_state()

    try:
        page_html = fetch_html()
    except requests.RequestException as e:
        logger.error("Failed to fetch releases from %s: %s", URL, e)
        return

    # Extract all latest items: system, version, suffix (beta/RC label), build, release date
    matches = RELEASE_PATTERN.findall(page_html)

    if not matches:
        # A single empty result can be a transient blip (stale cache, WAF
        # challenge page, mid-deploy hiccup on their end) rather than a real
        # markup change, so re-fetch once before treating it as broken.
        logger.warning("No releases found on first attempt, retrying fetch once...")
        time.sleep(5)
        try:
            page_html = fetch_html()
        except requests.RequestException as e:
            logger.error("Retry fetch from %s failed: %s", URL, e)
            return
        matches = RELEASE_PATTERN.findall(page_html)

    if not matches:
        logger.error("No releases found in HTML – page structure may have changed")
        # Alert once, not on every cron run, until the page is parseable again
        if last_state != PARSE_ERROR_STATE:
            alert = (
                "⚠️ <b>iBetaBot: no releases found on the page.</b>\n"
                "The site structure may have changed and the scraper needs updating.\n"
                f"🌐 <a href=\"{URL}\">{URL}</a>"
            )
            if send_telegram(alert):
                save_state(PARSE_ERROR_STATE)
        send_heartbeat_if_due()
        return

    releases = []
    for system, version, suffix, build, date_text in matches:
        # Add suffix (beta N / RC / v2 etc.) if present
        full_version = f"{system} {version}"
        if suffix:
            full_version += f" {suffix}"
        releases.append((full_version, build.strip(), date_text.strip()))

    # Current state = latest version + its build + its date
    first_release, first_build, first_date = releases[0]
    current_state = f"{first_release} ({first_build}) | {first_date}"

    # If state changed (or file was empty) → notify
    if last_state != current_state:
        message = build_release_message(matches)

        if send_telegram(message):
            save_state(current_state)
            logger.info("New release saved as last state")
        else:
            logger.error("Telegram send failed – state NOT updated, will retry next run")
    else:
        logger.info("No new releases – skipping notification")

    send_heartbeat_if_due()


if __name__ == "__main__":
    run()
