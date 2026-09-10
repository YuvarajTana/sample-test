"""Best-effort filtering, not a complete secret or PII detector."""
import fnmatch
from pathlib import PurePosixPath
import re

SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "coverage", ".venv", "venv", "__pycache__", "vendor"}
SKIP_PATTERNS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.sqlite*", "*.db", "*.log",
                 "*lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "*.min.js", "*.map",
                 "credentials*", "id_rsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc")
PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(?:[\w.-]*(?:api[_-]?key|secret|password|access[_-]?token|auth[_-]?token)[\w.-]*)[\"']?\s*[:=]\s*[\"'][^\"'\n]{4,}[\"']"),
]


def exclusion(path: str, extra=()) -> str | None:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or any(ord(c) < 32 or ord(c) == 127 for c in path) or "\\" in path:
        return "unsafe or unsupported path"
    if any(part in SKIP_DIRS for part in p.parts):
        return "dependency or generated directory"
    if any(fnmatch.fnmatch(part.lower(), pattern) for part in p.parts for pattern in SKIP_PATTERNS):
        return "sensitive, generated, or non-source file"
    if any(fnmatch.fnmatch(path, pattern) for pattern in extra):
        return "configured exclusion"
    return None


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern in PATTERNS:
        def replacement(match):
            nonlocal count
            count += 1
            # Preserve all line numbers, including multi-line PEM blocks.
            parts = match.group(0).split("\n")
            return "\n".join((part[0] if i > 0 and part and part[0] in " +-" else "") + "[REDACTED]"
                             for i, part in enumerate(parts))
        text = pattern.sub(replacement, text)
    return text, count
