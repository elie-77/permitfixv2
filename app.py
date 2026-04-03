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

# /data = HF Spaces  |  /app/data = Railway volume  |  fallback = local
DATA_DIR   = (
    "/data"       if os.path.isdir("/data")      else
    "/app/data"   if os.path.isdir("/app/data")  else
    os.path.join(APP_DIR, "local_data")
)
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

from PIL import Image as _PILImage
_icon = _PILImage.open(os.path.join(APP_DIR, "icon.png"))

def _b64_img(filename):
    with open(os.path.join(APP_DIR, filename), "rb") as _f:
        return base64.b64encode(_f.read()).decode()

_LOGO_B64 = _b64_img("logo.png")

st.set_page_config(
    page_title="Ontario AI Permit Check",
    page_icon=_icon,
    layout="wide",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": None,
    }
)

# ── Magic link hash fragment handler ─────────────────────────────────────────
# Supabase magic links put tokens in URL hash (#access_token=...).
# Streamlit can't read hashes server-side. components.html() runs in an iframe
# that CAN execute JS. It reads the parent hash, saves to localStorage
# (survives browser refresh), then reloads with query params Streamlit CAN read.
import streamlit.components.v1 as components
components.html("""
<script>
// ── Inject hide-chrome CSS into parent as early as possible ──────────────────
(function() {
    var s = document.createElement('style');
    s.textContent = [
        '[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],',
        '[data-testid="stStatusWidget"],[data-testid="stToolbarActions"],',
        '[data-testid="stBaseButton-header"],button[kind="header"],',
        '#MainMenu,footer,.stDeployButton { display:none !important; }'
    ].join('');
    window.parent.document.head.appendChild(s);
})();
</script>
<script>
(function() {
    var hash = window.parent.location.hash;
    if (!hash || hash.length < 2) return;
    var params = new URLSearchParams(hash.replace('#', ''));
    var access = params.get('access_token');
    var error  = params.get('error');
    var url    = new URL(window.parent.location.href);
    url.hash   = '';
    if (access) {
        try {
            window.parent.localStorage.setItem('pf_access',  access);
            window.parent.localStorage.setItem('pf_refresh', params.get('refresh_token') || '');
        } catch(e) {}
        url.searchParams.set('access_token',  access);
        url.searchParams.set('refresh_token', params.get('refresh_token') || '');
        window.parent.location.replace(url.toString());
    } else if (error) {
        url.searchParams.set('auth_error', params.get('error_description') || error);
        window.parent.location.replace(url.toString());
    }
})();
</script>
""", height=0)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Hide all Streamlit chrome ── */
[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],
[data-testid="stStatusWidget"],[data-testid="stToolbarActions"],
[data-testid="stBaseButton-header"],[data-testid="InputInstructions"],
[data-testid="stSidebar"],[data-testid="stAppRunningMan"],
#MainMenu,footer,.stDeployButton,button[kind="header"],
iframe[title="streamlit_analytics"] { display:none !important; }
.stSpinner > div { border-top-color: #1a5e40 !important; }

/* ── Layout ── */
.block-container {
    padding: 0 2.5rem 2rem !important;
    max-width: 1100px !important;
}
@media (max-width: 768px) {
    .block-container { padding: 0 1rem 1.5rem !important; }
}

/* ── Top nav ── */
.pf-nav {
    display: flex;
    align-items: center;
    padding: 1rem 0 0.9rem;
    border-bottom: 1px solid #f0f0f0;
    margin-bottom: 0;
}
.pf-nav img { height: 48px; width: auto; display: block; }
.pf-nav-right {
    display: flex;
    align-items: center;
    gap: 0.75rem;
}

/* ── Plan badges ── */
.plan-badge {
    display: inline-block;
    padding: 0.2rem 0.65rem;
    border-radius: 20px;
    font-size: 0.7rem;
    font-weight: 600;
}
.plan-monthly { background: #dcfce7; color: #15803d; }
.plan-credits { background: #dbeafe; color: #1d4ed8; }
.plan-none    { background: #fee2e2; color: #dc2626; }

/* ── Page sections ── */
.pf-section-title {
    font-size: 1.3rem;
    font-weight: 700;
    color: #111827;
    margin: 1.75rem 0 1rem;
    letter-spacing: -0.3px;
}
.section-label {
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #9ca3af;
    margin-bottom: 0.75rem;
}

/* ── Project cards ── */
.proj-card {
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 1.1rem 1.4rem;
    margin-bottom: 0.6rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    transition: box-shadow 0.15s, border-color 0.15s;
}
.proj-card:hover {
    box-shadow: 0 6px 20px rgba(0,0,0,0.08);
    border-color: #1a5e40;
}
.proj-name { font-size: 0.97rem; font-weight: 600; color: #111827; margin: 0 0 0.25rem; }
.proj-meta { font-size: 0.78rem; color: #6b7280; margin: 0; line-height: 1.6; }
.proj-addr { font-size: 0.78rem; color: #9ca3af; margin-top: 0.2rem; }

/* ── Project view ── */
.pf-project-title {
    font-size: 1.3rem;
    font-weight: 700;
    color: #111827;
    margin: 0 0 0.15rem;
    letter-spacing: -0.3px;
}
.pf-project-addr { font-size: 0.82rem; color: #9ca3af; }

/* ── Buttons ── */
.stButton > button {
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 0.875rem !important;
    transition: all 0.15s !important;
    border: 1.5px solid #e5e7eb !important;
}
.stButton > button[kind="primary"] {
    background: #1a5e40 !important;
    border-color: #1a5e40 !important;
    color: #fff !important;
}
.stButton > button[kind="primary"]:hover {
    background: #155233 !important;
    border-color: #155233 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(26,94,64,0.25) !important;
}
.stButton > button:not([kind="primary"]):hover {
    border-color: #1a5e40 !important;
    color: #1a5e40 !important;
}

/* ── File pills ── */
.file-pills { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.75rem; }
.file-pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    background: #f9fafb; border: 1px solid #e5e7eb;
    border-radius: 20px; padding: 0.22rem 0.6rem;
    font-size: 0.77rem; color: #6b7280;
}

/* ── Attach expander ── */
[data-testid="stExpander"] > div:first-child {
    border-radius: 10px !important;
    border: 1.5px dashed #d1d5db !important;
    background: #f9fafb !important;
}

/* ── Chat ── */
[data-testid="stChatInput"] textarea {
    border-radius: 12px !important;
    border: 1.5px solid #e5e7eb !important;
    font-size: 0.9rem !important;
}
[data-testid="stChatInput"] textarea:focus {
    border-color: #1a5e40 !important;
    box-shadow: 0 0 0 3px rgba(26,94,64,0.1) !important;
}
[data-testid="stChatMessage"] { padding: 0.35rem 0 !important; }

/* ── Dividers ── */
hr { border-color: #f3f4f6 !important; margin: 1.25rem 0 !important; }

/* ── Empty state ── */
.pf-empty {
    text-align: center;
    padding: 3rem 2rem;
    background: #fafafa;
    border: 1.5px dashed #e5e7eb;
    border-radius: 16px;
    color: #9ca3af;
    font-size: 0.9rem;
    margin-top: 0.5rem;
}
.pf-empty strong { color: #6b7280; }

/* ── Login card ── */
.login-card {
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 20px;
    padding: 2rem 2rem 2.25rem;
    box-shadow: 0 4px 24px rgba(0,0,0,0.06);
    margin-top: 0.5rem;
}

/* ── Footer ── */
.footer {
    text-align: center;
    color: #d1d5db;
    font-size: 0.72rem;
    margin-top: 3rem;
    padding-top: 1rem;
    border-top: 1px solid #f3f4f6;
}
</style>
""", unsafe_allow_html=True)


# ── Supabase (one client per browser session) ─────────────────────────────────

def get_sb():
    if "sb_client" not in st.session_state:
        st.session_state.sb_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return st.session_state.sb_client


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _save_tokens(session):
    """Write Supabase tokens to session_state. localStorage is updated by JS component."""
    if not session:
        return
    st.session_state.sb_access_token  = session.access_token
    st.session_state.sb_refresh_token = session.refresh_token


def restore_session():
    """No-op: session_state survives reruns within a tab.
    Browser refresh is handled by the localStorage→query_params JS bridge."""
    pass

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
    # Signal the next render to clear localStorage via JS
    st.session_state["_clear_ls"] = True


def send_password_reset(email: str):
    try:
        get_sb().auth.reset_password_email(email.strip())
        return True
    except Exception:
        return False

def send_magic_link(email: str) -> bool:
    try:
        get_sb().auth.sign_in_with_otp({
            "email": email,
            "options": {"email_redirect_to": "https://app.permitfix.ca"}
        })
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
    st.markdown("<div style='height:2rem'></div>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown(
            f'<div style="text-align:center;margin-bottom:1.5rem">'
            f'<img src="data:image/png;base64,{_LOGO_B64}" '
            f'style="height:40px;width:auto"></div>',
            unsafe_allow_html=True,
        )
        st.markdown("<div class='login-card'>", unsafe_allow_html=True)

        auth_error = st.query_params.get("auth_error", "")
        if auth_error:
            st.query_params.clear()
            st.error("Your login link has expired or is no longer valid.", icon="🔒")
            st.link_button(
                "Get a new login link →",
                LOVABLE_URL + "/login",
                use_container_width=True,
                type="primary",
            )
        else:
            st.markdown(
                "<p style='font-size:1.05rem;font-weight:700;color:#111827;"
                "margin:0 0 0.3rem'>Welcome back</p>"
                "<p style='font-size:0.83rem;color:#6b7280;margin:0 0 1.25rem'>"
                "Sign in with the magic link from your email, or request a new one below.</p>",
                unsafe_allow_html=True,
            )
            st.link_button(
                "✉️  Sign in at permitfix.ca",
                LOVABLE_URL + "/login",
                use_container_width=True,
                type="primary",
            )
            st.markdown(
                "<div style='text-align:center;margin:1rem 0 0.75rem;"
                "font-size:0.78rem;color:#9ca3af'>— or —</div>",
                unsafe_allow_html=True,
            )
            st.link_button(
                "Get Access — Starting at $20",
                LOVABLE_URL,
                use_container_width=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown(
            "<p style='text-align:center;font-size:0.72rem;color:#d1d5db;margin-top:1rem'>"
            "Ontario Building Code compliance — Beta · PermitFix by 77Inc</p>",
            unsafe_allow_html=True,
        )


# ── Paywall view ──────────────────────────────────────────────────────────────

def show_paywall_view():
    sub        = get_subscription()
    user       = st.session_state.sb_user
    user_email = user.email if user else ""

    st.markdown("<div style='height:2rem'></div>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.4, 1])
    with col:
        st.markdown(
            f'<div style="text-align:center;margin-bottom:1.5rem">'
            f'<img src="data:image/png;base64,{_LOGO_B64}" '
            f'style="height:40px;width:auto"></div>',
            unsafe_allow_html=True,
        )
        st.markdown("<div class='login-card'>", unsafe_allow_html=True)

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

        st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
        if st.button("Log out", key="paywall_logout", use_container_width=True):
            do_logout()
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


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
    """Build a colour-coded PDF compliance report and return as bytes."""
    from fpdf import FPDF
    import re

    # ── Text helpers ──────────────────────────────────────────────────────────
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

    # ── Compliance classifier ─────────────────────────────────────────────────
    CRITICAL = [
        "non-compliant", "not compliant", "does not comply", "does not meet",
        "violation", "violates", "action required", "must be corrected",
        "fails to", "not permitted", "exceeds maximum", "below minimum",
        "insufficient", "deficient", "missing required", "inadequate",
        "not acceptable", "not allowed",
    ]
    GOOD = [
        "compliant", "meets requirement", "satisfies", "no issue",
        "no violation", "conforms", "within the required", "no deficien",
        "acceptable", "adequate", "passes", "appears to meet",
    ]
    WARN = [
        "review", "verify", "unclear", "consider", "may not", "recommend",
        "suggest", "should ensure", "confirm", "cannot verify", "unable to",
        "potential", "could", "needs clarification",
    ]

    PALETTE = {
        # status: (header_fill_rgb, content_bg_rgb, badge_text)
        "good":     ((22, 101, 52),   (240, 253, 244), "COMPLIANT"),
        "warning":  ((146, 64, 14),   (255, 251, 235), "REVIEW REQUIRED"),
        "critical": ((153, 27, 27),   (254, 242, 242), "ACTION REQUIRED"),
        "neutral":  ((30, 58, 138),   (239, 246, 255), "NOTE"),
    }

    def classify(header_str: str, body_str: str) -> str:
        t = (header_str + " " + body_str[:600]).lower()
        if any(k in t for k in CRITICAL): return "critical"
        if any(k in t for k in GOOD):     return "good"
        if any(k in t for k in WARN):     return "warning"
        return "neutral"

    def parse_sections(text: str):
        """Return list of (header|None, [body lines])."""
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

    # ── Section renderer ──────────────────────────────────────────────────────
    def render_section(pdf, title, body_lines, status):
        (hr, hg, hb), (br, bg, bb), badge = PALETTE[status]

        # Coloured header bar
        pdf.set_fill_color(hr, hg, hb)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_x(10)
        label = f"  {badge}"
        if title:
            label += f"  |  {safe(title)}"
        pdf.cell(190, 7, label, fill=True, ln=True)

        # Light-coloured content area
        pdf.set_fill_color(br, bg, bb)
        pdf.set_text_color(30, 41, 59)

        has_content = False
        for line in body_lines:
            s = line.strip()
            if not s:
                if has_content:
                    pdf.ln(1.5)
                continue
            has_content = True
            if is_bullet(s):
                pdf.set_font("Helvetica", "", 8.5)
                pdf.set_x(16)
                pdf.multi_cell(184, 4.5, f"-  {bullet_body(s)}", fill=True)
            else:
                # Bold inline: lines fully wrapped in **...**
                c = clean(s)
                if s.startswith("**") and s.endswith("**") and len(s) > 4:
                    pdf.set_font("Helvetica", "B", 8.5)
                else:
                    pdf.set_font("Helvetica", "", 8.5)
                pdf.set_x(13)
                pdf.multi_cell(187, 4.5, c, fill=True)

        pdf.ln(4)

    # ── Build PDF ─────────────────────────────────────────────────────────────
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # HEADER BANNER
    pdf.set_fill_color(15, 41, 66)
    pdf.rect(0, 0, 220, 50, "F")
    pdf.set_fill_color(27, 94, 64)
    pdf.rect(0, 47, 220, 5, "F")

    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_xy(12, 7)
    pdf.cell(0, 11, "PermitFix AI", ln=True)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(180, 210, 255)
    pdf.set_xy(12, 20)
    pdf.cell(0, 6, "Ontario Building Code Compliance Report", ln=True)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(12, 29)
    pdf.cell(0, 7, safe(meta.get("name", "Untitled Project")), ln=True)

    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(160, 200, 255)
    pdf.set_xy(12, 40)
    pdf.cell(0, 5,
             f"Generated {datetime.now().strftime('%B %d, %Y  at  %I:%M %p')}",
             ln=True)

    pdf.set_text_color(30, 41, 59)
    pdf.set_xy(10, 60)

    # FILES REVIEWED (compact single line, no box)
    n_docs = len(docs)
    n_imgs = len(images)
    if docs or images:
        parts = []
        if n_docs: parts.append(f"{n_docs} PDF document{'s' if n_docs > 1 else ''}")
        if n_imgs: parts.append(f"{n_imgs} drawing{'s' if n_imgs > 1 else ''}")
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(100, 116, 139)
        pdf.set_x(10)
        pdf.cell(0, 5, "Documents reviewed:  " + "   |   ".join(parts), ln=True)
        pdf.set_font("Helvetica", "", 7.5)
        for d in docs:
            pdf.set_x(16)
            pages = d.get("page_count", "?")
            pdf.cell(0, 4,
                     f"-  {safe(d['name'])}  ({pages} pg{'s' if pages != 1 else ''})",
                     ln=True)
        for i in images:
            pdf.set_x(16)
            pdf.cell(0, 4, f"-  {safe(i['name'])}", ln=True)
        pdf.ln(3)

    # COLOUR LEGEND
    pdf.set_text_color(30, 41, 59)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_x(10)
    pdf.cell(0, 5, "HOW TO READ THIS REPORT:", ln=True)
    pdf.ln(1)

    legend_items = [
        ((22, 101, 52),  "COMPLIANT          Meets Ontario Building Code requirements"),
        ((146, 64, 14),  "REVIEW REQUIRED    Needs clarification or minor attention"),
        ((153, 27, 27),  "ACTION REQUIRED    Must be corrected before submission"),
        ((30, 58, 138),  "NOTE               General information or recommendation"),
    ]
    for (r, g, b), txt in legend_items:
        pdf.set_fill_color(r, g, b)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_x(10)
        pdf.cell(130, 5.5, f"  {safe(txt)}", fill=True, ln=True)
        pdf.ln(0.8)

    pdf.ln(3)

    # HOW TO USE — brief, plain, no scary box
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(100, 116, 139)
    pdf.set_x(10)
    pdf.multi_cell(190, 4,
        "This report tells you WHAT may need to change in your drawings -- "
        "it does not make those changes for you. Review each flagged item, "
        "update your drawings accordingly, and confirm with your architect or "
        "engineer before submitting to the building department.")
    pdf.ln(5)

    # COMPLIANCE ANALYSIS SECTIONS
    pdf.set_fill_color(15, 41, 66)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_x(10)
    pdf.cell(190, 7, "  Compliance Analysis", fill=True, ln=True)
    pdf.ln(4)

    assistant_msgs = [m for m in messages if m["role"] == "assistant"]

    if not assistant_msgs:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 116, 139)
        pdf.set_x(10)
        pdf.cell(0, 6, "No analysis has been generated for this project yet.", ln=True)
    else:
        for msg in assistant_msgs:
            sections = parse_sections(msg["content"])
            for title, body_lines in sections:
                body_text = " ".join(l.strip() for l in body_lines if l.strip())
                if not title and not body_text.strip():
                    continue
                status = classify(title or "", body_text)
                render_section(pdf, title or "Summary", body_lines, status)

    # ABOUT THIS REPORT — warm, reassuring, light
    pdf.ln(4)
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(190, 6, "  About This Report", ln=True, fill=True, border=1)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(71, 85, 105)
    pdf.set_x(10)
    pdf.multi_cell(190, 4.5,
        "PermitFix AI is built to help you catch issues early and walk into the permit process "
        "better prepared. Think of this report as a knowledgeable second set of eyes -- one that "
        "has cross-referenced your drawings against the 2024 Ontario Building Code so you can "
        "focus on making the right corrections.\n\n"
        "Like any tool, it works best alongside professional expertise. Some findings may not "
        "apply to your specific site or project type, and there may be nuances a professional "
        "reviewer would catch that AI cannot. We always recommend reviewing these findings with "
        "your architect, engineer, or building official before finalising your submission.\n\n"
        "For internal use only. Not a permit, code compliance certificate, or professional opinion. "
        "PermitFix AI and 77Inc are not liable for decisions made based on this report.",
        fill=True, border=1)

    # FOOTER
    pdf.set_text_color(148, 163, 184)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_xy(10, 285)
    pdf.cell(0, 4,
             "PermitFix AI  |  Brought to you by 77Inc  |  Ontario Building Code Compliance Tool",
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

# ── 1. Clear localStorage on logout (JS runs client-side) ────────────────────
if st.session_state.get("_clear_ls"):
    st.session_state.pop("_clear_ls", None)
    components.html("""
    <script>
    try {
        window.parent.localStorage.removeItem('pf_access');
        window.parent.localStorage.removeItem('pf_refresh');
    } catch(e) {}
    </script>
    """, height=0)

# ── 2. Authenticate from query params (magic link OR localStorage restore) ────
if not st.session_state.get("sb_user"):
    _p = st.query_params
    if "access_token" in _p:
        _access  = _p.get("access_token")
        _refresh = _p.get("refresh_token", "")
        _authed  = False

        # Try set_session first (works when access token still valid)
        try:
            _res = get_sb().auth.set_session(_access, _refresh)
            if _res.user:
                st.session_state.sb_user = _res.user
                _save_tokens(_res.session)
                _authed = True
        except Exception:
            pass

        # Access token expired — try refreshing with the refresh token
        if not _authed and _refresh:
            try:
                _res = get_sb().auth.refresh_session(_refresh)
                if _res.user:
                    st.session_state.sb_user = _res.user
                    _save_tokens(_res.session)
                    _authed = True
            except Exception:
                pass

        if _authed:
            st.session_state.pop("subscription", None)
            # Don't clear query_params or call st.rerun() here —
            # both trigger extra render cycles. The logged-in JS component
            # below removes the tokens from the URL via replaceState (no reload).
            # Just fall through and render the app directly on this same pass.
        else:
            # Tokens are completely dead — clear localStorage to stop the loop
            components.html("""
            <script>
            try {
                window.parent.localStorage.removeItem('pf_access');
                window.parent.localStorage.removeItem('pf_refresh');
            } catch(e) {}
            </script>
            """, height=0)
            st.query_params.clear()

# ── 3. Not logged in — inject localStorage→URL restore JS, then show login ───
if not st.session_state.get("sb_user"):
    # This JS runs in the browser: reads localStorage tokens and redirects
    # with them as query params so Streamlit can read them (step 2 above).
    # Only fires if tokens exist AND no access_token is already in the URL
    # (preventing a redirect loop).
    components.html("""
    <script>
    (function() {
        var url = new URL(window.parent.location.href);
        if (url.searchParams.get('access_token')) return;
        try {
            var access  = window.parent.localStorage.getItem('pf_access');
            var refresh = window.parent.localStorage.getItem('pf_refresh');
            if (access) {
                url.searchParams.set('access_token',  access);
                url.searchParams.set('refresh_token', refresh || '');
                window.parent.location.replace(url.toString());
            }
        } catch(e) {}
    })();
    </script>
    """, height=0)
    show_login_view()
    st.stop()

# ── 4. Logged in — keep localStorage fresh + clean tokens from URL ────────────
_at = st.session_state.get("sb_access_token", "")
_rt = st.session_state.get("sb_refresh_token", "")
components.html(f"""
<script>
(function() {{
    try {{
        if ({repr(_at)}) window.parent.localStorage.setItem('pf_access',  {repr(_at)});
        if ({repr(_rt)}) window.parent.localStorage.setItem('pf_refresh', {repr(_rt)});
    }} catch(e) {{}}
    var url = new URL(window.parent.location.href);
    if (url.searchParams.has('access_token') || url.searchParams.has('refresh_token')) {{
        url.searchParams.delete('access_token');
        url.searchParams.delete('refresh_token');
        window.parent.history.replaceState({{}}, '', url.toString());
    }}
}})();
</script>
""", height=0)

if not has_access():
    show_paywall_view()
    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED — nav bar
# ═════════════════════════════════════════════════════════════════════════════

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

# Nav: logo left | badge + email + logout right
nav_logo, nav_gap, nav_user = st.columns([3, 4, 3])
with nav_logo:
    st.markdown(
        f'<div class="pf-nav">'
        f'<img src="data:image/png;base64,{_LOGO_B64}" alt="PermitFix">'
        f'</div>',
        unsafe_allow_html=True,
    )
with nav_user:
    st.markdown("<div style='height:0.6rem'></div>", unsafe_allow_html=True)
    nu1, nu2, nu3 = st.columns([3, 2.5, 1.5])
    with nu1:
        st.markdown(
            f"<div style='font-size:0.78rem;color:#6b7280;padding-top:0.45rem;"
            f"text-align:right;white-space:nowrap;overflow:hidden;"
            f"text-overflow:ellipsis'>{user_email}</div>",
            unsafe_allow_html=True,
        )
    with nu2:
        st.markdown(
            f"<div style='padding-top:0.35rem;text-align:center'>"
            f"<span class='plan-badge {badge_cls}'>{badge_text}</span></div>",
            unsafe_allow_html=True,
        )
    with nu3:
        if st.button("Log out", key="header_logout"):
            do_logout()
            st.rerun()

st.markdown("<div style='border-bottom:1px solid #f0f0f0;margin-bottom:0'></div>",
            unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# HOME VIEW
# ═════════════════════════════════════════════════════════════════════════════

if st.session_state.view == "home":

    col_lbl, col_btn = st.columns([5, 1])
    with col_lbl:
        st.markdown('<div class="pf-section-title">Your Projects</div>',
                    unsafe_allow_html=True)
    with col_btn:
        st.markdown("<div style='margin-top:1.75rem'></div>", unsafe_allow_html=True)
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
            "<div class='pf-empty'>"
            "No projects yet.<br><strong>Click ＋ New to start your first compliance check.</strong>"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        for p in projects:
            fd      = files_dir(p["id"])
            n_files = len(os.listdir(fd)) if os.path.isdir(fd) else 0
            status  = p.get("status", "")
            addr    = p.get("address", "")
            mod     = p.get("modified", "—")
            col_info, col_open, col_del = st.columns([6, 1, 0.6])
            with col_info:
                addr_html = (
                    f'<div class="proj-addr">📍 {addr}</div>' if addr else ""
                )
                st.markdown(
                    f'<div class="proj-card">'
                    f'<p class="proj-name">{p["name"]}</p>'
                    f'<p class="proj-meta">{status}'
                    f'&nbsp;&nbsp;·&nbsp;&nbsp;{n_files} file{"s" if n_files != 1 else ""}'
                    f'&nbsp;&nbsp;·&nbsp;&nbsp;Updated {mod}</p>'
                    f'{addr_html}</div>',
                    unsafe_allow_html=True,
                )
            with col_open:
                st.markdown("<div style='margin-top:0.85rem'></div>",
                            unsafe_allow_html=True)
                if st.button("Open →", key=f"open_{p['id']}",
                             type="primary", use_container_width=True):
                    open_project(p["id"])
                    st.rerun()
            with col_del:
                st.markdown("<div style='margin-top:0.85rem'></div>",
                            unsafe_allow_html=True)
                if st.button("🗑", key=f"del_{p['id']}",
                             help="Delete project", use_container_width=True):
                    delete_project(p["id"])
                    st.rerun()

    if not api_key or api_key == "your_api_key_here":
        st.error("Set ANTHROPIC_API_KEY in Railway environment variables.")

    st.markdown(
        '<div class="footer">PermitFix · Ontario AI Permit Check · Brought to you by 77Inc</div>',
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT VIEW
# ═════════════════════════════════════════════════════════════════════════════

else:
    pid = st.session_state.current_pid
    # Guard: pid is None or project no longer exists on disk
    if not pid:
        go_home()
        st.rerun()
    try:
        meta = load_meta(pid)
    except Exception:
        go_home()
        st.rerun()

    col_back, col_title, col_status, col_pdf = st.columns([1, 4, 2, 1.5])
    with col_back:
        st.markdown("<div style='margin-top:1.1rem'></div>", unsafe_allow_html=True)
        if st.button("← Projects"):
            go_home()
            st.rerun()
    with col_title:
        addr_line = (
            f'<div class="pf-project-addr">📍 {meta["address"]}</div>'
            if meta.get("address") else ""
        )
        st.markdown(
            f'<div style="padding-top:0.6rem">'
            f'<div class="pf-project-title">{meta["name"]}</div>'
            f'{addr_line}</div>',
            unsafe_allow_html=True,
        )
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
        '<div class="footer">PermitFix · Ontario AI Permit Check · Brought to you by 77Inc</div>',
        unsafe_allow_html=True,
    )
