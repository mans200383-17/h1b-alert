"""
H-1B (Regular) India slot alert -> Telegram on your phone
==========================================================
This script checks two public tracker sites for H-1B interview slots in India
and sends a Telegram message to your phone when a new one is reported:
  1. checkvisaslots.com  (H-1B Regular page, all 5 consulates)
  2. visagrader.com      (H-1B pages for each consulate; data from VisaJar)

It only reads public pages. It never logs in to the official visa portal,
so your visa account can't be frozen by it.

Ways to run it
--------------
A) On GitHub, free, no computer needed: see SETUP_GITHUB.md. It checks
   about every 5-15 minutes.
B) On your own computer:  python h1b_slot_alert.py
   It checks every 5 minutes until you press Ctrl+C.
Test that alerts reach your phone:  python h1b_slot_alert.py --test

Telegram settings
-----------------
Set the environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (this
is how it works on GitHub), or paste the values into the two settings below.
"""

import html as htmllib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------- SETTINGS -----------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")   # or paste here
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")       # or paste here

# Interview consulates to watch. Remove any you don't want.
LOCATIONS = ["CHENNAI", "HYDERABAD", "NEW DELHI", "MUMBAI", "KOLKATA"]

# Also alert on biometrics (VAC/OFC) slots? These are far more frequent.
INCLUDE_VAC = os.environ.get("INCLUDE_VAC", "false").lower() == "true"

# Only alert for appointment dates on or before this date (YYYY-MM-DD), or ""
# for any date. For example "2027-03-31" ignores far-off dates.
LATEST_USEFUL_DATE = os.environ.get("LATEST_USEFUL_DATE", "")

CHECK_EVERY_MINUTES = 5       # loop mode only; please keep 5 or more
FRESH_WITHIN_MINUTES = 90     # checkvisaslots: only alert on recent sightings
# ---------------------------------------------------------------------------

CVS_URL = "https://www.checkvisaslots.com/visa-slots-info/in/h-1b-regular/"
VG_URLS = {
    "CHENNAI": "https://visagrader.com/us-visa-time-slots-availability/india-ind/chennai-P48/h1b-visa-H1B",
    "HYDERABAD": "https://visagrader.com/us-visa-time-slots-availability/india-ind/hyderabad-P85/h1b-visa-H1B",
    "KOLKATA": "https://visagrader.com/us-visa-time-slots-availability/india-ind/kolkata-P100/h1b-visa-H1B",
    "MUMBAI": "https://visagrader.com/us-visa-time-slots-availability/india-ind/mumbai-P139/h1b-visa-H1B",
    "NEW DELHI": "https://visagrader.com/us-visa-time-slots-availability/india-ind/new-delhi-P147/h1b-visa-H1B",
}
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "h1b_alert_state.json"))
IST = timezone(timedelta(hours=5, minutes=30))


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal slot alert)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def date_ok(iso_date: str) -> bool:
    return not LATEST_USEFUL_DATE or iso_date <= LATEST_USEFUL_DATE


def nice(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %b %Y")


# ---------------------------- checkvisaslots ------------------------------
def cvs_days(page: str) -> list:
    """The page embeds {"days":[{"date", "locations":[{"name","lastSeenAt"}]}]},
    usually JSON-escaped inside a script tag."""
    for marker, escaped in (('{\\"days\\":[', True), ('{"days":[', False)):
        start = page.find(marker)
        if start == -1:
            continue
        depth = 0
        for end in range(start, len(page)):
            if page[end] == "{":
                depth += 1
            elif page[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        chunk = page[start:end + 1]
        if escaped:
            chunk = json.loads('"' + chunk + '"')
        return json.loads(chunk)["days"]
    raise ValueError("checkvisaslots: slot data not found (site layout may have changed)")


def check_cvs(state: dict, hits: dict, now: float) -> None:
    for day in cvs_days(fetch(CVS_URL)):
        if not date_ok(day["date"]):
            continue
        for loc in day["locations"]:
            name, seen = loc["name"], int(loc["lastSeenAt"])
            is_vac = "VAC" in name
            if name.replace(" VAC", "") not in LOCATIONS or (is_vac and not INCLUDE_VAC):
                continue
            key = f'cvs|{day["date"]}|{name}'
            if seen > state.get(key, 0):
                state[key] = seen
                if now - seen <= FRESH_WITHIN_MINUTES * 60:
                    when = datetime.fromtimestamp(seen, IST).strftime("%d %b %H:%M IST")
                    hits.setdefault(f"{name.title()} (checkvisaslots)", []).append((day["date"], f"seen {when}"))


# ------------------------------ visagrader --------------------------------
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def vg_rows(page: str) -> list:
    i = page.find("Recent slot activity")
    if i == -1:
        raise ValueError("visagrader: slot table not found (site layout may have changed)")
    j = page.find("</table>", i)
    rows = []
    for tr in ROW_RE.findall(page[i:j]):
        cells = [htmllib.unescape(TAG_RE.sub("", c)).strip() for c in CELL_RE.findall(tr)]
        if len(cells) >= 4:
            rows.append(cells[:4])   # type, availability date (M/D/YYYY), slots, reported
    return rows


def check_vg(state: dict, hits: dict) -> None:
    for city in LOCATIONS:
        url = VG_URLS.get(city)
        if not url:
            continue
        for typ, avail, slots, reported in vg_rows(fetch(url)):
            if typ.lower() != "interview" and not (INCLUDE_VAC and typ.lower() in ("biometrics", "ofc")):
                continue
            try:
                iso = datetime.strptime(avail, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
            key = f"vg|{city}|{typ}|{iso}|{reported}"
            if key in state or not date_ok(iso):
                state.setdefault(key, 1)
                continue
            state[key] = 1
            hits.setdefault(f"{city.title()} {typ} (visagrader)", []).append((iso, f"{slots} slot(s)"))


# ------------------------------- alerts -----------------------------------
def send_telegram(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("  (Telegram not set up, so the alert is only printed here)")
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, timeout=20)


def notify(text: str) -> None:
    print(text)
    try:
        send_telegram(text)
    except Exception as e:
        print("  ! Telegram send failed:", e)
    try:
        from plyer import notification          # optional desktop pop-up
        notification.notify(title="H-1B slot spotted", message=text[:250], timeout=20)
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def check_once() -> None:
    state = load_state()
    first_run = not state
    hits: dict = {}
    errors = []
    for name, fn in (("checkvisaslots", lambda: check_cvs(state, hits, time.time())),
                     ("visagrader", lambda: check_vg(state, hits))):
        try:
            fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
    save_state(state)

    stamp = datetime.now(IST).strftime("%d %b %H:%M IST")
    for e in errors:
        print(f"[{stamp}] Check failed for {e}")
    if first_run:
        print(f"[{stamp}] First run: saved what's already listed. You'll be alerted on anything new from now on.")
        return
    if not hits:
        print(f"[{stamp}] No new sightings.")
        return
    lines = ["H-1B Regular slots reported:"]
    for label, items in sorted(hits.items()):
        items.sort()
        shown = ", ".join(f"{nice(d)} ({extra})" for d, extra in items[:6])
        more = f" +{len(items) - 6} more" if len(items) > 6 else ""
        lines.append(f"- {label}: {shown}{more}")
    lines.append("Book: https://www.usvisascheduling.com")
    notify("\n".join(lines))


def main() -> None:
    if "--test" in sys.argv:
        notify("Test alert from h1b_slot_alert.py - notifications are working.")
    elif "--once" in sys.argv:
        check_once()
    else:
        while True:
            check_once()
            time.sleep(max(CHECK_EVERY_MINUTES, 3) * 60)


if __name__ == "__main__":
    main()
