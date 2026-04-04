"""
load_obc.py — Downloads OBC/municipality PDFs from Supabase Storage,
chunks them by section, embeds with Voyage AI, and loads into obc_sections.

Usage:
    pip install voyageai supabase pdfplumber python-dotenv
    python load_obc.py

Set these env vars (or add to .env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY   ← service role key, not anon (needed to list bucket)
    VOYAGE_API_KEY         ← free at voyageai.com, no credit card needed
    BUCKET_FOLDER          ← subfolder inside permit-files, e.g. "obc" or "" for root
"""

import os
import io
import re
import time
import pdfplumber
import voyageai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL         = os.getenv("SUPABASE_URL", "https://mqqbdkmjfameufouhewa.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
VOYAGE_API_KEY       = os.getenv("VOYAGE_API_KEY", "")
BUCKET               = "permit-files"
BUCKET_FOLDER        = os.getenv("BUCKET_FOLDER", "")   # e.g. "obc" or "" for root
EMBED_MODEL          = "voyage-large-2"                  # 1536 dims — matches our schema
CHUNK_SIZE           = 500    # words per chunk
CHUNK_OVERLAP        = 50     # words overlap between chunks
BATCH_SIZE           = 8      # Voyage AI max per batch

sb      = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
vo      = voyageai.Client(api_key=VOYAGE_API_KEY)


# ── Helpers ────────────────────────────────────────────────────────────────────

def list_pdfs() -> list[str]:
    """Return all PDF paths inside the bucket folder."""
    prefix = (BUCKET_FOLDER.rstrip("/") + "/") if BUCKET_FOLDER else ""
    res    = sb.storage.from_(BUCKET).list(BUCKET_FOLDER or "")
    paths  = []
    for item in res:
        name = item["name"]
        if name.lower().endswith(".pdf"):
            paths.append(prefix + name)
    print(f"Found {len(paths)} PDFs in bucket/{BUCKET_FOLDER or 'root'}")
    return paths


def download_pdf(path: str) -> bytes:
    return sb.storage.from_(BUCKET).download(path)


def extract_text(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF."""
    text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text.append(t)
    return "\n".join(text)


def detect_section(line: str):
    """Return a section number if the line looks like an OBC section header."""
    # Matches patterns like "9.10.9.12", "Part 3", "Article 3.2.1.4."
    m = re.match(
        r"^(Part\s+\d+|Division\s+[A-C]|Section\s+\d+|"
        r"\d+\.\d+(\.\d+)*\.?\s+[A-Z])",
        line.strip()
    )
    if m:
        return m.group(0).strip()
    return None


def chunk_text(text: str, filename: str) -> list[dict]:
    """
    Split text into overlapping word-based chunks.
    Tries to preserve section boundaries when possible.
    """
    words       = text.split()
    chunks      = []
    start       = 0
    current_sec = filename  # fallback section label

    # Also track section headers as we scan
    lines = text.splitlines()
    sec_map: dict[int, str] = {}
    word_pos = 0
    for line in lines:
        sec = detect_section(line)
        if sec:
            sec_map[word_pos] = sec
        word_pos += len(line.split())

    while start < len(words):
        end        = min(start + CHUNK_SIZE, len(words))
        chunk_text = " ".join(words[start:end])

        # Find the most recent section header for this chunk
        for pos in sorted(sec_map.keys(), reverse=True):
            if pos <= start:
                current_sec = sec_map[pos]
                break

        chunks.append({
            "section_number": current_sec,
            "title":          filename,
            "content":        chunk_text,
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings with Voyage AI, retry on rate limit."""
    for attempt in range(3):
        try:
            res = vo.embed(texts, model=EMBED_MODEL, input_type="document")
            return res.embeddings
        except Exception as e:
            if "rate" in str(e).lower() and attempt < 2:
                print(f"  Rate limited — waiting 15s...")
                time.sleep(15)
            else:
                raise


def already_loaded(filename: str) -> bool:
    """Skip files already in the DB (based on title match)."""
    res = sb.table("obc_sections").select("id").eq("title", filename).limit(1).execute()
    return len(res.data) > 0


def upsert_chunks(chunks: list[dict], embeddings: list[list[float]]):
    rows = [
        {
            "section_number": c["section_number"],
            "title":          c["title"],
            "content":        c["content"],
            "embedding":      e,
        }
        for c, e in zip(chunks, embeddings)
    ]
    sb.table("obc_sections").insert(rows).execute()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SUPABASE_SERVICE_KEY:
        print("ERROR: Set SUPABASE_SERVICE_KEY in your .env")
        return
    if not VOYAGE_API_KEY:
        print("ERROR: Set VOYAGE_API_KEY in your .env — free at voyageai.com")
        return

    pdf_paths = list_pdfs()
    if not pdf_paths:
        print("No PDFs found. Check BUCKET_FOLDER in your .env")
        return

    for path in pdf_paths:
        filename = path.split("/")[-1]
        print(f"\n── {filename}")

        if already_loaded(filename):
            print(f"   Already loaded — skipping")
            continue

        print(f"   Downloading...")
        pdf_bytes = download_pdf(path)

        print(f"   Extracting text...")
        text = extract_text(pdf_bytes)
        if not text.strip():
            print(f"   No extractable text — skipping")
            continue

        print(f"   Chunking...")
        chunks = chunk_text(text, filename)
        print(f"   {len(chunks)} chunks")

        print(f"   Embedding + uploading...")
        for i in range(0, len(chunks), BATCH_SIZE):
            batch      = chunks[i : i + BATCH_SIZE]
            texts      = [c["content"] for c in batch]
            embeddings = embed_batch(texts)
            upsert_chunks(batch, embeddings)
            print(f"   {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")
            time.sleep(0.5)  # be gentle with the API

        print(f"   Done.")

    print("\n\nAll files loaded. obc_sections is ready.")


if __name__ == "__main__":
    main()
