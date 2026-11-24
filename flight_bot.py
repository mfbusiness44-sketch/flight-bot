#!/usr/bin/env python3
"""
Christmas flight-deal bot (v2): Prince George (YXS) -> Toronto Pearson / Hamilton, round trip.

Every 90 minutes it searches every combination of
  departure  Dec 17-23, 2026
  return     Dec 26-29, 2026
  to         Toronto Pearson (YYZ) and Hamilton (YHM)
and emails you any round trip at or under the price limit (CAD).

What's new in v2
  * A search that fails is reported as FAILED, not silently counted as "no flights".
  * Every search prints a progress line with the cheapest fare it saw, so you can
    compare with what you see on Google Flights.
  * The price limit and stop filters are applied by the bot, not sent to the API,
    so the API can't drop fares on us.
  * Failed searches are retried with longer waits (Ignav's 424 errors are usually brief).
  * If a whole check fails, you get a warning email (at most every 12 hours).
  * --check lets you test one date pair and see exactly what the API returns.

Fares come from the Ignav flight-price API (https://ignav.com).
Only the Python standard library is used - nothing to pip install.

Setup: set IGNAV_API_KEY, GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see README.md).
Alerts go to GMAIL_ADDRESS, or to ALERT_TO if you set that too.

Run:
  python flight_bot.py                                     # run forever
  python flight_bot.py --once                              # one check, then exit
  python flight_bot.py --dry-run --once                    # one check, print instead of email
  python flight_bot.py --check YYZ 2026-12-18 2026-12-27   # inspect one search
  python flight_bot.py --test-email                        # send a test email
"""

import argparse
import itertools
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

# ---------------------------------------------------------------- settings --
SHORT_TRIPS_ONLY = False    # True = only trips with <= MAX_STOPS stops and < MAX_LEG_HOURS each way

ORIGIN = "YXS"                                   # Prince George, BC
DESTINATIONS = {"YYZ": "Toronto Pearson",
                "YHM": "Hamilton"}
DEPART_DATES = [date(2026, 12, d) for d in range(17, 24)]   # Dec 17-23
RETURN_DATES = [date(2026, 12, d) for d in range(26, 30)]   # Dec 26-29
MAX_PRICE = 750                                  # round-trip total, CAD
MARKET = "CA"                                    # prices in Canadian dollars
ADULTS = 1
CHECK_EVERY_MINUTES = 90

MAX_STOPS = 1               # per direction (short-trip mode only)
MAX_LEG_HOURS = 10          # per direction, layovers included (short-trip mode only)
START_DELAY_MINUTES = 45 if SHORT_TRIPS_ONLY else 0   # short-trip bot alternates with the main bot

MAX_BOOKING_LINK_LOOKUPS = 5
RETRY_WAITS = [10, 30, 60]  # seconds to wait before each retry of a failed search
WARNING_EVERY_HOURS = 12    # how often to email if the bot can't get any fares

API_BASE = "https://ignav.com/api/fares"
BOT_NAME = "flight_bot_short" if SHORT_TRIPS_ONLY else "flight_bot"
STATE_FILE = Path(__file__).with_name(f"{BOT_NAME}_state.json")
LOG_FILE = Path(__file__).with_name(f"{BOT_NAME}.log")
TAG = f"[≤{MAX_STOPS} stop, <{MAX_LEG_HOURS}h] " if SHORT_TRIPS_ONLY else ""
# ----------------------------------------------------------------------------


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing environment variable {name}. See README.md for setup.")
    return value


def alert_to():
    return os.environ.get("ALERT_TO", "").strip() or env("GMAIL_ADDRESS")


# ------------------------------------------------------------- Ignav API ----
def api_post(endpoint, payload, api_key):
    """POST to Ignav. Returns (status, data): status is 'ok', 'empty' or 'failed'."""
    body = json.dumps(payload).encode()
    last_error = ""
    for attempt in range(len(RETRY_WAITS) + 1):
        if attempt:
            time.sleep(RETRY_WAITS[attempt - 1])
        req = urllib.request.Request(
            f"{API_BASE}/{endpoint}", data=body, method="POST",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "yxs-flight-bot/2"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return "ok", json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            try:
                code = json.loads(detail).get("error", {}).get("code", "")
            except (ValueError, AttributeError):
                code = ""
            if e.code == 404:
                return "empty", None
            if e.code in (401, 403):
                sys.exit(f"Ignav rejected the API key ({e.code}): {detail}")
            if e.code == 402:
                sys.exit(f"Ignav says the account is out of credit (402): {detail}")
            last_error = f"HTTP {e.code} {code or detail}"
            if code == "unsupported_search" or e.code in (400, 422):
                break                                  # retrying won't help
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_error = f"network error: {e}"
    return "failed", last_error


def search_round_trip(dest, dep, ret, api_key):
    """Returns (status, itineraries or error text)."""
    payload = {"origin": ORIGIN, "destination": dest,
               "departure_date": dep.isoformat(), "return_date": ret.isoformat(),
               "adults": ADULTS, "market": MARKET}
    status, data = api_post("round-trip", payload, api_key)
    if status == "ok":
        return "ok", (data or {}).get("itineraries") or []
    if status == "empty":
        return "ok", []
    return "failed", data


def get_booking_links(ignav_id, api_key):
    status, data = api_post("booking-links", {"ignav_id": ignav_id}, api_key)
    links = []
    for option in ((data or {}).get("booking_options") or []) if status == "ok" else []:
        legs = "+".join(option.get("legs") or []) or "trip"
        for link in option.get("links") or []:
            url = link.get("url", "")
            if url and not url.startswith("http"):
                url = "https://" + url
            price = (link.get("price") or {}).get("amount")
            links.append({"provider": link.get("provider_name", "Seller"),
                          "legs": legs, "price": price, "url": url})
    return links


# ----------------------------------------------------------- trip rules -----
def leg_minutes(leg):
    leg = leg or {}
    if isinstance(leg.get("duration_minutes"), int):
        return leg["duration_minutes"]
    segs = leg.get("segments") or []
    try:
        start = datetime.fromisoformat(segs[0]["departure_time_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(segs[-1]["arrival_time_utc"].replace("Z", "+00:00"))
        return int((end - start).total_seconds() // 60)
    except (IndexError, KeyError, TypeError, AttributeError, ValueError):
        return None


def why_not_short(it):
    """None if the trip meets the short-trip rules, otherwise the reason it doesn't."""
    for name, leg in (("outbound", it.get("outbound")), ("return", it.get("inbound"))):
        segs = (leg or {}).get("segments") or []
        if not segs:
            return f"{name} has no flight details"
        if len(segs) - 1 > MAX_STOPS:
            return f"{name} has {len(segs) - 1} stops"
        mins = leg_minutes(leg)
        if mins is None:
            return f"{name} travel time unknown"
        if mins >= MAX_LEG_HOURS * 60:
            return f"{name} takes {mins // 60}h{mins % 60:02d}"
    return None


def price_of(it):
    p = (it.get("price") or {}).get("amount")
    return float(p) if isinstance(p, (int, float)) and p > 0 else None


# ------------------------------------------------------------ formatting ----
def fmt_leg(leg):
    segs = (leg or {}).get("segments") or []
    if not segs:
        return "?"
    first, last = segs[0], segs[-1]
    dep_t = (first.get("departure_time_local") or "")[11:16]
    arr_t = (last.get("arrival_time_local") or "")[11:16]
    route = " → ".join([s.get("departure_airport") or "?" for s in segs] + [last.get("arrival_airport") or "?"])
    flights = ", ".join(f"{s.get('marketing_carrier_code') or ''}{s.get('flight_number') or ''}" for s in segs)
    stops = len(segs) - 1
    stop_txt = "nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
    mins = leg_minutes(leg)
    dur = f"{mins // 60}h{mins % 60:02d}" if isinstance(mins, int) else ""
    return f"{leg.get('carrier') or ''} {flights} | {route} | {dep_t}–{arr_t} | {stop_txt} {dur}".strip()


def deal_key(it):
    def flights(leg):
        return "/".join(f"{s.get('marketing_carrier_code')}{s.get('flight_number')}@{s.get('departure_time_local')}"
                        for s in (leg or {}).get("segments") or [])
    return flights(it.get("outbound")) + "|" + flights(it.get("inbound"))


def google_flights_url(dest, dep, ret):
    q = f"Flights from {ORIGIN} to {dest} on {dep} through {ret}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + "&curr=CAD"


# ----------------------------------------------------------------- email ----
def send_email(subject, html, text):
    sender = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD").replace(" ", "")
    to = alert_to()
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(sender, password)
        s.sendmail(sender, [to], msg.as_string())


def build_email(deals):
    deals = sorted(deals, key=lambda d: d["price"])
    cheapest = deals[0]
    subject = (f"✈️ {TAG}YXS→{cheapest['dest_code']} ${cheapest['price']:.0f} CAD round trip "
               f"({len(deals)} deal{'s' if len(deals) > 1 else ''} ≤ ${MAX_PRICE})")
    rows_html, rows_text = [], []
    for d in deals:
        parts = []
        for l in d["links"]:
            legs = f" ({escape(l['legs'])})" if l["legs"] not in ("trip", "outbound+inbound") else ""
            price = f" ${l['price']:.0f}" if isinstance(l["price"], (int, float)) else ""
            parts.append(f'<div><a href="{escape(l["url"])}">{escape(l["provider"])}</a>{legs}{price}</div>')
        links_html = "".join(parts) or f'<a href="{escape(d["gf_url"])}">Search on Google Flights</a>'
        drop = f'<br><small>was ${d["prev_price"]:.0f}</small>' if d.get("prev_price") else ""
        rows_html.append(
            f"<tr><td><b>${d['price']:.0f}</b> {escape(d['currency'])}{drop}</td>"
            f"<td>{escape(d['dest_name'])}</td>"
            f"<td>{d['dep']:%a %b %d}<br>{d['ret']:%a %b %d}</td>"
            f"<td><small>Out: {escape(d['out'])}<br>Back: {escape(d['back'])}</small></td>"
            f"<td><small>{links_html}</small></td></tr>")
        rows_text.append(
            f"${d['price']:.0f} {d['currency']} — {d['dest_name']} — {d['dep']:%b %d} to {d['ret']:%b %d}\n"
            f"   Out:  {d['out']}\n   Back: {d['back']}\n"
            + "".join(f"   {l['provider']}: {l['url']}\n" for l in d["links"])
            + (f"   Google Flights: {d['gf_url']}\n" if not d["links"] else ""))
    rule = (f"<p>Short-trip bot: max {MAX_STOPS} stop and under {MAX_LEG_HOURS} hours each way.</p>"
            if SHORT_TRIPS_ONLY else "")
    html = f"""<html><body style="font-family:Arial,sans-serif">
<h2>Round trips from Prince George at or under ${MAX_PRICE} CAD</h2>{rule}
<table cellpadding="8" cellspacing="0" border="1" style="border-collapse:collapse;font-size:14px">
<tr style="background:#eee"><th>Price</th><th>To</th><th>Dates</th><th>Flights</th><th>Book</th></tr>
{''.join(rows_html)}
</table>
<p style="color:#666;font-size:12px">Prices are total round-trip for {ADULTS} adult, economy, as quoted at
{datetime.now():%b %d %H:%M}. Fares change fast — confirm the price and baggage with the seller before buying.
Some cheap results are two separate one-way tickets; the booking links say which legs each covers.</p>
</body></html>"""
    text = f"Round trips from Prince George at or under ${MAX_PRICE} CAD:\n\n" + "\n".join(rows_text)
    return subject, html, text


# ----------------------------------------------------------------- state ----
def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("alerted", {})
    return state


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f"Could not save state: {e}")


# ------------------------------------------------------------- one check ----
def run_check(api_key, dry_run=False):
    today = date.today()
    dep_dates = [d for d in DEPART_DATES if d >= today]
    if not dep_dates:
        log("All departure dates have passed — nothing left to search.")
        return False

    combos = list(itertools.product(DESTINATIONS, dep_dates, RETURN_DATES))
    log(f"Starting check: {len(combos)} searches"
        + (f" (only ≤{MAX_STOPS} stop, <{MAX_LEG_HOURS}h each way)" if SHORT_TRIPS_ONLY else "") + ".")
    state = load_state()
    found, seen_now, failures = [], set(), []
    best = None   # cheapest qualifying fare seen this check, even if over the limit

    for n, (dest, dep, ret) in enumerate(combos, 1):
        label = f"{n:>2}/{len(combos)} {dest} {dep:%b %d}→{ret:%b %d}"
        status, result = search_round_trip(dest, dep, ret, api_key)
        if status != "ok":
            failures.append(label)
            log(f"{label}: FAILED ({result})")
            time.sleep(1)
            continue

        priced = [(price_of(it), it) for it in result]
        priced = [(p, it) for p, it in priced if p is not None]
        qualifying = [(p, it) for p, it in priced if not SHORT_TRIPS_ONLY or why_not_short(it) is None]
        if qualifying:
            p_min = min(p for p, _ in qualifying)
            note = f"cheapest ${p_min:.0f}" + (" ✅" if p_min <= MAX_PRICE else "")
            if best is None or p_min < best[0]:
                best = (p_min, f"{dest} {dep:%b %d}→{ret:%b %d}")
        elif priced:
            note = "none meet the stop/time rules"
        else:
            note = "no fares returned"
        log(f"{label}: {note} ({len(priced)} fares)")

        for price, it in qualifying:
            if price > MAX_PRICE:
                continue
            key = deal_key(it)
            if key in seen_now:
                continue
            seen_now.add(key)
            found.append({
                "key": key, "price": price,
                "currency": (it.get("price") or {}).get("currency", "CAD"),
                "dest_code": dest, "dest_name": DESTINATIONS[dest], "dep": dep, "ret": ret,
                "out": fmt_leg(it.get("outbound")), "back": fmt_leg(it.get("inbound")),
                "ignav_id": it.get("ignav_id"), "gf_url": google_flights_url(dest, dep, ret),
                "links": [],
            })
        time.sleep(1)

    ok = len(combos) - len(failures)
    log(f"Check done: {ok}/{len(combos)} searches worked, {len(failures)} failed. "
        + (f"Cheapest fare seen: ${best[0]:.0f} ({best[1]})." if best else "No fares seen."))

    if failures and ok == 0:
        warn_all_failed(state, len(combos), dry_run)
        return True

    if not found:
        log(f"No round trips at or under ${MAX_PRICE} this check.")
        return True

    new = []
    for d in found:
        prev = state["alerted"].get(d["key"])
        if prev is None or d["price"] <= prev - 10:
            d["prev_price"] = prev
            new.append(d)
    log(f"{len(found)} deal(s) ≤ ${MAX_PRICE}; {len(new)} new or cheaper than last alert.")
    if not new:
        return True

    new.sort(key=lambda d: d["price"])
    for d in new[:MAX_BOOKING_LINK_LOOKUPS]:
        if d["ignav_id"]:
            d["links"] = get_booking_links(d["ignav_id"], api_key)

    subject, html, text = build_email(new)
    if dry_run:
        log("[dry run] Would email:\n" + subject + "\n" + text)
        return True
    try:
        send_email(subject, html, text)
        log(f"Emailed alert: {subject}")
        for d in new:
            state["alerted"][d["key"]] = d["price"]
        save_state(state)
    except Exception as e:
        log(f"EMAIL FAILED: {e}")
    return True


def warn_all_failed(state, total, dry_run):
    log("WARNING: every search failed this check. The flight API may be down.")
    last = state.get("last_warning", 0)
    if dry_run or time.time() - last < WARNING_EVERY_HOURS * 3600:
        return
    try:
        send_email(f"⚠️ {TAG}Flight bot can't get fares",
                   f"<p>All {total} searches failed in the last check. The bot will keep trying "
                   f"every {CHECK_EVERY_MINUTES} minutes. See {LOG_FILE.name} for the error messages.</p>",
                   f"All {total} searches failed in the last check. The bot will keep trying. "
                   f"See {LOG_FILE.name} for the error messages.")
        state["last_warning"] = time.time()
        save_state(state)
        log("Warning email sent.")
    except Exception as e:
        log(f"Warning email failed: {e}")


# ---------------------------------------------------------- one search -----
def check_one(api_key, dest, dep, ret):
    dest = dest.upper()
    dep, ret = date.fromisoformat(dep), date.fromisoformat(ret)
    print(f"Searching {ORIGIN} → {dest}, {dep} to {ret}, market {MARKET}...")
    status, result = search_round_trip(dest, dep, ret, api_key)
    if status != "ok":
        print(f"FAILED: {result}")
        return
    priced = sorted([(price_of(it), it) for it in result if price_of(it)], key=lambda x: x[0])
    print(f"{len(result)} itineraries returned.\n")
    for p, it in priced[:15]:
        cur = (it.get("price") or {}).get("currency", "")
        flags = ["UNDER LIMIT"] if p <= MAX_PRICE else []
        reason = why_not_short(it)
        flags.append("short-trip OK" if reason is None else f"not short: {reason}")
        print(f"${p:.0f} {cur}  [{', '.join(flags)}]")
        print(f"   Out:  {fmt_leg(it.get('outbound'))}")
        print(f"   Back: {fmt_leg(it.get('inbound'))}\n")


def main():
    p = argparse.ArgumentParser(description="YXS → Toronto/Hamilton Christmas fare alert bot")
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument("--dry-run", action="store_true", help="print results instead of emailing")
    p.add_argument("--test-email", action="store_true", help="send a test email and exit")
    p.add_argument("--check", nargs=3, metavar=("DEST", "DEPART", "RETURN"),
                   help="inspect one search, e.g. --check YYZ 2026-12-18 2026-12-27")
    args = p.parse_args()

    if args.test_email:
        send_email(f"{TAG}Flight bot test ✈️", "<p>Your flight bot can send email. 🎄</p>",
                   "Your flight bot can send email.")
        log("Test email sent.")
        return

    api_key = env("IGNAV_API_KEY")
    if args.check:
        check_one(api_key, *args.check)
        return
    if not args.dry_run:
        env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD")

    if not args.once and START_DELAY_MINUTES > 0:
        log(f"Short-trip bot started. First check at "
            f"{datetime.now() + timedelta(minutes=START_DELAY_MINUTES):%H:%M} (offset from the main bot).")
        time.sleep(START_DELAY_MINUTES * 60)

    while True:
        started = time.time()
        try:
            keep_going = run_check(api_key, dry_run=args.dry_run)
        except Exception as e:
            log(f"Unexpected error: {e!r}")
            keep_going = True
        if args.once or not keep_going:
            break
        wait = max(60, CHECK_EVERY_MINUTES * 60 - (time.time() - started))
        log(f"Next check at {datetime.now() + timedelta(seconds=wait):%H:%M}.")
        time.sleep(wait)


if __name__ == "__main__":
    main()
