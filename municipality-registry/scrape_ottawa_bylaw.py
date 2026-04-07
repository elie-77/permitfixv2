"""
scrape_ottawa_bylaw.py — Scrapes Ottawa Zoning By-law No. 2026-50 into a
single text file ready for load_municipality_docs.py to embed.

Usage:
    pip3 install requests beautifulsoup4
    python3 scrape_ottawa_bylaw.py

Output: ../municipality-registry/docs/ottawa/ottawa-zoning-bylaw-2026-50.txt
"""

import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urldefrag

BASE_URL   = "https://ottawa.ca"
INDEX_URL  = "https://ottawa.ca/en/living-ottawa/laws-licences-and-permits/laws/laws-z/zoning-law-law-no-2026-50"
OUTPUT     = Path(__file__).parent / "docs" / "ottawa" / "ottawa-zoning-bylaw-2026-50.txt"
DELAY      = 1.2   # seconds between requests — be polite

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Only follow links that stay within the bylaw
BYLAW_PREFIX = "/en/living-ottawa/laws-licences-and-permits/laws/laws-z/zoning-law-law-no-2026-50"


SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = False   # bypass LibreSSL issues on older Macs

def fetch(url: str):
    try:
        resp = SESSION.get(url, timeout=20)
        if resp.status_code != 200:
            print(f"  [skip] {resp.status_code} — {url}")
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  [error] {e} — {url}")
        return None


def extract_text(soup: BeautifulSoup, url: str) -> str:
    """Pull the main content text from an Ottawa bylaw page."""
    # Ottawa uses a main content div — try common selectors
    for selector in ["main", ".field--name-body", "article", ".view-content", "#content"]:
        el = soup.select_one(selector)
        if el:
            # Remove nav/breadcrumb noise
            for tag in el.select("nav, .breadcrumb, .pager, script, style, header, footer"):
                tag.decompose()
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return text
    # Fallback to body
    body = soup.find("body")
    return body.get_text(separator="\n", strip=True) if body else ""


def collect_section_urls(soup: BeautifulSoup) -> list[str]:
    """Find all bylaw section URLs from the index page."""
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Resolve relative URLs
        full = urljoin(BASE_URL, href)
        # Strip fragment (#section-UUID) — same page content
        clean, _ = urldefrag(full)
        if (
            BYLAW_PREFIX in clean
            and clean != INDEX_URL
            and clean not in seen
            and clean.startswith("https://ottawa.ca")
        ):
            seen.add(clean)
            urls.append(clean)
    return urls


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching index: {INDEX_URL}")
    index_soup = fetch(INDEX_URL)
    if not index_soup:
        print("ERROR: Could not fetch index page.")
        return

    # Extract index page text first
    sections_text = []
    index_text = extract_text(index_soup, INDEX_URL)
    if index_text:
        sections_text.append(f"=== Ottawa Zoning By-law 2026-50 — Index ===\n{index_text}")

    # Collect all section URLs
    section_urls = collect_section_urls(index_soup)
    print(f"Found {len(section_urls)} section pages to scrape\n")

    for i, url in enumerate(section_urls, 1):
        slug = url.replace(BASE_URL + BYLAW_PREFIX, "").strip("/") or "index"
        print(f"[{i}/{len(section_urls)}] {slug}")

        soup = fetch(url)
        if not soup:
            continue

        text = extract_text(soup, url)
        if text.strip():
            sections_text.append(f"\n\n=== {slug} ===\n{url}\n\n{text}")
            print(f"  {len(text):,} chars")
        else:
            print(f"  [empty]")

        time.sleep(DELAY)

    full_text = "\n\n".join(sections_text)
    OUTPUT.write_text(full_text, encoding="utf-8")

    print(f"\nDone. {len(section_urls)+1} pages scraped.")
    print(f"Total: {len(full_text):,} characters")
    print(f"Saved to: {OUTPUT}")
    print(f"\nNext: run  python3 ../load_municipality_docs.py")


if __name__ == "__main__":
    main()
