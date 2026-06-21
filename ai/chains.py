"""
chains.py — LangChain RAG chain construction.

LangChain is used here for:
  - Structured prompt templates with variable injection
  - LLM abstraction (easy to swap Gemini → GPT-4 → Claude)
  - Streaming support

We deliberately keep the chain simple and explicit.
Over-engineered LangChain code (too many nested chains/agents)
is hard to debug and impress no one. Clarity > cleverness.
"""

import logging
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from langchain.schema import HumanMessage, AIMessage, SystemMessage
from langchain.schema.output_parser import StrOutputParser

from backend.config import settings
from ai.prompts import SYSTEM_PROMPT, RAG_PROMPT, QUERY_REWRITER_PROMPT

logger = logging.getLogger(__name__)


# ── LLM singleton ─────────────────────────────────────────────────────────────

class _LLMSingleton:
    """
    Gemini model loaded once per process.
    Two variants: streaming (for chat) and non-streaming (for rewrites).
    """
    _streaming: ChatGoogleGenerativeAI | None = None
    _standard:  ChatGoogleGenerativeAI | None = None

    @classmethod
    def get_streaming(cls) -> ChatGoogleGenerativeAI:
        if cls._streaming is None:
            logger.info("Initialising Gemini (streaming)...")
            cls._streaming = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                google_api_key=settings.gemini_api_key,
                temperature=0.2,          # low = more factual, less creative
                streaming=True,
                convert_system_message_to_human=True,
            )
        return cls._streaming

    @classmethod
    def get_standard(cls) -> ChatGoogleGenerativeAI:
        if cls._standard is None:
            cls._standard = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                google_api_key=settings.gemini_api_key,
                temperature=0.1,
                convert_system_message_to_human=True,
            )
        return cls._standard


# ── History formatter ─────────────────────────────────────────────────────────

def format_history(messages: list[dict]) -> str:
    """
    Convert stored chat messages to a readable string for prompt injection.

    Args:
        messages: list of {"role": "user"|"assistant", "content": "..."}

    Returns:
        Formatted string like:
            User: what does this project do?
            Assistant: This is a FastAPI application that...
    """
    if not messages:
        return "No previous conversation."

    lines = []
    for msg in messages[-6:]:   # last 3 turns (6 messages) to stay within context window
        role = "User" if msg["role"] == "user" else "Assistant"
        # Truncate long assistant messages in history to save tokens
        content = msg["content"]
        if role == "Assistant" and len(content) > 500:
            content = content[:500] + "... [truncated]"
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


# ── Query rewriter ────────────────────────────────────────────────────────────

def rewrite_query(question: str, history: list[dict]) -> str:
    """
    Rewrite a follow-up question to be self-contained for FAISS retrieval.

    If there's no history, the original question is returned as-is
    (no LLM call needed — saves latency and tokens).
    """
    if not history:
        return question

    history_str = format_history(history)

    prompt = QUERY_REWRITER_PROMPT.format(
        history=history_str,
        question=question,
    )

    llm = _LLMSingleton.get_standard()
    response = llm.invoke([HumanMessage(content=prompt)])
    rewritten = response.content.strip()

    logger.debug(f"Query rewrite: '{question}' → '{rewritten}'")
    return rewritten


# ── RAG chain ─────────────────────────────────────────────────────────────────

class RAGChain:
    """
    The main RAG chain: retrieve → prompt → generate.

    Usage:
        chain = RAGChain()

        # Non-streaming
        answer = chain.invoke(repo_id=1, question="...", history=[...])

        # Streaming
        for token in chain.stream(repo_id=1, question="...", history=[...]):
            print(token, end="", flush=True)
    """

    def __init__(self):
        from backend.services.embedder import EmbedderService
        self.embedder = EmbedderService()

    def _retrieve(
        self,
        repo_id: int,
        query: str,
        k: int = 6,
    ) -> tuple[str, list]:
        """
        Run FAISS similarity search and format results as LLM context.

        Returns:
            (context_string, raw_results)
            context_string: formatted for prompt injection
            raw_results:    SearchResult list (for citations in API response)
        """
        results = self.embedder.similarity_search(repo_id, query, k=k)

        if not results:
            return "No relevant code found in the repository for this query.", []

        # Format each result as a labelled code block
        context_parts = []
        for i, r in enumerate(results, start=1):
            context_parts.append(f"**[{i}] {r.to_context_string()}**")

        return "\n\n".join(context_parts), results

    def _build_messages(
        self,
        context: str,
        history: list[dict],
        question: str,
    ) -> list:
        """
        Build the message list for the LLM.

        Structure:
          SystemMessage  — persona + rules (once)
          HumanMessage   — RAG prompt with context + history + question
        """
        rag_prompt_text = RAG_PROMPT.format(
            context=context,
            history=format_history(history),
            question=question,
        )

        return [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=rag_prompt_text),
        ]

    def invoke(
        self,
        repo_id: int,
        question: str,
        history: list[dict],
        k: int = 6,
    ) -> dict:
        """
        Non-streaming RAG call. Returns full answer + sources.

        Returns:
            {
                "answer":   str,
                "sources":  [{"file_path", "node_name", "start_line", "end_line", "score"}],
                "rewritten_query": str,
            }
        """
        # Step 1: Rewrite query for history-aware retrieval
        rewritten = rewrite_query(question, history)

        # Step 2: Retrieve relevant chunks
        context, results = self._retrieve(repo_id, rewritten, k=k)

        # Step 3: Build messages and call LLM
        messages = self._build_messages(context, history, question)
        llm      = _LLMSingleton.get_standard()
        response = llm.invoke(messages)
        answer   = response.content

        return {
            "answer": answer,
            "sources": [
                {
                    "file_path":  r.file_path,
                    "node_name":  r.node_name,
                    "start_line": r.start_line,
                    "end_line":   r.end_line,
                    "score":      round(r.score, 4),
                    "language":   r.language,
                }
                for r in results
            ],
            "rewritten_query": rewritten,
        }

    def stream(
        self,
        repo_id: int,
        question: str,
        history: list[dict],
        k: int = 6,
    ):
        """
        Streaming generator — yields text tokens as they arrive from Gemini.

        Usage:
            for token in chain.stream(repo_id, question, history):
                yield token   # in FastAPI: yield f"data: {token}\\n\\n"

        Yields:
            str — one token or small chunk at a time
        Also yields a final sentinel dict with sources (for API metadata).
        """
        rewritten          = rewrite_query(question, history)
        context, results   = self._retrieve(repo_id, rewritten, k=k)
        messages           = self._build_messages(context, history, question)
        llm                = _LLMSingleton.get_streaming()

        full_response = ""
        for chunk in llm.stream(messages):
            token = chunk.content
            if token:
                full_response += token
                yield token

        # After streaming completes, yield sources as a metadata payload
        # The client can use this to render source citations
        yield {
            "__sources__": [
                {
                    "file_path":  r.file_path,
                    "node_name":  r.node_name,
                    "start_line": r.start_line,
                    "end_line":   r.end_line,
                    "score":      round(r.score, 4),
                }
                for r in results
            ],
            "__rewritten_query__": rewritten,
            "__full_response__":   full_response,
        }