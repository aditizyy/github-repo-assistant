"""
app.py — Streamlit frontend for Phase 2.
Allows users to submit a repo URL and watch it get processed.
"""
import time
import streamlit as st
import httpx

API_BASE = "http://localhost:8000/api"

st.set_page_config(
    page_title="GitHub Repo Assistant",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 GitHub Repository Assistant")
st.caption("Submit a GitHub repository to analyse it with AI")

# ── Sidebar: submit new repo ─────────────────────────────────────────────────
with st.sidebar:
    st.header("Add Repository")
    github_url = st.text_input(
        "GitHub URL",
        placeholder="https://github.com/owner/repo",
    )
    submit = st.button("Clone & Index", type="primary", use_container_width=True)

    if submit and github_url:
        with st.spinner("Submitting..."):
            try:
                resp = httpx.post(
                    f"{API_BASE}/repos/",
                    json={"github_url": github_url},
                    timeout=10,
                )
                if resp.status_code == 202:
                    data = resp.json()
                    st.success(f"Submitted! Repo ID: {data['id']}")
                    st.session_state["active_repo_id"] = data["id"]
                elif resp.status_code == 409:
                    st.warning(resp.json()["detail"])
                else:
                    st.error(resp.json().get("detail", "Unknown error"))
            except httpx.ConnectError:
                st.error("Cannot connect to backend. Is it running on port 8000?")

# ── Main area: repository list ────────────────────────────────────────────────
st.subheader("Repositories")

try:
    resp = httpx.get(f"{API_BASE}/repos/", timeout=5)
    repos = resp.json()
except Exception:
    st.error("Could not fetch repositories. Is the backend running?")
    repos = []

if not repos:
    st.info("No repositories yet. Submit one in the sidebar.")
else:
    for repo in repos:
        status_emoji = {
            "pending":  "⏳",
            "cloning":  "🔄",
            "indexing": "📦",
            "ready":    "✅",
            "error":    "❌",
        }.get(repo["status"], "❓")

        with st.expander(f"{status_emoji} {repo['repo_name']} — {repo['status']}"):
            col1, col2, col3 = st.columns(3)
            col1.metric("Files", repo["file_count"])
            col2.metric("Chunks", repo["chunk_count"])
            col3.metric("Status", repo["status"])

            if repo["status"] == "error":
                st.error(f"Error: {repo['error_message']}")

            if repo["status"] in ("cloning", "indexing"):
                if st.button(f"Refresh status", key=f"refresh_{repo['id']}"):
                    st.rerun()

            if repo["status"] == "ready":
                if st.button(f"View files", key=f"files_{repo['id']}"):
                    file_resp = httpx.get(
                        f"{API_BASE}/repos/{repo['id']}/files", timeout=5
                    )
                    files = file_resp.json()
                    st.dataframe(
                        [{"path": f["file_path"],
                          "language": f["language"],
                          "lines": f["line_count"]} for f in files],
                        use_container_width=True,
                    )
                
                # ── Added Phase 4 Features: Indexing Button & Semantic Search ──
                st.divider()
                st.subheader("🔍 Semantic Search")
                query = st.text_input(
                    "Search the codebase",
                    placeholder="Where is authentication handled?",
                    key=f"search_{repo['id']}",
                )
                if query:
                    with st.spinner("Searching..."):
                        resp = httpx.get(
                            f"{API_BASE}/repos/{repo['id']}/search",
                            params={"q": query, "k": 5},
                            timeout=15,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            results = data["results"]
                            if not results:
                                st.info("No relevant results found. Try a different query.")
                            for r in results:
                                score_pct = int(r["score"] * 100)
                                with st.container():
                                    st.markdown(
                                        f"**{r['file_path']}** "
                                        f"→ `{r['node_name'] or 'chunk'}` "
                                        f"· lines {r['start_line']}–{r['end_line']} "
                                        f"· **{score_pct}% match**"
                                    )
                                    lang_highlight = r["language"].lower() if r["language"] else "python"
                                    st.code(r["preview"], language=lang_highlight)
                                    st.divider()
                        else:
                            st.error("Build the FAISS index first.")

                # Build index button (only show if not yet indexed)
                if st.button("⚡ Build AI Index", key=f"idx_{repo['id']}"):
                    resp = httpx.post(
                        f"{API_BASE}/repos/{repo['id']}/index", timeout=10
                    )
                    if resp.status_code == 200:
                        st.success("Indexing started. Refresh in a moment.")
                    else:
                        st.error(resp.text)

            if st.button(f"🗑 Delete", key=f"del_{repo['id']}"):
                httpx.delete(f"{API_BASE}/repos/{repo['id']}", timeout=5)
                st.rerun()