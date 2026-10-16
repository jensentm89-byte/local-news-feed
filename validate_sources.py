#!/usr/bin/env python3
"""Check that every active source in metros/*.yaml still works.

For each active feed: robots.txt allows it, it downloads and parses, it has
entries, and the newest entry is recent. Prints one line per source and exits
non-zero if any source fails. Use it after adding a metro or when a feed seems dead.

    python validate_sources.py                 # all metros
    python validate_sources.py jacksonville    # one metro
    python validate_sources.py --stale-days 14 # tighten the freshness check
"""

import argparse
import calendar
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser

import fetch


def check_feed(feed_url, stale_days):
    """Return (ok, message) for one feed URL."""
    try:
        if not fetch.allowed_by_robots(feed_url):
            return False, "robots.txt disallows"
        parsed = feedparser.parse(fetch.http_get(feed_url))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not parsed.entries:
        return False, "no entries (not a feed?)"
    times = [calendar.timegm(e.published_parsed) for e in parsed.entries if getattr(e, "published_parsed", None)]
    if not times:
        return True, f"{len(parsed.entries)} entries (no dates)"
    age_days = (datetime.now(timezone.utc).timestamp() - max(times)) / 86400
    if age_days > stale_days:
        return False, f"stale: newest entry is {age_days:.0f} days old"
    return True, f"{len(parsed.entries)} entries, newest {age_days:.1f} days old"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("metros", nargs="*", help="metro ids to check (default: all)")
    parser.add_argument("--stale-days", type=float, default=30)
    args = parser.parse_args()

    jobs = []
    for metro in fetch.load_metros():
        if args.metros and metro["id"] not in args.metros:
            continue
        for source in metro["sources"]:
            if source.get("status") == "active":
                jobs.append((metro["id"], source))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: check_feed(j[1]["feed_url"], args.stale_days), jobs))

    failures = 0
    for (metro_id, source), (ok, message) in zip(jobs, results):
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {metro_id:<14} {source['name']:<40} {message}")
    print(f"\n{len(jobs) - failures} ok, {failures} failing")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
