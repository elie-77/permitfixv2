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
from datetime import datetime, timezone, timedelta
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

MODEL                = "claude-opus-4-5"
EMBED_MODEL          = "voyage-large-2"  # 1536-dim, matches DB and load_obc.py
OBC_MATCH_COUNT      = 25
TRIAL_DURATION_DAYS  = 3

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
ac_async = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None

IMAGE_TYPES    = {"jpg", "jpeg", "png", "webp", "gif"}
MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "webp": "image/webp", "gif": "image/gif",
}

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PermitFix AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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

# Access modes returned by get_access_mode():
#   "full"             — paid subscriber or admin
#   "chat_only"        — per_submission user with 0 credits; may ask questions, no new submissions
#   "trialing"         — Stripe-managed trial (subscription_status='trialing'), not yet expired
#   "trial"            — legacy custom trial, scan not yet used
#   "trial_scan_used"  — legacy custom trial, scan already used
#   "trial_expired"    — trial window has passed
#   "blocked"          — no subscription, no trial


def _parse_ts(ts_str: str):
    """Parse ISO timestamp string from Supabase to UTC-aware datetime."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def get_access_mode(user_id: str, user_email: str = "") -> str:
    if user_email in ADMIN_EMAILS:
        return "full"
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if not res.data:
            return "blocked"
        d = res.data[0]
        status = d.get("subscription_status", "")
        # Paid access
        if status == "active":
            return "full"
        if d.get("plan_type") == "per_submission":
            if d.get("submissions_remaining", 0) > 0:
                return "full"
            # Credits exhausted — can still chat about existing reports, no new submissions
            return "chat_only"
        # Stripe-managed trial
        if status == "trialing":
            trial_exp = d.get("trial_expires_at")
            if trial_exp:
                now = datetime.now(timezone.utc)
                if now >= _parse_ts(trial_exp):
                    return "trial_expired"
            return "trialing"
        # Legacy custom trial
        trial_exp = d.get("trial_expires_at")
        if trial_exp:
            now = datetime.now(timezone.utc)
            if now >= _parse_ts(trial_exp):
                return "trial_expired"
            if d.get("trial_scan_used"):
                return "trial_scan_used"
            return "trial"
        return "blocked"
    except Exception:
        return "blocked"


def get_trial_info(user_id: str) -> dict:
    """Return trial metadata for a user."""
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if not res.data:
            return {"has_trial": False}
        d = res.data[0]
        trial_started = d.get("trial_started_at")
        trial_expires = d.get("trial_expires_at")
        if not trial_started or not trial_expires:
            return {"has_trial": False}
        now = datetime.now(timezone.utc)
        exp = _parse_ts(trial_expires)
        delta = exp - now
        return {
            "has_trial": True,
            "is_active": now < exp,
            "trial_scan_used": bool(d.get("trial_scan_used")),
            "trial_started_at": trial_started,
            "trial_expires_at": trial_expires,
            "hours_remaining": max(0, int(delta.total_seconds() / 3600)),
        }
    except Exception:
        return {"has_trial": False}


def start_trial(user_id: str) -> dict:
    """Start 3-day trial for user. Idempotent — won't overwrite an existing trial or paid plan."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=TRIAL_DURATION_DAYS)
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if res.data:
            d = res.data[0]
            # Don't restart if already on paid plan or already trialing
            if d.get("subscription_status") == "active" or d.get("trial_started_at"):
                return get_trial_info(user_id)
            sb.table("stripe_customers").update({
                "trial_started_at": now.isoformat(),
                "trial_expires_at": exp.isoformat(),
                "trial_scan_used":  False,
            }).eq("user_id", user_id).execute()
        else:
            sb.table("stripe_customers").insert({
                "user_id":               user_id,
                "subscription_status":   "inactive",
                "plan_type":             "per_submission",
                "submissions_remaining": 0,
                "trial_started_at":      now.isoformat(),
                "trial_expires_at":      exp.isoformat(),
                "trial_scan_used":       False,
            }).execute()
        return get_trial_info(user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not start trial: {e}")


def mark_trial_scan_used(user_id: str, project_id: str = ""):
    try:
        sb.table("stripe_customers").update({
            "trial_scan_used":       True,
            "trial_scan_project_id": project_id,
        }).eq("user_id", user_id).execute()
    except Exception:
        pass


def deduct_credit(user_id: str):
    try:
        res = sb.table("stripe_customers").select("*").eq("user_id", user_id).execute()
        if res.data:
            d = res.data[0]
            if d.get("plan_type") == "per_submission":
                new_count = max(0, d.get("submissions_remaining", 1) - 1)
                update = {"submissions_remaining": new_count}
                if new_count == 0:
                    update["subscription_status"] = "inactive"
                sb.table("stripe_customers") \
                  .update(update) \
                  .eq("user_id", user_id) \
                  .execute()
    except Exception:
        pass


# ── Municipality auto-detection ───────────────────────────────────────────────

# Ontario municipalities we support — checked against extracted addresses
_KNOWN_MUNICIPALITIES = [
    "Toronto", "Mississauga", "Brampton", "Hamilton", "Ottawa", "London",
    "Markham", "Vaughan", "Kitchener", "Windsor", "Richmond Hill", "Oakville",
    "Burlington", "Oshawa", "Barrie", "St. Catharines", "Cambridge", "Kingston",
    "Whitby", "Guelph", "Ajax", "Thunder Bay", "Waterloo", "Chatham",
    "Brantford", "Pickering", "Niagara Falls", "Peterborough", "Sudbury",
    "Newmarket", "East Gwillimbury", "Georgina", "Uxbridge", "Aurora",
    "Clarington", "Halton Hills", "Milton", "Caledon", "Orangeville",
]

def extract_municipality(doc_texts: list[dict]) -> str:
    """
    Scan the first ~3000 words of uploaded docs for an Ontario municipality name.
    Checks common permit form fields first, then falls back to address pattern matching.
    Returns the matched municipality name or "" if not found.
    """
    import re

    # Gather text from first few doc pages
    sample = ""
    for doc in doc_texts[:3]:
        sample += doc.get("text", doc.get("content", "")) + "\n"
        if len(sample) > 6000:
            break
    sample = sample[:6000]

    # 1. Explicit form field: "Municipality: Toronto" or "City/Town: Brampton"
    field_match = re.search(
        r"(?:municipality|city[/ ]*town|city|town|local\s+municipality)\s*[:/]\s*([A-Za-z .''-]{2,40})",
        sample, re.IGNORECASE
    )
    if field_match:
        candidate = field_match.group(1).strip().rstrip(",.")
        for muni in _KNOWN_MUNICIPALITIES:
            if muni.lower() in candidate.lower():
                return muni

    # 2. Address pattern: "123 Main St, Toronto, ON" or "Toronto, Ontario"
    addr_match = re.findall(
        r",\s*([A-Za-z .''-]{2,30}),?\s*(?:ON|Ontario)\b",
        sample, re.IGNORECASE
    )
    for candidate in addr_match:
        candidate = candidate.strip()
        for muni in _KNOWN_MUNICIPALITIES:
            if muni.lower() in candidate.lower():
                return muni

    # 3. Bare name anywhere in text (lower confidence — only if clearly present)
    for muni in _KNOWN_MUNICIPALITIES:
        pattern = r'\b' + re.escape(muni) + r'\b'
        if re.search(pattern, sample, re.IGNORECASE):
            return muni

    return ""


# ── OBC semantic search ───────────────────────────────────────────────────────

def search_obc(query: str, municipality: str = "") -> list[dict]:
    """
    Embed query with Voyage AI and search pgvector.

    When municipality is provided, runs TWO separate searches and combines:
      1. OBC-only (municipality_id IS NULL) — top 15 provincial chunks
      2. Municipal-only (municipality_id = muni_id) — top 15 bylaw chunks
    This guarantees municipal bylaw sections always appear regardless of how
    OBC sections rank by similarity (OBC pool is 20x larger and would otherwise
    dominate a single combined search).

    Without municipality: returns top OBC_MATCH_COUNT provincial chunks only.
    """
    if not vo:
        return []
    try:
        import concurrent.futures
        def _search():
            res = vo.embed([query], model=EMBED_MODEL, input_type="query")
            embedding = res.embeddings[0]

            if not municipality:
                rows = sb.rpc("match_obc_sections", {
                    "query_embedding": embedding,
                    "match_count":     OBC_MATCH_COUNT,
                }).execute()
                return rows.data or []

            # Look up municipality_id
            muni_res = (
                sb.table("municipalities")
                .select("id")
                .ilike("name", f"%{municipality}%")
                .limit(1)
                .execute()
            )
            if not muni_res.data:
                # Unknown municipality — fall back to OBC only
                rows = sb.rpc("match_obc_sections", {
                    "query_embedding": embedding,
                    "match_count":     OBC_MATCH_COUNT,
                }).execute()
                return rows.data or []

            muni_id = muni_res.data[0]["id"]

            # Search 1 + 2 in parallel: OBC-only and municipal-only simultaneously
            def _obc():
                rows = sb.rpc("match_obc_sections", {
                    "query_embedding": embedding,
                    "match_count":     15,
                }).execute().data or []
                return [r for r in rows if not r.get("municipality_id")]

            def _muni():
                rows = sb.rpc("match_obc_sections", {
                    "query_embedding":   embedding,
                    "match_count":       20,
                    "p_municipality_id": muni_id,
                }).execute().data or []
                return [r for r in rows if r.get("municipality_id")]

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f_obc  = pool.submit(_obc)
                f_muni = pool.submit(_muni)
                obc_only  = f_obc.result()
                muni_only = f_muni.result()

            return obc_only + muni_only

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(_search)
            return future.result(timeout=12)
    except Exception as e:
        print(f"OBC search error: {e}")
        return []


# ── Claude helpers ────────────────────────────────────────────────────────────

OBC_EXPERT_SYSTEM = (
    "You are an expert Ontario building permit assistant specializing in the "
    "2024 Ontario Building Code (OBC), local municipal bylaws, and residential "
    "construction compliance. Analyze drawings and documents for issues with "
    "dimensions, setbacks, lot coverage, building height, fire separations, "
    "egress, accessibility, structural elements, and grading.\n\n"

    "## DOCUMENT TYPE IDENTIFICATION — do this first, every time:\n"
    "Before analyzing, identify what type of document was submitted:\n"
    "- **Actual permit submission**: real drawings, plans, site surveys, or completed applications "
    "with specific project measurements, addresses, and site-specific data\n"
    "- **Compliance template / checklist**: a blank or pre-filled checklist, template, or "
    "benchmark document used to verify what should be included — not an actual submission\n"
    "- **Reference document**: OBC extracts, bylaw text, guides, or standards\n\n"
    "If the document is a **template or checklist**, state this clearly at the top of your response: "
    "'⚠️ This appears to be a compliance template/checklist, not an actual permit submission. "
    "Analysis below reflects what a real submission would need to include.' "
    "Then evaluate the template's completeness, not a real project's compliance. "
    "Do NOT flag items as missing from the project — flag them as missing from the template itself.\n\n"
    "If the document is an **actual submission**, proceed with full compliance analysis.\n\n"

    "## KNOWLEDGE BASE DOCUMENT HIERARCHY — how to read amendments and appeals:\n"
    "The knowledge base may contain multiple document types for the same municipality. "
    "Apply this hierarchy when documents conflict:\n\n"
    "1. **Consolidated Bylaw** (labelled 'Consolidated YYYY') — highest authority. "
    "Incorporates all amendments up to its consolidation date. Use as the primary source.\n"
    "2. **Amendment** (labelled 'Amendment No. XXXX' or 'Amendment Summary') — "
    "supersedes ONLY the specific sections it modifies. The rest of the base bylaw still applies. "
    "When citing an amended rule, write: 'As amended by [Amendment No.], s.[X] now requires...'\n"
    "3. **OLT / OMB Appeal Decision** (labelled 'Appeal' or 'Appeal Index') — "
    "may partially or fully reverse an amendment or bylaw provision. "
    "Always flag affected sections: '⚠️ Subject to OLT Appeal — confirm current status with municipality.' "
    "Never apply an appealed provision without this warning.\n"
    "4. **Amendment Index / Summary** — reference only. Lists what was changed but is not the binding text. "
    "Use to identify which amendment numbers to look for, not as a source of rules.\n\n"
    "**Currency warning:** Always state the document date at the end of any municipal citation: "
    "'(Source: [Document Name], [Date] — confirm no subsequent amendments apply).' "
    "If you cannot confirm currency, say so explicitly rather than presenting the rule as current fact.\n\n"

    "## CITATION RULES — non-negotiable:\n"
    "1. Every regulatory claim must include: the specific bylaw/code name, bylaw NUMBER, "
    "and section number. Never cite just the bylaw name without a number. "
    "WRONG: 'Township of Muskoka Lakes Zoning Bylaw limits height to 4.5m' "
    "RIGHT: 'Township of Muskoka Lakes Zoning Bylaw No. 2014-14, s.6.3.2 limits boathouse height to 4.5m'\n"
    "2. Every numerical value (setbacks, heights, coverage, loads, dimensions) must have a citation. "
    "No orphaned numbers — if you state 3.0m, 1.2m, 40%, you must cite the source immediately.\n"
    "3. If the exact number is in the provided knowledge base, quote it directly with its section number.\n"
    "4. If a value is NOT in the knowledge base, state this explicitly: "
    "'[Not found in provided documents — confirm with municipality]' then give the OBC default if one exists.\n"
    "5. Never use vague language like 'typically regulated', 'generally requires', or 'may apply'. "
    "Either cite the specific rule or say explicitly that it could not be verified from the provided documents.\n"
    "6. For Conservation Authorities: always name the SPECIFIC authority (e.g. Muskoka Watershed, "
    "Lake Simcoe Region, TRCA) — never say 'the local Conservation Authority'.\n"
    "7. For climate data (HDD, climate zones, frost depths): always cite the source "
    "(e.g. 'OBC Table C-2', 'NBC Climate Data').\n\n"

    "## SEVERITY RULES:\n"
    "1. SB-12 non-compliance is ❌ CRITICAL only if energy documentation was submitted and "
    "shows a violation. If no energy docs were submitted, note it in one line under Advisory "
    "as 'SB-12 compliance package not included — required before permit issuance.' "
    "Do NOT make it a Critical finding.\n"
    "2. Conservation Authority: flag as ❌ CRITICAL only if the submitted documents show "
    "development in a regulated area without CA approval. If CA jurisdiction is simply unknown "
    "from the documents, note it once under Advisory. Do not speculate.\n"
    "3. Outdated OBC edition referenced in drawings is ❌ CRITICAL.\n"
    "4. Thermal performance issues are ⚠️ IMPORTANT only if energy docs were submitted and "
    "show a deficiency.\n"
    "5. Missing documents (structural drawings, floor plans, etc.) are NOT critical findings. "
    "They are a single ℹ️ Advisory line: 'X not included — submit before permit issuance.' "
    "One line each. Never expand missing documents into multi-point critical findings.\n"
    "6. Complete all calculations that CAN be done from the submitted documents. "
    "Do not flag calculations as findings when the data to perform them wasn't submitted.\n"
    "7. **If a submission is compliant, say so clearly and positively. Reward the effort.** "
    "A clean report after revisions deserves genuine acknowledgment — e.g. 'This submission "
    "is well-prepared and appears compliant with the reviewed requirements. The items below "
    "are minor.' Omit Critical/Important/Advisory sections entirely when empty. "
    "Never manufacture issues just to appear thorough.\n\n"

    "## CITATION INTEGRITY:\n"
    "1. Never put citation disclaimers inline in brackets within a finding. "
    "Instead, write the finding cleanly and move ALL unverified citations to a dedicated "
    "'## Unverified Citations' section at the end of the report.\n"
    "2. For Conservation Authority: if the specific CA cannot be confirmed from the documents, "
    "write 'Conservation Authority jurisdiction requires confirmation before submission' — "
    "do not guess or hedge inline.\n\n"

    "## ANALYSIS FOCUS — critical principle:\n"
    "Analyze ONLY what is actually in the submitted documents. Your findings must be grounded "
    "in what you can see, measure, or read — not in what was not submitted.\n"
    "- A site plan submission should be reviewed as a site plan. Do not flag missing structural "
    "drawings, SB-12 packages, or floor plans as Critical findings — those are separate documents "
    "for a later stage. Mention them once as Advisory if relevant.\n"
    "- If a document bears a municipal approval stamp, acknowledge that zoning review was "
    "completed at that stage. Do not re-litigate zoning compliance as if it never happened.\n"
    "- If dimensions on the drawing satisfy the bylaw requirements you can verify, say so: "
    "'✅ Front yard setback: 6.0m provided, 4.5m required — compliant.'\n"
    "- A finding must describe an actual problem visible in the documents, not a hypothetical "
    "risk. 'SB-12 may be required' is not a finding — it is noise.\n"
    "- Keep the report tight: real findings about what was submitted, "
    "one-liners for anything to add at a later stage.\n\n"

    "## REQUIRED REPORT STRUCTURE — always follow this order:\n"
    "1. Executive Summary (2-4 sentences: what was reviewed, top-line verdict. "
    "If the submission is well-prepared, say so warmly and specifically — e.g. "
    "'This is a well-prepared site plan. Setbacks, lot coverage, and grading all appear "
    "compliant. A few minor items are noted below for completeness.')\n"
    "2. ✅ Compliant Items (only if there are verified passes — list what checked out "
    "with citations, e.g. '✅ Rear yard setback: 7.5m provided vs 6.0m required (s.6.2.3)'). "
    "**Omit if nothing could be positively verified.**\n"
    "3. ❌ CRITICAL Findings (numbered C1, C2... — **omit entirely if none**)\n"
    "4. ⚠️ IMPORTANT Findings (numbered I1, I2... — **omit entirely if none**)\n"
    "5. ℹ️ ADVISORY Notes (numbered A1, A2... — **omit entirely if none**)\n"
    "6. Summary Table (omit if no findings)\n"
    "7. Before You Submit (3-6 bullets MAX. Include a brief checklist of what a complete "
    "permit package typically requires — but frame it as helpful context, not a list of "
    "failures. e.g. 'For a complete permit submission you will also need: structural drawings, "
    "SB-12 energy compliance, floor plans.' One clean list, not repeated throughout the report.)\n"
    "8. Unverified Citations (bylaw sections that could not be confirmed — omit if none)\n\n"

    "## RESPONSE LENGTH — scale to the submission:\n"
    "Match your response length to what was actually submitted. Do not pad.\n"
    "- Single page or simple site plan → concise report, 300-500 words\n"
    "- Multi-document package (3-5 files) → standard report, 500-900 words\n"
    "- Large complex submission (6+ files, mixed occupancies, Part 3) → full report, up to 1200 words\n"
    "If a submission has no findings, the entire response can be 150 words. That is correct.\n"
    "Never repeat the same point in multiple sections. Say it once, in the right place.\n\n"

    "## FORMAT RULES:\n"
    "1. Use proper markdown hierarchy for ALL responses — this is rendered in a UI:\n"
    "   # Report Title (once, at the top)\n"
    "   ## Major sections (Executive Summary, Critical Findings, Important Findings, etc.)\n"
    "   ### Individual findings (C1: Finding Name, I1: Finding Name, etc.)\n"
    "   #### Sub-points within a finding (Requirement, Finding, Action Required)\n"
    "   **Bold** for key values, measurements, bylaw numbers, and section references\n"
    "   Regular text for body copy\n"
    "   Use --- horizontal rules to separate major sections\n"
    "2. Number all sections and findings within sections (C1, C2, I1, I2, A1, A2).\n"
    "3. Use tables (markdown format) for the Summary Table and all checklists.\n"
    "4. Use minimal emojis — only: ✅ (compliant), ❌ (critical), ⚠️ (important), ℹ️ (advisory), "
    "📋 (reference). No decorative or expressive emojis.\n"
    "5. CHECKLIST FORMATTING — every checklist line item MUST use exactly one of:\n"
    "   ✅ = already present, no action needed\n"
    "   🟡 = needs review or conditional\n"
    "   ❌ = missing or non-compliant, must be corrected\n"
    "Never use any other symbol on checklist items. Always assess each item against "
    "the submitted documents — do not default everything to one colour."
)

TRIAL_SYSTEM = (
    "You are an expert Ontario building permit assistant. The user is on a free trial "
    "and will only see a limited preview of your analysis.\n\n"

    "## YOUR TASK:\n"
    "Analyze the submitted documents and produce a short preview report with exactly "
    "three sections — nothing more:\n\n"

    "### 1. Issues Found\n"
    "State the total number of compliance issues identified (e.g. '14 issues found'). "
    "Then give a one-sentence verdict on overall readiness.\n\n"

    "### 2. Top 3 Issue Categories\n"
    "List the three most significant categories of issues (e.g. Setbacks, Energy Code, "
    "Structural). For each, write one sentence describing the nature of the problem. "
    "Do NOT list all individual findings — just the categories.\n\n"

    "### 3. Example Fix\n"
    "Pick the single most important finding and show what the fix looks like: "
    "state the issue, cite the specific OBC section or bylaw, and state the corrective action.\n\n"

    "---\n"
    "After the three sections, add this exact line:\n"
    "> **Upgrade to Pro** to unlock all findings, citations, severity tiers, and the full compliance report.\n\n"

    "## RULES:\n"
    "- Do NOT produce a full report. Stop after the three sections above.\n"
    "- Do NOT list every finding — only the three categories and one example.\n"
    "- Use the same citation standards: bylaw name, number, and section.\n"
    "- If the user asks a question instead of uploading a document, answer it briefly "
    "(2-4 sentences max) then remind them that detailed analysis requires Pro.\n"
    "- Minimal emojis: only ❌ ⚠️ ✅ where genuinely useful."
)


def build_system_blocks(doc_texts: list[dict], obc_chunks: list[dict], is_trial: bool = False) -> list[dict]:
    system_prompt = TRIAL_SYSTEM if is_trial else OBC_EXPERT_SYSTEM
    blocks = [{"type": "text", "text": system_prompt}]

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
    """Extract text from a PDF using PyMuPDF (fast)."""
    import fitz
    pages = []
    total_chars = 0
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page_count = len(doc)
    for i in range(min(page_count, MAX_PDF_PAGES)):
        try:
            text = doc[i].get_text()
        except Exception:
            continue
        if text and text.strip():
            pages.append(text.strip())
            total_chars += len(text)
            if total_chars >= MAX_PDF_CHARS:
                pages.append(f"[Truncated: text limit reached at page {i+1}]")
                break
    doc.close()
    if page_count > MAX_PDF_PAGES:
        pages.append(f"[Truncated: first {MAX_PDF_PAGES} of {page_count} pages]")
    return "\n\n".join(pages), page_count


def pdf_to_images(file_bytes: bytes) -> list[str]:
    """Render PDF pages to base64 PNG images for vision analysis."""
    import fitz  # PyMuPDF
    images = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total = len(doc)
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
        doc_texts.append({"name": name, "text": text})
    else:
        imgs = pdf_to_images(file_bytes)
        for b64 in imgs:
            image_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })
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
    municipality: str = ""   # e.g. "Toronto" — used to pull municipal bylaw chunks


class GeneratePdfRequest(BaseModel):
    project_name: str
    messages: list[Message]
    doc_names: list[str] = []


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "permitfix-api"}


@app.get("/trial-status")
async def trial_status_endpoint(request: Request):
    """Return trial info + access mode for the authenticated user."""
    token = get_bearer(request)
    user  = verify_token(token)
    info  = get_trial_info(user["id"])
    mode  = get_access_mode(user["id"], user.get("email", ""))
    return {**info, "access_mode": mode, "email": user.get("email", "")}


@app.post("/start-trial")
async def start_trial_endpoint(request: Request):
    """Start a 3-day free trial for the authenticated user (idempotent)."""
    token = get_bearer(request)
    user  = verify_token(token)
    # If already on a paid plan, return 400
    res = sb.table("stripe_customers").select("subscription_status") \
            .eq("user_id", user["id"]).execute()
    if res.data and res.data[0].get("subscription_status") == "active":
        raise HTTPException(status_code=400, detail="User already has an active subscription.")
    info = start_trial(user["id"])
    return info


@app.post("/analyze")
async def analyze(req: AnalyzeRequest, request: Request):
    # ── Auth (sync — must complete before we can stream) ──────────────────────
    token = get_bearer(request)
    user  = verify_token(token)

    access_mode = get_access_mode(user["id"], user.get("email", ""))
    has_new_files = bool(req.files or req.storage_paths)

    if access_mode == "blocked":
        raise HTTPException(status_code=402, detail="No active subscription or trial. Sign up for a free trial.")
    if access_mode == "trial_expired":
        raise HTTPException(status_code=402, detail="Your free trial has expired. Upgrade to continue.")
    if access_mode == "trial_scan_used":
        raise HTTPException(status_code=402, detail="Trial scan already used. Upgrade to run more analyses.")
    if access_mode == "chat_only" and has_new_files:
        raise HTTPException(status_code=402, detail="No submission credits remaining. Upgrade to analyze new documents.")

    is_trial      = access_mode in ("trial", "trialing")
    is_submission = access_mode == "full" and has_new_files
    _user_id    = user["id"]
    _project_id = req.project_id

    # Everything else runs inside generate() so we can emit status events
    # immediately and keep the perceived latency near zero.
    async def generate() -> AsyncGenerator[str, None]:
        from urllib.parse import unquote
        import asyncio

        try:
            # ── 1. Acknowledge immediately ────────────────────────────────────
            yield f"data: {json.dumps({'status': 'Reviewing your submission...'})}\n\n"
            if is_trial:
                yield f"data: {json.dumps({'trial_mode': True})}\n\n"

            # ── 2. Kick off OBC search in background thread immediately ───────
            # It only needs req.message, so it can run while we download files.
            loop = asyncio.get_event_loop()
            obc_future = loop.run_in_executor(None, search_obc, req.message, req.municipality)

            # ── 3. Download and process files (parallel) ──────────────────────
            yield f"data: {json.dumps({'status': 'Reading uploaded documents...'})}\n\n"

            doc_texts: list[dict]    = []
            image_blocks: list[dict] = []

            async def fetch_and_process(f: dict):
                name      = unquote(f.get("name", "file"))
                file_type = f.get("file_type") or f.get("type", "pdf")
                if "data" not in f and "bucket" in f and "path" in f:
                    try:
                        raw = await loop.run_in_executor(
                            None, download_from_storage, f["bucket"], f["path"], token
                        )
                    except Exception as e:
                        print(f"Error fetching file {name}: {e}")
                        return
                elif "data" in f:
                    raw = base64.b64decode(f["data"])
                else:
                    return
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext == "pdf" or file_type == "pdf":
                    await loop.run_in_executor(None, process_pdf, raw, name, doc_texts, image_blocks)
                elif ext in IMAGE_TYPES or file_type in IMAGE_TYPES or file_type == "image":
                    b64  = base64.b64encode(raw).decode() if "data" not in f else f["data"]
                    mime = MEDIA_TYPE_MAP.get(ext, "image/png")
                    image_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

            async def fetch_storage_path(path: str):
                name = unquote(path.split("/")[-1])
                ext  = name.rsplit(".", 1)[-1].lower()
                try:
                    raw = await loop.run_in_executor(
                        None, download_from_storage, "permit-files", path, token
                    )
                    if ext == "pdf":
                        await loop.run_in_executor(None, process_pdf, raw, name, doc_texts, image_blocks)
                    elif ext in IMAGE_TYPES:
                        b64  = base64.b64encode(raw).decode()
                        mime = MEDIA_TYPE_MAP.get(ext, "image/png")
                        image_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
                except Exception as e:
                    print(f"Error fetching storage path {path}: {e}")

            # Collect all tasks and run in parallel
            tasks = [fetch_and_process(f) for f in req.files]
            already_fetched = {unquote(f["path"]) for f in req.files if "path" in f}
            seen_paths: set[str] = set()
            for raw_path in req.storage_paths:
                path = unquote(raw_path)
                if path not in seen_paths and path not in already_fetched:
                    seen_paths.add(path)
                    tasks.append(fetch_storage_path(path))

            if tasks:
                await asyncio.gather(*tasks)

            # ── 4. Auto-detect municipality from uploaded docs ────────────────
            detected_municipality = req.municipality  # use explicit value if provided
            if not detected_municipality and doc_texts:
                detected_municipality = extract_municipality(doc_texts)
                if detected_municipality:
                    print(f"Auto-detected municipality: {detected_municipality}")
                    yield f"data: {json.dumps({'status': f'Detected municipality: {detected_municipality}...'})}\n\n"

            # ── 5. Collect OBC results (likely already done) ──────────────────
            yield f"data: {json.dumps({'status': 'Cross-referencing Ontario Building Code...'})}\n\n"
            try:
                obc_chunks = await asyncio.wait_for(obc_future, timeout=8)
            except Exception:
                obc_chunks = []

            # If we detected a municipality that wasn't used in the initial search,
            # re-run with the detected municipality. search_obc() now runs two
            # separate queries (OBC-only + municipal-only) and combines them,
            # guaranteeing bylaw sections appear regardless of OBC similarity ranking.
            if detected_municipality and detected_municipality != req.municipality:
                try:
                    muni_query = (
                        f"{detected_municipality} zoning bylaw setbacks lot coverage "
                        f"building height parking requirements permitted uses {req.message}"
                    )
                    obc_chunks = await loop.run_in_executor(
                        None, search_obc, muni_query, detected_municipality
                    )
                except Exception as e:
                    print(f"Municipality re-search error: {e}")

            # ── 6. Build Claude messages ──────────────────────────────────────
            system_blocks = build_system_blocks(doc_texts, obc_chunks, is_trial=is_trial)
            api_messages: list[dict] = []

            if image_blocks:
                image_blocks.append({
                    "type": "text",
                    "text": f"I've uploaded {len(image_blocks)} drawing(s). Analyze carefully for Ontario Building Code compliance.",
                })
                api_messages.append({"role": "user", "content": image_blocks})
                api_messages.append({
                    "role": "assistant",
                    "content": f"I've reviewed the {len(image_blocks)-1} drawing(s) and am ready to analyze them.",
                })

            for m in req.history:
                api_messages.append({"role": m.role, "content": m.content})

            api_messages.append({"role": "user", "content": req.message})

            # ── 6. Stream Claude response ─────────────────────────────────────
            yield f"data: {json.dumps({'status': 'Generating report...'})}\n\n"

            token_count  = 0
            report_parts = []
            async with ac_async.messages.stream(
                model=MODEL,
                max_tokens=600 if is_trial else 8192,
                system=system_blocks,
                messages=api_messages,
            ) as stream:
                async for text in stream.text_stream:
                    token_count += 1
                    report_parts.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

            if token_count == 0:
                yield f"data: {json.dumps({'text': 'I received your document but was unable to generate a response. Please try again.'})}\n\n"

            if is_trial:
                mark_trial_scan_used(_user_id, _project_id)
                yield f"data: {json.dumps({'trial_scan_complete': True})}\n\n"

            # ── 7. Save analysis result to DB ─────────────────────────────────
            if token_count > 0 and not is_trial:
                def _save_analysis():
                    try:
                        file_paths = [
                            f.get("path", f.get("name", ""))
                            for f in req.files
                        ] + list(seen_paths)
                        row = {
                            "user_id":      _user_id,
                            "report_text":  "".join(report_parts),
                            "municipality": detected_municipality or None,
                            "file_paths":   file_paths or None,
                        }
                        if _project_id:
                            row["project_id"] = _project_id
                        sb.table("project_analyses").insert(row).execute()
                    except Exception as e:
                        print(f"Save analysis error: {e}")
                await loop.run_in_executor(None, _save_analysis)

            # Deduct one credit after a successful paid submission
            if is_submission and token_count > 0:
                await loop.run_in_executor(None, deduct_credit, _user_id)

            yield "data: [DONE]\n\n"

        except Exception as e:
            print(f"Stream error: {e}")
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
async def generate_pdf(req: GeneratePdfRequest):
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
        text = str(text)
        # Translate common Unicode to readable ASCII before stripping
        _UNI = {
            "\u2014": "-",    # em dash —
            "\u2013": "-",    # en dash –
            "\u2022": "-",    # bullet •
            "\u2018": "'",    # left single quote
            "\u2019": "'",    # right single quote
            "\u201c": '"',    # left double quote
            "\u201d": '"',    # right double quote
            "\u2026": "...",  # ellipsis
            "\u00a0": " ",    # non-breaking space
            "\u2264": "<=",   # ≤
            "\u2265": ">=",   # ≥
            "\u00b0": " deg", # °
            "\u2705": "[OK]", # ✅
            "\u274c": "[X]",  # ❌
            "\u26a0": "[!]",  # ⚠
            "\ufe0f": "",     # variation selector (emoji modifier)
            "\u2139": "[i]",  # ℹ
            "\U0001f7e1": "[~]",  # 🟡
            "\U0001f4cb": "",     # 📋
        }
        for ch, rep in _UNI.items():
            text = text.replace(ch, rep)
        return text.encode("latin-1", errors="ignore").decode("latin-1")

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
                pdf.multi_cell(182, 5, f"-  {bullet_body(line)}", fill=False)
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
    pdf.cell(190, 8, safe("PermitFix AI - Compliance Report"), ln=True)
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
