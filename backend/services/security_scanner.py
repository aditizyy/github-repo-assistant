"""
security_scanner.py — Regex-based security vulnerability detection.

Language-agnostic (works on any text file, not just Python). Detects:
  1. Hardcoded secrets (API keys, passwords, tokens, AWS keys, private keys)
  2. SQL injection via string concatenation/formatting
  3. Unsafe deserialisation (pickle.loads, yaml.load, eval, exec)
  4. Weak cryptography (MD5, SHA1, DES/3DES/RC4)
  5. Command injection (shell=True, os.system, os.popen)
  6. Debug / config issues left enabled
  7. Path traversal risks

Each check is a small list of (regex, rule, title) tuples run through a
shared `_run_patterns` helper — adding a new check means adding a tuple,
not writing new scanning logic.
"""

import re
import logging
from typing import List

from backend.services.bug_detector import Issue

logger = logging.getLogger(__name__)

# ── Secret patterns ───────────────────────────────────────────────────────────

SECRET_PATTERNS: list[tuple[str, str, str]] = [
    (
        r'(?i)(api[_-]?key|apikey)\s*=\s*["\'][A-Za-z0-9_\-]{16,}["\']',
        "hardcoded-api-key",
        "Hardcoded API key",
    ),
    (
        r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{4,}["\']',
        "hardcoded-password",
        "Hardcoded password",
    ),
    (
        r'(?i)(secret[_-]?key|secret)\s*=\s*["\'][^"\']{8,}["\']',
        "hardcoded-secret",
        "Hardcoded secret key",
    ),
    (
        r'(?i)(token|access[_-]?token|auth[_-]?token)\s*=\s*["\'][A-Za-z0-9_\-\.]{16,}["\']',
        "hardcoded-token",
        "Hardcoded authentication token",
    ),
    (
        r'(?i)(aws[_-]?access[_-]?key[_-]?id)\s*=\s*["\']AKIA[A-Z0-9]{16}["\']',
        "aws-access-key",
        "Hardcoded AWS access key",
    ),
    (
        r'(?i)(aws[_-]?secret[_-]?access[_-]?key)\s*=\s*["\'][^"\']{30,}["\']',
        "aws-secret-key",
        "Hardcoded AWS secret key",
    ),
    (
        r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----',
        "private-key",
        "Private key material in source code",
    ),
    (
        r'(?i)(db[_-]?password|database[_-]?password)\s*=\s*["\'][^"\']{2,}["\']',
        "hardcoded-db-password",
        "Hardcoded database password",
    ),
]

# Known-safe placeholder values — don't flag these as real secrets
SECRET_ALLOWLIST = {
    'password = ""',
    "password = ''",
    'password = "your_password_here"',
    'password = "changeme"',
    'password = "example"',
    'secret_key = "your-secret-key"',
    'token = "your_token_here"',
}

# ── SQL injection patterns ────────────────────────────────────────────────────

SQL_INJECTION_PATTERNS = [
    (
        r'(?i)(execute|cursor\.execute|db\.execute)\s*\(\s*["\'].*%s.*["\'\s]*%',
        "sql-format-string",
        "SQL query built with % string formatting",
    ),
    (
        r'(?i)(execute|cursor\.execute|db\.execute)\s*\(\s*f["\'].*\{',
        "sql-fstring",
        "SQL query built with f-string interpolation",
    ),
    (
        r'(?i)(execute|cursor\.execute|db\.execute)\s*\(\s*["\'].*["\'\s]*\+',
        "sql-concatenation",
        "SQL query built with string concatenation",
    ),
    (
        r'(?i)SELECT.*FROM.*WHERE.*\+',
        "sql-where-concat",
        "SQL WHERE clause built with string concatenation",
    ),
]

# ── Unsafe deserialisation ────────────────────────────────────────────────────

UNSAFE_DESERIALISE_PATTERNS = [
    (r'\bpickle\.loads?\b',     "unsafe-pickle",      "pickle.load/loads can execute arbitrary code"),
    (r'\byaml\.load\s*\([^)]*\)', "unsafe-yaml-load", "yaml.load() without Loader is unsafe (use yaml.safe_load)"),
    (r'\beval\s*\(',            "unsafe-eval",        "eval() executes arbitrary Python — never use with user input"),
    (r'\bexec\s*\(',            "unsafe-exec",        "exec() executes arbitrary Python — dangerous with user input"),
    (r'\b__import__\s*\(',     "dynamic-import",      "Dynamic __import__() can load arbitrary modules"),
]

# ── Weak cryptography ─────────────────────────────────────────────────────────

WEAK_CRYPTO_PATTERNS = [
    (r'\bhashlib\.md5\b',  "weak-md5",  "MD5 is cryptographically broken — don't use for passwords or integrity"),
    (r'\bhashlib\.sha1\b', "weak-sha1", "SHA1 is deprecated for security use — use SHA256 or better"),
    (r'(?i)\bDES\b|\b3DES\b|\bRC4\b|\bBlowfish\b', "weak-cipher", "Weak cipher algorithm detected (DES/3DES/RC4/Blowfish)"),
]

# ── Command injection ─────────────────────────────────────────────────────────

COMMAND_INJECTION_PATTERNS = [
    (r'\bsubprocess\.(run|Popen|call|check_output)\s*\(.*shell\s*=\s*True',
     "shell-injection", "subprocess called with shell=True — vulnerable to command injection"),
    (r'\bos\.system\s*\(', "os-system", "os.system() is vulnerable to shell injection"),
    (r'\bos\.popen\s*\(',  "os-popen",  "os.popen() is vulnerable to shell injection"),
]

# ── Debug / config issues ─────────────────────────────────────────────────────

DEBUG_PATTERNS = [
    (r'(?i)DEBUG\s*=\s*True', "debug-enabled", "DEBUG mode is enabled — must be False in production"),
    (r'(?i)SSL_VERIFY\s*=\s*False|verify\s*=\s*False', "ssl-verify-disabled", "SSL certificate verification is disabled"),
]

# ── Path traversal ────────────────────────────────────────────────────────────

PATH_TRAVERSAL_PATTERNS = [
    (r'open\s*\(\s*(request|user|input|param)', "path-traversal-open", "File open() with user-controlled path — path traversal risk"),
    (r'\.\./|\.\.\\', "path-traversal-dotdot", "Directory traversal sequence `../` found"),
]


def _run_patterns(
    patterns: list[tuple[str, str, str]],
    file_path: str,
    lines: list[str],
    severity: str,
    category: str,
    detail_template: str,
    suggestion: str,
    allowlist: set[str] | None = None,
) -> List[Issue]:
    """Generic pattern runner — applies (regex, rule, title) tuples line by line."""
    issues: List[Issue] = []

    for line_num, line in enumerate(lines, start=1):
        stripped = line.strip()

        if stripped.startswith("#") and category != "debt":
            continue
        if allowlist and any(a in line for a in allowlist):
            continue

        for pattern, rule, title in patterns:
            if re.search(pattern, line):
                issues.append(Issue(
                    file_path  = file_path,
                    line       = line_num,
                    column     = 0,
                    severity   = severity,
                    category   = category,
                    rule       = rule,
                    title      = title,
                    detail     = detail_template,
                    suggestion = suggestion,
                    snippet    = stripped[:200],
                ))
                break   # one issue per line max — avoid duplicate alerts

    return issues


class SecurityScanner:
    """Runs all security checks against a source file's content."""

    def scan(self, content: str, file_path: str) -> List[Issue]:
        lines  = content.splitlines()
        issues: List[Issue] = []

        issues.extend(_run_patterns(
            SECRET_PATTERNS, file_path, lines,
            severity="critical", category="security",
            detail_template=(
                "Hardcoded credentials in source code are exposed to anyone with "
                "repository access and cannot be rotated without a code change."
            ),
            suggestion=(
                "Move to environment variables (`os.getenv('KEY_NAME')`) or a "
                "secrets manager. Rotate the exposed credential immediately."
            ),
            allowlist=SECRET_ALLOWLIST,
        ))

        issues.extend(_run_patterns(
            SQL_INJECTION_PATTERNS, file_path, lines,
            severity="critical", category="security",
            detail_template=(
                "Building SQL with string formatting, f-strings, or concatenation "
                "allows attackers to inject arbitrary SQL (OWASP #1 risk)."
            ),
            suggestion=(
                "Use parameterised queries: "
                "`cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))`"
            ),
        ))

        issues.extend(_run_patterns(
            UNSAFE_DESERIALISE_PATTERNS, file_path, lines,
            severity="high", category="security",
            detail_template=(
                "Deserialising untrusted data with pickle, yaml.load, or eval/exec "
                "can execute arbitrary code supplied by an attacker."
            ),
            suggestion=(
                "Use `yaml.safe_load()` instead of `yaml.load()`. Never deserialise "
                "untrusted pickle data. Replace eval with `ast.literal_eval()`."
            ),
        ))

        issues.extend(_run_patterns(
            WEAK_CRYPTO_PATTERNS, file_path, lines,
            severity="high", category="security",
            detail_template=(
                "Weak cryptographic algorithms can be broken, compromising password "
                "storage, data integrity, or signatures."
            ),
            suggestion=(
                "For passwords use bcrypt/scrypt/argon2 (via passlib). "
                "For integrity use SHA-256 or better."
            ),
        ))

        issues.extend(_run_patterns(
            COMMAND_INJECTION_PATTERNS, file_path, lines,
            severity="critical", category="security",
            detail_template=(
                "Passing user-controlled input to shell commands allows attackers "
                "to execute arbitrary system commands."
            ),
            suggestion=(
                "Use `subprocess.run([cmd, arg1, arg2])` with a list, not shell=True. "
                "Validate all inputs that touch system commands."
            ),
        ))

        issues.extend(_run_patterns(
            DEBUG_PATTERNS, file_path, lines,
            severity="medium", category="security",
            detail_template=(
                "Debug settings or disabled security controls left in code can "
                "expose sensitive information or weaken security in production."
            ),
            suggestion="Use environment-based configuration. Never hardcode production settings.",
        ))

        issues.extend(_run_patterns(
            PATH_TRAVERSAL_PATTERNS, file_path, lines,
            severity="high", category="security",
            detail_template=(
                "Path traversal vulnerabilities allow attackers to read or write "
                "files outside the intended directory."
            ),
            suggestion=(
                "Validate file paths with `Path(user_input).resolve()` and check "
                "it starts with the expected base directory."
            ),
        ))

        SORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        issues.sort(key=lambda i: SORDER.get(i.severity, 99))
        return issues
