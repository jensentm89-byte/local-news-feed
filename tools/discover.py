#!/usr/bin/env python3
"""Find and vet feeds for a metro's candidate outlets, and draft its metro file.

You supply a candidates file (YAML); this tool does the tedious part:

  * tries the feed URL you gave, then auto-discovers one (homepage <link rel="alternate">,
    common paths, and the TownNews/BLOX "search/?f=rss" pattern with a local section),
  * checks robots.txt, that the feed parses, has several entries, and is recent,
  * flags duplicate feeds (same URL or mostly the same stories as another candidate),
  * measures how much of each feed points elsewhere (other states, national topics) and
    suggests `filter: local` for general feeds,
  * for outlets with no feed, checks the homepage for recent-date markers to see whether
    the outlet is still publishing (otherwise: "unconfirmed" - confirm by hand),
  * writes a review table and, with --write, the metro file (new, or appended to).

Candidates file format:

    metro: boston                      # id from metros/top50.json (or an existing metro file)
    areas: [Boston, North Shore]       # filter buttons; each outlet needs one of them
    aliases: [Cambridge, Quincy]       # extra search words (cities, nicknames)
    local_terms: [Red Sox, Celtics]    # extra words that mark a story as local
    outlets:
      - {name: Boston Globe, area: Boston, homepage: https://www.bostonglobe.com/}
      - {name: WBUR, area: Boston, homepage: https://www.wbur.org/, feed_url: https://www.wbur.org/feed}
      - {name: Mass Live, area: Boston, homepage: https://www.masslive.com/, statewide: true}  # skipped

Usage:
    python tools/discover.py candidates/boston.yaml            # report only
    python tools/discover.py candidates/boston.yaml --write    # also write metros/boston.yaml
"""

import argparse
import calendar
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch  # noqa: E402

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
LINK_TAG_RE = re.compile(r"<link\s[^>]*>", re.I)
COMMON_PATHS = ["/feed/", "/feed", "/rss", "/rss.xml", "/index.rss", "/?rss=y"]
PLATFORM_CATEGORIES = ["news/local", "local", "news"]
PLATFORM_URL = "{base}/search/?f=rss&t=article&c={cat}&l=50&s=start_time&sd=desc"
RECENT_DATE_PATTERNS = [
    r'datetime=["\']?{y}-{m}-', r"/{y}/{m}/", r'datePublished["\']?\s*:\s*["\']{y}-{m}',
    r"{y}-{m}-\d\d",
]
MAX_FRESH_DAYS = 30
MIN_ENTRIES = 3
LOCAL_SHARE_FOR_PLATFORM = 0.3  # a platform feed must have at least this share of local-term hits
ELSEWHERE_SHARE_FOR_FILTER = 0.15  # suggest filter: local above this share of off-topic stories


def check_feed(url, regexes):
    """Return a dict describing the feed at url, or {"ok": False, "reason": ...}."""
    try:
        if not fetch.allowed_by_robots(url):
            return {"ok": False, "reason": "robots.txt disallows"}
        parsed = feedparser.parse(fetch.http_get(url))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:60]}"}
    entries = parsed.entries
    if len(entries) < MIN_ENTRIES:
        return {"ok": False, "reason": f"only {len(entries)} entries"}
    stamps = [calendar.timegm(e.published_parsed) for e in entries if getattr(e, "published_parsed", None)]
    if not stamps:
        return {"ok": False, "reason": "no dates"}
    age = (time.time() - max(stamps)) / 86400
    if age < -1 or age > MAX_FRESH_DAYS:
        return {"ok": False, "reason": f"newest entry {age:.0f} days old" if age > 0 else "dates in the future"}
    local_re, elsewhere_re = regexes
    texts = [f"{e.get('title', '')} {e.get('summary', '')}" for e in entries]
    local_hits = sum(bool(local_re.search(t)) for t in texts)
    elsewhere = sum(bool(elsewhere_re.search(t)) and not local_re.search(t) for t in texts if elsewhere_re)
    return {
        "ok": True, "url": url, "n": len(entries), "age_days": round(age, 1),
        "local_share": local_hits / len(entries), "elsewhere_share": elsewhere / len(entries),
        "links": {e.get("link") for e in entries if e.get("link")},
    }


def autodiscover_urls(homepage):
    urls = []
    try:
        if fetch.allowed_by_robots(homepage):
            page = fetch.http_get(homepage, 1_500_000).decode("utf-8", "replace")
            for tag in LINK_TAG_RE.findall(page):
                if re.search(r"alternate", tag, re.I) and re.search(r"(rss|atom)\+xml", tag, re.I):
                    m = re.search(r'href=["\']?([^"\' >]+)', tag)
                    if m and "comment" not in m.group(1).lower():
                        urls.append(urljoin(homepage, m.group(1).replace("&amp;", "&")))
    except Exception:  # noqa: BLE001
        pass
    root = f"{urlparse(homepage).scheme}://{urlparse(homepage).netloc}"
    urls += [root + p for p in COMMON_PATHS]
    return list(dict.fromkeys(urls))[:8]


def activity_check(homepage):
    """'active' if the homepage shows dates from the last ~6 weeks, else 'unconfirmed' + reason."""
    try:
        if not fetch.allowed_by_robots(homepage):
            return "unconfirmed", "robots.txt blocks our check"
        page = fetch.http_get(homepage, 2_000_000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "unconfirmed", f"{type(exc).__name__}: {str(exc)[:50]}"
    now = time.gmtime()
    prev_month = (now.tm_year, now.tm_mon - 1) if now.tm_mon > 1 else (now.tm_year - 1, 12)
    hits = 0
    for y, m in ((now.tm_year, now.tm_mon), prev_month):
        for pat in RECENT_DATE_PATTERNS:
            hits += len(re.findall(pat.format(y=y, m=f"{m:02d}"), page))
    return ("active", f"{hits} recent-date markers") if hits else ("unconfirmed", "no recent-date markers")


def probe_outlet(outlet, regexes):
    """Find the best feed for one outlet. Returns a result dict."""
    result = {"name": outlet["name"], "area": outlet["area"], "homepage": outlet["homepage"]}
    notes = []
    tried = []
    if outlet.get("feed_url"):
        tried.append(("given", outlet["feed_url"]))
    tried += [("discovered", u) for u in autodiscover_urls(outlet["homepage"]) if u != outlet.get("feed_url")]
    root = f"{urlparse(outlet['homepage']).scheme}://{urlparse(outlet['homepage']).netloc}"
    for kind, url in tried:
        info = check_feed(url, regexes)
        if info["ok"]:
            result.update(feed_url=url, feed=info, how=kind)
            break
        if info["reason"] == "robots.txt disallows" and kind == "given":
            notes.append("feed disallowed by robots.txt")
    if "feed" not in result:
        for cat in PLATFORM_CATEGORIES:  # TownNews/BLOX sites share this search-feed pattern
            url = PLATFORM_URL.format(base=root, cat=cat)
            info = check_feed(url, regexes)
            if info["ok"] and info["local_share"] >= LOCAL_SHARE_FOR_PLATFORM:
                result.update(feed_url=url, feed=info, how=f"platform:{cat}")
                break
            if not info["ok"] and "robots" in info["reason"]:
                break
    if "feed" in result:
        feed = result["feed"]
        result["status"] = "active"
        if feed["elsewhere_share"] >= ELSEWHERE_SHARE_FOR_FILTER or result["how"].startswith("platform"):
            result["filter"] = True
        if feed["local_share"] < 0.1 and not outlet.get("hyperlocal"):
            notes.append(f"few local terms ({feed['local_share']:.0%}) - check it is local")
    else:
        status, detail = activity_check(outlet["homepage"])
        result["status"] = "link_only" if status == "active" else "unconfirmed"
        notes.append(detail)
    result["notes"] = notes
    return result


def mark_duplicates(results):
    """A feed is a duplicate if another outlet has the same URL or mostly the same stories."""
    seen = []
    for r in results:
        if "feed" not in r:
            continue
        for other in seen:
            same_url = r["feed_url"] == other["feed_url"]
            overlap = len(r["feed"]["links"] & other["feed"]["links"]) / max(1, min(r["feed"]["n"], other["feed"]["n"]))
            if same_url or overlap >= 0.6:
                r["status"] = "duplicate"
                r["notes"].append(f"same stories as {other['name']}")
                break
        else:
            seen.append(r)


def metro_context(cfg):
    """Metro dict (like fetch.load_metros produces) for filtering, from metro file or top50.json."""
    path = ROOT / "metros" / f"{cfg['metro']}.yaml"
    if path.exists():
        metro = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        top = {m["id"]: m for m in json.loads((ROOT / "metros" / "top50.json").read_text())["metros"]}
        ref = top[cfg["metro"]]
        counties = [c.rsplit(", ", 1)[0].replace(" County", "").replace(" Parish", "") for c in ref["counties"]]
        metro = {
            "name": ref["name"],
            "aliases": counties,
            "states": [STATE_NAMES[s] for s in ref["states"]],
        }
    metro["id"] = cfg["metro"]
    metro["areas"] = cfg.get("areas", metro.get("areas", []))
    metro["aliases"] = list(dict.fromkeys(metro.get("aliases", []) + cfg.get("aliases", [])))
    metro["local_terms"] = list(dict.fromkeys(metro.get("local_terms", []) + cfg.get("local_terms", [])))
    return metro


def render_yaml(metro, results):
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731 - JSON strings are valid YAML
    lines = [
        f"# {metro['name']}. See jacksonville.yaml for the file format.", "",
        f"name: {q(metro['name'])}", "aliases:", *[f"  - {q(a)}" for a in metro["aliases"]], "",
        f"areas: {q(metro['areas'])}", f"states: {q(metro['states'])}",
        f"local_terms: {q(metro['local_terms'])}", "", "sources:",
    ]
    for r in results:
        if r["status"] == "active":
            lines += [f"  - name: {q(r['name'])}", f"    area: {q(r['area'])}", f"    feed_url: {q(r['feed_url'])}",
                      "    status: active"]
            if r.get("filter"):
                lines.append("    filter: local")
        elif r["status"] == "link_only":
            lines += [f"  - name: {q(r['name'])}", f"    area: {q(r['area'])}", "    status: link_only",
                      f"    homepage: {q(r['homepage'])}",
                      '    note: "No usable open feed (not found, or blocked by robots.txt / the site), '
                      'so only the outlet name and link are shown."']
        else:
            continue
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--write", action="store_true", help="write metros/<id>.yaml from the vetted outlets")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    cfg = yaml.safe_load(args.candidates.read_text(encoding="utf-8"))
    metro = metro_context(cfg)
    regexes = fetch.local_filter_regexes(metro)
    outlets = [o for o in cfg["outlets"] if not o.get("statewide")]
    skipped = [o["name"] for o in cfg["outlets"] if o.get("statewide")]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda o: probe_outlet(o, regexes), outlets))
    mark_duplicates(results)

    order = {"active": 0, "link_only": 1, "duplicate": 2, "unconfirmed": 3}
    for r in sorted(results, key=lambda r: (order[r["status"]], r["area"], r["name"])):
        extra = ""
        if r["status"] == "active":
            f = r["feed"]
            extra = f"{f['n']} entries, newest {f['age_days']}d, local {f['local_share']:.0%}, {r['how']}"
            if r.get("filter"):
                extra += ", filter: local"
        print(f"{r['status']:<11} {r['area'][:16]:<16} {r['name'][:38]:<38} {extra} {'; '.join(r['notes'])}")
    if skipped:
        print("skipped (statewide):", ", ".join(skipped))

    if args.write:
        path = ROOT / "metros" / f"{cfg['metro']}.yaml"
        if path.exists():
            print(f"{path.relative_to(ROOT)} already exists; not overwriting. Merge by hand.")
        else:
            path.write_text(render_yaml(metro, results), encoding="utf-8")
            print(f"Wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
