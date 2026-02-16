#!/usr/bin/env python3

import requests
import re
import os
from datetime import datetime
from dotenv import load_dotenv

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
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }
    try:
        r = requests.post(endpoint, json=payload, timeout=10)
        print("Telegram status:", r.status_code)
    except requests.RequestException as e:
        print(f"❌ Failed to send Telegram message: {e}")


# -------------------------------
# MAIN FUNCTION
# -------------------------------
def run():
    last_state = get_last_state()

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        html = requests.get(URL, headers=headers, timeout=10).text
    except requests.RequestException as e:
        print(f"❌ Failed to fetch releases from {URL}: {e}")
        return

    # Extract all latest items: system, version, release date
    matches = re.findall(
        r'<h3 class="fs-6 m-0 fw-semibold">([A-Za-z]+)\s+([0-9\.]+)\s*(RC|Beta)?\s*\([0-9A-Z]+\)</h3>\s*<p class="mb-0"><small>(.*?)</small>',
        html,
        flags=re.DOTALL
    )

    if not matches:
        print("❌ No releases found in HTML")
        return

    releases = []
    for system, version, suffix, date_text in matches:
        # Dodaj suffix (RC/Beta) jeśli jest
        full_version = f"{system} {version}"
        if suffix:
            full_version += f" {suffix}"
        releases.append((full_version, date_text.strip()))

    # Current state = pierwsza wersja + jej data
    first_release, first_date = releases[0]
    current_state = f"{first_release} | {first_date}"

    # Jeśli plik pusty lub stan się zmienił → wysyłamy
    if last_state != current_state:
        message_lines = [f"🔹 {r[0]} – {r[1]}" for r in releases]
        message = (
            "🚀 New beta releases available! 🚀\n\n"
            + "\n".join(message_lines)
            + f"\n\n🌐 Details: {URL}"
        )

        send_telegram(message)
        save_state(current_state)
        print("✅ New release saved as last state")
    else:
        print("ℹ️ No new releases – skipping notification")


if __name__ == "__main__":
    run()
