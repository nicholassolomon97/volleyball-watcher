import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

URL = os.environ["TARGET_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "volleyball_state.json")


# .strip() so an accidental space after a comma (easy to introduce when
# hand-editing the list on GitHub) doesn't silently drop a venue.
PRIORITY_VENUES = {
    s.strip() for s in os.environ.get("PRIORITY_VENUES", "").split(",") if s.strip()
}

EASTERN = ZoneInfo("America/New_York")
WEEKDAY_ABBR = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thurs", 4: "Fri", 5: "Sat", 6: "Sun"}


def extract_venue_from_name(full_name):
    """Fallback: pull the venue out of the readable session name when the
    structured venue field is a shared reference elsewhere on the page."""
    if not full_name:
        return None
    segment = full_name.split(" - ")[-1].strip()
    m = re.match(r"^(.*?)\s+\d{1,2}:\d{2}(?:am|pm)$", segment, re.IGNORECASE)
    return m.group(1).strip() if m else segment


def fetch_events(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    # Build a venue-name -> slug lookup from every fully-inlined venue object
    # anywhere on the page, since some events only reference their venue by
    # pointer (no inline slug in that event's own chunk).
    venue_slug_by_name = {}
    for m in re.finditer(r'slug:"([^"]+)",shorthand_name:"([^"]+)",latitude:', html):
        venue_slug_by_name[m.group(2)] = m.group(1)

    anchor = '__typename:"discover_daily",_id:"'
    parts = html.split(anchor)[1:]
    events = {}
    for part in parts:
        m_id = re.match(r"([a-f0-9-]+)", part)
        if not m_id:
            continue
        eid = m_id.group(1)

        # Each "part" runs from this event's own id all the way to the end of
        # the page, not just to its own object - the page repeats the same
        # handful of events many times (list view, map pins, etc.), so an
        # unbounded search can "find" a field that actually belongs to a
        # different, later event. A real event's own fields all sit within
        # the first ~3000 chars of its chunk (verified empirically), so the
        # window is capped there to stop that cross-event bleed.
        local_window = part[:3000]

        def grab(pattern, window=local_window):
            m = re.search(pattern, window, re.DOTALL)
            return m.group(1) if m else None

        fields = {
            "date": grab(r'event_start_date:"([^"]+)"'),
            "start": grab(r'event_start_time_str:"([^"]+)"'),
            "end": grab(r'event_end_time_str:"([^"]+)"'),
            "venue": grab(r'shorthand_name:"([^"]+)"'),
            "venue_slug": grab(r'slug:"([^"]+)",shorthand_name:"[^"]+",latitude:'),
            "full_name": grab(r'__typename:"leagues".*?name:"([^"]+)",display_name:'),
            "type": grab(r'display_name:"([^"]+)"'),
            "count": grab(r'__typename:"registrants_aggregate_fields",count:(\d+)'),
            "max": grab(r'max_registration_size:(\d+)'),
        }
        # Different chunks of the page carry different subsets of fields for
        # the same event; merge them, keeping the first non-null value found.
        if eid not in events:
            events[eid] = fields
        else:
            for k, v in fields.items():
                if events[eid].get(k) is None and v is not None:
                    events[eid][k] = v

    def slug_for(name):
        """Resolve a venue name to its slug. Exact match first; falling back
        to prefix matching, since the name extracted from an event's full
        session title is often a longer variant of the venue's own shorthand
        name (e.g. "St. Patrick's Youth Center Lower East Side" vs. the
        known "St. Patrick's Youth Center")."""
        if name in venue_slug_by_name:
            return venue_slug_by_name[name]
        for known_name, slug in venue_slug_by_name.items():
            if name.startswith(known_name):
                return slug
        return None

    for eid, e in events.items():
        if not e.get("venue"):
            e["venue"] = extract_venue_from_name(e.get("full_name"))
        if not e.get("venue_slug") and e.get("venue"):
            e["venue_slug"] = slug_for(e["venue"])

    # Keep every event citywide that resolved to a real date. Venue is used
    # only to decide priority star/topic/loudness below - it's never a
    # reason to drop a game.
    return {eid: e for eid, e in events.items() if e.get("date")}


def local_date(iso_str):
    """Convert a UTC event_start_date to the correct US/Eastern calendar
    date. Naively slicing the date off the raw UTC string is wrong for any
    game starting at 8pm ET or later, since its UTC timestamp has already
    rolled into the next calendar day."""
    dt = datetime.fromisoformat(iso_str)
    return dt.astimezone(EASTERN).date()


def format_time(t):
    return datetime.strptime(t, "%H:%M").strftime("%-I:%M%p")


def format_duration(start_str, end_str):
    fmt = "%H:%M"
    start = datetime.strptime(start_str, fmt)
    end = datetime.strptime(end_str, fmt)
    if end <= start:
        end += timedelta(days=1)
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    if hours == int(hours):
        h = int(hours)
        return f"{h} hr" if h == 1 else f"{h} hrs"
    return f"{hours:g} hrs"


def describe(e):
    d = local_date(e["date"])
    when = f"{WEEKDAY_ABBR[d.weekday()]} {d.strftime('%m/%d')}"
    time_str = format_time(e["start"])
    duration = format_duration(e["start"], e["end"])
    venue = e.get("venue") or "Unknown venue"
    return f"{venue} | {when} | {time_str} | {duration} | {e['count']}/{e['max']}"


def booking_link(eid):
    return f"https://www.volosports.com/d/{eid}"


def is_priority(e):
    return e.get("venue_slug") in PRIORITY_VENUES


def notify(title, message, click_url=None):
    # Headers must be Latin-1, which breaks on emoji titles - use ntfy's JSON
    # publish endpoint instead, which handles full UTF-8 in the body.
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if click_url:
        payload["click"] = click_url
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(current, seen):
    with open(STATE_FILE, "w") as f:
        json.dump({"current": current, "seen": seen}, f, indent=2, sort_keys=True)


def main():
    current = fetch_events(URL)
    print(f"Fetched {len(current)} current listing(s) citywide.")

    state = load_state()
    previous_current = state.get("current", {}) if state else None
    seen = state.get("seen", {}) if state else {}

    if previous_current is None:
        print(f"First run - saving baseline of {len(current)} listing(s), no alert sent.")
    else:
        new_ids = set(current) - set(previous_current)
        gone_ids = set(previous_current) - set(current)

        for eid in new_ids:
            e = current[eid]
            msg = describe(e)
            star = "⭐" if is_priority(e) else ""  # ⭐ marks a priority venue, nothing else differs
            kind = "\U0001F513 Spot Opened" if eid in seen else "\U0001F195 New Game"  # 🔓 / 🆕
            title = f"{star}{kind}"
            print(f"{title}: {msg}")
            notify(title, msg, click_url=booking_link(eid))

        for eid in gone_ids:
            # Filled up (or removed) - no notification, nothing to book.
            print(f"FILLED (no alert): {describe(previous_current[eid])}")

        if not new_ids:
            print("No new listings or reopened spots.")

    # Track every id ever seen (by its game date) so a game that disappears
    # and later reappears is recognized as "Spot Opened" rather than "New".
    for eid, e in current.items():
        seen[eid] = local_date(e["date"]).isoformat()

    # Prune ids whose game date has already passed - they can't come back,
    # so there's no reason to keep tracking them forever.
    today_str = datetime.now(EASTERN).date().isoformat()
    seen = {eid: d for eid, d in seen.items() if d >= today_str}

    save_state(current, seen)


if __name__ == "__main__":
    main()
