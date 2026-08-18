#!/usr/bin/env python3
"""
Ultra-light CRSHMARKET Total Markets watcher (API only).
Uses a few KB per check instead of megabytes.
"""

import os
import time
import json
from datetime import datetime
import requests

# ---------------- CONFIG ----------------
USERNAMES = [
    "aman9427", "banti2994", "chirag8492", "dhruv__sharma",
    "elyas8327", "faisal73792", "gagan83772", "harsh12999",
    "ishaan1993", "jayant19993",
]

BASELINE = {
    "aman9427": 55, "banti2994": 59, "chirag8492": 54, "dhruv__sharma": 56,
    "elyas8327": 13, "faisal73792": 15, "gagan83772": 3, "harsh12999": 3,
    "ishaan1993": 13, "jayant19993": 11,
}

CONVEX_URL = "https://impartial-newt-333.convex.cloud/api/query"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_SECONDS = 15          # can safely lower to 8–10
BATCH_WINDOW_SECONDS = 10
STATE_FILE = "last_values.json"
# -----------------------------------------

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "User-Agent": "CRSH-Watcher/1.0",
})

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def telegram_configured():
    return "PUT_YOUR" not in TELEGRAM_BOT_TOKEN and "PUT_YOUR" not in TELEGRAM_CHAT_ID

def send_telegram(message: str):
    if not telegram_configured():
        print(f"[{now()}] NOTIFY: {message}")
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        if not r.ok:
            print(f"[{now()}] Telegram error: {r.status_code}")
    except Exception as e:
        print(f"[{now()}] Telegram error: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def relative(username, absolute):
    return absolute - BASELINE.get(username, 0)

def get_total_markets(username: str) -> int:
    payload = {
        "path": "usernamePages:getByHandle",
        "args": {"handle": username},
        "format": "json",
    }
    r = session.post(CONVEX_URL, json=payload, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"API error: {data}")
    stats = data["value"]["profile"]["stats"]
    # Prefer resolvedMarkets (matches UI "Total Markets")
    return int(stats.get("resolvedMarkets") or stats.get("resolvedBetCount") or 0)

def build_batch_message(state, pending_deltas):
    increased = [u for u, d in pending_deltas.items() if d > 0]
    lines = [f"📈 New bet activity ({len(increased)} of {len(USERNAMES)} users):", ""]
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

def main():
    if telegram_configured():
        send_telegram(f"✅ API watcher started. Monitoring {len(USERNAMES)} users.")
    else:
        print(f"[{now()}] Telegram not configured – console only.")

    state = load_state()
    pending_deltas = {}
    window_open_since = None

    print(f"[{now()}] Starting lightweight API watcher")

    # Initial read
    for username in USERNAMES:
        try:
            val = get_total_markets(username)
            state[username] = val
            print(f"[{now()}] {username}: abs={val}  rel={relative(username, val)}")
        except Exception as e:
            print(f"[{now()}] {username}: error {e}")
        save_state(state)

    while True:
        any_increase = False

        for username in USERNAMES:
            try:
                current = get_total_markets(username)
                last = state.get(username)

                if last is not None and current > last:
                    delta = current - last
                    pending_deltas[username] = pending_deltas.get(username, 0) + delta
                    any_increase = True
                    print(f"[{now()}] {username}: +{delta}  (abs {last} → {current})")

                state[username] = current
            except Exception as e:
                print(f"[{now()}] {username}: error {e}")

            save_state(state)

        now_mono = time.monotonic()

        if any_increase and window_open_since is None:
            window_open_since = now_mono
            print(f"[{now()}] Collection window opened ({BATCH_WINDOW_SECONDS}s)")

        if window_open_since is not None and (now_mono - window_open_since) >= BATCH_WINDOW_SECONDS:
            if pending_deltas:
                msg = build_batch_message(state, pending_deltas)
                print(f"[{now()}] Sending batch:\n{msg}")
                send_telegram(msg)
            pending_deltas = {}
            window_open_since = None

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
