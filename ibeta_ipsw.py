#!/usr/bin/env python3
"""audioOS and HomeView Software builds, read from ipsw.dev.

Apple's releases feed (ibeta_bot.py, runs on GitHub Actions) does not carry these
two products, ipsw.dev does. ipsw.dev answers GitHub's datacenter IPs with a
Cloudflare challenge (HTTP 403) but serves a normal home connection fine, so this
runs from a LaunchAgent on a Mac instead of from Actions.

It reuses the bot's formatting, Telegram sending and state/alert/heartbeat logic,
but keeps its own machine-local state (ibeta_ipsw_*.txt, git-ignored): it must not
touch the files the Actions workflow commits, and the two never overlap because
each tracks a disjoint list of products (FEED_SYSTEMS vs IPSW_SYSTEMS).

Needs the same TELEGRAM_TOKEN / CHAT_ID as the bot, from a .env next to this file.
"""

import html
import os
import re
from datetime import datetime, timezone

import requests

import ibeta_bot as core
from ibeta_bot import SourceError, logger

IPSW_URL = "https://ipsw.dev/"
SOURCE_NAME = "ipsw.dev"

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "ibeta_ipsw_state.txt")
HEARTBEAT_FILE = os.path.join(_HERE, "ibeta_ipsw_heartbeat.txt")
FLAG_FILE = os.path.join(_HERE, "ibeta_ipsw_flag.txt")

# The parser keys on structure, not on CSS classes: every release is an
# <a href="/build/<build>"> card whose last three <p> elements are title, build
# and date. ipsw.dev has restyled twice already, and each time a class-based
# regex silently stopped matching anything.
BUILD_LINK_PATTERN = re.compile(r'<a\s+href="/build/([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
PARAGRAPH_PATTERN = re.compile(r"<p[^>]*>\s*([^<]*?)\s*</p>")
TITLE_PATTERN = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)*)\s*(.*)$")


def page_summary(page_html):
    """One-line look at what came back - tells a markup change from a block /
    consent / error page without guessing."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.DOTALL | re.IGNORECASE)
    return "title=%r, length=%d" % (html.unescape(title.group(1).strip())[:80] if title else None, len(page_html))


def parse_cards(page_html):
    """(system, version, label, build, date_text) for every DATED release card,
    whatever the product. Cards without a date (the stable-release section) are
    skipped; the caller decides which products it wants."""
    cards = []
    for href_build, inner in BUILD_LINK_PATTERN.findall(page_html):
        paragraphs = [html.unescape(p) for p in PARAGRAPH_PATTERN.findall(inner)]
        if len(paragraphs) < 3:
            continue
        title, build, date_text = paragraphs[-3:]
        title_match = TITLE_PATTERN.match(title)
        if not title_match or build != href_build:
            logger.warning("Skipping unrecognised release card: %r", paragraphs)
            continue
        system, version, label = title_match.groups()
        cards.append((system, version, label, build, date_text))
    return cards


def load_cards():
    response = core.session.get(IPSW_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    logger.info("Fetched %s: HTTP %s, %d bytes", IPSW_URL, response.status_code, len(response.content))
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code} ({page_summary(response.text)})")
    cards = parse_cards(response.text)
    if not cards:
        raise SourceError(f"no release cards found ({page_summary(response.text)})")
    return cards


def select_releases(cards, today=None):
    """Only the products the Apple feed lacks, and nothing older than
    MAX_AGE_DAYS. A page with cards but none of ours is NOT an error."""
    today = today or datetime.now(timezone.utc).date()
    selected = []
    for card in cards:
        if card[0] not in core.IPSW_SYSTEMS:
            continue
        published = core.parse_display_date(card[4])
        if published and (today - published).days > core.MAX_AGE_DAYS:
            continue
        selected.append(card)
    return selected


def run():
    last_keys = core.get_last_release_keys(STATE_FILE)

    try:
        cards, failure_reason = core.read_with_one_retry(load_cards)
    except requests.RequestException as e:
        # Mac just woke up and the network isn't back yet, etc. - not worth an alert.
        logger.error("Failed to fetch %s: %s", IPSW_URL, e)
        return

    heartbeat = dict(source_label="ipsw.dev (Mac agent)", heartbeat_file=HEARTBEAT_FILE, state_file=STATE_FILE, flag_file=FLAG_FILE)

    if cards is None:
        logger.error("ipsw.dev unusable: %s", failure_reason)
        core.report_source_failure(SOURCE_NAME, IPSW_URL, failure_reason, flag_file=FLAG_FILE)
        core.send_heartbeat_if_due(**heartbeat)
        return

    core.set_parse_error_flag(False, FLAG_FILE)
    core.announce_new_releases(
        select_releases(cards),
        last_keys,
        state_file=STATE_FILE,
        details=(IPSW_URL, "ipsw.dev"),
        note=None,  # the Public Beta note is about the main OS betas
    )
    core.send_heartbeat_if_due(**heartbeat)


if __name__ == "__main__":
    run()
