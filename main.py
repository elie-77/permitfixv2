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
AC_API_KEY           = os.getenv("AC_API_KEY", "")
AC_BASE_URL          = os.getenv("AC_BASE_URL", "https://77inc59539.api-us1.com")
AC_LIST_ID           = "3"  # Master Contact List

STRIPE_WEBHOOK_SECRET   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SINGLE_PRODUCT   = "prod_UIWRfDQkvF5eui"   # $77 Single Report
STRIPE_UNLIMITED_PRODUCT = "prod_UI8EeNoC48yHJw"  # $200 Unlimited Pro

MODEL                = "claude-opus-4-5"
EMBED_MODEL          = "voyage-large-2"  # 1536-dim, matches DB and load_obc.py
OBC_MATCH_COUNT      = 25
TRIAL_DURATION_DAYS  = 3

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
ac_async = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
vo = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None

# ── ActiveCampaign helpers ────────────────────────────────────────────────────

import urllib.request
import urllib.parse

def _ac_tag_contact(email: str, first_name: str, tag: str) -> None:
    """Create/update a contact in ActiveCampaign and apply a tag. Fire-and-forget."""
    if not AC_API_KEY or not email:
        return
    headers = {
        "Api-Token": AC_API_KEY,
        "Content-Type": "application/json",
    }

    def _post(url: str, body: dict):
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[AC] {url} failed: {e}")
            return {}

    def _get_json(url: str):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[AC] GET {url} failed: {e}")
            return {}

    base = AC_BASE_URL.rstrip("/")

    # 1. Upsert contact
    result = _post(f"{base}/api/3/contact/sync", {
        "contact": {
            "email": email,
            "firstName": first_name,
            "fieldValues": [],
        }
    })
    contact_id = (result.get("contact") or {}).get("id")
    if not contact_id:
        print(f"[AC] Could not upsert contact for {email}")
        return

    # 2. Add to list
    _post(f"{base}/api/3/contactLists", {
        "contactList": {
            "list": AC_LIST_ID,
            "contact": contact_id,
            "status": "1",
        }
    })

    # 3. Find or create tag
    tags_resp = _get_json(f"{base}/api/3/tags?search={urllib.parse.quote(tag)}")
    tag_id = None
    for t in (tags_resp.get("tags") or []):
        if t.get("tag") == tag:
            tag_id = t["id"]
            break
    if not tag_id:
        tag_result = _post(f"{base}/api/3/tags", {"tag": {"tag": tag, "tagType": "contact"}})
        tag_id = (tag_result.get("tag") or {}).get("id")

    if not tag_id:
        print(f"[AC] Could not find/create tag: {tag}")
        return

    # 4. Apply tag to contact
    _post(f"{base}/api/3/contactTags", {
        "contactTag": {"contact": contact_id, "tag": tag_id}
    })
    print(f"[AC] Tagged {email} with '{tag}'")


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
    Scan uploaded docs + filenames for an Ontario municipality name.
    Checks common permit form fields first, then address patterns, then bare name.
    Returns the matched municipality name or "" if not found.
    """
    import re

    # 1. Check filenames — sometimes files are named with the address/municipality
    all_filenames = " ".join(
        doc.get("filename", doc.get("name", "")) for doc in doc_texts
    )
    for muni in _KNOWN_MUNICIPALITIES:
        pattern = r'\b' + re.escape(muni) + r'\b'
        if re.search(pattern, all_filenames, re.IGNORECASE):
            print(f"Municipality detected from filename: {muni}")
            return muni

    # 2. Gather text from ALL doc pages (not just first 3) — CAD drawings
    #    often have very little text so scanning more doesn't hurt performance
    sample = ""
    for doc in doc_texts:
        sample += doc.get("text", doc.get("content", "")) + "\n"
    sample = sample[:20000]  # up from 6000

    if not sample.strip():
        return ""

    # 3. Explicit form field: "Municipality: Toronto" or "City/Town: Brampton"
    field_match = re.search(
        r"(?:municipality|city[/ ]*town|city|town|local\s+municipality)\s*[:/]\s*([A-Za-z .''-]{2,40})",
        sample, re.IGNORECASE
    )
    if field_match:
        candidate = field_match.group(1).strip().rstrip(",.")
        for muni in _KNOWN_MUNICIPALITIES:
            if muni.lower() in candidate.lower():
                return muni

    # 4. Address pattern: "123 Main St, Toronto, ON" or "Toronto, Ontario"
    addr_match = re.findall(
        r",\s*([A-Za-z .''-]{2,30}),?\s*(?:ON|Ontario)\b",
        sample, re.IGNORECASE
    )
    for candidate in addr_match:
        candidate = candidate.strip()
        for muni in _KNOWN_MUNICIPALITIES:
            if muni.lower() in candidate.lower():
                return muni

    # 5. "Town of X" / "City of X" / "Township of X" pattern
    govt_match = re.findall(
        r"(?:town|city|township|municipality)\s+of\s+([A-Za-z .''-]{2,30})",
        sample, re.IGNORECASE
    )
    for candidate in govt_match:
        candidate = candidate.strip()
        for muni in _KNOWN_MUNICIPALITIES:
            if muni.lower() in candidate.lower():
                return muni

    # 6. Bare name anywhere in text (lower confidence)
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
                muni_rows = [r for r in rows if r.get("municipality_id")]

                # Fallback: if municipality_id isn't set on the bylaw rows
                # (load_obc.py doesn't populate that field), run a semantic
                # search scoped to titles matching the municipality name so
                # we get the most relevant chunks rather than insertion order.
                if not muni_rows:
                    title_rows = sb.rpc("match_sections_by_title", {
                        "query_embedding": embedding,
                        "title_filter":    municipality,
                        "match_count":     15,
                    }).execute().data or []
                    return title_rows

                return muni_rows

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


@app.get("/llms.txt", response_class=Response)
async def llms_txt():
    with open("llms.txt", "r") as f:
        content = f.read()
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.get("/llms-full.txt", response_class=Response)
async def llms_full_txt():
    with open("llms-full.txt", "r") as f:
        content = f.read()
    return Response(content=content, media_type="text/plain; charset=utf-8")


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

    # Tag new trial users in ActiveCampaign (fire-and-forget, never blocks the response)
    email      = user.get("email", "")
    first_name = email.split("@")[0] if email else ""
    try:
        _ac_tag_contact(email, first_name, "trial_active")
    except Exception as e:
        print(f"[AC] trial tagging failed silently: {e}")

    return info


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """
    Receives Stripe checkout.session.completed events.
    Tags the customer in ActiveCampaign based on which product they bought,
    which triggers the correct AC automation and exits the trial sequence.
    """
    import hmac
    import hashlib

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # ── Verify signature (skip if no secret configured — useful for testing) ──
    if STRIPE_WEBHOOK_SECRET:
        try:
            # Stripe signature format: t=<ts>,v1=<sig>
            parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p)}
            timestamp = parts.get("t", "")
            expected_sig = parts.get("v1", "")
            signed_payload = f"{timestamp}.{payload.decode()}"
            computed = hmac.new(
                STRIPE_WEBHOOK_SECRET.encode(),
                signed_payload.encode(),
                hashlib.sha256,
            ).hexdigest()  # type: ignore[attr-defined]
            if not hmac.compare_digest(computed, expected_sig):
                raise HTTPException(status_code=400, detail="Invalid Stripe signature")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Signature error: {e}")

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("type", "")

    # ── Only handle completed checkouts ───────────────────────────────────────
    if event_type != "checkout.session.completed":
        return {"received": True}

    session = event.get("data", {}).get("object", {})
    email   = (session.get("customer_details") or {}).get("email", "")
    name    = (session.get("customer_details") or {}).get("name", "") or ""
    first_name = name.split()[0] if name else (email.split("@")[0] if email else "")

    # Resolve product ID from line items embedded in the session metadata,
    # or fall back to the metadata field set at checkout creation time.
    product_id = (
        session.get("metadata", {}).get("product_id")          # set by Lovable at checkout
        or session.get("metadata", {}).get("stripe_product")
        or ""
    )

    # If metadata isn't set, pull from the first line item (works for one-time payments)
    if not product_id:
        items = (session.get("display_items") or
                 session.get("line_items", {}).get("data", []))
        if items:
            price = items[0].get("price") or items[0].get("plan") or {}
            product_id = price.get("product", "")

    print(f"[Stripe] checkout.session.completed — email={email} product={product_id}")

    if not email:
        return {"received": True}

    if product_id == STRIPE_SINGLE_PRODUCT:
        try:
            _ac_tag_contact(email, first_name, "purchased_single")
        except Exception as e:
            print(f"[AC] purchased_single tagging failed: {e}")

    elif product_id == STRIPE_UNLIMITED_PRODUCT:
        try:
            _ac_tag_contact(email, first_name, "upgraded_unlimited")
        except Exception as e:
            print(f"[AC] upgraded_unlimited tagging failed: {e}")

    else:
        print(f"[Stripe] Unknown product_id: {product_id} — no AC tag applied")

    return {"received": True}


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

    # Log what the frontend sent so we can verify in Railway logs
    print(f"[analyze] user={user['id'][:8]}… municipality='{req.municipality}' "
          f"files={len(req.files or [])} storage={len(req.storage_paths or [])} "
          f"msg_len={len(req.message)}")

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
            # Try to detect municipality from the message before kicking off search.
            # Docs aren't loaded yet, but the user's message often contains the address.
            early_municipality = req.municipality or extract_municipality([{"text": req.message}])
            if early_municipality:
                print(f"Municipality (early): {early_municipality}")
            else:
                print(f"Municipality: not detected early — will retry after doc load")
            loop = asyncio.get_event_loop()
            obc_future = loop.run_in_executor(None, search_obc, req.message, early_municipality)

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
            detected_municipality = early_municipality  # may already be set from message
            if not detected_municipality and doc_texts:
                detected_municipality = extract_municipality(doc_texts)
                if detected_municipality:
                    print(f"Municipality (from docs): {detected_municipality}")
                    yield f"data: {json.dumps({'status': f'Detected municipality: {detected_municipality}...'})}\n\n"
            if not detected_municipality:
                print(f"Municipality: not detected from message or docs")

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
            if detected_municipality and detected_municipality != early_municipality:
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
    import os
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether, PageBreak
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from datetime import datetime

    # ── Register custom fonts (Cardo + Libre Baskerville, same as the ebook) ──
    FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

    _registered: set = getattr(pdfmetrics, "_permitfix_registered", set())

    def _try_register(name: str, filename: str) -> bool:
        if name in _registered:
            return True
        path = os.path.join(FONTS_DIR, filename)
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                _registered.add(name)
                return True
            except Exception:
                pass
        return False

    cardo_ok = all([
        _try_register("Cardo",        "Cardo-Regular.ttf"),
        _try_register("Cardo-Bold",   "Cardo-Bold.ttf"),
        _try_register("Cardo-Italic", "Cardo-Italic.ttf"),
    ])
    libre_ok = all([
        _try_register("LibreBaskerville",      "LibreBaskerville-Regular.ttf"),
        _try_register("LibreBaskerville-Bold", "LibreBaskerville-Bold.ttf"),
    ])
    pdfmetrics._permitfix_registered = _registered  # type: ignore[attr-defined]

    if cardo_ok and "Cardo-family" not in _registered:
        pdfmetrics.registerFontFamily(
            "Cardo",
            normal="Cardo", bold="Cardo-Bold",
            italic="Cardo-Italic", boldItalic="Cardo-Italic",
        )
        _registered.add("Cardo-family")

    BODY_FONT    = "Cardo"             if cardo_ok else "Times-Roman"
    BODY_BOLD    = "Cardo-Bold"        if cardo_ok else "Times-Bold"
    BODY_ITALIC  = "Cardo-Italic"      if cardo_ok else "Times-Italic"
    HDG_BOLD     = "LibreBaskerville-Bold" if libre_ok else "Times-Bold"
    HDG_REG      = "LibreBaskerville"  if libre_ok else "Times-Roman"

    # ── Colour palette ────────────────────────────────────────────────────────
    BLACK      = colors.HexColor("#000000")
    DARK_GRAY  = colors.HexColor("#333333")
    MID_GRAY   = colors.HexColor("#666666")
    LIGHT_GRAY = colors.HexColor("#F5F5F5")
    RULE_COLOR = colors.HexColor("#DDDDDD")
    WHITE      = colors.white

    # Status colours — used only for finding accent bars
    RED    = colors.HexColor("#C0392B")
    AMBER  = colors.HexColor("#D4691E")
    GREEN  = colors.HexColor("#1E7E45")
    BLUE   = colors.HexColor("#2471A3")

    # ── Paragraph styles (matching ebook typography) ──────────────────────────
    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    sty = {
        # Cover / report title — LibreBaskerville-Bold 36pt
        "cover_title": S("cover_title",
                          fontName=HDG_BOLD, fontSize=36, leading=44,
                          textColor=BLACK, spaceAfter=10),
        # Cover subtitle — Cardo-Italic 16pt gray
        "cover_sub":   S("cover_sub",
                          fontName=BODY_ITALIC, fontSize=16, leading=22,
                          textColor=MID_GRAY, spaceAfter=6),
        # Cover meta — Cardo 11pt gray
        "cover_meta":  S("cover_meta",
                          fontName=BODY_FONT, fontSize=11, leading=16,
                          textColor=MID_GRAY, spaceAfter=4),

        # H1 — LibreBaskerville-Bold 24pt (major section, like chapter title)
        "h1": S("h1", fontName=HDG_BOLD, fontSize=24, leading=30,
                 textColor=BLACK, spaceBefore=28, spaceAfter=10),
        # H2 — LibreBaskerville-Bold 18pt (section heading)
        "h2": S("h2", fontName=HDG_BOLD, fontSize=18, leading=24,
                 textColor=BLACK, spaceBefore=20, spaceAfter=6),
        # H3 — Cardo-Regular 12pt gray (sub-section label, like ebook style)
        "h3": S("h3", fontName=BODY_FONT, fontSize=12, leading=16,
                 textColor=MID_GRAY, spaceBefore=14, spaceAfter=4),

        # Body — Cardo 12pt black
        "body": S("body", fontName=BODY_FONT, fontSize=12, leading=18,
                   textColor=BLACK, spaceAfter=6),
        # Bullet — Cardo 12pt, indented
        "bullet": S("bullet", fontName=BODY_FONT, fontSize=12, leading=18,
                     textColor=BLACK, leftIndent=20, firstLineIndent=0,
                     spaceBefore=2, spaceAfter=2),

        # Meta / caption — Cardo-Italic 9pt gray
        "meta": S("meta", fontName=BODY_ITALIC, fontSize=9, leading=13,
                   textColor=MID_GRAY, spaceAfter=6),

        # Table header — Cardo-Bold 10pt white
        "table_hdr":  S("th", fontName=BODY_BOLD, fontSize=10, leading=14,
                          textColor=WHITE, alignment=TA_LEFT),
        # Table cell — Cardo 10pt dark
        "table_cell": S("td", fontName=BODY_FONT, fontSize=10, leading=14,
                          textColor=DARK_GRAY, alignment=TA_LEFT),
    }

    # ── Helpers ───────────────────────────────────────────────────────────────
    def esc(text: str) -> str:
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

    def md_to_rl(text: str) -> str:
        """Markdown bold/italic → ReportLab XML tags using the correct fonts."""
        text = esc(text)
        text = re.sub(r"\*\*(.+?)\*\*",
                      rf"<font name='{BODY_BOLD}'><b>\1</b></font>", text)
        text = re.sub(r"\*(.+?)\*",
                      rf"<font name='{BODY_ITALIC}'><i>\1</i></font>", text)
        text = re.sub(r"__(.+?)__",
                      rf"<font name='{BODY_BOLD}'><b>\1</b></font>", text)
        text = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", text)
        for ch, rep in {
            "✅": "✓", "❌": "✗", "⚠️": "!", "⚠": "!",
            "ℹ️": "i", "🟡": "~", "—": "\u2014", "–": "\u2013",
            "\u00a0": " ",
        }.items():
            text = text.replace(ch, rep)
        return text

    def strip_md_header(line: str) -> str:
        return re.sub(r"^#{1,6}\s+", "", line).strip()

    def is_header(line: str):
        m = re.match(r"^(#{1,6})\s+", line)
        return (len(m.group(1)), strip_md_header(line)) if m else None

    def is_bullet_line(line: str) -> bool:
        return bool(re.match(r"^\s*[-*•]\s+", line))

    def bullet_text(line: str) -> str:
        return re.sub(r"^\s*[-*•]\s+", "", line).strip()

    def is_table_line(line: str) -> bool:
        return line.strip().startswith("|") and "|" in line[1:]

    def is_table_sep(line: str) -> bool:
        return bool(re.match(r"^\s*\|[-| :]+\|\s*$", line))

    def parse_table(lines: list) -> list:
        rows = []
        for line in lines:
            if is_table_sep(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        return rows

    def finding_block(title: str, body_lines: list, accent: colors.Color) -> list:
        """Render a finding as a left-accent card with ebook-style typography."""
        inner: list = []
        if title:
            inner.append(Paragraph(md_to_rl(title),
                                   ParagraphStyle("fh", fontName=BODY_BOLD, fontSize=12,
                                                  leading=16, textColor=BLACK,
                                                  spaceBefore=0, spaceAfter=4)))
        for line in body_lines:
            line = line.rstrip()
            if not line or re.match(r"^-{3,}$", line):
                continue
            if is_bullet_line(line):
                inner.append(Paragraph(
                    f"\u2022 {md_to_rl(bullet_text(line))}", sty["bullet"]))
            else:
                txt = md_to_rl(line)
                if txt.strip():
                    inner.append(Paragraph(txt, sty["body"]))

        if not inner:
            return []

        # Light tint derived from accent (very faint)
        card = Table([[inner]], colWidths=[5.7 * inch])
        card.setStyle(TableStyle([
            ("LINEBEFORE",    (0, 0), (0, -1), 2.5, accent),
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#FAFAFA")),
            ("LEFTPADDING",   (0, 0), (-1, -1), 16),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return [card, Spacer(1, 10)]

    # ── Page header/footer — minimal, like the ebook ──────────────────────────
    buf = io.BytesIO()
    generated = datetime.now().strftime("%B %d, %Y")

    HDR_FONT_REG  = BODY_FONT
    HDR_FONT_ITAL = BODY_ITALIC

    def on_page(canvas, doc):
        W, H = letter
        margin = 0.85 * inch

        # Running header: "PermitFix AI  |  <italic>Project Name</italic>"
        # Mimics ebook: "Elite Fitness |  CEO Cyclist" in 8pt gray
        canvas.setFillColor(MID_GRAY)
        canvas.setFont(HDR_FONT_REG, 8)
        brand = "PermitFix AI  |  "
        canvas.drawString(margin, H - 0.55 * inch, brand)
        brand_w = canvas.stringWidth(brand, HDR_FONT_REG, 8)
        canvas.setFont(HDR_FONT_ITAL, 8)
        proj_display = project_name if project_name else "Compliance Report"
        canvas.drawString(margin + brand_w, H - 0.55 * inch, proj_display)

        # Thin rule beneath header
        canvas.setStrokeColor(RULE_COLOR)
        canvas.setLineWidth(0.4)
        canvas.line(margin, H - 0.62 * inch, W - margin, H - 0.62 * inch)

        # Page number — just the number, bottom center (like the ebook)
        canvas.setFillColor(BLACK)
        canvas.setFont(BODY_FONT, 12)
        canvas.drawCentredString(W / 2, 0.5 * inch, str(doc.page))

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.85 * inch,
    )

    # ── Cover / title block ───────────────────────────────────────────────────
    story: list = []
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph("Compliance Review Report", sty["cover_title"]))
    story.append(Paragraph(
        "Ontario Building Code Analysis &amp; Permit Compliance",
        sty["cover_sub"],
    ))
    story.append(Spacer(1, 0.15 * inch))
    if project_name:
        story.append(Paragraph(f"Project: {esc(project_name)}", sty["cover_meta"]))
    story.append(Paragraph(f"Generated: {generated}", sty["cover_meta"]))
    if doc_names:
        story.append(Paragraph(
            "Documents reviewed: " + esc(", ".join(doc_names)),
            sty["cover_meta"],
        ))
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=RULE_COLOR, spaceAfter=16))

    # ── Parse and render all assistant messages ───────────────────────────────
    for msg in messages:
        if msg.role != "assistant":
            continue

        lines = msg.content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            # Horizontal rules → thin spacer
            if re.match(r"^-{3,}$", line.strip()):
                story.append(Spacer(1, 6))
                i += 1
                continue

            # Markdown headers
            hinfo = is_header(line)
            if hinfo:
                level, title_text = hinfo
                if level == 1:
                    story.append(HRFlowable(width="100%", thickness=0.4,
                                             color=RULE_COLOR,
                                             spaceBefore=10, spaceAfter=6))
                    story.append(Paragraph(md_to_rl(title_text), sty["h1"]))
                elif level == 2:
                    story.append(Paragraph(md_to_rl(title_text), sty["h2"]))
                elif level >= 3:
                    tl = title_text.lower()
                    if any(k in tl for k in ["critical", "action required", "c1","c2","c3","c4","c5"]):
                        accent = RED
                    elif any(k in tl for k in ["important", "warning", "i1","i2","i3","i4","i5"]):
                        accent = AMBER
                    elif any(k in tl for k in ["compliant", "passed", "approved", "meets"]):
                        accent = GREEN
                    else:
                        accent = BLUE

                    body: list = []
                    i += 1
                    while i < len(lines):
                        if is_header(lines[i]):
                            break
                        body.append(lines[i])
                        i += 1
                    story.extend(finding_block(title_text, body, accent))
                    continue
                i += 1
                continue

            # Markdown table
            if is_table_line(line):
                tbl_lines: list = []
                while i < len(lines) and (is_table_line(lines[i]) or is_table_sep(lines[i])):
                    tbl_lines.append(lines[i])
                    i += 1
                rows = parse_table(tbl_lines)
                if rows:
                    col_count = max(len(r) for r in rows)
                    rows = [r + [""] * (col_count - len(r)) for r in rows]
                    avail = 5.7 * inch
                    col_w = [avail / col_count] * col_count
                    tbl_data = []
                    for ri, row in enumerate(rows):
                        style_s = sty["table_hdr"] if ri == 0 else sty["table_cell"]
                        tbl_data.append([Paragraph(md_to_rl(c), style_s) for c in row])
                    tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
                    tbl.setStyle(TableStyle([
                        ("BACKGROUND",     (0, 0), (-1, 0),  DARK_GRAY),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
                        ("GRID",           (0, 0), (-1, -1), 0.4, RULE_COLOR),
                        ("TOPPADDING",     (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING",  (0, 0), (-1, -1), 6),
                        ("LEFTPADDING",    (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING",   (0, 0), (-1, -1), 8),
                        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
                    ]))
                    story.append(tbl)
                    story.append(Spacer(1, 12))
                continue

            # Bullet
            if is_bullet_line(line):
                story.append(Paragraph(
                    f"\u2022 {md_to_rl(bullet_text(line))}", sty["bullet"]))
                i += 1
                continue

            # Plain paragraph
            txt = md_to_rl(line)
            if txt.strip():
                story.append(Paragraph(txt, sty["body"]))
            elif story:
                story.append(Spacer(1, 5))
            i += 1

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
