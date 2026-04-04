"""
main.py — PermitFix AI FastAPI backend
Endpoints:
  GET  /health
  POST /analyze   (streaming Claude response with OBC pgvector context)
  POST /generate-pdf
"""

import os
import io
import base64
import json
import re
import time
from typing import AsyncGenerator

import anthropic
import pdfplumber
import voyageai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from supabase import create_client

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY", "")
VOYAGE_API_KEY       = os.getenv("VOYAGE_API_KEY", "")

# Startup diagnostics
print(f"[STARTUP] SUPABASE_URL={SUPABASE_URL[:40] if SUPABASE_URL else 'NOT SET'}")
print(f"[STARTUP] SUPABASE_SERVICE_KEY={'SET (' + SUPABASE_SERVICE_KEY[:20] + '...)' if SUPABASE_SERVICE_KEY else 'NOT SET'}")
print(f"[STARTUP] SUPABASE_ANON_KEY={'SET' if SUPABASE_ANON_KEY else 'NOT SET'}")
MODEL                = "claude-3-5-sonnet-20241022"
EMBED_MODEL          = "voyage-large-2"  # 1536-dim, matches DB and load_obc.py
OBC_MATCH_COUNT      = 8

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None

IMAGE_TYPES    = {"jpg", "jpeg", "png", "webp", "gif"}
MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "webp": "image/webp", "gif": "image/gif",
}

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PermitFix AI API", version="1.0.0")

ALLOWED_ORIGINS = [
    "https://app.permitfix.ca",
    "https://www.permitfix.ca",
    "https://permitfix.ca",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def verify_token(token: str) -> dict:
    """Validate Supabase JWT via REST API (avoids local ES256 verification)."""
    import httpx
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail=f"Supabase rejected token: {resp.status_code} {resp.text}")
        data = resp.json()
        return {"id": data["id"], "email": data["email"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {e}")


def get_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return auth[7:]


ADMIN_EMAILS = {"elie.samaha77@gmail.com"}

def check_access(user_id: str, user_email: str = "") -> bool:
    """Return True if user has active subscription or remaining credits."""
    # Admin bypass for testing
    if user_email in ADMIN_EMAILS:
        return True
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if not res.data:
            return False
        d = res.data[0]
        if d.get("subscription_status") == "active":
            return True
        if d.get("plan_type") == "per_submission" and d.get("submissions_remaining", 0) > 0:
            return True
        return False
    except Exception:
        return False


def deduct_credit(user_id: str):
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if res.data:
            d = res.data[0]
            if d.get("plan_type") == "per_submission":
                new_count = max(0, d.get("submissions_remaining", 1) - 1)
                sb.table("stripe_customers") \
                  .update({"submissions_remaining": new_count}) \
                  .eq("user_id", user_id) \
                  .execute()
    except Exception:
        pass


# ── OBC semantic search ───────────────────────────────────────────────────────

def search_obc(query: str) -> list[dict]:
    """Embed query with Voyage AI, search pgvector, return relevant OBC chunks."""
    if not vo:
        return []
    try:
        import concurrent.futures
        def _search():
            res = vo.embed([query], model=EMBED_MODEL, input_type="query")
            embedding = res.embeddings[0]
            rows = sb.rpc("match_obc_sections", {
                "query_embedding": embedding,
                "match_count": OBC_MATCH_COUNT,
            }).execute()
            return rows.data or []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(_search)
            return future.result(timeout=5)  # fail fast after 5s
    except Exception as e:
        print(f"OBC search error: {e}")
        return []


# ── Claude helpers ────────────────────────────────────────────────────────────

OBC_EXPERT_SYSTEM = (
    "You are an expert Ontario building permit assistant specializing in the "
    "2024 Ontario Building Code (OBC), local municipal bylaws, and residential "
    "construction compliance. Analyze drawings and documents for issues with "
    "dimensions, setbacks, lot coverage, building height, fire separations, "
    "egress, accessibility, structural elements, and grading. "
    "Provide plain-English explanations, actionable fixes, and precise OBC "
    "section citations. Be thorough, conservative, and flag anything uncertain. "
    "Use minimal emojis — only functional symbols are permitted such as "
    "✅ (compliant), ❌ (non-compliant), ⚠️ (warning), and 📋 (document reference). "
    "Do not use decorative, celebratory, or expressive emojis (e.g. 🎉 🏗️ 😊 🙌 etc.)."
)


def build_system_blocks(doc_texts: list[dict], obc_chunks: list[dict]) -> list[dict]:
    blocks = [{"type": "text", "text": OBC_EXPERT_SYSTEM}]

    if obc_chunks:
        obc_text = "\n\n".join(
            f"[{c.get('section_number','OBC')} — {c.get('title','')}]\n{c.get('content','')}"
            for c in obc_chunks
        )
        blocks.append({
            "type": "text",
            "text": f"<obc_knowledge>\n{obc_text}\n</obc_knowledge>",
        })

    if doc_texts:
        kb = "\n\n".join(f"=== {d['name']} ===\n{d['text'][:12000]}" for d in doc_texts)
        blocks.append({
            "type": "text",
            "text": f"<project_documents>\n{kb}\n</project_documents>",
        })

    return blocks


def download_from_storage(bucket: str, path: str, user_token: str = "") -> bytes:
    """Download a file from Supabase storage via REST API."""
    from urllib.parse import unquote, quote
    import httpx
    decoded_path = unquote(path)
    encoded_path = quote(decoded_path, safe="/")

    # Try service key first, then fall back to user token
    tokens_to_try = []
    if SUPABASE_SERVICE_KEY:
        tokens_to_try.append(("service", SUPABASE_SERVICE_KEY))
    if user_token:
        tokens_to_try.append(("user", user_token))

    for label, token in tokens_to_try:
        url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{encoded_path}"
        print(f"[STORAGE] GET {url} (auth={label})")
        resp = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            },
            timeout=30,
            follow_redirects=True,
        )
        print(f"[STORAGE] Response {resp.status_code}: {resp.text[:100] if resp.status_code != 200 else 'OK'}")
        if resp.status_code == 200:
            return resp.content

    raise Exception(f"Storage fetch failed for {bucket}/{decoded_path}")


MAX_PDF_PAGES    = 20
MAX_PDF_CHARS    = 80_000
MAX_IMAGE_PAGES  = 10   # pages to render as images if scanned
IMAGE_DPI_SCALE  = 1.5  # scale factor relative to 72dpi base
MIN_TEXT_PER_PAGE = 80  # chars/page threshold — below this = scanned PDF


def extract_pdf_text(file_bytes: bytes) -> tuple[str, int]:
    """Extract text from a digitally-created PDF."""
    pages = []
    total_chars = 0
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PDF_PAGES:
                pages.append(f"[Truncated: first {MAX_PDF_PAGES} of {page_count} pages]")
                break
            try:
                text = page.extract_text()
            except Exception:
                continue
            if text:
                pages.append(text.strip())
                total_chars += len(text)
                if total_chars >= MAX_PDF_CHARS:
                    pages.append(f"[Truncated: text limit reached at page {i+1}]")
                    break
    return "\n\n".join(pages), page_count


def pdf_to_images(file_bytes: bytes) -> list[str]:
    """Render PDF pages to base64 PNG images for vision analysis."""
    import fitz  # PyMuPDF
    images = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total = len(doc)
    print(f"[PDF→IMG] Converting {min(total, MAX_IMAGE_PAGES)} of {total} pages to images")
    for i in range(min(total, MAX_IMAGE_PAGES)):
        page = doc[i]
        # Scale so longest side is ~1600px (good balance of quality vs memory)
        rect = page.rect
        scale = min(1600 / max(rect.width, rect.height, 1), IMAGE_DPI_SCALE)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        images.append(b64)
        pix = None  # free memory immediately
    doc.close()
    return images


def process_pdf(file_bytes: bytes, name: str, doc_texts: list, image_blocks: list):
    """Smart PDF processor: text extraction for digital PDFs, vision for scanned."""
    text, page_count = extract_pdf_text(file_bytes)
    chars_per_page = len(text) / max(page_count, 1)

    if chars_per_page >= MIN_TEXT_PER_PAGE:
        # Digital PDF — use extracted text
        print(f"[PDF] Text PDF: {len(text)} chars, {page_count} pages — {name}")
        doc_texts.append({"name": name, "text": text})
    else:
        # Scanned/image PDF — render pages and send to Claude vision
        print(f"[PDF] Scanned PDF detected ({chars_per_page:.0f} chars/page) — converting to images: {name}")
        imgs = pdf_to_images(file_bytes)
        for idx, b64 in enumerate(imgs):
            image_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })
        print(f"[PDF→IMG] Added {len(imgs)} page images for {name}")
        if text.strip():
            doc_texts.append({"name": name, "text": text})


# ── Request/Response models ───────────────────────────────────────────────────

class Message(BaseModel):
    role: str    # "user" | "assistant"
    content: str


class AnalyzeRequest(BaseModel):
    message: str
    history: list[Message] = []
    # Optional: base64-encoded files sent directly
    files: list[dict] = []   # [{"name": str, "data": base64str, "type": "pdf"|"image"}]
    # Optional: Supabase storage paths to fetch server-side
    storage_paths: list[str] = []
    project_id: str = ""


class GeneratePdfRequest(BaseModel):
    project_name: str
    messages: list[Message]
    doc_names: list[str] = []


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "permitfix-api"}


@app.get("/debug")
async def debug(request: Request):
    """Temporary debug endpoint — remove after fixing."""
    token = get_bearer(request)
    # 1. Which Supabase are we hitting?
    supabase_url = SUPABASE_URL
    # 2. Validate token
    import httpx
    auth_resp = httpx.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY},
        timeout=10,
    )
    user_data = auth_resp.json() if auth_resp.status_code == 200 else {"error": auth_resp.text}
    user_id = user_data.get("id", "unknown")
    # 3. Check stripe_customers
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        stripe_data = res.data
    except Exception as e:
        stripe_data = f"ERROR: {e}"
    return {
        "supabase_url": supabase_url,
        "token_status": auth_resp.status_code,
        "user": user_data,
        "stripe_customers": stripe_data,
    }


@app.post("/analyze")
async def analyze(req: AnalyzeRequest, request: Request):
    # ── Auth ──────────────────────────────────────────────────────────────────
    token = get_bearer(request)
    user  = verify_token(token)

    if not check_access(user["id"], user.get("email", "")):
        raise HTTPException(status_code=402, detail="No active subscription or credits remaining.")

    # ── Debug: log what was received ─────────────────────────────────────────
    print(f"[ANALYZE] message={req.message!r}")
    print(f"[ANALYZE] files count={len(req.files)}")
    print(f"[ANALYZE] storage_paths={req.storage_paths}")
    print(f"[ANALYZE] project_id={req.project_id!r}")

    # ── Decode files ─────────────────────────────────────────────────────────
    doc_texts: list[dict] = []
    image_blocks: list[dict] = []

    for f in req.files:
        from urllib.parse import unquote
        name      = unquote(f.get("name", "file"))
        file_type = f.get("file_type") or f.get("type", "pdf")

        # Lovable sends bucket + path instead of base64 data
        if "data" not in f and "bucket" in f and "path" in f:
            bucket    = f["bucket"]
            file_path = f["path"]
            print(f"[FILES] fetching from storage bucket={bucket} path={file_path}")
            try:
                raw = download_from_storage(bucket, file_path, user_token=token)
            except Exception as e:
                print(f"[FILES] storage fetch error: {e}")
                continue
        elif "data" in f:
            raw = base64.b64decode(f["data"])
        else:
            print(f"[FILES] skipping {name} — unrecognised format, keys={list(f.keys())}")
            continue

        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext == "pdf" or file_type == "pdf":
            process_pdf(raw, name, doc_texts, image_blocks)
        elif ext in IMAGE_TYPES or file_type in IMAGE_TYPES or file_type == "image":
            b64  = base64.b64encode(raw).decode() if "data" not in f else f["data"]
            mime = MEDIA_TYPE_MAP.get(ext, "image/png")
            image_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
            print(f"[FILES] added image {name}")

    # Track paths already fetched via the files array to avoid duplicates
    already_fetched = set()
    for f in req.files:
        if "path" in f:
            from urllib.parse import unquote
            already_fetched.add(unquote(f["path"]))

    # Fetch from Supabase storage if paths provided
    seen_paths = set()
    for raw_path in req.storage_paths:
        # URL-decode the path
        from urllib.parse import unquote
        path = unquote(raw_path)

        # Deduplicate and skip if already fetched via files array
        if path in seen_paths or path in already_fetched:
            print(f"[STORAGE] skipping duplicate: {path.split('/')[-1]}")
            continue
        seen_paths.add(path)

        bucket = "permit-files"
        file_path = path  # full path within the bucket

        try:
            raw  = download_from_storage(bucket, file_path, user_token=token)
            name = unquote(path.split("/")[-1])
            ext  = name.rsplit(".", 1)[-1].lower()
            print(f"[STORAGE] fetched {name} from bucket={bucket}")
            if ext == "pdf":
                process_pdf(raw, name, doc_texts, image_blocks)
            elif ext in IMAGE_TYPES:
                b64  = base64.b64encode(raw).decode()
                mime = MEDIA_TYPE_MAP.get(ext, "image/png")
                image_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
        except Exception as e:
            print(f"Storage fetch error for {path}: {e}")

    # ── OBC search ────────────────────────────────────────────────────────────
    obc_chunks = search_obc(req.message)

    # ── Build messages ────────────────────────────────────────────────────────
    system_blocks = build_system_blocks(doc_texts, obc_chunks)

    # History
    api_messages: list[dict] = []

    # Prepend images as first user turn if any
    if image_blocks:
        image_blocks.append({
            "type": "text",
            "text": f"I've uploaded {len(image_blocks)} drawing(s). Analyze carefully for Ontario Building Code compliance.",
        })
        api_messages.append({"role": "user",      "content": image_blocks})
        api_messages.append({"role": "assistant",
                              "content": f"I've reviewed the {len(image_blocks)-1} drawing(s) and am ready to analyze them."})

    for m in req.history:
        api_messages.append({"role": m.role, "content": m.content})

    api_messages.append({"role": "user", "content": req.message})

    # ── Stream response ───────────────────────────────────────────────────────
    async def generate() -> AsyncGenerator[str, None]:
        try:
            print(f"[STREAM] Starting — model={MODEL}, system_blocks={len(system_blocks)}, messages={len(api_messages)}, doc_texts={len(doc_texts)}, images={len(image_blocks)}")
            token_count = 0
            with ac.messages.stream(
                model=MODEL,
                max_tokens=2048,
                system=system_blocks,
                messages=api_messages,
            ) as stream:
                for text in stream.text_stream:
                    token_count += 1
                    yield f"data: {json.dumps({'text': text})}\n\n"
            print(f"[STREAM] Done — {token_count} chunks yielded")
            if token_count == 0:
                yield f"data: {json.dumps({'text': 'I received your document but was unable to generate a response. Please try again.'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            print(f"[STREAM] Error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/generate-pdf")
async def generate_pdf(req: GeneratePdfRequest, request: Request):
    token = get_bearer(request)
    verify_token(token)  # auth check only, no credit deduction for PDF

    pdf_bytes = _build_pdf(req.project_name, req.messages, req.doc_names)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="permitfix-report.pdf"',
        },
    )


# ── PDF generation ─────────────────────────────────────────────────────────────

def _build_pdf(project_name: str, messages: list[Message], doc_names: list[str]) -> bytes:
    from fpdf import FPDF

    def safe(text: str) -> str:
        return str(text).encode("latin-1", errors="ignore").decode("latin-1")

    def clean(text: str) -> str:
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*",     r"\1", text)
        text = re.sub(r"__(.+?)__",     r"\1", text)
        text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
        text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
        return safe(text)

    def hdr(line: str) -> str:
        return safe(re.sub(r"^#{1,6}\s+", "", line).strip())

    def is_md_header(line: str) -> bool:
        return bool(re.match(r"^#{1,6}\s+", line))

    def is_bullet(line: str) -> bool:
        return bool(re.match(r"^\s*[-*]\s+", line))

    def bullet_body(line: str) -> str:
        return clean(re.sub(r"^\s*[-*]\s+", "", line))

    CRITICAL_KW = [
        "non-compliant","not compliant","does not comply","does not meet",
        "violation","violates","action required","must be corrected",
        "fails to","not permitted","exceeds maximum","below minimum",
        "insufficient","deficient","missing required","inadequate",
        "not acceptable","not allowed",
    ]
    GOOD_KW = [
        "compliant","meets requirement","satisfies","no issue",
        "no violation","conforms","within the required","no deficien",
        "acceptable","adequate","passes","appears to meet",
    ]
    WARN_KW = [
        "review","verify","unclear","consider","may not","recommend",
        "suggest","should ensure","confirm","cannot verify","unable to",
        "potential","could","needs clarification",
    ]

    PALETTE = {
        "good":     ((22, 101, 52),   (240, 253, 244), "COMPLIANT"),
        "warning":  ((146, 64, 14),   (255, 251, 235), "REVIEW REQUIRED"),
        "critical": ((153, 27, 27),   (254, 242, 242), "ACTION REQUIRED"),
        "neutral":  ((30, 58, 138),   (239, 246, 255), "NOTE"),
    }

    def classify(header_str: str, body_str: str) -> str:
        t = (header_str + " " + body_str[:600]).lower()
        if any(k in t for k in CRITICAL_KW): return "critical"
        if any(k in t for k in GOOD_KW):     return "good"
        if any(k in t for k in WARN_KW):     return "warning"
        return "neutral"

    def parse_sections(text: str):
        sections, cur_hdr, cur_body = [], None, []
        for line in text.split("\n"):
            if is_md_header(line):
                if cur_hdr is not None or cur_body:
                    sections.append((cur_hdr, cur_body))
                cur_hdr, cur_body = hdr(line), []
            else:
                cur_body.append(line)
        if cur_hdr is not None or cur_body:
            sections.append((cur_hdr, cur_body))
        return sections

    def render_section(pdf, title, body_lines, status):
        (hr, hg, hb), (br, bg, bb), badge = PALETTE[status]

        pdf.set_fill_color(hr, hg, hb)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_x(10)
        label = f"  {badge}"
        if title:
            label += f"  |  {safe(title)}"
        pdf.cell(190, 7, label, fill=True, ln=True)

        pdf.set_fill_color(br, bg, bb)
        pdf.set_text_color(40, 40, 40)
        body_str = " ".join(body_lines)
        pdf.set_x(10)
        pdf.rect(10, pdf.get_y(), 190, 0.3, style="F")

        y_start = pdf.get_y() + 1
        pdf.set_xy(10, y_start)
        pdf.set_font("Helvetica", "", 8.5)

        for line in body_lines:
            line = line.rstrip()
            if not line:
                pdf.ln(2)
                continue
            if is_bullet(line):
                pdf.set_x(16)
                pdf.set_font("Helvetica", "", 8.5)
                pdf.multi_cell(182, 5, f"\u2022  {bullet_body(line)}", fill=False)
            else:
                cleaned = clean(line)
                if cleaned.strip():
                    pdf.set_x(12)
                    pdf.multi_cell(186, 5, cleaned, fill=False)

        pdf.set_fill_color(br, bg, bb)
        pdf.rect(10, pdf.get_y(), 190, 2, style="F")
        pdf.ln(4)

    # Build PDF
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(10, 10, 10)

    # Header
    pdf.set_fill_color(22, 101, 52)
    pdf.rect(0, 0, 210, 28, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_xy(10, 8)
    pdf.cell(190, 8, "PermitFix AI — Compliance Report", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    from datetime import datetime
    pdf.cell(190, 5,
             f"Project: {safe(project_name)}   |   Generated: {datetime.now().strftime('%B %d, %Y')}",
             ln=True)
    pdf.ln(6)

    if doc_names:
        pdf.set_text_color(80, 80, 80)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_x(10)
        pdf.cell(190, 5, f"Documents reviewed: {', '.join(safe(d) for d in doc_names)}", ln=True)
        pdf.ln(3)

    # Body — only assistant messages
    for msg in messages:
        if msg.role != "assistant":
            continue
        for title, body_lines in parse_sections(msg.content):
            body_str = " ".join(body_lines)
            status = classify(title or "", body_str)
            render_section(pdf, title, body_lines, status)

    out = io.BytesIO()
    pdf.output(out)
    return out.getvalue()
