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

load_dotenv()

MODEL      = "claude-opus-4-6"
api_key    = os.getenv("ANTHROPIC_API_KEY", "")
APP_DIR    = os.path.dirname(__file__)

# Use /data (HF persistent bucket) when available, else fall back to app dir
DATA_DIR   = "/data" if os.path.isdir("/data") else APP_DIR
MASTER_DIR = os.path.join(DATA_DIR, "master_uploads")
os.makedirs(MASTER_DIR, exist_ok=True)

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

# ── Modern CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stHeader"]        { display: none !important; }
[data-testid="InputInstructions"] { display: none !important; }
[data-testid="stSidebar"]       { display: none !important; }

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

/* Back button looks like a link */
button[data-testid="baseButton-secondary"]:has(span:contains("←")) {
    background: transparent !important;
    border: none !important;
    color: #64748b !important;
    padding-left: 0 !important;
}
</style>
""", unsafe_allow_html=True)


# ── IP-based user namespace ───────────────────────────────────────────────────

def get_user_namespace() -> str:
    """Derive a stable, per-device namespace from the client IP address."""
    if "_user_ns" in st.session_state:
        return st.session_state._user_ns

    ip = ""
    try:
        h = st.context.headers
        ip = (
            h.get("X-Forwarded-For") or
            h.get("X-Real-Ip") or
            h.get("Remote-Addr") or ""
        )
        ip = ip.split(",")[0].strip()
    except Exception:
        pass

    if ip and ip not in ("127.0.0.1", "::1", ""):
        ns = hashlib.sha256(ip.encode()).hexdigest()[:16]
    else:
        # Local dev fallback — stable for this browser session
        ns = str(uuid.uuid4()).replace("-", "")[:16]

    st.session_state._user_ns = ns
    return ns


def get_projects_dir() -> str:
    ns = get_user_namespace()
    d = os.path.join(DATA_DIR, "projects", ns)
    os.makedirs(d, exist_ok=True)
    return d


# ── Persistence helpers ───────────────────────────────────────────────────────

def project_dir(pid):
    return os.path.join(get_projects_dir(), pid)

def meta_path(pid):
    return os.path.join(project_dir(pid), "meta.json")

def chat_path(pid):
    return os.path.join(project_dir(pid), "chat.json")

def files_dir(pid):
    return os.path.join(project_dir(pid), "files")

def load_all_projects():
    pdir = get_projects_dir()
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
    fd = files_dir(pid)
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
                thumb = pdf_first_page_b64(raw)
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
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(MASTER_DIR, f"{ts}_{pid}_{fname}")
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


# ── Session state ─────────────────────────────────────────────────────────────

_defaults = dict(
    view="home",
    current_pid=None,
    docs=[],
    images=[],
    messages=[],
    creating=False,
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


# ── Shared header ─────────────────────────────────────────────────────────────

st.markdown("""
<div class="app-header">
  <div>
    <div class="app-title">🏗️ Ontario AI Permit PreChecker</div>
    <div class="app-subtitle">Ontario Building Code compliance analysis — Beta</div>
  </div>
  <div class="app-byline">Brought to you by 77Inc</div>
</div>
""", unsafe_allow_html=True)


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
        st.error("⚠️ Set ANTHROPIC_API_KEY in your .env file to enable AI analysis.")

    st.markdown(
        '<div class="footer">Ontario AI Permit PreChecker · Brought to you by 77Inc</div>',
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT VIEW — chat only, no inner dashboard tab
# ═════════════════════════════════════════════════════════════════════════════

else:
    pid  = st.session_state.current_pid
    meta = load_meta(pid)

    # Top bar
    col_back, col_title, col_status = st.columns([1, 4, 2])
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

    st.divider()

    # ── File attach area (above chat) ─────────────────────────────────────────
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
        st.markdown(
            f'<div class="file-pills">{pills}</div>',
            unsafe_allow_html=True,
        )

    # ── Chat ──────────────────────────────────────────────────────────────────
    # Render history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Empty-state hint
    if not st.session_state.messages:
        if not existing_names:
            st.info(
                "Attach permit drawings or PDFs above, then ask me anything about "
                "Ontario Building Code compliance, setbacks, fire separations, and more."
            )
        else:
            st.info("Files attached. Ask me to check compliance, flag issues, or cite code sections.")

    # Clear button (compact, right-aligned)
    if st.session_state.messages:
        _, col_clr = st.columns([6, 1])
        with col_clr:
            if st.button("Clear chat", use_container_width=True):
                st.session_state.messages = []
                save_chat(pid, [])
                st.rerun()

    # Chat input
    if prompt := st.chat_input("Ask about compliance, setbacks, OBC code citations…"):
        if not api_key or api_key == "your_api_key_here":
            st.error("Set ANTHROPIC_API_KEY in your .env file first.")
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
