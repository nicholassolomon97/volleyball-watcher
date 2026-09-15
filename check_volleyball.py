import json
import os
import re
import urllib.request

URL = os.environ["TARGET_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "volleyball_state.json")


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
        window = part[:4000]

        def grab(pattern):
            m = re.search(pattern, window)
            return m.group(1) if m else None

        date = grab(r'event_start_date:"([^"]+)"')
        if not date:
            continue  # duplicate escaped-JSON copy on the page; skip it
        events[eid] = {
            "date": date,
            "start": grab(r'event_start_time_str:"([^"]+)"'),
            "end": grab(r'event_end_time_str:"([^"]+)"'),
            "venue": grab(r'shorthand_name:"([^"]+)"'),
            "type": grab(r'display_name:"([^"]+)"'),
            "count": grab(r'__typename:"registrants_aggregate_fields",count:(\d+)'),
            "max": grab(r'max_registration_size:(\d+)'),
        }
    return events


def describe(e):
    return f"{e['date'][:10]} {e['start']}-{e['end']} @ {e['venue']} ({e['count']}/{e['max']})"


def notify(message):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": "Volleyball pickup update"},
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

        lines = [f"NEW: {describe(current[eid])}" for eid in new_ids]
        lines += [f"GONE: {describe(previous[eid])}" for eid in gone_ids]

        if lines:
            message = "\n".join(lines[:10])
            print("Change detected:\n" + message)
            notify(message)
        else:
            print("No new or removed listings.")

    if current != previous:
        with open(STATE_FILE, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
