#!/usr/bin/env python3

import logging
import os
import re
import time

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


# -------------------------------
# TELEGRAM NOTIFICATIONS
# -------------------------------
def send_telegram(message):
    """Returns True if Telegram confirmed delivery, False otherwise."""
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
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
# MAIN FUNCTION
# -------------------------------
PARSE_ERROR_STATE = "PARSE_ERROR"

RELEASE_PATTERN = re.compile(
    r'<h3 class="font-semibold text-gray-900 dark:text-gray-100">([A-Za-z]+)\s+([0-9][0-9.]*)\s*(.*?)</h3>\s*'
    r'<p class="font-mono text-sm">([0-9A-Za-z]+)</p>\s*'
    r'<p[^>]*>\s*<span class="text-sm">(.*?)</span>',
    flags=re.DOTALL
)


def fetch_html():
    headers = {"User-Agent": "Mozilla/5.0"}
    return session.get(URL, headers=headers, timeout=10).text


def run():
    last_state = get_last_state()

    try:
        html = fetch_html()
    except requests.RequestException as e:
        logger.error("Failed to fetch releases from %s: %s", URL, e)
        return

    # Extract all latest items: system, version, suffix (beta/RC label), build, release date
    matches = RELEASE_PATTERN.findall(html)

    if not matches:
        # A single empty result can be a transient blip (stale cache, WAF
        # challenge page, mid-deploy hiccup on their end) rather than a real
        # markup change, so re-fetch once before treating it as broken.
        logger.warning("No releases found on first attempt, retrying fetch once...")
        time.sleep(5)
        try:
            html = fetch_html()
        except requests.RequestException as e:
            logger.error("Retry fetch from %s failed: %s", URL, e)
            return
        matches = RELEASE_PATTERN.findall(html)

    if not matches:
        logger.error("No releases found in HTML – page structure may have changed")
        # Alert once, not on every cron run, until the page is parseable again
        if last_state != PARSE_ERROR_STATE:
            alert = (
                "⚠️ iBetaBot: no releases found on the page.\n"
                "The site structure may have changed and the scraper needs updating.\n"
                f"🌐 {URL}"
            )
            if send_telegram(alert):
                save_state(PARSE_ERROR_STATE)
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
        message_lines = [f"🔹 {r[0]} ({r[1]}) – {r[2]}" for r in releases]
        message = (
            "🚀 New beta releases available! 🚀\n\n"
            + "\n".join(message_lines)
            + f"\n\n🌐 Details: {URL}"
        )

        if send_telegram(message):
            save_state(current_state)
            logger.info("New release saved as last state")
        else:
            logger.error("Telegram send failed – state NOT updated, will retry next run")
    else:
        logger.info("No new releases – skipping notification")


if __name__ == "__main__":
    run()
