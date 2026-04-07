"""
Step 2: Load reviewed municipality_urls.csv into Supabase.

Usage:
    python 2_load_to_supabase.py

Only loads rows where confidence is 'auto' or 'manual' (skips 'skip').
Run after you've reviewed municipality_urls.csv in Excel/Sheets and
corrected any wrong URLs.

Requires the municipalities table to exist — run the SQL in
municipality-registry/supabase_municipalities.sql first.
"""

import csv
import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
INPUT_FILE           = Path(__file__).parent / "municipality_urls.csv"

MUNI_TYPE_MAP = {
    "city":                  "city",
    "town":                  "town",
    "township":              "township",
    "village":               "village",
    "municipality":          "municipality",
    "united counties":       "county",
    "county":                "county",
    "district municipality": "district",
    "regional municipality": "region",
    "separated town":        "town",
    "improvement district":  "other",
}

def normalise_type(raw: str) -> str:
    return MUNI_TYPE_MAP.get(raw.lower().strip(), "other")


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        return

    if not INPUT_FILE.exists():
        print(f"ERROR: {INPUT_FILE} not found. Run 1_search_municipalities.py first.")
        return

    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    to_load  = [r for r in rows if r.get("confidence") in ("auto", "manual")]
    to_skip  = [r for r in rows if r.get("confidence") == "skip"]
    print(f"Rows to load:  {len(to_load)}")
    print(f"Rows skipped:  {len(to_skip)}  (confidence=skip)")

    inserted = 0
    updated  = 0
    errors   = 0

    for row in to_load:
        name = row["name"].strip()
        if not name:
            continue

        record = {
            "name":              name,
            "municipality_type": normalise_type(row.get("municipality_type", "")),
            "region":            row.get("region", "").strip() or None,
            "building_dept_url": row.get("building_dept_url", "").strip() or None,
            "active":            True,
        }

        try:
            # Upsert on name — safe to re-run
            res = sb.table("municipalities").upsert(record, on_conflict="name").execute()
            if res.data:
                updated += 1
                print(f"  [ok] {name}")
            else:
                errors += 1
                print(f"  [err] {name}: empty response")
        except Exception as e:
            errors += 1
            print(f"  [err] {name}: {e}")

    print(f"\nDone. {updated} upserted, {errors} errors.")
    print("Check your Supabase municipalities table.")


if __name__ == "__main__":
    main()
