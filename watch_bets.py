#!/usr/bin/env python3
"""
Watch multiple CRSHMARKET profile pages and send a Telegram message whenever
any user's "Total Markets" number increases.

The number is rendered client-side (React), so a plain HTTP request won't
see it -- this uses Playwright (a real headless browser) to load the page
and read the value out of the DOM, then polls on an interval.

This version:
  - Starts counting from 0 relative to a fixed baseline (the values that
    existed when you "reset" the counters). Absolute site numbers are still
    read; we subtract the baseline so notifications and /start show relative
    counts that begin at 0.
  - On the first increase: start a 10-second collection window (do NOT send yet).
  - Keep watching the other accounts during the window. Any further increases
    are accumulated.
  - When the 10-second window expires, send ONE Telegram message with all
    the collected activity.
  - Replies to /start with the current relative totals (no grand-total line).
  - Sends a one-time "watcher is online" message on startup.

SETUP
-----
1. Install dependencies:
     pip install playwright requests
     playwright install chromium

2. Create a Telegram bot:
     - Message @BotFather on Telegram -> /newbot -> follow prompts
     - Copy the bot token it gives you

3. Get your chat id:
     - Message your new bot anything (e.g. "hi")
     - Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     - Find "chat":{"id": ...} in the JSON -> that's your CHAT_ID

4. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below (or set them as
   environment variables of the same name).

5. Run:
     python3 watch_bets.py

   Leave it running (e.g. in a `screen`/`tmux` session, or as a background
   service) -- it checks every POLL_SECONDS and only messages you on change.
"""

import os
import re
import time
import json
import sys
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

# ---------------- CONFIG ----------------
USERNAMES = [
    "aman9427",
    "banti2994",
    "chirag8492",
    "dhruv__sharma",
    "elyas8327",
    "faisal73792",
    "gagan83772",
    "harsh12999",
    "ishaan1993",
    "jayant19993",
]

# Absolute Total Markets values that existed when you decided to start
# counting from zero. Relative count for a user = current_absolute - baseline.
BASELINE = {
    "aman9427": 55,
    "banti2994": 59,
    "chirag8492": 54,
    "dhruv__sharma": 56,
    "elyas8327": 13,
    "faisal73792": 15,
    "gagan83772": 3,
    "harsh12999": 3,
    "ishaan1993": 13,
    "jayant19993": 11,
}

PROFILE_URL_TEMPLATE = "https://app.crshmarket.com/{username}"

# The stat card to watch. data-testid on the <section> in the page:
STAT_TESTID = "profile-stat-markets"   # "Total Markets" card

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_SECONDS = 5           # how often we re-check every user
BATCH_WINDOW_SECONDS = 10  # after first increase, wait this long then send one combined message
STATE_FILE = "last_values.json"
UPDATE_OFFSET_FILE = "last_update_id.json"  # tracks which incoming Telegram messages we've handled
# -----------------------------------------


def telegram_configured():
    return "PUT_YOUR" not in TELEGRAM_BOT_TOKEN and "PUT_YOUR" not in TELEGRAM_CHAT_ID


def send_telegram(message: str, chat_id: str = None):
    """
    Sends a new Telegram message.
    Returns the message_id (int) on success, or None if Telegram is not
    configured / the call failed.
    """
    if not telegram_configured():
        print(f"[{now()}] NOTIFY (telegram not set up yet): {message}")
        return None
    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": target_chat_id, "text": message},
            timeout=15,
        )
        if not r.ok:
            print(f"[{now()}] Telegram send failed: {r.status_code} {r.text}")
            return None
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"[{now()}] Telegram send error: {e}")
        return None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    """Returns {username: last_absolute_value, ...}"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_update_offset():
    if os.path.exists(UPDATE_OFFSET_FILE):
        with open(UPDATE_OFFSET_FILE) as f:
            return json.load(f).get("offset", 0)
    return 0


def save_update_offset(offset):
    with open(UPDATE_OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


def relative(username, absolute):
    """Absolute site count minus the fixed baseline → starts at 0."""
    return absolute - BASELINE.get(username, 0)


def build_summary_message(state):
    """
    Current relative Total Markets for every user (no grand-total line).
    Missing users are shown as "pending first check".
    """
    lines = ["📊 Current Total Markets (relative, started from 0):", ""]
    for username in USERNAMES:
        abs_val = state.get(username)
        if abs_val is None:
            lines.append(f"• {username}: pending first check")
        else:
            lines.append(f"• {username}: {relative(username, abs_val)}")
    return "\n".join(lines)


def build_batch_message(state, pending_deltas):
    """
    Message body for a finished batch window.
    Lists every watched user; users who increased are marked with the delta
    and their new relative total.
    """
    increased = [u for u, d in pending_deltas.items() if d > 0]
    lines = [
        f"📈 New bet activity ({len(increased)} of {len(USERNAMES)} users):",
        "",
    ]
    for username in USERNAMES:
        abs_val = state.get(username)
        if abs_val is None:
            lines.append(f"• {username}: pending")
            continue
        rel = relative(username, abs_val)
        delta = pending_deltas.get(username, 0)
        if delta > 0:
            lines.append(f"• {username}: {rel}  (+{delta})")
        else:
            lines.append(f"• {username}: {rel}")
    return "\n".join(lines)


def check_incoming_messages(offset, state):
    """
    Polls Telegram for any new messages sent to the bot (e.g. /start) and
    replies with a short status message. Returns the new offset to use on
    the next call.
    """
    if not telegram_configured():
        return offset

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
        if not r.ok:
            print(f"[{now()}] getUpdates failed: {r.status_code} {r.text}")
            return offset
        results = r.json().get("result", [])
    except Exception as e:
        print(f"[{now()}] getUpdates error: {e}")
        return offset

    new_offset = offset
    for update in results:
        new_offset = max(new_offset, update["update_id"] + 1)
        message = update.get("message")
        if not message:
            continue
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        if chat_id is None:
            continue

        if text.strip().lower().startswith("/start"):
            send_telegram(build_summary_message(state), chat_id=chat_id)
        else:
            send_telegram(
                "👋 I'm a one-way alert bot. Send /start for the current relative totals. "
                "I'll message you when any watched users increase their Total Markets "
                "(first increase starts a 10 s collection window; one combined message "
                "is sent when the window closes).",
                chat_id=chat_id,
            )

    return new_offset


def read_total_markets(page):
    """Reads the aria-label number out of the stat card, e.g. '49'."""
    selector = f'[data-testid="{STAT_TESTID}"] .slot-text'
    page.wait_for_selector(selector, timeout=15000)
    aria_label = page.locator(selector).first.get_attribute("aria-label")
    if aria_label is None:
        raise RuntimeError("Could not find aria-label on stat element")
    # Keep only digits (value is a plain integer like "49")
    digits = re.sub(r"[^\d]", "", aria_label)
    if digits == "":
        raise RuntimeError(f"Unexpected value in aria-label: {aria_label!r}")
    return int(digits)


def check_user(page, username, state):
    """
    Returns the positive delta (absolute increase) since the last recorded
    value, or 0 if nothing changed / first reading / error.
    Always updates state[username] to the latest absolute value.
    """
    url = PROFILE_URL_TEMPLATE.format(username=username)
    last_abs = state.get(username)

    page.goto(url, wait_until="networkidle", timeout=30000)
    current_abs = read_total_markets(page)

    delta = 0
    if last_abs is None:
        print(f"[{now()}] {username}: baseline reading abs={current_abs}  rel={relative(username, current_abs)}")
    elif current_abs > last_abs:
        delta = current_abs - last_abs
        print(f"[{now()}] {username}: +{delta}  (abs {last_abs} → {current_abs},  rel now {relative(username, current_abs)})")
    # decreases and no-change are silent

    state[username] = current_abs
    return delta


def main():
    if not telegram_configured():
        print(f"[{now()}] Telegram not configured yet -- changes will just be printed to console.")
        print(f"[{now()}] Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID later to also get Telegram messages.")
    else:
        # One-time confirmation so you know immediately the bot is alive and configured correctly.
        send_telegram(
            f"✅ Watcher started (relative counts from 0). Monitoring {len(USERNAMES)} users. "
            f"First increase → 10 s collection window → one combined message when the window closes."
        )

    state = load_state()
    update_offset = load_update_offset()
    print(f"[{now()}] Starting watcher for {len(USERNAMES)} users: {', '.join(USERNAMES)}")
    print(f"[{now()}] Relative baseline locked to: {BASELINE}")

    # Live batch state
    pending_deltas = {}          # username → total absolute increase in this window
    window_open_since = None     # monotonic time when the current window started

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Do one pass up front so /start has real numbers right away instead of "pending".
        for username in USERNAMES:
            try:
                check_user(page, username, state)
            except Exception as e:
                print(f"[{now()}] {username}: error during initial check: {e}")
            save_state(state)

        while True:
            # Reply to any /start or other message sent to the bot since we last checked.
            update_offset = check_incoming_messages(update_offset, state)
            save_update_offset(update_offset)

            # ----- poll every user -----
            any_increase_this_round = False
            for username in USERNAMES:
                try:
                    delta = check_user(page, username, state)
                    if delta > 0:
                        pending_deltas[username] = pending_deltas.get(username, 0) + delta
                        any_increase_this_round = True
                except Exception as e:
                    print(f"[{now()}] {username}: error during check: {e}")
                save_state(state)

            now_mono = time.monotonic()

            # First increase of a new window → open the collection window (do NOT send yet)
            if any_increase_this_round and window_open_since is None:
                window_open_since = now_mono
                print(f"[{now()}] First increase detected – starting {BATCH_WINDOW_SECONDS}s collection window")

            # Window expired → send the combined message and close the window
            if window_open_since is not None and (now_mono - window_open_since) >= BATCH_WINDOW_SECONDS:
                if pending_deltas:
                    msg = build_batch_message(state, pending_deltas)
                    print(f"[{now()}] Batch window closed – sending message:\n{msg}")
                    send_telegram(msg)
                else:
                    print(f"[{now()}] Batch window closed (no deltas – nothing to send)")
                pending_deltas = {}
                window_open_since = None

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
