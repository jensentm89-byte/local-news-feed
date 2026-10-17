#!/usr/bin/env bash
# One-time cleanup: remove the "excerpt" field from every commit in this repo's history.
#
# The site used to store ~300-character story excerpts in data/ and docs/data/. They are
# gone from the current files, but older commits still contain them. This rewrites history
# so they are gone everywhere, then force-pushes main.
#
# It REWRITES HISTORY: every commit gets a new ID. A backup bundle is written first
# (restore with: git clone <bundle> restored). Anyone else with a clone must re-clone.
# Even after this, GitHub may keep the old commits reachable by their old IDs for a while
# (cached views, forks); ask GitHub Support to run garbage collection to drop them.
#
#   bash tools/scrub_excerpt_history.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is not clean; commit or stash first." >&2
  exit 1
fi
git fetch origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "Local main and origin/main differ; pull/push first so nothing is lost." >&2
  exit 1
fi

BACKUP="../local-news-feed-before-scrub.bundle"
git bundle create "$BACKUP" --all
echo "Backup written to $BACKUP"

SCRIPT="$(mktemp)"
cat > "$SCRIPT" <<'PY'
import json, os

def scrub_jsonl(path):
    out = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            out.append(line.rstrip("\n"))
            continue
        d.pop("excerpt", None)
        out.append(json.dumps(d, ensure_ascii=False))
    open(path, "w", encoding="utf-8").write("\n".join(out) + ("\n" if out else ""))

def scrub_json(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return
    changed = False
    if isinstance(d, dict) and isinstance(d.get("articles"), list):
        for a in d["articles"]:
            if isinstance(a, dict) and "excerpt" in a:
                a.pop("excerpt")
                changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")

for root in ("data", "docs"):
    for dp, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dp, fn)
            if fn.endswith(".jsonl"):
                scrub_jsonl(p)
            elif fn.endswith(".json"):
                scrub_json(p)
PY

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --tree-filter "python3 $SCRIPT" -- main
rm -f "$SCRIPT"

# Verify nothing is left in any commit.
LEFT=$(git rev-list main | xargs -I{} git grep -l '"excerpt"' {} -- data docs 2>/dev/null | wc -l | tr -d ' ')
echo "Commits/files still containing excerpts: $LEFT"
[ "$LEFT" = "0" ] || { echo "Scrub incomplete; not pushing." >&2; exit 1; }

git push --force-with-lease origin main

# Drop the old objects locally.
rm -rf .git/refs/original
git reflog expire --expire=now --all
git gc --prune=now --aggressive
echo "Done."
