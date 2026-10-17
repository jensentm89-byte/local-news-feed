#!/usr/bin/env python3
"""Build metros/top50.json: the 50 largest US metro areas, from Census data.

Downloads the Census Bureau's metro population estimates (county rows included), ranks
metropolitan statistical areas by the latest estimate, and writes the top 50 with their
states and counties. The output is a reference for creating metro files; it is not read
by fetch.py.

    python tools/metro_list.py
"""

import csv
import io
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/metro/totals/cbsa-est2024-alldata.csv"
OUT = ROOT / "metros" / "top50.json"
YEAR = "POPESTIMATE2024"
TOP_N = 50

# CBSA code -> (our metro id, display name). Metros we already have use their existing ids.
METROS = {
    "35620": ("new-york", "New York, NY"),
    "31080": ("los-angeles", "Los Angeles, CA"),
    "16980": ("chicago", "Chicago, IL"),
    "19100": ("dallas-fort-worth", "Dallas–Fort Worth, TX"),
    "26420": ("houston", "Houston, TX"),
    "33100": ("miami", "Miami, FL"),
    "47900": ("washington-dc", "Washington, DC"),
    "12060": ("atlanta", "Atlanta, GA"),
    "37980": ("philadelphia", "Philadelphia, PA"),
    "38060": ("phoenix", "Phoenix, AZ"),
    "14460": ("boston", "Boston, MA"),
    "40140": ("riverside-san-bernardino", "Riverside–San Bernardino, CA"),
    "41860": ("san-francisco", "San Francisco–Oakland, CA"),
    "19820": ("detroit", "Detroit, MI"),
    "42660": ("seattle", "Seattle, WA"),
    "33460": ("minneapolis-st-paul", "Minneapolis–St. Paul, MN"),
    "45300": ("tampa", "Tampa–St. Petersburg, FL"),
    "41740": ("san-diego", "San Diego, CA"),
    "19740": ("denver", "Denver, CO"),
    "36740": ("orlando", "Orlando, FL"),
    "16740": ("charlotte", "Charlotte, NC"),
    "12580": ("baltimore", "Baltimore, MD"),
    "41180": ("st-louis", "St. Louis, MO"),
    "41700": ("san-antonio", "San Antonio, TX"),
    "12420": ("austin", "Austin, TX"),
    "38900": ("portland", "Portland, OR"),
    "40900": ("sacramento", "Sacramento, CA"),
    "38300": ("pittsburgh", "Pittsburgh, PA"),
    "29820": ("las-vegas", "Las Vegas, NV"),
    "17140": ("cincinnati", "Cincinnati, OH"),
    "28140": ("kansas-city", "Kansas City, MO"),
    "18140": ("columbus", "Columbus, OH"),
    "26900": ("indianapolis", "Indianapolis, IN"),
    "17410": ("cleveland", "Cleveland, OH"),
    "34980": ("nashville", "Nashville, TN"),
    "41940": ("san-jose", "San Jose, CA"),
    "47260": ("virginia-beach-norfolk", "Virginia Beach–Norfolk, VA"),
    "27260": ("jacksonville", "Jacksonville, FL"),
    "39300": ("providence", "Providence, RI"),
    "33340": ("milwaukee", "Milwaukee, WI"),
    "39580": ("raleigh", "Raleigh, NC"),
    "36420": ("oklahoma-city", "Oklahoma City, OK"),
    "31140": ("louisville", "Louisville, KY"),
    "40060": ("richmond", "Richmond, VA"),
    "32820": ("memphis", "Memphis, TN"),
    "41620": ("salt-lake-city", "Salt Lake City, UT"),
    "13820": ("birmingham", "Birmingham, AL"),
    "23420": ("fresno", "Fresno, CA"),
    "24340": ("grand-rapids", "Grand Rapids, MI"),
    "25540": ("hartford", "Hartford, CT"),
}


def main():
    with urllib.request.urlopen(URL, timeout=60) as response:
        text = response.read().decode("latin-1")
    rows = list(csv.DictReader(io.StringIO(text)))
    msas = sorted(
        (r for r in rows if r["LSAD"] == "Metropolitan Statistical Area"),
        key=lambda r: -int(r[YEAR]),
    )[:TOP_N]

    out = []
    for rank, msa in enumerate(msas, 1):
        cbsa = msa["CBSA"]
        if cbsa not in METROS:
            raise SystemExit(f"Rank {rank} {msa['NAME']} (CBSA {cbsa}) is not in METROS; add it.")
        metro_id, display = METROS[cbsa]
        counties = [r["NAME"] for r in rows if r["CBSA"] == cbsa and r["LSAD"].startswith("County")]
        states = sorted({name.rsplit(", ", 1)[1] for name in counties})
        out.append(
            {
                "rank": rank,
                "id": metro_id,
                "name": display,
                "cbsa": cbsa,
                "census_name": msa["NAME"],
                "population": int(msa[YEAR]),
                "states": states,
                "counties": sorted(counties),
            }
        )
    OUT.write_text(json.dumps({"source": URL, "year": YEAR, "metros": out}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    have = {p.stem for p in (ROOT / "metros").glob("*.yaml")}
    todo = [m["id"] for m in out if m["id"] not in have]
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(out)} metros, {len(todo)} still need a metro file.")


if __name__ == "__main__":
    main()
