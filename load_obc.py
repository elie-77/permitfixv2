"""
load_obc.py — Downloads OBC/municipality PDFs from Supabase Storage (or local
knowledge_base/ folder), chunks them by section, embeds with Voyage AI, and
loads into obc_sections.

Usage:
    # Load from Supabase Storage (default):
    python load_obc.py

    # Load from local knowledge_base/ folder:
    python load_obc.py --local

    # Delete all existing OBC entries first, then reload:
    python load_obc.py --reload
    python load_obc.py --reload --local

Set these env vars (or add to .env):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY   ← service role key, not anon (needed to list bucket)
    VOYAGE_API_KEY         ← free at voyageai.com, no credit card needed
    BUCKET_FOLDER          ← subfolder inside permit-files, e.g. "obc" or "" for root
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
BUCKET               = "permit-files"
BUCKET_FOLDER        = os.getenv("BUCKET_FOLDER", "")   # e.g. "obc" or "" for root
EMBED_MODEL          = "voyage-large-2"                  # 1536 dims — matches our schema
CHUNK_SIZE           = 500    # words per chunk
CHUNK_OVERLAP        = 50     # words overlap between chunks
BATCH_SIZE           = 8      # Voyage AI max per batch
LOCAL_KB_DIR         = Path(__file__).parent / "knowledge_base"

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY)


# ── Doc type detection (same rules as load_municipality_docs.py) ───────────────
DOC_TYPE_RULES = [
    ("ontario building code",    "obc"),
    ("ontario fire code",        "obc"),
    ("tacboc",                   "obc"),
    ("building permit application form", "permit_guide"),
    ("permit application guide", "permit_guide"),
    ("sample building permit",   "permit_guide"),
    ("consolidated",             "consolidated_bylaw"),
    ("amendment summary",        "amendment_index"),
    ("amendment index",          "amendment_index"),
    ("appeal index",             "appeal_index"),
    ("appeal decision",          "olt_appeal"),
    ("olt appeal",               "olt_appeal"),
    ("omb appeal",               "olt_appeal"),
    ("amendment",                "amendment"),
    ("permit guide",             "permit_guide"),
    ("application guide",        "permit_guide"),
    ("zoning maps",              "other"),
]

def detect_doc_type(filename: str) -> str:
    lower = filename.lower()
    for keyword, doc_type in DOC_TYPE_RULES:
        if keyword in lower:
            return doc_type
    return "base_bylaw"


# ── Helpers ────────────────────────────────────────────────────────────────────

def list_pdfs_from_storage() -> list:
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


def list_pdfs_from_local() -> list:
    """Return all PDF paths in local knowledge_base/ folder."""
    if not LOCAL_KB_DIR.exists():
        print(f"ERROR: {LOCAL_KB_DIR} does not exist")
        return []
    paths = list(LOCAL_KB_DIR.glob("*.pdf")) + list(LOCAL_KB_DIR.glob("*.PDF"))
    print(f"Found {len(paths)} PDFs in {LOCAL_KB_DIR}")
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
    m = re.match(
        r"^(Part\s+\d+|Division\s+[A-C]|Section\s+\d+|"
        r"\d+\.\d+(\.\d+)*\.?\s+[A-Z])",
        line.strip()
    )
    if m:
        return m.group(0).strip()
    return None


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
        chunks.append({
            "section_number": current_sec,
            "title":          filename,
            "content":        chunk,
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def embed_batch(texts: list) -> list:
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


# Old filenames that were renamed — delete these so they can be re-embedded with new names
OLD_FILENAMES = [
    "2024-OBC.Volume-1.January-16-2025.pdf",
    "2026 Fire Code.pdf",
    "301881.pdf",
    "EG Building-Permit-Application-Guide.pdf",
    "EG Zoning-By-law-Maps-2018-043-Oct-2020.pdf",
    "East Gwillumbury Zoning-By-law-2018-043-Apr-2025.pdf",
    "Georgina Zoning By-law No. 600 (November 2023) Modified as Approved.pdf",
    "Newmarket Zoning By-law 2010-40 Consolidated 2022.pdf",
    "Toronto City-Planning-Zoning-Zoning-By-law-Part-1.pdf",
    "Toronto-City-Planning-Zoning-Zoning-By-law-Part-2.pdf",
    "Toronto-City-Planning-Zoning-Zoning-By-law-Part-3.pdf",
    "UXBRIDGE Zoning-By-law.pdf",
    "mmah-building-development-application-for-a-permit-to-construct-or-demolish-2014-en-2021-11-01.pdf",
    "tacboc_details_2012.pdf",
    # Also purge new names so they get re-embedded with doc_type set
    "Ontario Building Code 2024 Volume 1.pdf",
    "Ontario Fire Code 2026.pdf",
    "Vaughan OLT Appeal 301881.pdf",
    "East Gwillimbury Permit Application Guide.pdf",
    "East Gwillimbury Zoning Maps 2018-043.pdf",
    "East Gwillimbury Consolidated Bylaw 2018-043 Apr 2025.pdf",
    "Georgina Consolidated Bylaw 600 Nov 2023.pdf",
    "Newmarket Consolidated Bylaw 2010-40 2022.pdf",
    "Toronto Consolidated Bylaw 569-2013 Part 1.pdf",
    "Toronto Consolidated Bylaw 569-2013 Part 2.pdf",
    "Toronto Consolidated Bylaw 569-2013 Part 3.pdf",
    "Uxbridge Base Bylaw.pdf",
    "Ontario Building Permit Application Form MMAH 2021.pdf",
    "Ontario TACBOC Building Code Details 2012.pdf",
    "Sample Building Permit Package Drawings.pdf",
    "Vaughan Amendment Index 2024.pdf",
    "Vaughan Appeal Index OLT.pdf",
    "Vaughan Base Bylaw 1-88.pdf",
]

def delete_obc_entries():
    """Delete obc_sections rows for all known OBC filenames (old and new names)."""
    print("Deleting existing OBC entries by filename...")
    total = 0
    for fname in OLD_FILENAMES:
        res = sb.table("obc_sections").delete().eq("title", fname).execute()
        count = len(res.data)
        if count:
            print(f"  Deleted {count} rows for: {fname}")
            total += count
    print(f"  Total deleted: {total} rows")


def upsert_chunks(chunks: list, embeddings: list, doc_type: str):
    rows = [
        {
            "section_number": c["section_number"],
            "title":          c["title"],
            "content":        c["content"],
            "embedding":      e,
            "doc_type":       doc_type,
        }
        for c, e in zip(chunks, embeddings)
    ]
    try:
        sb.table("obc_sections").insert(rows).execute()
    except Exception as e:
        if "doc_type" in str(e):
            # doc_type column not yet added — insert without it
            rows_slim = [
                {k: v for k, v in row.items() if k != "doc_type"}
                for row in rows
            ]
            sb.table("obc_sections").insert(rows_slim).execute()
        else:
            raise


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SUPABASE_SERVICE_KEY:
        print("ERROR: Set SUPABASE_SERVICE_KEY in your .env")
        return
    if not VOYAGE_API_KEY:
        print("ERROR: Set VOYAGE_API_KEY in your .env — free at voyageai.com")
        return

    use_local = "--local" in sys.argv
    do_reload = "--reload" in sys.argv

    if do_reload:
        delete_obc_entries()

    if use_local:
        pdf_paths = list_pdfs_from_local()
        if not pdf_paths:
            return
        for file_path in sorted(pdf_paths):
            filename = file_path.name
            print(f"\n── {filename}")

            if not do_reload and already_loaded(filename):
                print(f"   Already loaded — skipping")
                continue

            print(f"   Reading...")
            pdf_bytes = file_path.read_bytes()
            print(f"   Extracting text...")
            text = extract_text(pdf_bytes)
            if not text.strip():
                print(f"   No extractable text — skipping")
                continue

            doc_type = detect_doc_type(filename)
            print(f"   Doc type: {doc_type}")
            print(f"   Chunking...")
            chunks = chunk_text(text, filename)
            print(f"   {len(chunks)} chunks")

            print(f"   Embedding + uploading...")
            for i in range(0, len(chunks), BATCH_SIZE):
                batch      = chunks[i : i + BATCH_SIZE]
                texts      = [c["content"] for c in batch]
                embeddings = embed_batch(texts)
                upsert_chunks(batch, embeddings, doc_type)
                print(f"   {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")
                time.sleep(0.5)

            print(f"   Done.")
    else:
        pdf_paths = list_pdfs_from_storage()
        if not pdf_paths:
            print("No PDFs found. Check BUCKET_FOLDER in your .env")
            return

        for path in pdf_paths:
            filename = path.split("/")[-1]
            print(f"\n── {filename}")

            if not do_reload and already_loaded(filename):
                print(f"   Already loaded — skipping")
                continue

            print(f"   Downloading...")
            pdf_bytes = download_pdf(path)

            print(f"   Extracting text...")
            text = extract_text(pdf_bytes)
            if not text.strip():
                print(f"   No extractable text — skipping")
                continue

            doc_type = detect_doc_type(filename)
            print(f"   Doc type: {doc_type}")
            print(f"   Chunking...")
            chunks = chunk_text(text, filename)
            print(f"   {len(chunks)} chunks")

            print(f"   Embedding + uploading...")
            for i in range(0, len(chunks), BATCH_SIZE):
                batch      = chunks[i : i + BATCH_SIZE]
                texts      = [c["content"] for c in batch]
                embeddings = embed_batch(texts)
                upsert_chunks(batch, embeddings, doc_type)
                print(f"   {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")
                time.sleep(0.5)

            print(f"   Done.")

    print("\n\nAll files loaded. obc_sections is ready.")


if __name__ == "__main__":
    main()
