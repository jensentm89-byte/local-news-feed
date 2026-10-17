#!/usr/bin/env python3
"""Write data/health.md: which sources are failing, silent or missing, per metro.

Offline: reads data/state.json (per-source failure counts recorded by fetch.py) and the
article history, so it is cheap to run after every fetch or on a weekly schedule.

    python tools/health_report.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch  # noqa: E402

OUT = ROOT / "data" / "health.md"
FAILING_AFTER = 3      # consecutive failed runs before a source is listed as failing
SILENT_AFTER_DAYS = 10  # no new story in this long -> listed as silent


def newest_by_source(metro_id):
    newest = {}
    path = fetch.ARTICLES_DIR / f"{metro_id}.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            if a["published"] > newest.get(a["source"], ""):
                newest[a["source"]] = a["published"]
    return newest


def main():
    state = fetch.load_state()
    health = state.get("health", {})
    now = datetime.now(timezone.utc)
    lines = [f"# Source health", "", f"Generated {now.strftime('%Y-%m-%d %H:%MZ')}.", ""]
    totals = {"active": 0, "link_only": 0, "inactive": 0}
    problem_blocks = []

    for metro in fetch.load_metros():
        newest = newest_by_source(metro["id"])
        problems = []
        for source in metro["sources"]:
            status = source.get("status")
            totals[status] = totals.get(status, 0) + 1
            if status != "active":
                continue
            key = f"{metro['id']}|{source['name']}"
            h = health.get(key, {})
            if h.get("failures", 0) >= FAILING_AFTER:
                problems.append(f"- **failing** {source['name']}: {h['failures']} runs in a row ({h.get('last_error', '')})")
                continue
            last = newest.get(source["name"])
            if last is None:
                problems.append(f"- **no stories yet** {source['name']}")
            else:
                days = (now - datetime.strptime(last, fetch.TIME_FORMAT).replace(tzinfo=timezone.utc)).days
                if days >= SILENT_AFTER_DAYS:
                    problems.append(f"- **silent** {source['name']}: newest story {days} days old")
        if problems:
            problem_blocks.append(f"## {metro['name']}\n\n" + "\n".join(problems) + "\n")

    lines += [f"Active feeds: {totals['active']} · link-only: {totals['link_only']} · paused: {totals.get('inactive', 0)}", ""]
    lines += problem_blocks or ["All active sources look healthy.", ""]
    OUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(problem_blocks)} metros with problems")


if __name__ == "__main__":
    main()
