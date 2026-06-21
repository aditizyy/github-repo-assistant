"""
prompts.py — All LLM prompts in one place.

Centralising prompts is a software engineering best practice:
  - Easy to tune without touching business logic
  - Version-controllable prompt history
  - Clear separation of concerns

Prompt engineering principles applied here:
  1. Role assignment    — tells the model what persona to adopt
  2. Grounding context — code chunks injected as source of truth
  3. Citation mandate  — forces the model to cite file + line
  4. Format constraint — structured output for clean parsing
  5. Refusal handling  — tells model what to do when context is insufficient
"""

# ── System prompt ─────────────────────────────────────────────────────────────
# This sets the model's behaviour for the entire conversation.
# Injected once at the start of every chat completion call.

SYSTEM_PROMPT = """You are an expert AI code analyst and software engineer assistant.
You help developers understand codebases by analysing source code precisely and accurately.

## Your capabilities
- Explain what code does, how it works, and why it was designed that way
- Trace execution flows across multiple files
- Identify patterns, frameworks, and architectural decisions
- Analyse time and space complexity of algorithms
- Detect potential bugs, security issues, and code smells

## Rules you must follow
1. **Ground every answer in the provided code context.** Do not invent behaviour 
   that isn't visible in the retrieved chunks.
2. **Always cite your sources** using the format: `[filename, lines X–Y]`
3. **If the context is insufficient**, say so clearly and suggest what the user 
   should search for instead.
4. **Be precise about line numbers** — they help developers navigate directly.
5. **Use markdown formatting** for readability: headers, code blocks, bullet lists.
6. **For follow-up questions**, use the conversation history to maintain continuity.

## Response format
Structure complex answers as:
- **Summary**: one-sentence answer
- **Details**: deeper explanation with citations
- **Code references**: exact snippets if helpful
- **Related areas**: suggest what else to look at
"""

# ── Retrieval-aware query rewriter ────────────────────────────────────────────
# When conversation has history, the user's question may be a follow-up
# that doesn't make sense in isolation.
# e.g. "And how does it handle errors?" → context-free for FAISS
# We rewrite it to: "How does the authentication flow handle errors?"

QUERY_REWRITER_PROMPT = """Given a conversation history and a follow-up question,
rewrite the follow-up question to be fully self-contained and searchable.

The rewritten query will be used for semantic code search, so:
- Include relevant technical terms from the history
- Expand pronouns ("it", "this", "that") to their actual referents  
- Keep it concise (1–2 sentences maximum)
- If the question is already self-contained, return it unchanged

Conversation history:
{history}

Follow-up question: {question}

Rewritten query (return ONLY the rewritten question, no explanation):"""

# ── RAG answer prompt ─────────────────────────────────────────────────────────
# This is the main prompt that generates the answer.
# {context} = retrieved code chunks formatted by SearchResult.to_context_string()
# {history} = last N conversation turns
# {question} = user's current question

RAG_PROMPT = """Use the following retrieved code snippets to answer the question.
Each snippet includes its source file and line numbers.

## Retrieved Code Context
{context}

## Conversation History
{history}

## Question
{question}

## Instructions
- Answer based ONLY on the code context provided above
- Cite every claim with [filename, lines X–Y]
- If you reference a function or class, name it explicitly
- If the context doesn't contain enough information, say: 
  "The retrieved context doesn't cover this fully. Try searching for: [suggested query]"
- Format your answer in clear markdown

## Answer:"""

# ── Summary prompt ────────────────────────────────────────────────────────────
# Used by the /summary endpoint (Phase 7)

SUMMARY_PROMPT = """Analyse the following repository file structure and selected 
code samples to generate a comprehensive project overview.

## Repository: {repo_name}

## File structure (top-level):
{file_tree}

## Key file samples:
{samples}

## Generate a structured summary including:
1. **Project Purpose** — what does this project do?
2. **Tech Stack** — languages, frameworks, libraries detected
3. **Architecture** — how is the project organised?
4. **Entry Points** — where does execution begin?
5. **Key Components** — most important files and what they do
6. **API Endpoints** — if this is a web project, list the routes
7. **Database** — schema or data models if present
8. **How to Run** — inferred setup steps

Be specific and cite file names throughout."""

# ── Function explanation prompt ───────────────────────────────────────────────

FUNCTION_EXPLAIN_PROMPT = """Analyse this code and provide a detailed explanation.

## Code
```{language}
{code}
```

## File: {file_path} | Lines: {start_line}–{end_line}

Provide:
1. **Purpose** — what does this code do in plain English?
2. **Parameters** — each input with type and meaning
3. **Return value** — what it returns and when
4. **Algorithm** — step-by-step logic walkthrough
5. **Dependencies** — what it calls or imports
6. **Time complexity** — Big-O with justification
7. **Space complexity** — Big-O with justification
8. **Edge cases** — what could go wrong?
9. **Suggested improvements** — one concrete suggestion"""