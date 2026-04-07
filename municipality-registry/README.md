# Municipality Registry

Builds and maintains the master list of Ontario building department URLs.

## Setup order

### 1. Create the Supabase table
Go to Supabase → SQL Editor → paste and run `supabase_municipalities.sql`

### 2. Add SerpAPI key to .env
```
SERPAPI_KEY=your_key_here
```

Get it here: serpapi.com → sign up (free, 100 searches/month) → Dashboard → copy API Key

### 3. Install dependencies
```bash
pip install requests python-dotenv supabase
```

### 4. Run the search script
```bash
python 1_search_municipalities.py
```
- Processes up to 100/day on the free tier (set `DAILY_LIMIT = 0` if billing is enabled)
- Saves results to `municipality_urls.csv` — safe to re-run, resumes where it left off
- Takes ~5 days on free tier, or ~$4.50 to run all 444 at once with billing

### 5. Review the CSV
Open `municipality_urls.csv` in Excel or Google Sheets.
- `confidence = auto` → URL was found automatically, **review these**
- `confidence = skip` → Google returned nothing, **look these up manually**
- Change `confidence` to `manual` once you've verified/corrected a URL
- Leave `confidence = skip` for any you can't find (small townships with no web presence)

### 6. Load to Supabase
```bash
python 2_load_to_supabase.py
```
- Upserts all rows where `confidence` is `auto` or `manual`
- Safe to re-run after making corrections

## Cost estimate
| Scenario | Queries | Cost |
|---|---|---|
| Free tier (100/day × 5 days) | 444 | $0 |
| All at once with billing | 444 | ~$4.50 |
| Re-run after corrections | varies | ~$0 (no re-search needed) |
