import os
import io
import json
import uuid
import base64
import shutil
import hashlib
from datetime import datetime

import anthropic
import streamlit as st
import pdfplumber
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

MODEL      = "claude-opus-4-6"
api_key    = os.getenv("ANTHROPIC_API_KEY", "")
APP_DIR    = os.path.dirname(__file__)

DATA_DIR   = "/data" if os.path.isdir("/data") else APP_DIR
MASTER_DIR = os.path.join(DATA_DIR, "master_uploads")
os.makedirs(MASTER_DIR, exist_ok=True)

SUPABASE_URL      = os.getenv("SUPABASE_URL", "https://mqqbdkmjfameufouhewa.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1xcWJka21qZmFtZXVmb3VoZXdhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUyMDc1MTIsImV4cCI6MjA5MDc4MzUxMn0.omtDKGLnqg8AE3xb_AvDLx7enjUAhFdXM-QhJV_xn4w")
LOVABLE_URL       = os.getenv("LOVABLE_URL", "https://your-permitfix-app.lovable.app")

IMAGE_TYPES = ["png", "jpg", "jpeg", "webp", "gif"]
MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "webp": "image/webp", "gif": "image/gif",
}
STATUSES = ["🔵 In Review", "🟡 Corrections Needed", "🟢 Approved", "⚫ On Hold"]

st.set_page_config(
    page_title="Ontario AI Permit PreChecker",
    page_icon="🏗️",
    layout="wide",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stHeader"]          { display: none !important; }
[data-testid="InputInstructions"] { display: none !important; }
[data-testid="stSidebar"]         { display: none !important; }

.block-container {
    padding: 1.8rem 2.5rem !important;
    max-width: 900px !important;
}

/* App header */
.app-header {
    background: linear-gradient(135deg, #0f2942 0%, #1b5e40 100%);
    color: white;
    padding: 1.4rem 1.8rem;
    border-radius: 14px;
    margin-bottom: 1.8rem;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
}
.app-title    { font-size: 1.55rem; font-weight: 700; margin: 0; letter-spacing: -0.3px; }
.app-subtitle { font-size: 0.78rem; opacity: 0.65; margin-top: 0.25rem; }
.app-byline   { font-size: 0.72rem; opacity: 0.55; text-align: right; }

/* Section labels */
.section-label {
    color: #64748b;
    font-size: 0.74rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 0.6rem;
}

/* Project cards */
.proj-card {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 1rem 1.25rem;
    margin-bottom: 0.55rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    transition: box-shadow 0.15s;
}
.proj-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.09); }
.proj-name    { font-size: 1rem; font-weight: 600; color: #1e293b; margin: 0; }
.proj-meta    { font-size: 0.78rem; color: #94a3b8; margin-top: 0.15rem; }

/* Buttons */
.stButton > button {
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 0.875rem !important;
    transition: all 0.15s !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #0f2942, #1b5e40) !important;
    border: none !important;
    color: white !important;
}
.stButton > button[kind="primary"]:hover {
    opacity: 0.92 !important;
    transform: translateY(-1px) !important;
}

/* File pills */
.file-pills { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.75rem; }
.file-pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    background: #f1f5f9; border: 1px solid #e2e8f0;
    border-radius: 20px; padding: 0.25rem 0.65rem;
    font-size: 0.78rem; color: #475569;
}

/* Attach expander */
[data-testid="stExpander"] > div:first-child {
    border-radius: 10px !important;
    border: 1.5px dashed #cbd5e1 !important;
    background: #f8fafc !important;
}

/* Chat input */
[data-testid="stChatInput"] textarea {
    border-radius: 12px !important;
    border: 1.5px solid #e2e8f0 !important;
    font-size: 0.9rem !important;
}
[data-testid="stChatInput"] textarea:focus {
    border-color: #1b5e40 !important;
    box-shadow: 0 0 0 3px rgba(27,94,64,0.12) !important;
}

/* Chat messages */
[data-testid="stChatMessage"] { padding: 0.4rem 0 !important; }

/* Divider */
hr { border-color: #e2e8f0 !important; margin: 1.2rem 0 !important; }

/* Footer */
.footer {
    text-align: center;
    color: #94a3b8;
    font-size: 0.74rem;
    margin-top: 2.5rem;
    padding-top: 0.8rem;
    border-top: 1px solid #e2e8f0;
}

/* User bar */
.user-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.78rem;
    color: #64748b;
    margin-bottom: 0.5rem;
    justify-content: flex-end;
}
.plan-badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 20px;
    font-size: 0.7rem;
    font-weight: 600;
}
.plan-monthly { background: #dcfce7; color: #166534; }
.plan-credits { background: #dbeafe; color: #1e40af; }
.plan-none    { background: #fee2e2; color: #991b1b; }
</style>
""", unsafe_allow_html=True)


# ── Supabase (one client per browser session) ─────────────────────────────────

def get_sb():
    if "sb_client" not in st.session_state:
        st.session_state.sb_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return st.session_state.sb_client


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _save_tokens(session):
    """Persist Supabase tokens in st.session_state so restore_session() can use them."""
    if session:
        st.session_state.sb_access_token  = session.access_token
        st.session_state.sb_refresh_token = session.refresh_token

def restore_session():
    """On every page load, try to restore a previous Supabase session from stored tokens."""
    if st.session_state.get("sb_user"):
        return  # already logged in this session
    access  = st.session_state.get("sb_access_token")
    refresh = st.session_state.get("sb_refresh_token")
    if not access:
        return
    try:
        res = get_sb().auth.set_session(access, refresh)
        if res.user:
            st.session_state.sb_user = res.user
            # Store refreshed tokens (Supabase may have issued new ones)
            _save_tokens(res.session)
            st.session_state.pop("subscription", None)
    except Exception:
        # Tokens expired or invalid — clear them so login screen shows
        st.session_state.pop("sb_access_token",  None)
        st.session_state.pop("sb_refresh_token", None)

def do_login(email: str, password: str) -> bool:
    try:
        res = get_sb().auth.sign_in_with_password({"email": email.strip(), "password": password})
        st.session_state.sb_user = res.user
        _save_tokens(res.session)
        st.session_state.pop("subscription", None)
        return True
    except Exception as e:
        msg = str(e)
        if "Invalid login" in msg or "invalid_grant" in msg or "credentials" in msg.lower():
            st.error("Invalid email or password.")
        else:
            st.error(f"Login error: {msg}")
        return False


def do_logout():
    try:
        get_sb().auth.sign_out()
    except Exception:
        pass
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def send_password_reset(email: str):
    try:
        get_sb().auth.reset_password_email(email.strip())
        return True
    except Exception:
        return False


# ── Subscription check ────────────────────────────────────────────────────────

def get_subscription() -> dict:
    """Cached per-session. Returns dict with status, plan_type, submissions_remaining."""
    if "subscription" in st.session_state:
        return st.session_state.subscription

    user = st.session_state.get("sb_user")
    if not user:
        return {"status": "none"}

    try:
        res = get_sb().table("stripe_customers").select("*").eq("user_id", user.id).execute()
        if res.data:
            d   = res.data[0]
            sub = {
                "status":                d.get("subscription_status", "inactive"),
                "plan_type":             d.get("plan_type", "per_submission"),
                "submissions_remaining": d.get("submissions_remaining", 0),
            }
        else:
            sub = {"status": "none", "plan_type": None, "submissions_remaining": 0}
    except Exception as e:
        sub = {"status": "error", "error": str(e)}

    st.session_state.subscription = sub
    return sub


def has_access() -> bool:
    sub = get_subscription()
    if sub["status"] == "active":                                         return True
    if sub.get("plan_type") == "per_submission" and \
       sub.get("submissions_remaining", 0) > 0:                           return True
    return False


def deduct_submission():
    """Subtract one credit when creating a project on a per_submission plan."""
    sub = get_subscription()
    if sub.get("plan_type") != "per_submission":
        return
    user      = st.session_state.sb_user
    new_count = max(0, sub.get("submissions_remaining", 1) - 1)
    try:
        get_sb().table("stripe_customers") \
            .update({"submissions_remaining": new_count}) \
            .eq("user_id", user.id) \
            .execute()
        st.session_state.subscription["submissions_remaining"] = new_count
    except Exception:
        pass


# ── Login view ────────────────────────────────────────────────────────────────

def show_login_view():
    _, col, _ = st.columns([1, 1.4, 1])
    with col:
        st.markdown("""
        <div class="app-header" style="margin-bottom:1.5rem">
          <div>
            <div class="app-title">🏗️ PermitFix AI</div>
            <div class="app-subtitle">Ontario Building Code compliance — Beta</div>
          </div>
          <div class="app-byline">Brought to you by 77Inc</div>
        </div>
        """, unsafe_allow_html=True)

        # ── Primary CTA: new users ────────────────────────────────────────────
        st.markdown(
            "<p style='font-size:0.95rem;color:#1e293b;font-weight:600;"
            "margin-bottom:0.3rem'>Don't have an account?</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<p style='font-size:0.83rem;color:#64748b;margin-bottom:0.75rem'>"
            "Get instant access for <strong>$20/submission</strong> or "
            "<strong>$77/month</strong> unlimited. Your login credentials "
            "are emailed to you after payment.</p>",
            unsafe_allow_html=True,
        )
        st.link_button(
            "🏗️  Get Access — Starting at $20",
            LOVABLE_URL,
            use_container_width=True,
            type="primary",
        )

        st.markdown(
            "<p style='text-align:center;font-size:0.78rem;color:#94a3b8;"
            "margin:0.8rem 0'>── Already have an account? Sign in below ──</p>",
            unsafe_allow_html=True,
        )

        # ── Secondary: returning customers ────────────────────────────────────
        with st.container(border=True):
            email    = st.text_input("Email", key="login_email",
                                     placeholder="you@firm.com")
            password = st.text_input("Password", key="login_password",
                                     type="password", placeholder="••••••••")

            if st.button("Sign In", use_container_width=True):
                if email and password:
                    if do_login(email, password):
                        st.session_state.view = "home"
                        st.rerun()
                else:
                    st.error("Please enter your email and password.")

            if st.button("Forgot password?", use_container_width=True,
                         key="forgot_btn"):
                if email:
                    if send_password_reset(email):
                        st.success("Reset link sent — check your inbox.")
                    else:
                        st.error("Could not send reset email.")
                else:
                    st.warning("Enter your email above first.")


# ── Paywall view ──────────────────────────────────────────────────────────────

def show_paywall_view():
    sub        = get_subscription()
    user       = st.session_state.sb_user
    user_email = user.email if user else ""

    _, col, _ = st.columns([1, 1.4, 1])
    with col:
        st.markdown("""
        <div class="app-header" style="margin-bottom:1.5rem">
          <div>
            <div class="app-title">🏗️ PermitFix AI</div>
            <div class="app-subtitle">Ontario Building Code compliance — Beta</div>
          </div>
          <div class="app-byline">Brought to you by 77Inc</div>
        </div>
        """, unsafe_allow_html=True)

        # Status message
        if sub.get("plan_type") == "per_submission" and \
           sub.get("submissions_remaining", 0) == 0:
            st.warning(
                f"**{user_email}** — you have **0 submissions remaining**. "
                "Purchase another to continue.",
                icon="⚠️",
            )
        elif sub["status"] in ("inactive", "none", "cancelled"):
            st.info(
                f"**{user_email}** — your account doesn't have an active plan yet.",
                icon="ℹ️",
            )
        else:
            st.warning(
                f"**{user_email}** — subscription status: **{sub['status']}**",
                icon="⚠️",
            )

        st.markdown(
            "<p style='font-size:0.85rem;color:#64748b;margin:0.5rem 0 0.25rem'>"
            "Choose a plan to get access:</p>",
            unsafe_allow_html=True,
        )

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                "<div style='border:1px solid #e2e8f0;border-radius:10px;"
                "padding:0.9rem 1rem;background:white'>"
                "<p style='font-size:1rem;font-weight:700;margin:0'>$20</p>"
                "<p style='font-size:0.78rem;color:#64748b;margin:0.1rem 0 0'>Per submission · One project</p>"
                "</div>",
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                "<div style='border:2px solid #1b5e40;border-radius:10px;"
                "padding:0.9rem 1rem;background:white'>"
                "<p style='font-size:1rem;font-weight:700;margin:0'>$77<span style='font-size:0.7rem;font-weight:400;color:#64748b'>/mo</span></p>"
                "<p style='font-size:0.78rem;color:#64748b;margin:0.1rem 0 0'>Unlimited · Best for firms</p>"
                "</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)
        st.link_button("Get Access →", LOVABLE_URL,
                       type="primary", use_container_width=True)

        if st.button("Log out", key="paywall_logout"):
            do_logout()
            st.rerun()


# ── User namespace (Supabase user ID → filesystem namespace) ──────────────────

def get_user_id() -> str:
    user = st.session_state.get("sb_user")
    if user:
        return user.id.replace("-", "")[:16]
    return str(uuid.uuid4()).replace("-", "")[:16]


def get_projects_dir() -> str:
    d = os.path.join(DATA_DIR, "projects", get_user_id())
    os.makedirs(d, exist_ok=True)
    return d


# ── Persistence helpers ───────────────────────────────────────────────────────

def project_dir(pid):      return os.path.join(get_projects_dir(), pid)
def meta_path(pid):        return os.path.join(project_dir(pid), "meta.json")
def chat_path(pid):        return os.path.join(project_dir(pid), "chat.json")
def files_dir(pid):        return os.path.join(project_dir(pid), "files")

def load_all_projects():
    pdir     = get_projects_dir()
    projects = []
    for pid in os.listdir(pdir):
        mp = meta_path(pid)
        if os.path.isfile(mp):
            with open(mp) as f:
                projects.append(json.load(f))
    projects.sort(key=lambda p: p.get("modified", ""), reverse=True)
    return projects

def save_meta(meta):
    os.makedirs(project_dir(meta["id"]), exist_ok=True)
    with open(meta_path(meta["id"]), "w") as f:
        json.dump(meta, f, indent=2)

def load_meta(pid):
    with open(meta_path(pid)) as f:
        return json.load(f)

def load_chat(pid):
    cp = chat_path(pid)
    if os.path.isfile(cp):
        with open(cp) as f:
            return json.load(f)
    return []

def save_chat(pid, messages):
    with open(chat_path(pid), "w") as f:
        json.dump(messages, f, indent=2)

def load_project_files(pid):
    fd           = files_dir(pid)
    docs, images = [], []
    if not os.path.isdir(fd):
        return docs, images
    for fname in sorted(os.listdir(fd)):
        fpath = os.path.join(fd, fname)
        ext   = fname.rsplit(".", 1)[-1].lower()
        raw   = open(fpath, "rb").read()
        if ext == "pdf":
            try:
                text, pages = extract_pdf_text(raw)
                thumb       = pdf_first_page_b64(raw)
                if text.strip():
                    docs.append({"name": fname, "text": text,
                                 "page_count": pages, "thumb_b64": thumb})
            except Exception:
                pass
        elif ext in IMAGE_TYPES:
            b64 = base64.standard_b64encode(raw).decode()
            images.append({"name": fname,
                            "media_type": MEDIA_TYPE_MAP.get(ext, "image/png"),
                            "b64": b64})
    return docs, images

def save_file_to_project(pid, fname, raw_bytes):
    fd = files_dir(pid)
    os.makedirs(fd, exist_ok=True)
    with open(os.path.join(fd, fname), "wb") as f:
        f.write(raw_bytes)
    _save_to_master(fname, raw_bytes, pid)

def _save_to_master(fname, raw_bytes, pid):
    """Archive every uploaded file centrally for future model improvement."""
    user       = st.session_state.get("sb_user")
    user_tag   = user.email.replace("@", "_at_").replace(".", "_") if user else "anon"
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest       = os.path.join(MASTER_DIR, f"{ts}_{user_tag}_{pid}_{fname}")
    with open(dest, "wb") as f:
        f.write(raw_bytes)

def delete_project(pid):
    shutil.rmtree(project_dir(pid), ignore_errors=True)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── PDF helpers ───────────────────────────────────────────────────────────────

def extract_pdf_text(file_bytes: bytes):
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
    return "\n\n".join(pages), page_count

def pdf_first_page_b64(file_bytes: bytes):
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if pdf.pages:
                img = pdf.pages[0].to_image(resolution=120)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return base64.standard_b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return None


# ── Claude helpers ────────────────────────────────────────────────────────────

def build_system_prompt(docs):
    base = (
        "You are an expert Ontario building permit assistant specializing in the "
        "2024 Ontario Building Code (OBC), local municipal bylaws, and residential "
        "construction compliance. Analyze drawings and documents for issues with "
        "dimensions, setbacks, lot coverage, building height, fire separations, "
        "egress, accessibility, structural elements, and grading. "
        "Provide plain-English explanations, actionable fixes, and precise OBC "
        "section citations. Be thorough, conservative, and flag anything uncertain."
    )
    if not docs:
        return [{"type": "text", "text": base}]
    kb = "\n\n".join(f"=== {d['name']} ===\n{d['text']}" for d in docs)
    return [
        {"type": "text", "text": base},
        {"type": "text",
         "text": f"<documents>\n{kb}\n</documents>",
         "cache_control": {"type": "ephemeral"}},
    ]

def build_api_messages(chat_messages, images):
    if not images:
        return chat_messages
    blocks = [
        {"type": "image",
         "source": {"type": "base64",
                    "media_type": i["media_type"],
                    "data": i["b64"]}}
        for i in images
    ]
    blocks.append({"type": "text",
                   "text": f"The user uploaded {len(images)} drawing(s). "
                           "Analyze carefully for Ontario Building Code compliance."})
    return [
        {"role": "user",      "content": blocks},
        {"role": "assistant", "content":
            f"I've reviewed all {len(images)} drawing(s) and am ready to analyze them."},
    ] + chat_messages

def generate_report_pdf(meta, messages, docs, images) -> bytes:
    """Build a formatted PDF compliance report and return as bytes."""
    from fpdf import FPDF
    import re

    def safe(text: str) -> str:
        """Drop any character Helvetica can't render (emoji, non-Latin-1)."""
        return text.encode("latin-1", errors="ignore").decode("latin-1")

    def strip_status_emoji(text: str) -> str:
        """Replace status emoji prefixes with readable text equivalents."""
        replacements = {
            "\U0001f535": "[In Review]",
            "\U0001f7e1": "[Corrections Needed]",
            "\U0001f7e2": "[Approved]",
            "\u26ab":     "[On Hold]",
        }
        for emoji, label in replacements.items():
            text = text.replace(emoji, label)
        return safe(text)

    def clean(text: str) -> str:
        """Strip markdown and non-Latin-1 characters for PDF rendering."""
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*",   r"\1", text)
        text = re.sub(r"__(.+?)__",   r"\1", text)
        text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
        text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
        return safe(text)

    def is_header(line: str) -> bool:
        return re.match(r"^#{1,6}\s+", line) is not None

    def header_text(line: str) -> str:
        return re.sub(r"^#{1,6}\s+", "", line).strip()

    def is_bullet(line: str) -> bool:
        return re.match(r"^\s*[-*•]\s+", line) is not None

    def bullet_text(line: str) -> str:
        return re.sub(r"^\s*[-*•]\s+", "", line).strip()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ── Cover header bar ─────────────────────────────────────────────────────
    pdf.set_fill_color(15, 41, 66)          # dark blue
    pdf.rect(0, 0, 220, 38, "F")
    pdf.set_fill_color(27, 94, 64)          # green accent strip
    pdf.rect(0, 36, 220, 4, "F")

    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_xy(12, 8)
    pdf.cell(0, 9, "PermitFix AI", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(12, 19)
    pdf.cell(0, 7, "Ontario Building Code Compliance Report", ln=True)
    pdf.set_xy(12, 28)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 6, f"Generated {datetime.now().strftime('%B %d, %Y  %H:%M')}", ln=True)

    pdf.set_text_color(30, 41, 59)
    pdf.set_xy(10, 48)

    # ── Project info box ──────────────────────────────────────────────────────
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(10, 48, 190, 34, "FD")

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(14, 52)
    pdf.cell(0, 7, safe(meta.get("name", "Untitled Project")), ln=True)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 116, 139)
    pdf.set_xy(14, 60)
    if meta.get("address"):
        pdf.cell(60, 5, f"Address: {safe(meta['address'])}")
    pdf.cell(60, 5, f"Status: {strip_status_emoji(meta.get('status', '-'))}")
    pdf.cell(0,  5, f"Created: {safe(meta.get('created', '-'))}", ln=True)

    n_docs = len(docs)
    n_imgs = len(images)
    pdf.set_xy(14, 67)
    pdf.cell(0, 5,
             f"Documents reviewed: {n_docs} PDF{'s' if n_docs != 1 else ''}  ·  "
             f"{n_imgs} image{'s' if n_imgs != 1 else ''}", ln=True)

    pdf.set_text_color(30, 41, 59)
    pdf.ln(5)

    # ── Prominent "how to use this report" warning ────────────────────────────
    pdf.set_fill_color(239, 246, 255)       # light blue background
    pdf.set_draw_color(59, 130, 246)        # blue border
    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(30, 64, 175)
    pdf.cell(190, 6, "  [!]  HOW TO USE THIS REPORT -- PLEASE READ BEFORE PROCEEDING",
             ln=True, fill=True, border=1)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(30, 41, 59)
    pdf.set_x(10)
    pdf.multi_cell(190, 5,
        "This report identifies potential issues with your permit drawings based on the Ontario Building "
        "Code. It tells you WHAT may need to change -- it does NOT make those changes for you.\n\n"
        "You must review each flagged item and update your drawings, specifications, and documents "
        "accordingly before resubmitting. Do not submit this report to a building department -- "
        "it is a working reference for your own revisions.\n\n"
        "AI systems can and do make mistakes. Items flagged here may not all be actual violations, "
        "and there may be issues not caught by this review. Always have your final drawings reviewed "
        "by a licensed architect, engineer, or qualified building professional before submission.",
        fill=True, border=1)
    pdf.ln(5)

    # ── Files reviewed ────────────────────────────────────────────────────────
    if docs or images:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_fill_color(15, 41, 66)
        pdf.set_text_color(255, 255, 255)
        pdf.set_x(10)
        pdf.cell(190, 7, "  Files Reviewed", ln=True, fill=True)
        pdf.set_text_color(30, 41, 59)
        pdf.set_font("Helvetica", "", 9)
        pdf.ln(2)

        for d in docs:
            pdf.set_x(14)
            pages = d.get("page_count", "?")
            pdf.cell(0, 5, f"-  {safe(d['name'])}  ({pages} page{'s' if pages != 1 else ''})", ln=True)
        for i in images:
            pdf.set_x(14)
            pdf.cell(0, 5, f"-  {safe(i['name'])}  (image)", ln=True)
        pdf.ln(4)

    # ── Compliance analysis ───────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(15, 41, 66)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x(10)
    pdf.cell(190, 7, "  Compliance Analysis", ln=True, fill=True)
    pdf.set_text_color(30, 41, 59)
    pdf.ln(3)

    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    user_msgs      = [m for m in messages if m["role"] == "user"]

    if not assistant_msgs:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_x(10)
        pdf.cell(0, 6, "No analysis generated yet.", ln=True)
    else:
        for idx, msg in enumerate(assistant_msgs):
            # Optionally show the user question that prompted this response
            if idx < len(user_msgs):
                pdf.set_fill_color(241, 245, 249)
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(100, 116, 139)
                q = clean(user_msgs[idx]["content"])[:200]
                pdf.set_x(10)
                pdf.multi_cell(190, 4, f"Q: {q}", fill=True)
                pdf.ln(1)

            # Render AI response line by line
            pdf.set_text_color(30, 41, 59)
            for line in msg["content"].split("\n"):
                stripped = line.strip()
                if not stripped:
                    pdf.ln(2)
                    continue
                if is_header(stripped):
                    pdf.set_font("Helvetica", "B", 10)
                    pdf.set_x(10)
                    pdf.multi_cell(190, 5, safe(header_text(stripped)))
                    pdf.ln(1)
                elif is_bullet(stripped):
                    pdf.set_font("Helvetica", "", 9)
                    pdf.set_x(14)
                    pdf.multi_cell(186, 4, f"-  {clean(bullet_text(stripped))}")
                else:
                    pdf.set_font("Helvetica", "", 9)
                    pdf.set_x(10)
                    pdf.multi_cell(190, 4, clean(stripped))

            if idx < len(assistant_msgs) - 1:
                pdf.ln(3)
                pdf.set_draw_color(226, 232, 240)
                pdf.set_x(10)
                pdf.cell(190, 0, "", border="T", ln=True)
                pdf.ln(3)

    # ── Disclaimer ────────────────────────────────────────────────────────────
    pdf.ln(6)
    pdf.set_fill_color(254, 242, 242)
    pdf.set_draw_color(252, 165, 165)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(153, 27, 27)
    pdf.cell(190, 5, "  Legal Disclaimer - AI-Generated Content",
             ln=True, fill=True, border=1)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(30, 41, 59)
    pdf.set_x(10)
    pdf.multi_cell(190, 4,
        "This document was produced by an artificial intelligence system (PermitFix AI) and is provided "
        "for informational purposes only. It is a preliminary screening tool to help identify areas of "
        "your drawings that may require attention - it is not a code compliance certificate, "
        "professional opinion, or permit approval.\n\n"
        "AI can make errors. The analysis in this report may contain inaccuracies, omissions, or "
        "misinterpretations of the Ontario Building Code. Items flagged may not be actual violations, "
        "and real violations may not have been identified. You are responsible for verifying every "
        "item independently.\n\n"
        "This report does not replace the judgment of a licensed architect, professional engineer, "
        "or qualified building official. All revisions to your drawings must be made by a qualified "
        "professional and re-reviewed before submission to any authority having jurisdiction.",
        fill=True, border=1)

    # ── Footer ────────────────────────────────────────────────────────────────
    pdf.set_text_color(148, 163, 184)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_xy(10, 285)
    pdf.cell(0, 4, "PermitFix AI · Brought to you by 77Inc · Ontario Building Code Compliance Tool",
             align="C")

    return bytes(pdf.output())


def stream_response(client, messages, system):
    full, placeholder = "", st.empty()
    with client.messages.stream(model=MODEL, max_tokens=16000,
                                system=system, messages=messages) as stream:
        for ev in stream:
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                full += ev.delta.text
                placeholder.markdown(full + "▌")
    placeholder.markdown(full)
    return full


# ── Session state defaults ────────────────────────────────────────────────────

_defaults = dict(
    view="login",
    current_pid=None,
    docs=[],
    images=[],
    messages=[],
    creating=False,
    sb_user=None,
)
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

def open_project(pid):
    st.session_state.current_pid = pid
    st.session_state.view        = "project"
    st.session_state.messages    = load_chat(pid)
    docs, images                 = load_project_files(pid)
    st.session_state.docs        = docs
    st.session_state.images      = images

def go_home():
    if st.session_state.current_pid:
        save_chat(st.session_state.current_pid, st.session_state.messages)
    st.session_state.view        = "home"
    st.session_state.current_pid = None
    st.session_state.docs        = []
    st.session_state.images      = []
    st.session_state.messages    = []
    st.session_state.creating    = False


# ═════════════════════════════════════════════════════════════════════════════
# AUTH GATE
# ═════════════════════════════════════════════════════════════════════════════

# ── 1. Restore session from stored tokens (survives rerun, not full refresh) ──
restore_session()

# ── 2. SSO token handoff from Lovable ────────────────────────────────────────
# Lovable redirects here with ?access_token=...&refresh_token=... after login.
if not st.session_state.sb_user:
    params = st.query_params
    if "access_token" in params:
        try:
            sb  = get_sb()
            res = sb.auth.set_session(
                params.get("access_token"),
                params.get("refresh_token", ""),
            )
            if res.user:
                st.session_state.sb_user = res.user
                _save_tokens(res.session)          # persist so refresh survives
                st.session_state.pop("subscription", None)
                st.query_params.clear()
                st.rerun()
        except Exception:
            st.query_params.clear()

if not st.session_state.sb_user:
    show_login_view()
    st.stop()

if not has_access():
    show_paywall_view()
    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED — shared header
# ═════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div class="app-header">
  <div>
    <div class="app-title">🏗️ Ontario AI Permit PreChecker</div>
    <div class="app-subtitle">Ontario Building Code compliance analysis — Beta</div>
  </div>
  <div class="app-byline">Brought to you by 77Inc</div>
</div>
""", unsafe_allow_html=True)

# User bar: email, plan badge, logout
sub        = get_subscription()
user_email = st.session_state.sb_user.email

if sub["status"] == "active":
    badge_cls  = "plan-monthly"
    badge_text = "Unlimited · Monthly"
elif sub.get("plan_type") == "per_submission":
    n          = sub.get("submissions_remaining", 0)
    badge_cls  = "plan-credits"
    badge_text = f"{n} submission{'s' if n != 1 else ''} remaining"
else:
    badge_cls  = "plan-none"
    badge_text = "No active plan"

col_email, col_badge, col_logout = st.columns([5, 2, 1])
with col_email:
    st.caption(f"Signed in as **{user_email}**")
with col_badge:
    st.markdown(
        f'<span class="plan-badge {badge_cls}">{badge_text}</span>',
        unsafe_allow_html=True,
    )
with col_logout:
    if st.button("Log out", key="header_logout"):
        do_logout()
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# HOME VIEW
# ═════════════════════════════════════════════════════════════════════════════

if st.session_state.view == "home":

    col_lbl, col_btn = st.columns([5, 1])
    with col_lbl:
        st.markdown('<div class="section-label">Your Projects</div>',
                    unsafe_allow_html=True)
    with col_btn:
        if st.button("＋ New", type="primary", use_container_width=True):
            st.session_state.creating = True

    # New project form
    if st.session_state.creating:
        with st.container(border=True):
            st.markdown("**New Project**")

            # Per-submission: warn if 0 credits left (shouldn't get here, but guard)
            sub = get_subscription()
            if sub.get("plan_type") == "per_submission":
                n = sub.get("submissions_remaining", 0)
                st.info(
                    f"This will use **1 of your {n} remaining submission{'s' if n != 1 else ''}**.",
                    icon="💳",
                )

            new_name    = st.text_input("Project name *",
                                        placeholder="e.g. Smith Residence Addition")
            new_address = st.text_input("Address (optional)",
                                        placeholder="123 Main St, Toronto")
            new_status  = st.selectbox("Initial status", STATUSES)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Create Project", type="primary",
                             use_container_width=True):
                    if new_name.strip():
                        # Guard: re-check credits before creating
                        sub = get_subscription()
                        if sub.get("plan_type") == "per_submission" and \
                           sub.get("submissions_remaining", 0) <= 0:
                            st.error("No submissions remaining. Please purchase more.")
                            st.stop()

                        deduct_submission()

                        pid  = str(uuid.uuid4())[:8]
                        meta = dict(id=pid, name=new_name.strip(),
                                    address=new_address.strip(),
                                    status=new_status,
                                    created=now_str(), modified=now_str())
                        save_meta(meta)
                        st.session_state.creating = False
                        open_project(pid)
                        st.rerun()
                    else:
                        st.error("Please enter a project name.")
            with c2:
                if st.button("Cancel", use_container_width=True):
                    st.session_state.creating = False
                    st.rerun()

    # Project list
    projects = load_all_projects()
    if not projects:
        st.markdown(
            "<p style='color:#94a3b8;font-size:0.9rem;margin-top:0.5rem'>"
            "No projects yet — click <strong>＋ New</strong> to get started.</p>",
            unsafe_allow_html=True,
        )
    else:
        for p in projects:
            fd      = files_dir(p["id"])
            n_files = len(os.listdir(fd)) if os.path.isdir(fd) else 0
            col_info, col_open, col_del = st.columns([5, 1, 1])
            with col_info:
                st.markdown(
                    f'<div class="proj-card">'
                    f'<p class="proj-name">{p["name"]}</p>'
                    f'<p class="proj-meta">'
                    f'{p.get("status","")} &nbsp;·&nbsp; '
                    f'{n_files} file(s) &nbsp;·&nbsp; '
                    f'Modified {p.get("modified","—")}'
                    + (f'<br>📍 {p["address"]}' if p.get("address") else "")
                    + '</p></div>',
                    unsafe_allow_html=True,
                )
            with col_open:
                st.write("")
                if st.button("Open", key=f"open_{p['id']}",
                             type="primary", use_container_width=True):
                    open_project(p["id"])
                    st.rerun()
            with col_del:
                st.write("")
                if st.button("🗑️", key=f"del_{p['id']}",
                             help="Delete project", use_container_width=True):
                    delete_project(p["id"])
                    st.rerun()

    if not api_key or api_key == "your_api_key_here":
        st.error("⚠️ Set ANTHROPIC_API_KEY in your HF Space secrets to enable AI analysis.")

    st.markdown(
        '<div class="footer">Ontario AI Permit PreChecker · Brought to you by 77Inc</div>',
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT VIEW
# ═════════════════════════════════════════════════════════════════════════════

else:
    pid  = st.session_state.current_pid
    meta = load_meta(pid)

    col_back, col_title, col_status, col_pdf = st.columns([1, 4, 2, 1.5])
    with col_back:
        if st.button("← Projects"):
            go_home()
            st.rerun()
    with col_title:
        st.markdown(f"### {meta['name']}")
        if meta.get("address"):
            st.caption(f"📍 {meta['address']}")
    with col_status:
        new_status = st.selectbox(
            "Status", STATUSES,
            index=STATUSES.index(meta.get("status", STATUSES[0])),
            label_visibility="collapsed",
        )
        if new_status != meta.get("status"):
            meta["status"]   = new_status
            meta["modified"] = now_str()
            save_meta(meta)
    with col_pdf:
        has_analysis = any(m["role"] == "assistant" for m in st.session_state.messages)
        # Cache key: includes message count so stale PDFs are never shown
        # after new analysis messages arrive
        pdf_cache_key = f"pdf_{pid}_{len(st.session_state.messages)}"

        if not has_analysis:
            # No analysis yet — greyed out
            st.button("⬇ Report PDF", disabled=True, use_container_width=True,
                      help="Run an analysis first to generate a report.")

        elif pdf_cache_key in st.session_state:
            # PDF already built for this exact conversation state — show download
            safe_name = meta["name"].replace(" ", "_")[:40]
            st.download_button(
                label="⬇ Download PDF",
                data=st.session_state[pdf_cache_key],
                file_name=f"PermitFix_{safe_name}.pdf",
                mime="application/pdf",
                use_container_width=True,
                type="primary",
            )

        else:
            # PDF not yet built — show generate button
            if st.button("📄 Build Report", type="primary", use_container_width=True,
                         help="Generate a downloadable PDF of this analysis."):
                with st.spinner("Building PDF report…"):
                    try:
                        st.session_state[pdf_cache_key] = generate_report_pdf(
                            meta,
                            st.session_state.messages,
                            st.session_state.docs,
                            st.session_state.images,
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not generate PDF: {e}")

    st.divider()

    # ── File attach area ──────────────────────────────────────────────────────
    existing_names = (
        {d["name"] for d in st.session_state.docs} |
        {i["name"] for i in st.session_state.images}
    )

    with st.expander(
        f"📎  Attach files"
        + (f"  ·  {len(existing_names)} attached" if existing_names else "  (PDFs & drawings)"),
        expanded=not bool(existing_names),
    ):
        up1, up2 = st.columns(2)
        with up1:
            pdf_files = st.file_uploader(
                "PDF documents", type="pdf",
                accept_multiple_files=True, key=f"pdf_{pid}",
            )
            if pdf_files:
                for f in pdf_files:
                    if f.name not in existing_names:
                        with st.spinner(f"Reading {f.name}…"):
                            try:
                                raw   = f.read()
                                text, pages = extract_pdf_text(raw)
                                thumb = pdf_first_page_b64(raw)
                                if text.strip():
                                    save_file_to_project(pid, f.name, raw)
                                    st.session_state.docs.append(
                                        {"name": f.name, "text": text,
                                         "page_count": pages, "thumb_b64": thumb})
                                    existing_names.add(f.name)
                                    meta["modified"] = now_str()
                                    save_meta(meta)
                                    st.success(f"✅ {f.name}")
                                else:
                                    st.warning(f"⚠️ {f.name} — no extractable text")
                            except Exception as e:
                                st.error(f"❌ {f.name}: {e}")

        with up2:
            img_files = st.file_uploader(
                "Drawings / images", type=IMAGE_TYPES,
                accept_multiple_files=True, key=f"img_{pid}",
            )
            if img_files:
                for f in img_files:
                    if f.name not in existing_names:
                        with st.spinner(f"Loading {f.name}…"):
                            try:
                                ext = f.name.rsplit(".", 1)[-1].lower()
                                raw = f.read()
                                b64 = base64.standard_b64encode(raw).decode()
                                save_file_to_project(pid, f.name, raw)
                                st.session_state.images.append(
                                    {"name": f.name,
                                     "media_type": MEDIA_TYPE_MAP.get(ext, "image/png"),
                                     "b64": b64})
                                existing_names.add(f.name)
                                meta["modified"] = now_str()
                                save_meta(meta)
                                st.success(f"✅ {f.name}")
                            except Exception as e:
                                st.error(f"❌ {f.name}: {e}")

    # File pills
    if existing_names:
        pills = "".join(
            f'<span class="file-pill">📄 {n}</span>'
            for n in sorted(existing_names)
        )
        st.markdown(f'<div class="file-pills">{pills}</div>', unsafe_allow_html=True)

    # ── Chat ──────────────────────────────────────────────────────────────────
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if not st.session_state.messages:
        if not existing_names:
            st.info(
                "Attach permit drawings or PDFs above, then ask me anything about "
                "Ontario Building Code compliance, setbacks, fire separations, and more."
            )
        else:
            st.info("Files attached. Ask me to check compliance, flag issues, or cite code sections.")

    if st.session_state.messages:
        _, col_clr = st.columns([6, 1])
        with col_clr:
            if st.button("Clear chat", use_container_width=True):
                st.session_state.messages = []
                save_chat(pid, [])
                st.rerun()

    if prompt := st.chat_input("Ask about compliance, setbacks, OBC code citations…"):
        if not api_key or api_key == "your_api_key_here":
            st.error("Set ANTHROPIC_API_KEY in your HF Space secrets first.")
            st.stop()

        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        client   = anthropic.Anthropic(api_key=api_key)
        system   = build_system_prompt(st.session_state.docs)
        api_msgs = build_api_messages(st.session_state.messages,
                                      st.session_state.images)

        with st.chat_message("assistant"):
            reply = stream_response(client, api_msgs, system)

        st.session_state.messages.append({"role": "assistant", "content": reply})
        save_chat(pid, st.session_state.messages)
        meta["modified"] = now_str()
        save_meta(meta)
        st.rerun()

    st.markdown(
        '<div class="footer">Ontario AI Permit PreChecker · Brought to you by 77Inc</div>',
        unsafe_allow_html=True,
    )
