# Local News Feed

A Drudge-style headline aggregator for local news. Pick your metro area and see
the latest headlines from its local outlets, linking out to the original stories.
Only headlines, short excerpts and links are kept; the stories stay on the outlets' sites.

**Status:** Phase 1 — structure + Jacksonville, FL (Duval, Clay, St. Johns, Flagler).

## How it works

- `metros/<metro>.yaml` — one file per metro: name, search aliases, areas and its news sources.
- `fetch.py` — reads every metro file, pulls each source's RSS feed, and writes
  `data/articles/<metro>.jsonl` (full history) and `docs/data/<metro>.json` (last 14 days),
  plus `docs/metros.json` (the picker/search index).
- `docs/` — the static site (GitHub Pages serves this folder).
- `.github/workflows/fetch.yml` — runs `fetch.py` twice a day (6 AM and 3 PM Eastern) and commits the results.

## Source rules

- Feeds and homepages are only requested if the site's `robots.txt` allows it.
  The collector identifies itself honestly and makes a few requests per day.
- Outlets that block automated access are `status: link_only`: the page just links to them
  (with their top headline when their homepage allows us to read it).
- A broken feed is logged and skipped.

## Adding a metro

1. Copy `metros/jacksonville.yaml` to `metros/<new-metro>.yaml` and edit it.
2. `python fetch.py`
3. Open `docs/index.html` through a local server (`python3 -m http.server --directory docs`).

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python fetch.py
```
