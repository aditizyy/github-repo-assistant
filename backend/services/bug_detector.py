"""
bug_detector.py — AST-based static analysis for Python files.

Uses Python's built-in ast module to walk the syntax tree and detect:
  - Unused imports
  - Bare except clauses (anti-pattern)
  - Empty except blocks (swallowed errors)
  - Mutable default arguments (classic Python gotcha)
  - Functions with too many arguments (complexity smell)
  - Deeply nested code (complexity smell)
  - Unreachable code after return/raise/break/continue
  - Functions defined but never called within the same file
  - TODO/FIXME/HACK/XXX/BUG comments (technical debt markers)

Design: each detector is an ast.NodeVisitor subclass — the Visitor pattern.
Each visitor handles one concern; adding a new check means adding a new
visitor class, not modifying existing ones (Open/Closed Principle).

DSA connection: AST walking is tree traversal. Every visitor below does a
DFS over the syntax tree via ast.NodeVisitor's generic_visit dispatch.
"""

import ast
import re
import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


# ── Issue data structure ──────────────────────────────────────────────────────

@dataclass
class Issue:
    """
    One detected problem in a source file.
    Severity: critical > high > medium > low > info
    """
    file_path:   str
    line:        int
    column:      int
    severity:    str
    category:    str          # "security" | "bug" | "style" | "complexity" | "debt"
    rule:        str
    title:       str
    detail:      str
    suggestion:  str = ""
    snippet:     str = ""

    def to_dict(self) -> dict:
        return {
            "file_path":  self.file_path,
            "line":       self.line,
            "column":     self.column,
            "severity":   self.severity,
            "category":   self.category,
            "rule":       self.rule,
            "title":      self.title,
            "detail":     self.detail,
            "suggestion": self.suggestion,
            "snippet":    self.snippet,
        }


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


# ── AST Visitors ─────────────────────────────────────────────────────────────

class ImportCollector(ast.NodeVisitor):
    """Collects all imported names and where they're referenced."""

    def __init__(self):
        self.imports: dict[str, int] = {}
        self.used_names: set[str]    = set()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.imports[name] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self.imports[name] = node.lineno
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.value, ast.Name):
            self.used_names.add(node.value.id)
        self.generic_visit(node)

    def get_unused(self) -> list[tuple[str, int]]:
        return [
            (name, line)
            for name, line in self.imports.items()
            if name not in self.used_names
        ]


class FunctionAnalyser(ast.NodeVisitor):
    """
    Detects complexity smells in function definitions:
      - Too many parameters (> 6)
      - Mutable default arguments
      - Very long functions (> 50 lines)
    """

    def __init__(self, file_path: str, lines: list[str]):
        self.file_path = file_path
        self.lines     = lines
        self.issues:   List[Issue] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _check_function(self, node):
        name      = node.name
        num_args  = len(node.args.args)
        func_len  = (node.end_lineno - node.lineno) if node.end_lineno else 0
        snippet   = self.lines[node.lineno - 1].strip() if 0 < node.lineno <= len(self.lines) else ""

        if num_args > 6:
            self.issues.append(Issue(
                file_path  = self.file_path,
                line       = node.lineno,
                column     = node.col_offset,
                severity   = "medium",
                category   = "complexity",
                rule       = "too-many-args",
                title      = f"`{name}` has {num_args} parameters",
                detail     = (
                    "Functions with more than 6 parameters are hard to call "
                    "correctly and often indicate the function does too much."
                ),
                suggestion = (
                    "Group related parameters into a dataclass or TypedDict. "
                    "Consider splitting into smaller functions."
                ),
                snippet    = snippet,
            ))

        for default in node.args.defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                mutable_type = type(default).__name__.lower()
                self.issues.append(Issue(
                    file_path  = self.file_path,
                    line       = node.lineno,
                    column     = node.col_offset,
                    severity   = "high",
                    category   = "bug",
                    rule       = "mutable-default-arg",
                    title      = f"`{name}` uses mutable default argument ({mutable_type})",
                    detail     = (
                        f"Mutable default arguments are shared across ALL calls to "
                        f"`{name}`. Mutations in one call affect subsequent calls — "
                        f"a classic Python bug."
                    ),
                    suggestion = (
                        "Use `None` as default and initialise inside the function: "
                        f"`def {name}(..., items=None): if items is None: items = []`"
                    ),
                    snippet    = snippet,
                ))

        if func_len > 50:
            self.issues.append(Issue(
                file_path  = self.file_path,
                line       = node.lineno,
                column     = node.col_offset,
                severity   = "low",
                category   = "complexity",
                rule       = "long-function",
                title      = f"`{name}` is {func_len} lines long",
                detail     = (
                    "Functions longer than 50 lines are hard to read, test, and "
                    "maintain, and often violate the Single Responsibility Principle."
                ),
                suggestion = "Extract logical sub-steps into helper functions.",
                snippet    = snippet,
            ))


class ExceptAnalyser(ast.NodeVisitor):
    """
    Detects problematic exception handling:
      - Bare `except:` (catches everything, including KeyboardInterrupt)
      - `except X: pass` (silently swallows errors)
    """

    def __init__(self, file_path: str, lines: list[str]):
        self.file_path = file_path
        self.lines     = lines
        self.issues:   List[Issue] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        snippet = self.lines[node.lineno - 1].strip() if 0 < node.lineno <= len(self.lines) else ""

        if node.type is None:
            self.issues.append(Issue(
                file_path  = self.file_path,
                line       = node.lineno,
                column     = node.col_offset,
                severity   = "high",
                category   = "bug",
                rule       = "bare-except",
                title      = "Bare `except:` catches all exceptions",
                detail     = (
                    "A bare except catches KeyboardInterrupt, SystemExit, and "
                    "GeneratorExit — exceptions that should never be silently "
                    "handled. This can make programs impossible to stop or debug."
                ),
                suggestion = (
                    "Use `except Exception:` at minimum, or catch specific "
                    "exceptions like `except (ValueError, KeyError):`"
                ),
                snippet    = snippet,
            ))
        elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            exc_name = ast.unparse(node.type) if hasattr(ast, "unparse") else "Exception"
            self.issues.append(Issue(
                file_path  = self.file_path,
                line       = node.lineno,
                column     = node.col_offset,
                severity   = "high",
                category   = "bug",
                rule       = "empty-except",
                title      = f"Empty `except {exc_name}: pass` swallows errors silently",
                detail     = (
                    "Silently catching and ignoring exceptions hides bugs. "
                    "The error disappears and the program continues in an "
                    "undefined state."
                ),
                suggestion = "At minimum, log the exception: `logger.exception('...')`",
                snippet    = snippet,
            ))

        self.generic_visit(node)


class NestingAnalyser(ast.NodeVisitor):
    """
    Tracks code nesting depth. Deeply nested code is hard to read, test,
    and reason about — the "arrow anti-pattern."
    """
    MAX_DEPTH = 4

    def __init__(self, file_path: str, lines: list[str]):
        self.file_path  = file_path
        self.lines      = lines
        self.issues:    List[Issue] = []
        self._depth     = 0
        self._reported:  set[int] = set()

    def _visit_nesting(self, node):
        self._depth += 1
        if self._depth > self.MAX_DEPTH and node.lineno not in self._reported:
            self._reported.add(node.lineno)
            snippet = self.lines[node.lineno - 1].strip() if 0 < node.lineno <= len(self.lines) else ""
            self.issues.append(Issue(
                file_path  = self.file_path,
                line       = node.lineno,
                column     = node.col_offset,
                severity   = "low",
                category   = "complexity",
                rule       = "deep-nesting",
                title      = f"Code nested {self._depth} levels deep",
                detail     = (
                    f"Nesting beyond {self.MAX_DEPTH} levels makes code very hard "
                    f"to follow."
                ),
                suggestion = (
                    "Use early returns ('guard clauses') to flatten the structure. "
                    "Extract inner logic into helper functions."
                ),
                snippet    = snippet,
            ))
        self.generic_visit(node)
        self._depth -= 1

    visit_If               = _visit_nesting
    visit_For               = _visit_nesting
    visit_While             = _visit_nesting
    visit_With              = _visit_nesting
    visit_Try               = _visit_nesting
    visit_FunctionDef       = _visit_nesting
    visit_AsyncFunctionDef  = _visit_nesting


class DeadCodeDetector(ast.NodeVisitor):
    """
    Detects:
      - Code after return/raise/break/continue (unreachable)
      - Functions defined but never called within this file (best-effort)
    """

    def __init__(self, file_path: str, lines: list[str]):
        self.file_path          = file_path
        self.lines              = lines
        self.issues:            List[Issue] = []
        self.defined_functions: set[str]    = set()
        self.called_functions:  set[str]    = set()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.defined_functions.add(node.name)
        self._check_unreachable_after_return(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            self.called_functions.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_functions.add(node.func.attr)
        self.generic_visit(node)

    def _check_unreachable_after_return(self, func_node):
        TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
        body = func_node.body
        for i, stmt in enumerate(body):
            if isinstance(stmt, TERMINATORS) and i < len(body) - 1:
                next_stmt = body[i + 1]
                if isinstance(next_stmt, ast.Expr) and isinstance(next_stmt.value, ast.Constant):
                    continue  # docstring-like trailing string, ignore
                snippet = (
                    self.lines[next_stmt.lineno - 1].strip()
                    if 0 < next_stmt.lineno <= len(self.lines) else ""
                )
                self.issues.append(Issue(
                    file_path  = self.file_path,
                    line       = next_stmt.lineno,
                    column     = next_stmt.col_offset,
                    severity   = "medium",
                    category   = "bug",
                    rule       = "unreachable-code",
                    title      = "Unreachable code after return/raise",
                    detail     = (
                        "This statement can never execute because a "
                        "return/raise/break/continue precedes it in the same block."
                    ),
                    suggestion = "Remove the unreachable code or restructure the logic.",
                    snippet    = snippet,
                ))

    def get_uncalled_functions(self) -> list[str]:
        EXCLUDED = {
            "__init__", "__str__", "__repr__", "__eq__", "__hash__",
            "__enter__", "__exit__", "__len__", "__getitem__",
            "main", "setup", "teardown", "setUp", "tearDown",
        }
        return [
            f for f in self.defined_functions
            if f not in self.called_functions
            and f not in EXCLUDED
            and not f.startswith("_")
        ]


# ── Comment scanner ───────────────────────────────────────────────────────────

def scan_comments(content: str, file_path: str) -> List[Issue]:
    """Find TODO/FIXME/HACK/XXX/BUG markers — acknowledged technical debt."""
    DEBT_PATTERNS = {
        r"#.*\bTODO\b":  ("info",   "todo",    "TODO comment"),
        r"#.*\bFIXME\b": ("medium", "fixme",   "FIXME comment — known bug"),
        r"#.*\bHACK\b":  ("medium", "hack",    "HACK comment — workaround"),
        r"#.*\bXXX\b":   ("medium", "xxx",     "XXX comment — needs attention"),
        r"#.*\bBUG\b":   ("high",   "bug-tag", "BUG comment — known defect"),
    }

    issues: List[Issue] = []
    lines  = content.splitlines()

    for line_num, line in enumerate(lines, start=1):
        for pattern, (severity, rule, title) in DEBT_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                issues.append(Issue(
                    file_path  = file_path,
                    line       = line_num,
                    column     = 0,
                    severity   = severity,
                    category   = "debt",
                    rule       = rule,
                    title      = title,
                    detail     = f"Found in: `{line.strip()}`",
                    suggestion = "Schedule this for resolution or create a ticket.",
                    snippet    = line.strip(),
                ))
    return issues


# ── Main Python analyser ──────────────────────────────────────────────────────

class PythonBugDetector:
    """
    Runs all Python-specific static analysis detectors on one file.
    Returns a combined list of issues sorted by severity.
    """

    def analyse(self, content: str, file_path: str) -> List[Issue]:
        lines  = content.splitlines()
        issues: List[Issue] = []

        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            return [Issue(
                file_path  = file_path,
                line       = getattr(e, "lineno", 0) or 0,
                column     = getattr(e, "offset", 0) or 0,
                severity   = "critical",
                category   = "bug",
                rule       = "syntax-error",
                title      = "Syntax error",
                detail     = str(e),
                suggestion = "Fix the syntax error before other analysis can run.",
                snippet    = "",
            )]

        import_collector = ImportCollector()
        func_analyser     = FunctionAnalyser(file_path, lines)
        except_analyser   = ExceptAnalyser(file_path, lines)
        nesting_analyser  = NestingAnalyser(file_path, lines)
        dead_code         = DeadCodeDetector(file_path, lines)

        for visitor in (import_collector, func_analyser, except_analyser,
                        nesting_analyser, dead_code):
            visitor.visit(tree)

        issues.extend(func_analyser.issues)
        issues.extend(except_analyser.issues)
        issues.extend(nesting_analyser.issues)
        issues.extend(dead_code.issues)

        for name, line_num in import_collector.get_unused():
            snippet = lines[line_num - 1].strip() if 0 < line_num <= len(lines) else ""
            issues.append(Issue(
                file_path  = file_path,
                line       = line_num,
                column     = 0,
                severity   = "low",
                category   = "style",
                rule       = "unused-import",
                title      = f"Unused import: `{name}`",
                detail     = f"`{name}` is imported but never used in this file.",
                suggestion = f"Remove the import of `{name}`.",
                snippet    = snippet,
            ))

        for func_name in dead_code.get_uncalled_functions():
            issues.append(Issue(
                file_path  = file_path,
                line       = 0,
                column     = 0,
                severity   = "info",
                category   = "debt",
                rule       = "uncalled-function",
                title      = f"`{func_name}` is defined but never called in this file",
                detail     = "This function may be dead code, or called from another module.",
                suggestion = "Verify this is used externally, or remove it if unused.",
                snippet    = "",
            ))

        issues.extend(scan_comments(content, file_path))

        issues.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 99))
        return issues
