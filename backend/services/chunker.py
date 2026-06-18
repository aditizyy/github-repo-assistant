"""
chunker.py — Language-aware code chunking for embedding.

Strategy:
  Python      → AST-based: split at function/class boundaries (exact)
  JS/TS/Java  → Regex-based: detect function/class patterns (approximate)
  Everything  → Sliding window with overlap (universal fallback)

Why this matters:
  Naive fixed-size chunking can split a function in half.
  The embedding then represents an incomplete thought, producing
  poor retrieval. Semantic boundaries produce self-contained chunks
  that answer questions cleanly.

Data structure used: dataclass acts as a typed record (struct).
All chunkers return List[CodeChunk] — uniform interface.
"""

import ast
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Chunk size constants ─────────────────────────────────────────────────────
# These are tuned for code. Prose RAG typically uses 512 tokens.
# Code needs more context to be meaningful — 1500 chars ≈ 300–400 tokens.

CHUNK_SIZE     = 1500   # target characters per chunk
CHUNK_OVERLAP  = 200    # overlap between adjacent sliding-window chunks
MIN_CHUNK_SIZE = 50     # discard chunks smaller than this (e.g. empty files)


# ── Core data structure ───────────────────────────────────────────────────────

@dataclass
class CodeChunk:
    """
    Represents one embeddable unit of source code.

    Every field here becomes metadata attached to the FAISS vector,
    allowing the AI to say: "This comes from auth.py, lines 42-78,
    inside the function `validate_token`."
    """
    content:       str              # The actual source code text
    file_path:     str              # Relative path from repo root
    language:      str              # "Python", "JavaScript", etc.
    start_line:    int              # 1-indexed
    end_line:      int              # 1-indexed
    chunk_index:   int              # Position within the file (0-based)
    node_name:     Optional[str]    # Function or class name, if detected
    node_type:     Optional[str]    # "function", "class", "method", "module"
    repo_id:       int = 0          # Filled in by the pipeline

    def to_metadata(self) -> dict:
        """
        Serialise to a flat dict for FAISS metadata storage.
        FAISS only supports string values in metadata, so we stringify ints.
        """
        return {
            "file_path":   self.file_path,
            "language":    self.language,
            "start_line":  str(self.start_line),
            "end_line":    str(self.end_line),
            "chunk_index": str(self.chunk_index),
            "node_name":   self.node_name or "",
            "node_type":   self.node_type or "chunk",
            "repo_id":     str(self.repo_id),
        }

    def to_document_text(self) -> str:
        """
        The text that gets embedded. We prepend a header so the model
        has full context even if only this chunk is retrieved.

        This is prompt engineering at the data level — a critical detail
        that separates good RAG from bad RAG.
        """
        header_parts = [f"File: {self.file_path}"]
        if self.node_name:
            header_parts.append(f"{self.node_type}: {self.node_name}")
        header_parts.append(f"Lines: {self.start_line}–{self.end_line}")
        header = " | ".join(header_parts)
        return f"# {header}\n\n{self.content}"


# ── Python AST Chunker ────────────────────────────────────────────────────────

class PythonASTChunker:
    """
    Uses Python's built-in ast module to split at semantic boundaries.

    Algorithm:
      1. Parse the file into an AST.
      2. Walk top-level nodes (functions, classes).
      3. For classes, also extract methods.
      4. Any code outside these nodes becomes a "module-level" chunk.
      5. If a node is too large, apply sliding window within it.

    Time complexity:  O(n) where n = number of AST nodes
    Space complexity: O(n) for the list of chunks
    """

    def chunk(self, content: str, file_path: str, language: str) -> List[CodeChunk]:
        chunks: List[CodeChunk] = []
        lines  = content.splitlines()

        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            # File has a syntax error — fall back to sliding window
            logger.warning(f"AST parse failed for {file_path}: {e}. Using sliding window.")
            return SlidingWindowChunker().chunk(content, file_path, language)

        # Collect top-level nodes with line info
        top_level_nodes = [
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

        # Track which lines are covered by named nodes
        covered_lines: set[int] = set()

        for node in top_level_nodes:
            node_chunks = self._extract_node(node, lines, file_path, language, len(chunks))
            covered_lines.update(
                range(node.lineno, node.end_lineno + 1)
            )
            chunks.extend(node_chunks)

        # Collect module-level code (imports, constants, top-level statements)
        module_lines = [
            line for i, line in enumerate(lines, start=1)
            if i not in covered_lines
        ]
        if module_lines:
            module_content = "\n".join(module_lines).strip()
            if len(module_content) >= MIN_CHUNK_SIZE:
                chunks.append(CodeChunk(
                    content    = module_content,
                    file_path  = file_path,
                    language   = language,
                    start_line = 1,
                    end_line   = len(lines),
                    chunk_index= len(chunks),
                    node_name  = None,
                    node_type  = "module",
                ))

        # If nothing was extracted (e.g. empty file), return a single chunk
        if not chunks and content.strip():
            chunks = SlidingWindowChunker().chunk(content, file_path, language)

        return chunks

    def _extract_node(
        self,
        node: ast.AST,
        lines: list[str],
        file_path: str,
        language: str,
        base_index: int,
    ) -> List[CodeChunk]:
        """Extract one function or class, handling methods inside classes."""
        chunks: List[CodeChunk] = []
        start = node.lineno - 1      # 0-indexed for list slicing
        end   = node.end_lineno      # exclusive

        if isinstance(node, ast.ClassDef):
            # For classes: extract the class signature + docstring as one chunk,
            # then extract each method separately
            class_header_end = self._find_class_header_end(node)
            header_lines = lines[start:class_header_end]
            header_content = "\n".join(header_lines).strip()

            if len(header_content) >= MIN_CHUNK_SIZE:
                chunks.append(CodeChunk(
                    content    = header_content,
                    file_path  = file_path,
                    language   = language,
                    start_line = node.lineno,
                    end_line   = class_header_end,
                    chunk_index= base_index + len(chunks),
                    node_name  = node.name,
                    node_type  = "class",
                ))

            # Extract each method
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_chunks = self._extract_node(
                        child, lines, file_path, language, base_index + len(chunks)
                    )
                    # Prefix method name with class name for clarity
                    for mc in method_chunks:
                        mc.node_name = f"{node.name}.{child.name}"
                        mc.node_type = "method"
                    chunks.extend(method_chunks)

        else:
            # Function or async function
            node_content = "\n".join(lines[start:end]).strip()
            node_type = (
                "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )

            if len(node_content) > CHUNK_SIZE:
                # Large function — apply sliding window within the function body
                sub_chunks = SlidingWindowChunker().chunk(
                    node_content, file_path, language
                )
                for i, sc in enumerate(sub_chunks):
                    sc.node_name  = node.name
                    sc.node_type  = node_type
                    sc.start_line = node.lineno + sc.start_line - 1
                    sc.end_line   = node.lineno + sc.end_line - 1
                    sc.chunk_index = base_index + i
                chunks.extend(sub_chunks)
            elif len(node_content) >= MIN_CHUNK_SIZE:
                chunks.append(CodeChunk(
                    content    = node_content,
                    file_path  = file_path,
                    language   = language,
                    start_line = node.lineno,
                    end_line   = node.end_lineno,
                    chunk_index= base_index + len(chunks),
                    node_name  = node.name,
                    node_type  = node_type,
                ))

        return chunks

    def _find_class_header_end(self, class_node: ast.ClassDef) -> int:
        """
        Find the line where the class body's first method starts.
        Everything before that is the 'header' (decorators, docstring, class vars).
        """
        for node in ast.iter_child_nodes(class_node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.lineno - 1
        # No methods — the whole class is the header
        return class_node.end_lineno


# ── JavaScript / TypeScript Regex Chunker ────────────────────────────────────

class JavaScriptChunker:
    """
    Regex-based chunker for JS/TS.
    AST parsing JS in Python requires third-party parsers.
    Regex is a pragmatic tradeoff for a portfolio project.

    Detects:
      - Arrow functions:    const foo = (...) => {
      - Regular functions:  function foo(...) {
      - Class methods:      methodName(...) {
      - React components:   export default function Component
    """

    # Patterns ordered by specificity (most specific first)
    FUNCTION_PATTERNS = [
        # export const/let foo = (...) => {
        re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?"
            r"(?:\([^)]*\)|\w+)\s*=>"
        ),
        # export default function foo / async function foo
        re.compile(
            r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("
        ),
        # Class method: methodName(
        re.compile(
            r"^\s{2,}(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?(\w+)\s*\([^)]*\)\s*\{"
        ),
        # Class definition
        re.compile(r"^(?:export\s+)?class\s+(\w+)"),
    ]

    def chunk(self, content: str, file_path: str, language: str) -> List[CodeChunk]:
        lines   = content.splitlines()
        chunks: List[CodeChunk] = []
        current_start = 0
        current_name  = None
        brace_depth   = 0
        in_function   = False

        for i, line in enumerate(lines):
            # Track brace depth to find function end
            brace_depth += line.count("{") - line.count("}")

            if not in_function:
                # Try to detect a function start
                for pattern in self.FUNCTION_PATTERNS:
                    match = pattern.match(line.strip())
                    if match:
                        # Save any accumulated code before this function
                        if i > current_start:
                            prev_content = "\n".join(lines[current_start:i]).strip()
                            if len(prev_content) >= MIN_CHUNK_SIZE:
                                chunks.append(self._make_chunk(
                                    prev_content, file_path, language,
                                    current_start + 1, i, len(chunks), None
                                ))

                        current_start = i
                        current_name  = match.group(1) if match.lastindex else None
                        in_function   = True
                        brace_depth   = line.count("{") - line.count("}")
                        break

            elif brace_depth <= 0 and in_function:
                # Function ended
                func_content = "\n".join(lines[current_start:i + 1]).strip()
                if len(func_content) >= MIN_CHUNK_SIZE:
                    chunks.append(self._make_chunk(
                        func_content, file_path, language,
                        current_start + 1, i + 1, len(chunks), current_name
                    ))
                current_start = i + 1
                current_name  = None
                in_function   = False

        # Trailing content after last function
        if current_start < len(lines):
            trailing = "\n".join(lines[current_start:]).strip()
            if len(trailing) >= MIN_CHUNK_SIZE:
                chunks.append(self._make_chunk(
                    trailing, file_path, language,
                    current_start + 1, len(lines), len(chunks), current_name
                ))

        # If regex found nothing, fall back to sliding window
        if not chunks:
            return SlidingWindowChunker().chunk(content, file_path, language)

        return chunks

    def _make_chunk(
        self, content, file_path, language,
        start_line, end_line, index, node_name
    ) -> "CodeChunk":
        return CodeChunk(
            content    = content,
            file_path  = file_path,
            language   = language,
            start_line = start_line,
            end_line   = end_line,
            chunk_index= index,
            node_name  = node_name,
            node_type  = "function" if node_name else "chunk",
        )


# ── Sliding Window Chunker (Universal Fallback) ───────────────────────────────

class SlidingWindowChunker:
    """
    Splits content into overlapping windows.

    The overlap is critical: without it, context at chunk boundaries
    is lost. If a function call is at the end of chunk 1 and its
    definition is at the start of chunk 2, the overlap ensures
    at least one chunk contains both.

    This is the standard RAG chunking approach.
    Time complexity: O(n/chunk_size) chunks, each O(chunk_size) to create → O(n)
    """

    def chunk(
        self,
        content: str,
        file_path: str,
        language: str,
        chunk_size: int = CHUNK_SIZE,
        overlap: int = CHUNK_OVERLAP,
    ) -> List[CodeChunk]:
        lines  = content.splitlines()
        chunks: List[CodeChunk] = []

        if not lines:
            return chunks

        # Convert CHUNK_SIZE from chars to approximate lines
        # Assume average line length of ~40 chars for code
        lines_per_chunk = max(10, chunk_size // 40)
        overlap_lines   = max(2, overlap // 40)
        step            = lines_per_chunk - overlap_lines

        i = 0
        chunk_index = 0
        while i < len(lines):
            end = min(i + lines_per_chunk, len(lines))
            chunk_content = "\n".join(lines[i:end]).strip()

            if len(chunk_content) >= MIN_CHUNK_SIZE:
                chunks.append(CodeChunk(
                    content    = chunk_content,
                    file_path  = file_path,
                    language   = language,
                    start_line = i + 1,        # 1-indexed
                    end_line   = end,
                    chunk_index= chunk_index,
                    node_name  = None,
                    node_type  = "chunk",
                ))
                chunk_index += 1

            i += step

        return chunks


# ── Main dispatcher ───────────────────────────────────────────────────────────

class CodeChunker:
    """
    Public interface. Routes each file to the right chunker strategy.
    Caller only ever uses this class — the strategies are encapsulated.

    Design pattern: Strategy Pattern
    Each chunker implements the same .chunk() signature.
    Swapping strategies requires no changes to the calling code.
    """

    PYTHON_EXTENSIONS      = {".py", ".pyi"}
    JAVASCRIPT_EXTENSIONS  = {".js", ".jsx", ".ts", ".tsx", ".mjs"}

    def __init__(self):
        self._python_chunker = PythonASTChunker()
        self._js_chunker     = JavaScriptChunker()
        self._window_chunker = SlidingWindowChunker()

    def chunk_file(
        self,
        content: str,
        file_path: str,
        language: str,
    ) -> List[CodeChunk]:
        """
        Route to the right strategy and return chunks.
        Never raises — returns empty list on failure.
        """
        if not content or not content.strip():
            return []

        ext = Path(file_path).suffix.lower()

        try:
            if ext in self.PYTHON_EXTENSIONS:
                return self._python_chunker.chunk(content, file_path, language)
            elif ext in self.JAVASCRIPT_EXTENSIONS:
                return self._js_chunker.chunk(content, file_path, language)
            else:
                return self._window_chunker.chunk(content, file_path, language)

        except Exception as e:
            logger.error(f"Chunking failed for {file_path}: {e}")
            # Always return something — bad chunks are better than nothing
            return self._window_chunker.chunk(content, file_path, language)

    def chunk_repository(
        self,
        repo_id: int,
        file_paths: List[tuple[str, str, str]],
        # Each tuple: (absolute_path, relative_path, language)
    ) -> List[CodeChunk]:
        """
        Chunk all files in a repository.
        Returns all chunks with repo_id attached.
        """
        all_chunks: List[CodeChunk] = []

        for abs_path, rel_path, language in file_paths:
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
                content = content.replace("\r\n", "\n")
            except Exception as e:
                logger.warning(f"Could not read {abs_path}: {e}")
                continue

            file_chunks = self.chunk_file(content, rel_path, language)

            for chunk in file_chunks:
                chunk.repo_id = repo_id

            all_chunks.extend(file_chunks)
            logger.debug(
                f"  {rel_path}: {len(file_chunks)} chunks "
                f"({language})"
            )

        logger.info(
            f"Repo {repo_id}: {len(all_chunks)} total chunks "
            f"from {len(file_paths)} files"
        )
        return all_chunks