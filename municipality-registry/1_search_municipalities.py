"""
Step 1: Search Google (via SerpAPI) for each Ontario municipality's building department URL.

Usage:
    pip install requests python-dotenv
    python 1_search_municipalities.py

Reads:  Ontario Open Data municipalities CSV  (downloaded automatically)
Writes: municipality_urls.csv                 (review this before loading to Supabase)

Free tier: 100 searches/month. Paid: $50/month for 5,000 searches (~$4.50 for all 444).
Set BATCH_LIMIT below to control how many you process per run.
"""

import csv
import io
import os
import re
import time
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
SERPAPI_KEY   = os.getenv("SERPAPI_KEY", "")
BATCH_LIMIT   = 100      # How many to process per run. Set to 0 for unlimited.
DELAY_SECONDS = 1.0      # Pause between requests
OUTPUT_FILE   = Path(__file__).parent / "municipality_urls.csv"
ONTARIO_CSV_URL = (
    "https://data.ontario.ca/dataset/62e83cbc-0731-4d66-abdc-2f2b31bcd76c"
    "/resource/6783a586-6b05-4a73-9663-e60a6963c91e/download/municipalities_-_en.csv"
)
# ─────────────────────────────────────────────────────────────────────────────

# ── Priority order: top ~100 Ontario municipalities by 2021 Census population ─
# These are processed FIRST so the free-tier 100/day quota covers the
# municipalities that handle the vast majority of Ontario permit applications.
PRIORITY_ORDER = [
    "Toronto", "Ottawa", "Mississauga", "Brampton", "Hamilton",
    "London", "Markham", "Vaughan", "Kitchener", "Windsor",
    "Burlington", "Oakville", "Oshawa", "Greater Sudbury", "Barrie",
    "St. Catharines", "Cambridge", "Kingston", "Guelph", "Thunder Bay",
    "Waterloo", "Brantford", "Whitby", "Ajax", "Clarington",
    "Pickering", "Niagara Falls", "Newmarket", "Halton Hills", "Georgina",
    "Peterborough", "Sault Ste. Marie", "Kawartha Lakes", "Sarnia", "Norfolk County",
    "Welland", "Belleville", "North Bay", "Cornwall", "Caledon",
    "Richmond Hill", "Aurora", "King", "East Gwillimbury", "Whitchurch-Stouffville",
    "Brockville", "Owen Sound", "Woodstock", "Orillia", "Collingwood",
    "Milton", "Orangeville", "Innisfil", "Bradford West Gwillimbury", "New Tecumseth",
    "Chatham-Kent", "Quinte West", "Prince Edward County", "Timmins", "Stratford",
    "Cobourg", "Port Hope", "Lambton Shores", "Essa", "Springwater",
    "Penetanguishene", "Midland", "Wasaga Beach", "Meaford", "Collingwood",
    "Huntsville", "Bracebridge", "Gravenhurst", "Lake of Bays", "Muskoka Lakes",
    "Haldimand County", "Brant County", "Wellington County", "Grey County", "Simcoe County",
    "Dufferin County", "Northumberland County", "Hastings County", "Lennox and Addington County",
    "Frontenac County", "Leeds and Grenville", "Renfrew County", "Lanark County",
    "Prescott and Russell", "Stormont, Dundas and Glengarry",
    "Huron County", "Perth County", "Oxford County", "Elgin County",
    "Bruce County", "Manitoulin District", "Algoma District", "Cochrane District",
    "Timiskaming District", "Nipissing District", "Parry Sound District",
    "Muskoka District", "Haliburton County", "Hastings Highlands", "Bancroft",
]
# ─────────────────────────────────────────────────────────────────────────────


def parse_muni_cell(cell: str) -> tuple:
    """
    The CSV wraps names in HTML: <a title="NAME" href="URL">NAME</a>
    Returns (clean_name, website_url).
    """
    href  = re.search(r'href=["\']([^"\']+)["\']', cell)
    title = re.search(r'title=["\']([^"\']+)["\']', cell)
    url   = href.group(1).strip()  if href  else ""
    # Name: strip ", Township of" style suffix to get the short name
    raw   = title.group(1).strip() if title else re.sub(r"<[^>]+>", "", cell).strip()
    # Normalise "Muskoka Lakes, Township of" → "Township of Muskoka Lakes"
    if ", " in raw:
        parts = raw.split(", ", 1)
        name  = f"{parts[1]} {parts[0]}"
    else:
        name = raw
    return name, url


def fetch_ontario_municipalities() -> list[dict]:
    """Download the official Ontario Open Data municipalities CSV."""
    print("Downloading Ontario municipalities list...")
    resp = requests.get(ONTARIO_CSV_URL, timeout=30)
    resp.raise_for_status()
    # Strip BOM if present
    text = resp.text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    munis = []
    for row in reader:
        raw_cell = row.get("Municipality", "")
        name, website_url = parse_muni_cell(raw_cell)
        munis.append({
            "name":              name,
            "municipality_type": row.get("Municipal status", "").strip(),
            "region":            row.get("Geographic area", "").strip(),
            "website_url":       website_url,
        })
    print(f"  {len(munis)} municipalities loaded.")
    return munis


def serpapi_search(query: str):
    """Search Google via SerpAPI. Returns the best result or None."""
    params = {
        "api_key": SERPAPI_KEY,
        "engine":  "google",
        "q":       query,
        "gl":      "ca",    # Canada
        "hl":      "en",
        "num":     5,
    }
    resp = requests.get("https://serpapi.com/search", params=params, timeout=20)
    if resp.status_code == 429:
        print("  [rate limit] sleeping 60s...")
        time.sleep(60)
        return None
    if resp.status_code != 200:
        print(f"  [error] {resp.status_code}: {resp.text[:200]}")
        return None
    data = resp.json()

    # Check account credits
    credits = data.get("search_information", {}).get("total_results")
    if "error" in data:
        print(f"  [serpapi error] {data['error']}")
        return None

    results = data.get("organic_results", [])
    if not results:
        return None

    # Skip generic aggregators — prefer the actual municipal website
    skip_domains = {"ontario.ca", "wikipedia.org", "canada.ca", "bing.com", "cloudpermit.com"}
    for r in results:
        link = r.get("link", "")
        domain = link.split("/")[2] if link.startswith("http") else ""
        if not any(skip in domain for skip in skip_domains):
            return {"url": link, "title": r.get("title", ""), "snippet": r.get("snippet", "")}

    # Fall back to first result
    r = results[0]
    return {"url": r.get("link", ""), "title": r.get("title", ""), "snippet": r.get("snippet", "")}


def already_searched(name: str, existing: dict) -> bool:
    return name in existing


def load_existing_results() -> dict:
    """Load already-processed results so we can resume without re-searching."""
    existing = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["name"]] = row
    return existing


def main():
    if not SERPAPI_KEY:
        print("ERROR: Set SERPAPI_KEY in your .env file first.")
        print("  SERPAPI_KEY=your_key_from_serpapi.com")
        return

    municipalities = fetch_ontario_municipalities()
    existing       = load_existing_results()

    # Sort: priority municipalities first (in population order), then the rest alphabetically
    priority_index = {name: i for i, name in enumerate(PRIORITY_ORDER)}

    def sort_key(m):
        name = m.get("Municipality Name", m.get("name", "")).strip()
        return (priority_index.get(name, len(PRIORITY_ORDER)), name)

    municipalities.sort(key=sort_key)
    print(f"Processing order: priority municipalities first, then alphabetical.")
    print(f"  First: {municipalities[0].get('Municipality Name', '')}")
    print(f"  Second: {municipalities[1].get('Municipality Name', '')}")
    print()

    # Write header if new file
    write_header = not OUTPUT_FILE.exists()
    out_f = open(OUTPUT_FILE, "a", newline="", encoding="utf-8")
    fieldnames = [
        "name", "municipality_type", "region", "website_url",
        "building_dept_url", "page_title", "snippet",
        "confidence",   # auto | manual | skip
        "notes",        # human reviewer fills this in
        "searched_at",
    ]
    writer = csv.DictWriter(out_f, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    processed = 0
    skipped   = 0

    for muni in municipalities:
        name    = muni.get("name", "").strip()
        mtype   = muni.get("municipality_type", "").strip()
        region  = muni.get("region", "").strip()
        website = muni.get("website_url", "").strip()

        if not name:
            continue

        if already_searched(name, existing):
            skipped += 1
            continue

        if BATCH_LIMIT and processed >= BATCH_LIMIT:
            print(f"\nBatch limit of {BATCH_LIMIT} reached. Run again to continue.")
            break

        # Targeted query — "building permits" + municipality name + Ontario
        query = f'{name} Ontario building permits department'

        print(f"[{processed+1}] {name} ({mtype}) — searching...")
        result = serpapi_search(query)
        time.sleep(DELAY_SECONDS)

        if result:
            row = {
                "name":              name,
                "municipality_type": mtype,
                "region":            region,
                "website_url":       website,
                "building_dept_url": result["url"],
                "page_title":        result["title"],
                "snippet":           result["snippet"][:200],
                "confidence":        "auto",
                "notes":             "",
                "searched_at":       datetime.utcnow().isoformat(),
            }
            print(f"  -> {result['url']}")
        else:
            row = {
                "name":              name,
                "municipality_type": mtype,
                "region":            region,
                "website_url":       website,
                "building_dept_url": "",
                "page_title":        "",
                "snippet":           "",
                "confidence":        "skip",
                "notes":             "No result found",
                "searched_at":       datetime.utcnow().isoformat(),
            }
            print(f"  -> [no result]")

        writer.writerow(row)
        out_f.flush()
        processed += 1

    out_f.close()
    total_done = len(load_existing_results())
    print(f"\nDone. {processed} new searches, {skipped} skipped (already done).")
    print(f"Total in file: {total_done} / {len(municipalities)}")
    print(f"Output: {OUTPUT_FILE}")
    print("\nNext step: open municipality_urls.csv, review 'auto' rows, fix wrong URLs,")
    print("then run 2_load_to_supabase.py")


if __name__ == "__main__":
    main()
