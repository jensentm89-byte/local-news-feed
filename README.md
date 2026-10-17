# Local News Feed

A Drudge-style headline aggregator for local news. Pick your metro area and see
the latest headlines from its local outlets, linking out to the original stories.
Only headlines, short excerpts and links are kept; the stories stay on the outlets' sites.

**Status:** Phase 2 — Jacksonville plus the 10 largest US metros: New York, Los Angeles, Chicago, Dallas–Fort Worth, Houston, Washington DC, Philadelphia, Atlanta, Miami and Phoenix.

## How it works

- `metros/<metro>.yaml` — one file per metro: name, search aliases, areas and its news sources.
- `fetch.py` — reads every metro file, pulls each source's RSS feed, and writes
  `data/articles/<metro>.jsonl` (full history) and `docs/data/<metro>.json` (last 14 days),
  plus `docs/metros.json` (the picker/search index).
- `validate_sources.py` — checks every active feed (robots.txt, parses, recent). Run it after adding a metro.
- `docs/` — the static site (GitHub Pages serves this folder).
- `.github/workflows/fetch.yml` — runs `fetch.py` twice a day (6 AM and 3 PM Eastern) and commits the results.

## Source rules

- Feeds and homepages are only requested if the site's `robots.txt` allows it.
  The collector identifies itself honestly and makes a few requests per day.
- Outlets that block automated access are `status: link_only`: the page just links to them
  (with their top headline when their homepage allows us to read it).
- A broken feed is logged and skipped. `status: inactive` pauses a source without deleting it.
- General feeds (TV, radio, wire-style) are marked `filter: local`: stories about other states, other cities, foreign countries or national-only topics are hidden unless they also mention a local place. See the comments in `metros/jacksonville.yaml`.
- Statewide outlets and national wire feeds are left out; the goal is metro-level local news.

## Adding a metro

1. Copy `metros/jacksonville.yaml` to `metros/<new-metro>.yaml` and edit it.
2. `python validate_sources.py <new-metro>` and fix or drop failing sources.
3. `python fetch.py`
4. Open `docs/index.html` through a local server (`python3 -m http.server --directory docs`).

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python fetch.py
```

## Ideas for later

- **AI relevance filter.** The `filter: local` rules hide most national and world stories from
  TV/radio feeds, but some generic national stories (consumer news, entertainment) slip through
  because they name no place. A small AI model could classify each new headline as local or not.
  It needs an API key stored as a GitHub secret and costs a little per headline, so revisit it
  when there is revenue to cover it.
- ZIP-code lookup for the metro picker.
- Scale to 100 metros (phase 3).
