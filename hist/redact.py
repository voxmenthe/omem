"""Best-effort redaction of known secret formats, applied before storage.

Patterns name well-known credential shapes only; anything else is kept.
Each match becomes `[redacted:<kind>]` so a reader knows something was there.
"""

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
        ),
    ),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}")),
    ("github-token", re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws-secret",
        re.compile(
            r"(?i)(aws_secret_access_key\s*[=:]\s*[\"']?)[A-Za-z0-9/+]{40}"
        ),
    ),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("bearer-token", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}")),
    ("url-password", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?=@)")),
)


def redact(text: str) -> str:
    for kind, pattern in _PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m, k=kind: f"{m.group(1)}[redacted:{k}]", text)
        else:
            text = pattern.sub(f"[redacted:{kind}]", text)
    return text
