"""
load_municipality_docs.py — Load municipality-specific PDFs into obc_sections
with a municipality_id tag so the backend can serve municipal + OBC knowledge together.

Usage:
    python3 load_municipality_docs.py

Folder structure expected under municipality-registry/docs/:
    municipality-registry/docs/
        toronto/
            zoning-bylaw-569-2013.pdf
            building-permit-guide.pdf
        ottawa/
            zoning-bylaw-2008-250.pdf
        mississauga/
            zoning-bylaw-0225-2007.pdf
        ... etc.

The folder name must match (case-insensitive) a name in the municipalities table.
Drop PDFs in the right folder and run this script — it handles the rest.
"""

import os
import io
import re
import sys
import time
import pdfplumber
import voyageai
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
VOYAGE_API_KEY       = os.getenv("VOYAGE_API_KEY", "")
EMBED_MODEL          = "voyage-large-2"
CHUNK_SIZE           = 500
CHUNK_OVERLAP        = 50
BATCH_SIZE           = 8
DOCS_DIR             = Path(__file__).parent / "municipality-registry" / "docs"

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY)


# ── Helpers (same as load_obc.py) ─────────────────────────────────────────────

def extract_text(pdf_bytes: bytes) -> str:
    text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text.append(t)
    return "\n".join(text)


def detect_section(line: str):
    m = re.match(
        r"^(Part\s+\d+|Division\s+[A-C]|Section\s+\d+|\d+\.\d+(\.\d+)*\.?\s+[A-Z])",
        line.strip()
    )
    return m.group(0).strip() if m else None


def chunk_text(text: str, filename: str) -> list:
    words       = text.split()
    chunks      = []
    start       = 0
    current_sec = filename

    lines    = text.splitlines()
    sec_map  = {}
    word_pos = 0
    for line in lines:
        sec = detect_section(line)
        if sec:
            sec_map[word_pos] = sec
        word_pos += len(line.split())

    while start < len(words):
        end   = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end])
        for pos in sorted(sec_map.keys(), reverse=True):
            if pos <= start:
                current_sec = sec_map[pos]
                break
        chunks.append({"section_number": current_sec, "title": filename, "content": chunk})
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def embed_batch(texts: list) -> list:
    for attempt in range(3):
        try:
            res = vo.embed(texts, model=EMBED_MODEL, input_type="document")
            return res.embeddings
        except Exception as e:
            if "rate" in str(e).lower() and attempt < 2:
                print("  Rate limited — waiting 15s...")
                time.sleep(15)
            else:
                raise


def already_loaded(filename: str, municipality_id: str) -> bool:
    res = (
        sb.table("obc_sections")
        .select("id")
        .eq("title", filename)
        .eq("municipality_id", municipality_id)
        .limit(1)
        .execute()
    )
    return len(res.data) > 0


def upsert_chunks(chunks: list, embeddings: list, municipality_id: str):
    rows = [
        {
            "section_number": c["section_number"],
            "title":          c["title"],
            "content":        c["content"],
            "embedding":      e,
            "municipality_id": municipality_id,
        }
        for c, e in zip(chunks, embeddings)
    ]
    sb.table("obc_sections").insert(rows).execute()


def lookup_municipality_id(folder_name: str) -> str:
    """Find municipality ID by matching folder name to municipalities table."""
    # Try exact match first
    res = (
        sb.table("municipalities")
        .select("id, name")
        .ilike("name", f"%{folder_name}%")
        .limit(5)
        .execute()
    )
    if not res.data:
        return None

    # If only one match, use it
    if len(res.data) == 1:
        print(f"  Matched: {res.data[0]['name']} ({res.data[0]['id']})")
        return res.data[0]["id"]

    # Multiple matches — prefer exact
    for row in res.data:
        if row["name"].lower() == folder_name.lower():
            print(f"  Matched: {row['name']} ({row['id']})")
            return row["id"]

    # Take first and warn
    print(f"  Multiple matches — using: {res.data[0]['name']}")
    print(f"  Others: {[r['name'] for r in res.data[1:]]}")
    return res.data[0]["id"]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SUPABASE_SERVICE_KEY:
        print("ERROR: Set SUPABASE_SERVICE_KEY in .env")
        return
    if not VOYAGE_API_KEY:
        print("ERROR: Set VOYAGE_API_KEY in .env")
        return

    if not DOCS_DIR.exists():
        DOCS_DIR.mkdir(parents=True)
        print(f"Created docs folder: {DOCS_DIR}")
        print("Add municipality subfolders with PDFs, e.g.:")
        print("  municipality-registry/docs/toronto/zoning-bylaw.pdf")
        return

    municipality_folders = [d for d in DOCS_DIR.iterdir() if d.is_dir()]
    if not municipality_folders:
        print(f"No municipality folders found in {DOCS_DIR}")
        print("Create a subfolder named after the municipality and drop PDFs in it.")
        return

    total_loaded = 0

    for muni_dir in sorted(municipality_folders):
        muni_name = muni_dir.name
        print(f"\n{'='*60}")
        print(f"Municipality: {muni_name}")

        municipality_id = lookup_municipality_id(muni_name)
        if not municipality_id:
            print(f"  NOT FOUND in municipalities table — skipping.")
            print(f"  Run 2_load_to_supabase.py first or check the folder name.")
            continue

        pdfs = list(muni_dir.glob("*.pdf")) + list(muni_dir.glob("*.PDF"))
        if not pdfs:
            print(f"  No PDFs found in {muni_dir}")
            continue

        for pdf_path in sorted(pdfs):
            filename = pdf_path.name
            print(f"\n  ── {filename}")

            if already_loaded(filename, municipality_id):
                print(f"     Already loaded — skipping")
                continue

            print(f"     Extracting text...")
            pdf_bytes = pdf_path.read_bytes()
            text = extract_text(pdf_bytes)
            if not text.strip():
                print(f"     No extractable text — skipping (scanned PDF?)")
                continue

            print(f"     Chunking...")
            chunks = chunk_text(text, filename)
            print(f"     {len(chunks)} chunks")

            print(f"     Embedding + uploading...")
            for i in range(0, len(chunks), BATCH_SIZE):
                batch      = chunks[i : i + BATCH_SIZE]
                texts      = [c["content"] for c in batch]
                embeddings = embed_batch(texts)
                upsert_chunks(batch, embeddings, municipality_id)
                print(f"     {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")
                time.sleep(0.5)

            total_loaded += 1
            print(f"     Done.")

    print(f"\n\nFinished. {total_loaded} PDFs loaded into obc_sections.")


if __name__ == "__main__":
    main()
