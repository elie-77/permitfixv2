"""
load_obc.py — Chunks, embeds, and loads bylaw/OBC PDFs into obc_sections.

Two source modes:
  --local   Read from LOCAL directories (knowledge_base/ + municipality subfolders)
  default   Download from Supabase Storage bucket

Municipality-aware title prefixing:
  Any PDF inside a subfolder named after a municipality automatically gets
  prefixed: "Hamilton — zoningby-law05-200-section10-1-c1zone-oct2025.pdf"
  This ensures the title-based semantic search finds the right bylaw.

Usage:
    # Index from Supabase Storage bucket root:
    python load_obc.py

    # Index from local knowledge_base/ + MUNICIPALITY_REGISTRY_DIR subfolders:
    python load_obc.py --local

    # Wipe obc_sections and rebuild everything:
    python load_obc.py --reload --local
    python load_obc.py --reload

Set in .env:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY        ← service role key
    VOYAGE_API_KEY
    BUCKET_FOLDER               ← subfolder in permit-files bucket (or "" for root)
    MUNICIPALITY_REGISTRY_DIR   ← local path containing municipality subfolders
                                   e.g. /Users/you/docs/municipality-registry
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

SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY      = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
VOYAGE_API_KEY            = os.getenv("VOYAGE_API_KEY", "")
BUCKET                    = "permit-files"
BUCKET_FOLDER             = os.getenv("BUCKET_FOLDER", "")
MUNICIPALITY_REGISTRY_DIR = os.getenv("MUNICIPALITY_REGISTRY_DIR", "")
EMBED_MODEL               = "voyage-large-2"   # 1536 dims — matches DB schema
CHUNK_SIZE                = 500                # words per chunk
CHUNK_OVERLAP             = 50                 # words overlap between chunks
BATCH_SIZE                = 8                  # Voyage AI max per batch
LOCAL_KB_DIR              = Path(__file__).parent / "knowledge_base"

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY)


# ── Municipality name normalisation ───────────────────────────────────────────
# Maps lowercase folder names → display name used in the title prefix
MUNICIPALITY_DISPLAY = {
    "brampton":    "Brampton",
    "hamilton":    "Hamilton",
    "kitchener":   "Kitchener",
    "london":      "London",
    "markham":     "Markham",
    "mississauga": "Mississauga",
    "ottawa":      "Ottawa",
    "toronto":     "Toronto",
    "vaughan":     "Vaughan",
    "windsor":     "Windsor",
    # Add more as needed — folder name (lowercase) → display name
    "newmarket":       "Newmarket",
    "east gwillimbury": "East Gwillimbury",
    "georgina":        "Georgina",
    "uxbridge":        "Uxbridge",
    "aurora":          "Aurora",
    "richmond hill":   "Richmond Hill",
    "oakville":        "Oakville",
    "burlington":      "Burlington",
    "barrie":          "Barrie",
    "kingston":        "Kingston",
    "guelph":          "Guelph",
    "niagara falls":   "Niagara Falls",
    "sudbury":         "Sudbury",
}

def display_name(folder_name: str) -> str:
    """Return the display name for a municipality folder."""
    key = folder_name.strip().lower()
    return MUNICIPALITY_DISPLAY.get(key, folder_name.title())


# ── Doc type detection ─────────────────────────────────────────────────────────
DOC_TYPE_RULES = [
    ("ontario building code",          "obc"),
    ("ontario fire code",              "obc"),
    ("tacboc",                         "obc"),
    ("building permit application form", "permit_guide"),
    ("permit application guide",       "permit_guide"),
    ("sample building permit",         "permit_guide"),
    ("consolidated",                   "consolidated_bylaw"),
    ("amendment summary",              "amendment_index"),
    ("amendment index",                "amendment_index"),
    ("appeal index",                   "appeal_index"),
    ("appeal decision",                "olt_appeal"),
    ("olt appeal",                     "olt_appeal"),
    ("omb appeal",                     "olt_appeal"),
    ("amendment",                      "amendment"),
    ("permit guide",                   "permit_guide"),
    ("application guide",              "permit_guide"),
    ("zoning maps",                    "other"),
]

def detect_doc_type(filename: str) -> str:
    lower = filename.lower()
    for keyword, doc_type in DOC_TYPE_RULES:
        if keyword in lower:
            return doc_type
    return "base_bylaw"


# ── Text + table extraction ────────────────────────────────────────────────────

MAX_PAGES = 9999   # no page cap for indexing — we want everything

def extract_text(pdf_bytes: bytes) -> str:
    """
    Extract text AND tables from a PDF.
    Uses PyMuPDF for prose text, pdfplumber for structured tables.
    Tables are appended as pipe-separated rows so zone schedules
    (setbacks, lot coverage, parking rates) are captured correctly.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        fitz = None

    pages = []

    # ── Prose text via PyMuPDF (better than pdfplumber for most text) ─────────
    if fitz:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for i in range(len(doc)):
                try:
                    text = doc[i].get_text()
                    if text and text.strip():
                        pages.append(text.strip())
                except Exception:
                    pass
            doc.close()
        except Exception:
            pass

    # ── Fallback / table extraction via pdfplumber ────────────────────────────
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # If PyMuPDF failed, extract prose here too
            if not fitz or not pages:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages.append(t.strip())

            # Always extract tables separately — these hold zone schedule data
            table_blocks = []
            for i, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                for table in tables:
                    if not table:
                        continue
                    rows = []
                    for row in table:
                        cleaned = [str(cell).strip() if cell else "" for cell in row]
                        # Skip rows that are entirely empty
                        if any(c for c in cleaned):
                            rows.append(" | ".join(cleaned))
                    if rows:
                        block = f"[Table — page {i + 1}]\n" + "\n".join(rows)
                        table_blocks.append(block)
            if table_blocks:
                pages.append("\n\n--- EXTRACTED TABLES ---\n" + "\n\n".join(table_blocks))
    except Exception:
        pass

    return "\n\n".join(pages)


# ── Chunking ──────────────────────────────────────────────────────────────────

def detect_section(line: str):
    m = re.match(
        r"^(Part\s+\d+|Division\s+[A-C]|Section\s+\d+|"
        r"\d+\.\d+(\.\d+)*\.?\s+[A-Z])",
        line.strip()
    )
    return m.group(0).strip() if m else None


def chunk_text(text: str, title: str) -> list:
    words       = text.split()
    chunks      = []
    start       = 0
    current_sec = title

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
            "title":          title,
            "content":        chunk,
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_batch(texts: list) -> list:
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


# ── Supabase helpers ──────────────────────────────────────────────────────────

def already_loaded(title: str) -> bool:
    res = sb.table("obc_sections").select("id").eq("title", title).limit(1).execute()
    return len(res.data) > 0


def delete_all_entries():
    """Wipe the entire obc_sections table before a full reload."""
    print("Deleting all existing obc_sections rows...")
    try:
        # Fast path: call a TRUNCATE RPC if it exists in Supabase
        sb.rpc("truncate_obc_sections", {}).execute()
        print("  Table cleared via TRUNCATE.")
        return
    except Exception:
        pass
    # Fallback: delete in small batches with a pause to avoid statement timeout
    total = 0
    while True:
        res = sb.table("obc_sections").select("id").limit(200).execute()
        if not res.data:
            break
        ids = [r["id"] for r in res.data]
        sb.table("obc_sections").delete().in_("id", ids).execute()
        total += len(ids)
        print(f"  Deleted {total} rows...")
        time.sleep(0.3)
    print("  Table cleared.")


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
            rows_slim = [{k: v for k, v in r.items() if k != "doc_type"} for r in rows]
            sb.table("obc_sections").insert(rows_slim).execute()
        else:
            raise


# ── Storage helpers ───────────────────────────────────────────────────────────

def list_pdfs_from_storage() -> list:
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


# ── Local file helpers ────────────────────────────────────────────────────────

def list_pdfs_from_local() -> list[tuple[Path, str]]:
    """
    Returns list of (file_path, display_title) tuples.
    display_title = "{Municipality} — {filename}" for municipality subfolders,
                    just "{filename}" for knowledge_base/ root files.
    """
    results = []

    # 1. knowledge_base/ root — OBC, fire code, provincial docs (no municipality prefix)
    if LOCAL_KB_DIR.exists():
        for f in sorted(LOCAL_KB_DIR.glob("*.pdf")):
            results.append((f, f.name))
        print(f"knowledge_base/: {len([r for r in results])} files")

    # 2. Municipality registry directory — one subfolder per municipality
    if MUNICIPALITY_REGISTRY_DIR:
        reg_dir = Path(MUNICIPALITY_REGISTRY_DIR)
        if not reg_dir.exists():
            print(f"WARNING: MUNICIPALITY_REGISTRY_DIR not found: {reg_dir}")
        else:
            # Root-level PDFs in the registry dir (e.g. Uxbridge Base Bylaw.pdf)
            root_pdfs = sorted(list(reg_dir.glob("*.pdf")) + list(reg_dir.glob("*.PDF")))
            for f in root_pdfs:
                results.append((f, f.name))
            if root_pdfs:
                print(f"  (registry root): {len(root_pdfs)} files")

            muni_count = 0
            for subfolder in sorted(reg_dir.iterdir()):
                if not subfolder.is_dir():
                    continue
                muni = display_name(subfolder.name)
                pdfs = sorted(list(subfolder.glob("*.pdf")) + list(subfolder.glob("*.PDF")))
                if pdfs:
                    print(f"  {muni}: {len(pdfs)} files")
                    muni_count += len(pdfs)
                for f in pdfs:
                    title = f"{muni} — {f.name}"
                    results.append((f, title))
            print(f"Municipality registry: {muni_count} files across {sum(1 for s in reg_dir.iterdir() if s.is_dir())} municipalities")
    else:
        # Fall back: check if knowledge_base/ itself has municipality subfolders
        if LOCAL_KB_DIR.exists():
            for subfolder in sorted(LOCAL_KB_DIR.iterdir()):
                if not subfolder.is_dir():
                    continue
                muni = display_name(subfolder.name)
                pdfs = sorted(list(subfolder.glob("*.pdf")) + list(subfolder.glob("*.PDF")))
                if pdfs:
                    print(f"  {muni}: {len(pdfs)} files")
                for f in pdfs:
                    title = f"{muni} — {f.name}"
                    results.append((f, title))

    return results


# ── Process one file ──────────────────────────────────────────────────────────

def process_file(pdf_bytes: bytes, title: str, do_reload: bool):
    """Chunk, embed, and upsert one PDF. Returns number of chunks inserted."""
    if not do_reload and already_loaded(title):
        print(f"   Already loaded — skipping")
        return 0

    print(f"   Extracting text + tables...")
    text = extract_text(pdf_bytes)
    if not text.strip():
        print(f"   No extractable text — skipping")
        return 0

    doc_type = detect_doc_type(title)
    print(f"   Doc type: {doc_type}")

    chunks = chunk_text(text, title)
    print(f"   {len(chunks)} chunks")

    print(f"   Embedding + uploading...")
    for i in range(0, len(chunks), BATCH_SIZE):
        batch      = chunks[i: i + BATCH_SIZE]
        texts      = [c["content"] for c in batch]
        embeddings = embed_batch(texts)
        upsert_chunks(batch, embeddings, doc_type)
        print(f"   {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}")
        time.sleep(0.5)

    return len(chunks)


# ── Main ──────────────────────────────────────────────────────────────────────

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
        delete_all_entries()

    total_chunks = 0

    if use_local:
        file_list = list_pdfs_from_local()
        if not file_list:
            print("No PDFs found. Check LOCAL_KB_DIR and MUNICIPALITY_REGISTRY_DIR.")
            return
        print(f"\nTotal files to process: {len(file_list)}\n")
        for file_path, title in file_list:
            print(f"\n── {title}")
            try:
                pdf_bytes = file_path.read_bytes()
                n = process_file(pdf_bytes, title, do_reload)
                total_chunks += n
                if n:
                    print(f"   Done. ({n} chunks)")
            except Exception as e:
                print(f"   ERROR: {e}")

    else:
        pdf_paths = list_pdfs_from_storage()
        if not pdf_paths:
            print("No PDFs found. Check BUCKET_FOLDER in your .env")
            return
        for path in pdf_paths:
            filename = path.split("/")[-1]
            # Detect municipality from bucket subfolder if present
            parts = path.split("/")
            if len(parts) >= 2:
                folder = parts[-2]
                muni   = display_name(folder) if folder.lower() in MUNICIPALITY_DISPLAY else ""
                title  = f"{muni} — {filename}" if muni else filename
            else:
                title = filename

            print(f"\n── {title}")
            try:
                pdf_bytes = download_pdf(path)
                n = process_file(pdf_bytes, title, do_reload)
                total_chunks += n
                if n:
                    print(f"   Done. ({n} chunks)")
            except Exception as e:
                print(f"   ERROR: {e}")

    print(f"\n\nAll done. {total_chunks} total chunks inserted into obc_sections.")


if __name__ == "__main__":
    main()
