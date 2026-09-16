import json
import os
import re
import urllib.request

URL = os.environ["TARGET_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "volleyball_state.json")


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

    anchor = '__typename:"discover_daily",_id:"'
    parts = html.split(anchor)[1:]
    events = {}
    for part in parts:
        m_id = re.match(r"([a-f0-9-]+)", part)
        if not m_id:
            continue
        eid = m_id.group(1)

        def grab(pattern, window=part):
            m = re.search(pattern, window)
            return m.group(1) if m else None

        fields = {
            "date": grab(r'event_start_date:"([^"]+)"'),
            "start": grab(r'event_start_time_str:"([^"]+)"'),
            "end": grab(r'event_end_time_str:"([^"]+)"'),
            "venue": grab(r'shorthand_name:"([^"]+)"'),
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

    # Only keep entries that resolved to a real event (has a date).
    for eid, e in events.items():
        if not e.get("venue"):
            e["venue"] = extract_venue_from_name(e.get("full_name"))
    return {eid: e for eid, e in events.items() if e.get("date")}


from datetime import date as date_cls, datetime, timedelta


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
        return f"{minutes}min"
    hours = minutes / 60
    if hours == int(hours):
        h = int(hours)
        return f"{h}hr" if h == 1 else f"{h}hrs"
    return f"{hours:g}hrs"


def describe(e):
    d = date_cls.fromisoformat(e['date'][:10])
    when = d.strftime('%a %m/%d')
    time_str = format_time(e['start'])
    duration = format_duration(e['start'], e['end'])
    venue = e.get('venue') or 'Unknown venue'
    return f"{when} | {time_str} | {duration} | {venue} ({e['count']}/{e['max']})"


def booking_link(eid):
    return f"https://www.volosports.com/d/{eid}"


def notify(title, message, click_url=None):
    headers = {"Title": title}
    if click_url:
        headers["X-Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    current = fetch_events(URL)
    print(f"Fetched {len(current)} current listing(s).")

    previous = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            previous = json.load(f)

    if previous is None:
        print(f"First run - saving baseline of {len(current)} listing(s), no alert sent.")
    else:
        new_ids = set(current) - set(previous)
        gone_ids = set(previous) - set(current)

        for eid in new_ids:
            msg = describe(current[eid])
            print(f"NEW: {msg}")
            notify("New pickup volleyball game!", msg, click_url=booking_link(eid))

        for eid in gone_ids:
            msg = describe(previous[eid])
            print(f"GONE: {msg}")
            notify("Volleyball game no longer listed", msg)

        if not new_ids and not gone_ids:
            print("No new or removed listings.")

    if current != previous:
        with open(STATE_FILE, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
