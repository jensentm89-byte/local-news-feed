#!/usr/bin/env python3
"""Collector for the local news feed.

For every metro in metros/*.yaml:
  * fetches each active source's RSS/Atom feed (skipping a source that was
    fetched within the last MIN_HOURS_BETWEEN_FETCHES hours),
  * for link_only sources, reads the public homepage once to find a top headline,
  * appends new articles to data/articles/<metro>.jsonl and prunes that history to
    the last RETENTION_DAYS days,
  * writes docs/data/<metro>.json (last 14 days) for the page to load,
  * writes docs/metros.json, the index the page uses for its picker and search,
  * records each source's health (last success, consecutive failures) in data/state.json.

Sources are fetched by a small thread pool, with a minimum gap between requests to the
same site. Run `python fetch.py --metros boston,denver` to limit the run.

Everything is checked against the site's robots.txt first. A broken or blocked
source is logged and skipped; it never stops the run.
"""

import argparse
import calendar
import html
import json
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import yaml

ROOT = Path(__file__).parent
METROS_DIR = ROOT / "metros"
ARTICLES_DIR = ROOT / "data" / "articles"
STATE_FILE = ROOT / "data" / "state.json"
LOG_FILE = ROOT / "logs" / "fetch_log.txt"
DOCS_DIR = ROOT / "docs"
OUTPUT_DIR = DOCS_DIR / "data"
METRO_INDEX_FILE = DOCS_DIR / "metros.json"

USER_AGENT = (
    "LocalNewsFeedBot/0.1 (+https://github.com/jensentm89-byte/local-news-feed; "
    "personal local-news headline reader; a few requests per day)"
)
REQUEST_TIMEOUT = 15
EXCERPT_MAX_CHARS = 300
DAYS_TO_KEEP_ON_PAGE = 14
RETENTION_DAYS = 30
MAX_WORKERS = 8
MIN_SECONDS_PER_HOST = 2.0
MIN_HOURS_BETWEEN_FETCHES = 6
MAX_HOMEPAGE_BYTES = 2_000_000
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

HTML_TAG_RE = re.compile(r"<[^>]+>")
_WORD_CHARS = r"[A-Za-z0-9]"
WHITESPACE_RE = re.compile(r"\s+")
ANCHOR_RE = re.compile(r"<a\s[^>]*?href=[\"']?([^\"' >]+)[^>]*>(.*?)</a>", re.S | re.I)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)

_robots_cache = {}
_pace_lock = threading.Lock()
_next_request_at = {}


def now_utc():
    return datetime.now(timezone.utc)


def log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"{now_utc().strftime('%Y-%m-%d %H:%M:%SZ')} {message}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_metros():
    metros = []
    for path in sorted(METROS_DIR.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            metro = yaml.safe_load(f)
        metro["id"] = path.stem
        metros.append(metro)
    return metros


# ---------------------------------------------------------------- HTTP / robots


def pace(url):
    """Block until at least MIN_SECONDS_PER_HOST has passed since the last request to this host."""
    host = urlparse(url).netloc
    with _pace_lock:
        now = time.monotonic()
        start = max(now, _next_request_at.get(host, 0))
        _next_request_at[host] = start + MIN_SECONDS_PER_HOST
    if start > now:
        time.sleep(start - now)


def http_get(url, max_bytes=None):
    """GET with a polite retry: a 429/503 is retried after the server's Retry-After (max 30s)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        pace(url)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read(max_bytes) if max_bytes else response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 2:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            time.sleep(min(int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1), 30))


def allowed_by_robots(url):
    """True only if the site's robots.txt lets our user agent fetch this URL."""
    parts = urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    parser = _robots_cache.get(origin)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(http_get(origin + "/robots.txt").decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                parser.disallow_all = True
            else:  # 404 etc: no robots.txt means no restrictions
                parser.allow_all = True
        except (urllib.error.URLError, TimeoutError, OSError):
            parser.disallow_all = True  # can't confirm permission, so don't fetch
        _robots_cache[origin] = parser
    return parser.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------- text helpers


def to_text(raw_html):
    if not raw_html:
        return ""
    text = html.unescape(HTML_TAG_RE.sub(" ", raw_html))
    return WHITESPACE_RE.sub(" ", text).strip()


def clean_excerpt(raw_html):
    text = to_text(raw_html)
    if len(text) > EXCERPT_MAX_CHARS:
        text = text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return text


def entry_published_iso(entry):
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).strftime(TIME_FORMAT)
    return now_utc().strftime(TIME_FORMAT)


# ---------------------------------------------------------------- feed sources


def load_existing_links(path):
    links = set()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    links.add(json.loads(line)["link"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return links


def process_feed(source, existing_links):
    """Return new article dicts from a feed. Does not modify existing_links."""
    feed_url = source["feed_url"]
    if not allowed_by_robots(feed_url):
        raise PermissionError("robots.txt disallows this feed")

    keywords = source.get("county_filter_keywords")
    parsed = feedparser.parse(http_get(feed_url))
    fetched_at = now_utc().strftime(TIME_FORMAT)

    cutoff = (now_utc() - timedelta(days=RETENTION_DAYS)).strftime(TIME_FORMAT)
    new_items = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        headline = to_text(getattr(entry, "title", None))
        if not link or not headline or link in existing_links:
            continue
        if entry_published_iso(entry) < cutoff:
            continue
        if keywords and not any(k.lower() in link.lower() or k.lower() in headline.lower() for k in keywords):
            continue
        new_items.append(
            {
                "headline": headline,
                "link": link,
                "published": entry_published_iso(entry),
                "source": source["name"],
                "area": source["area"],
                "excerpt": clean_excerpt(getattr(entry, "summary", "") or ""),
                "fetched_at": fetched_at,
            }
        )
    return new_items


# ---------------------------------------------------------------- link-only homepages


def find_homepage_headline(page_html, base_url, spec):
    """Return (headline, url) for the top story on a homepage, or (None, None)."""
    if not spec:
        return None, None
    if spec.get("h1"):
        for m in H1_RE.finditer(page_html):
            anchor = ANCHOR_RE.search(m.group(1))
            if anchor and len(to_text(anchor.group(2))) >= 15:
                return to_text(anchor.group(2)), urljoin(base_url, html.unescape(anchor.group(1)))
        return None, None

    include = re.compile(spec["link_pattern"]) if spec.get("link_pattern") else None
    exclude = re.compile(spec["exclude_pattern"]) if spec.get("exclude_pattern") else None
    for m in ANCHOR_RE.finditer(page_html):
        href = html.unescape(m.group(1))
        path = urlparse(urljoin(base_url, href)).path
        if include and not include.search(path):
            continue
        if exclude and exclude.search(path):
            continue
        text = to_text(m.group(2))
        if len(text) >= 25:
            return text, urljoin(base_url, href)
    return None, None


def process_homepage(source):
    homepage = source["homepage"]
    entry = {"name": source["name"], "area": source["area"], "homepage": homepage, "headline": None, "url": homepage}
    if not source.get("headline") or not allowed_by_robots(homepage):
        return entry
    page = http_get(homepage, MAX_HOMEPAGE_BYTES).decode("utf-8", "replace")
    headline, url = find_homepage_headline(page, homepage, source["headline"])
    if headline:
        entry["headline"], entry["url"] = headline, url
    return entry


# ---------------------------------------------------------------- local-relevance filter
#
# Applied only to sources marked `filter: local` (TV/radio stations and other general
# feeds that mix in national and world stories). An article is KEPT when:
#   1. it mentions a local term for the metro (name, aliases, areas, local_terms), or
#   2. it mentions nothing that clearly points elsewhere (another state, another big
#      city, a foreign country, or a national-only topic like the White House).
# It is dropped only when it points elsewhere and has no local term. Unsure -> keep.

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "West Virginia", "Wisconsin", "Wyoming",
]
OTHER_CITIES = [
    "New York City", "Los Angeles", "Chicago", "Houston", "Dallas", "Phoenix", "Philadelphia",
    "San Antonio", "San Diego", "San Francisco", "Seattle", "Portland", "Denver", "Boston",
    "Detroit", "Minneapolis", "St. Louis", "Baltimore", "Nashville", "Memphis", "New Orleans",
    "Las Vegas", "Cleveland", "Pittsburgh", "Cincinnati", "Kansas City",
    "Milwaukee", "Oakland", "Sacramento", "Tampa", "Orlando", "Louisville", "Oklahoma City",
    "Atlanta", "Miami", "Jacksonville", "Salt Lake City", "Indianapolis", 
    "Albuquerque", "Tucson", "Honolulu", "Anchorage", "Charleston", 
]
FOREIGN = [
    "Canada", "Canadian", "Israel", "Israeli", "Gaza", "West Bank", "Ukraine", "Ukrainian", "Russia",
    "Russian", "China", "Iran", "Iranian", "Egypt", "Egyptian", "India",
    "Britain", "U.K.", "England", "London", "Scotland", "Ireland", 
    "France", "Italy", "Germany", "Spain", 
    "Japan", "Korea", "Pakistan", "Afghanistan", "Yemen", "Houthi", "Saudi",
    "Syria", "Iraq", "Venezuela", "Cuba", "Haiti", "Costa Rica", "Costa Rican",
    "Brazil", "Argentina", "Colombia", "Philippines", "Australia", 
    "New Zealand", "Africa", "Europe", "Asia", "Middle East",
    "Pope", "Vatican", "Puerto Rico", "Bahamas", 
]
NATIONAL_ONLY = [
    "White House", "Pentagon", "Supreme Court", "NASA", "NOAA", "Wall Street", "Fed rate",
    "Federal Reserve", "Nobel", "Oscars", "Grammy", 
]


def _regex(terms):
    parts = [
        rf"(?<!{_WORD_CHARS}){re.escape(t)}(?!{_WORD_CHARS})"
        for t in sorted(set(terms), key=len, reverse=True)
    ]
    return re.compile("|".join(parts), re.I) if parts else None


def local_filter_regexes(metro):
    """Return (local_regex, elsewhere_regex) for a metro."""
    local = [metro["name"].split(",")[0]]
    local += metro.get("aliases", []) + metro.get("areas", []) + metro.get("local_terms", [])
    local += metro.get("states", [])
    local = [t.strip() for t in local if t and t.strip() and t.strip().lower() != "region-wide"]
    local_lower = {t.lower() for t in local}
    # A term that is local for this metro (e.g. Mexico for Phoenix, Cuba for Miami) must
    # never count as "elsewhere".
    elsewhere = [t for t in US_STATES + OTHER_CITIES + FOREIGN + NATIONAL_ONLY if t.lower() not in local_lower]
    return _regex(local), _regex(elsewhere)


def is_local(article, regexes):
    """Keep unless the story points elsewhere and has no local term."""
    local, elsewhere = regexes
    text = f"{article['headline']} {article.get('excerpt', '')} {urlparse(article['link']).path.replace('-', ' ')}"
    if local and local.search(text):
        return True
    return not (elsewhere and elsewhere.search(text))


# ---------------------------------------------------------------- output


def recent_articles(path, keep=None):
    cutoff = now_utc() - timedelta(days=DAYS_TO_KEEP_ON_PAGE)
    recent = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    article = json.loads(line)
                    published = datetime.strptime(article["published"], TIME_FORMAT).replace(tzinfo=timezone.utc)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if published >= cutoff:
                    article["headline"] = to_text(article["headline"])
                    article["excerpt"] = to_text(article.get("excerpt", ""))
                    if keep is None or keep(article):
                        recent.append(article)
    recent.sort(key=lambda a: a["published"], reverse=True)
    return recent


def is_due(last_iso):
    if not last_iso:
        return True
    last = datetime.strptime(last_iso, TIME_FORMAT).replace(tzinfo=timezone.utc)
    return now_utc() - last >= timedelta(hours=MIN_HOURS_BETWEEN_FETCHES)


def prune_articles(path):
    """Rewrite the history file keeping only the last RETENTION_DAYS days."""
    if not path.exists():
        return
    cutoff = (now_utc() - timedelta(days=RETENTION_DAYS)).strftime(TIME_FORMAT)
    kept, dropped = [], 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                published = json.loads(line)["published"]
            except (json.JSONDecodeError, KeyError):
                dropped += 1
                continue
            if published >= cutoff:
                kept.append(line if line.endswith("\n") else line + "\n")
            else:
                dropped += 1
    if dropped:
        path.write_text("".join(kept), encoding="utf-8")


def fetch_one(task):
    """Worker: fetch one source. Returns (task, result, error)."""
    metro_id, source, existing_links = task
    try:
        if source["status"] == "active":
            return task, process_feed(source, existing_links), None
        return task, process_homepage(source), None
    except Exception as exc:  # noqa: BLE001 - a bad source must never crash the run
        return task, None, f"{type(exc).__name__}: {exc}"


def record_health(state, key, error):
    health = state.setdefault("health", {}).setdefault(key, {"failures": 0})
    if error is None:
        health.update(failures=0, last_success=now_utc().strftime(TIME_FORMAT))
        health.pop("last_error", None)
    else:
        health["failures"] = health.get("failures", 0) + 1
        health["last_error"] = error[:200]
        health["last_failure"] = now_utc().strftime(TIME_FORMAT)


def build_metro_output(metro, state):
    metro_id = metro["id"]
    homepages_state = state.setdefault("homepages", {})
    local_filtered = {s["name"] for s in metro["sources"] if s.get("filter") == "local"}
    local_regex = local_filter_regexes(metro)
    recent = recent_articles(
        ARTICLES_DIR / f"{metro_id}.jsonl",
        keep=lambda a: a["source"] not in local_filtered or is_local(a, local_regex),
    )
    outlets = []
    for source in metro["sources"]:
        if source.get("status") != "link_only":
            continue
        entry = homepages_state.get(f"{metro_id}|{source['name']}", {})
        outlets.append(
            {
                "name": source["name"],
                "area": source["area"],
                "headline": entry.get("headline"),
                "url": entry.get("url") or source["homepage"],
            }
        )
    write_json(
        OUTPUT_DIR / f"{metro_id}.json",
        {
            "generated_at": now_utc().strftime(TIME_FORMAT),
            "name": metro["name"],
            "areas": metro["areas"],
            "articles": recent,
            "link_only": outlets,
        },
    )
    return len(recent)


def main():
    parser = argparse.ArgumentParser(description="Collect local-news headlines for every metro.")
    parser.add_argument("--metros", help="comma-separated metro ids to fetch (default: all)")
    parser.add_argument("--force", action="store_true", help="ignore the per-source fetch throttle")
    args = parser.parse_args()

    all_metros = load_metros()
    wanted = set(args.metros.split(",")) if args.metros else None
    metros = [m for m in all_metros if wanted is None or m["id"] in wanted]
    state = load_state()
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    feeds_state = state.setdefault("feeds", {})
    homepages_state = state.setdefault("homepages", {})

    due = (lambda _last: True) if args.force else is_due

    # 1. Work out what is due.
    existing = {m["id"]: load_existing_links(ARTICLES_DIR / f"{m['id']}.jsonl") for m in metros}
    tasks = []
    for metro in metros:
        for source in metro["sources"]:
            key = f"{metro['id']}|{source['name']}"
            if source.get("status") == "active" and due(feeds_state.get(key)):
                tasks.append((metro["id"], source, existing[metro["id"]]))
            elif source.get("status") == "link_only" and due((homepages_state.get(key) or {}).get("checked_at")):
                tasks.append((metro["id"], source, None))
    log(f"start metros={len(metros)} sources_due={len(tasks)}")

    # 2. Fetch concurrently (paced per host).
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(fetch_one, tasks))

    # 3. Apply results one metro at a time (single thread: no file or set races).
    new_by_metro = {m["id"]: 0 for m in metros}
    for (metro_id, source, _), result, error in results:
        key = f"{metro_id}|{source['name']}"
        record_health(state, key, error)
        if source["status"] == "active":
            if error:
                log(f"error metro={metro_id} source={source['name']!r} error={error}")
                continue
            fresh = []
            for item in result:
                if item["link"] not in existing[metro_id]:
                    existing[metro_id].add(item["link"])
                    fresh.append(item)
            if fresh:
                with (ARTICLES_DIR / f"{metro_id}.jsonl").open("a", encoding="utf-8") as f:
                    for item in fresh:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
            new_by_metro[metro_id] += len(fresh)
            feeds_state[key] = now_utc().strftime(TIME_FORMAT)
            log(f"ok metro={metro_id} source={source['name']!r} new_items={len(fresh)}")
        else:
            if error:
                log(f"error metro={metro_id} source={source['name']!r} homepage error={error}")
                result = {"name": source["name"], "area": source["area"], "homepage": source["homepage"],
                          "headline": None, "url": source["homepage"]}
            result["checked_at"] = now_utc().strftime(TIME_FORMAT)
            homepages_state[key] = result
            log(f"ok metro={metro_id} source={source['name']!r} homepage_headline={bool(result['headline'])}")

    # 4. Prune history, write each metro's page data.
    for metro in metros:
        prune_articles(ARTICLES_DIR / f"{metro['id']}.jsonl")
        shown = build_metro_output(metro, state)
        log(f"metro_complete metro={metro['id']} new_items={new_by_metro[metro['id']]} articles_on_page={shown}")
    write_json(STATE_FILE, state)

    index = [
        {"id": m["id"], "name": m["name"], "aliases": m.get("aliases", []), "areas": m["areas"]}
        for m in sorted(all_metros, key=lambda m: m["name"].lower())
    ]
    write_json(METRO_INDEX_FILE, {"metros": index})
    failing = sum(1 for _, _, e in results if e)
    log(f"run_complete metros={len(metros)} sources_fetched={len(results)} failures={failing}")


if __name__ == "__main__":
    main()
