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

# ── Phase 6: Repository summary prompt (structured JSON) ──────────────────────

REPO_SUMMARY_PROMPT = """You are analysing a software repository called "{repo_name}".

## File Tree
{file_tree}

## Key File Samples
{samples}

Generate a structured JSON summary with EXACTLY these keys:
{{
  "purpose": "one paragraph describing what this project does",
  "tech_stack": {{
    "languages": ["list of programming languages"],
    "frameworks": ["list of frameworks/libraries"],
    "databases": ["list of databases used"],
    "tools": ["build tools, CI, docker, etc"]
  }},
  "architecture": "2–3 sentences describing the project structure",
  "entry_points": ["list of main entry files with brief description"],
  "key_components": [
    {{"file": "path", "role": "what this file does"}}
  ],
  "api_endpoints": ["list of detected routes, e.g. GET /users"],
  "setup_steps": ["inferred steps to run this project"],
  "complexity": "simple | moderate | complex"
}}

Return ONLY valid JSON. No markdown fences. No explanation."""


# ── Phase 6: README generation prompt ──────────────────────────────────────────

README_PROMPT = """Generate a professional, complete README.md for the following project.

## Project Summary
{summary}

## File Tree
{file_tree}

## Key File Samples
{samples}

Requirements:
- Start with a compelling one-line description and badges placeholder
- Include: Overview, Features, Tech Stack, Project Structure,
  Installation, Usage, API Reference (if applicable), Contributing, License
- Use real file names and actual code samples from what you see
- Format as proper GitHub Markdown with code blocks
- Make it look like a senior engineer wrote it
- Include example commands that actually match the project

Generate the complete README.md content:"""


# ── Phase 6: API documentation prompt ──────────────────────────────────────────

API_DOC_PROMPT = """Analyse these source files and generate complete API documentation.

## Source Files
{samples}

Generate markdown API documentation including:

### For each endpoint detected:
- HTTP method + path
- Description
- Request body (with field types)
- Response schema (with field types)
- Example request (curl)
- Example response (JSON)
- Possible error codes

### Authentication
- How auth works (if present)
- Required headers

Format as clean markdown. If no API endpoints are found, document
the main public functions/classes instead.

API Documentation:"""


# ── Phase 6: Function documentation prompt (per-file) ─────────────────────────

FUNCTION_DOC_PROMPT = """Generate comprehensive documentation for all functions and
classes in the following source file.

## File: {file_path}
## Language: {language}

```{language_lower}
{content}
```

For EACH function and class, provide:

```
### `function_or_class_name`
**Purpose:** What it does in plain English
**Parameters:**
  - `param_name` (type): description
**Returns:** type — description
**Raises:** exception type — when it's raised (if applicable)
**Time Complexity:** O(...) — brief justification
**Space Complexity:** O(...) — brief justification
**Example:**
```code
usage example here
```
```

Be thorough. Cover every public function and class."""


# ── Phase 6: Architecture analysis prompt ──────────────────────────────────────

ARCHITECTURE_PROMPT = """Analyse this repository and generate an architecture overview.

## Repository: {repo_name}
## File Tree
{file_tree}

## Key Files
{samples}

Generate:

## 1. Architecture Pattern
Identify the pattern (MVC, layered, microservices, event-driven, etc.)
and explain how this repo implements it.

## 2. Component Diagram (Mermaid)
```mermaid
graph TD
    [generate a real Mermaid component diagram based on actual files]
```

## 3. Data Flow
Step-by-step description of how data flows through the system
for the primary use case.

## 4. Module Responsibilities
Table mapping each major directory/module to its responsibility.

## 5. External Dependencies
List external services, APIs, or databases this project depends on.

## 6. Potential Improvements
3 concrete architectural improvements with justification."""