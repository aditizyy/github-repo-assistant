"""
app.py — Streamlit chat interface with streaming support.
"""
import json
import time
import httpx
import streamlit as st

API_BASE = "http://localhost:8000/api"

st.set_page_config(
    page_title="GitHub Repo Assistant",
    page_icon="🤖",
    layout="wide",
)

# ── Session state initialisation ──────────────────────────────────────────────
if "active_repo_id"    not in st.session_state: st.session_state.active_repo_id    = None
if "active_session_id" not in st.session_state: st.session_state.active_session_id = None
if "messages"          not in st.session_state: st.session_state.messages           = []
if "sources"           not in st.session_state: st.session_state.sources            = []


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🤖 Repo Assistant")

    # Submit new repository
    st.header("Add Repository")
    url    = st.text_input("GitHub URL", placeholder="https://github.com/owner/repo")
    submit = st.button("Clone & Index", type="primary", use_container_width=True)

    if submit and url:
        with st.spinner("Submitting..."):
            try:
                resp = httpx.post(f"{API_BASE}/repos/", json={"github_url": url}, timeout=10)
                if resp.status_code == 202:
                    data = resp.json()
                    st.success(f"Submitted! ID: {data['id']}")
                    st.session_state.active_repo_id = data["id"]
                else:
                    st.error(resp.json().get("detail", "Error"))
            except httpx.ConnectError:
                st.error("Backend not running on port 8000")

    st.divider()

    # Repository selector
    st.header("Repositories")
    try:
        repos = httpx.get(f"{API_BASE}/repos/", timeout=5).json()
    except Exception:
        repos = []

    for repo in repos:
        emoji  = {"ready": "✅", "cloning": "🔄", "indexing": "📦",
                  "error": "❌", "pending": "⏳"}.get(repo["status"], "❓")
        label  = f"{emoji} {repo['repo_name']}"
        if st.button(label, key=f"sel_{repo['id']}", use_container_width=True):
            if repo["status"] == "ready":
                st.session_state.active_repo_id    = repo["id"]
                st.session_state.active_session_id = None
                st.session_state.messages          = []
                st.session_state.sources           = []
            else:
                st.warning(f"Status: {repo['status']}. Wait for indexing to complete.")

    # New chat button
    if st.session_state.active_repo_id:
        st.divider()
        if st.button("➕ New Chat", use_container_width=True):
            st.session_state.active_session_id = None
            st.session_state.messages          = []
            st.session_state.sources           = []

        # Past sessions
        st.subheader("Sessions")
        try:
            sessions = httpx.get(
                f"{API_BASE}/chat/{st.session_state.active_repo_id}/sessions",
                timeout=5,
            ).json()
            for s in sessions[:8]:
                if st.button(
                    f"💬 {s['session_name'][:30]}",
                    key=f"sess_{s['id']}",
                    use_container_width=True,
                ):
                    st.session_state.active_session_id = s["id"]
                    # Load history from API
                    hist = httpx.get(
                        f"{API_BASE}/chat/sessions/{s['id']}/history",
                        timeout=5,
                    ).json()
                    st.session_state.messages = hist["messages"]
        except Exception:
            pass


# ── Main chat area ────────────────────────────────────────────────────────────
if not st.session_state.active_repo_id:
    st.info("👈 Submit a GitHub repo URL in the sidebar to get started.")
    st.stop()

repo_resp = httpx.get(f"{API_BASE}/repos/{st.session_state.active_repo_id}", timeout=5)
repo      = repo_resp.json()
st.title(f"💬 {repo['repo_name']}")
st.caption(f"Status: {repo['status']} · {repo['file_count']} files · {repo['chunk_count']} chunks")

if repo["status"] != "ready":
    st.warning(f"Repository is {repo['status']}. Please wait...")
    time.sleep(3)
    st.rerun()

# Display chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Source citations display
if st.session_state.sources:
    with st.expander(f"📎 {len(st.session_state.sources)} source(s) used", expanded=False):
        for src in st.session_state.sources:
            st.markdown(
                f"**{src['file_path']}** "
                f"→ `{src.get('node_name') or 'chunk'}` "
                f"· lines {src['start_line']}–{src['end_line']} "
                f"· score **{src['score']:.0%}**"
            )

# Chat input
if question := st.chat_input("Ask about this codebase..."):
    # Add user message to UI immediately
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    # Stream assistant response
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        full_response        = ""
        sources              = []

        try:
            with httpx.stream(
                "POST",
                f"{API_BASE}/chat/{st.session_state.active_repo_id}/stream",
                json={
                    "question":   question,
                    "session_id": st.session_state.active_session_id,
                },
                timeout=60,
            ) as stream_resp:
                for line in stream_resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]   # strip "data: " prefix

                    # Check if this is a metadata event
                    if payload.startswith("{"):
                        try:
                            meta = json.loads(payload)
                            if "__sources__" in meta:
                                sources = meta["__sources__"]
                                # Extract session_id from response if new session
                                if not st.session_state.active_session_id:
                                    pass   # session_id comes from /message endpoint
                            elif "__error__" in meta:
                                full_response = f"⚠️ Error: {meta['__error__']}"
                        except json.JSONDecodeError:
                            pass
                    else:
                        # Text token — unescape newlines
                        token         = payload.replace("\\n", "\n")
                        full_response += token
                        response_placeholder.markdown(full_response + "▌")

            response_placeholder.markdown(full_response)

        except Exception as e:
            full_response = f"⚠️ Connection error: {e}"
            response_placeholder.markdown(full_response)

    # Save to session state
    st.session_state.messages.append({"role": "assistant", "content": full_response})
    st.session_state.sources = sources
    st.rerun()
    