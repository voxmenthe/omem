"""Best-effort redaction of known secret formats, applied before storage.

Patterns name well-known credential shapes only; anything else is kept.
Each match becomes `[redacted:<kind>]` so a reader knows something was there.
Most vendor shapes follow gitleaks' rules (MIT), as flopwire rewrote them
(`internal/redact/rules.go`). A rule's regex runs only on text holding one
of its keywords, which keeps redaction cheap and lets `hist refresh` find
the stored rows a rule could change. A token's start may follow a JSON
escape such as `\\n`, which `\\b` would not see as a boundary.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

# Not inside a run of letters and digits, or just after an escaped \n, \r or \t.
_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[nrt]))"
_END = r"(?![A-Za-z0-9])"


@dataclass(frozen=True)
class Rule:
    kind: str
    keywords: tuple[str, ...]
    pattern: re.Pattern[str]
    # Keywords are lower case and match the text in any case.
    folded: bool = False
    # Checked on the masked value; a rule with a group keeps group 1 as context.
    valid: Callable[[str], bool] | None = None


def _entropy(value: str) -> float:
    counts = [value.count(c) for c in set(value)]
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts)


def _cookie(value: str) -> bool:
    """A real header value, not code that builds one or prose: its first
    `name=value` pair's value looks random."""

    if any(c in value for c in "$`{}()<>") or value.startswith("[redacted"):
        return False
    name, equals, rest = value.partition("=")
    if not equals or not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return False
    first = re.split(r"[; ]", rest, maxsplit=1)[0]
    return len(first) >= 8 and _entropy(first) >= 3.0


def _token(kind: str, keywords: tuple[str, ...], body: str) -> Rule:
    return Rule(kind, keywords, re.compile(_START + body + _END))


RULES: tuple[Rule, ...] = (
    Rule(
        "private-key",
        ("PRIVATE KEY",),
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
        ),
    ),
    Rule("anthropic-key", ("sk-ant-",), re.compile(_START + r"sk-ant-[A-Za-z0-9_-]{20,}")),
    Rule("openai-key", ("sk-",), re.compile(_START + r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}")),
    Rule(
        "github-token",
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
        re.compile(_START + r"(?:gh[opusr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ),
    Rule("gitlab-token", ("glpat-",), re.compile(_START + r"glpat-[A-Za-z0-9_-]{20,}")),
    _token("aws-access-key", ("AKIA", "ASIA"), r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    Rule(
        "aws-secret",
        ("aws_secret_access_key",),
        re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*[\"']?)[A-Za-z0-9/+]{40}"),
        folded=True,
    ),
    _token("google-api-key", ("AIza",), r"AIza[0-9A-Za-z_-]{35}"),
    _token("google-oauth-secret", ("GOCSPX-",), r"GOCSPX-[0-9A-Za-z_-]{28}"),
    Rule("google-oauth-token", ("ya29.",), re.compile(_START + r"ya29\.[0-9A-Za-z_-]{30,}")),
    Rule("slack-token", ("xox",), re.compile(_START + r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    _token("slack-app-token", ("xapp-",), r"xapp-[0-9]-[0-9A-Za-z]+-[0-9]+-[0-9A-Za-z]+"),
    Rule(
        "slack-webhook",
        ("hooks.slack.com",),
        re.compile(r"hooks\.slack\.com/(?:services|workflows|triggers)/[0-9A-Za-z+/]{20,}"),
    ),
    Rule(
        "stripe-key",
        ("sk_live_", "rk_live_", "sk_test_", "rk_test_"),
        re.compile(_START + r"[sr]k_(?:live|test)_[A-Za-z0-9]{20,}"),
    ),
    Rule("huggingface-token", ("hf_",), re.compile(_START + r"hf_[A-Za-z0-9]{30,}")),
    _token("npm-token", ("npm_",), r"npm_[0-9A-Za-z]{36}"),
    Rule("pypi-token", ("pypi-AgEIcHlwaS5vcmc",), re.compile(_START + r"pypi-AgEIcHlwaS5vcmc[0-9A-Za-z_-]{50,}")),
    _token("sendgrid-key", ("SG.",), r"SG\.[0-9A-Za-z_-]{22}\.[0-9A-Za-z_-]{43}"),
    _token("digitalocean-token", ("dop_v1_", "doo_v1_", "dor_v1_"), r"do[por]_v1_[0-9a-f]{64}"),
    _token("tailscale-key", ("tskey-",), r"tskey-[a-z]+-[0-9A-Za-z]+-[0-9A-Za-z]{16,}"),
    _token("databricks-token", ("dapi",), r"dapi[0-9a-f]{32}(?:-[0-9])?"),
    _token("linear-key", ("lin_api_",), r"lin_api_[0-9A-Za-z]{40}"),
    _token("age-secret-key", ("AGE-SECRET-KEY-1",), r"AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}"),
    Rule("onepassword-token", ("ops_eyJ",), re.compile(_START + r"ops_eyJ[0-9A-Za-z+/]{100,}={0,3}")),
    Rule(
        "jwt",
        ("eyJ",),
        re.compile(_START + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    Rule("azure-account-key", ("AccountKey=",), re.compile(r"(AccountKey=)[0-9A-Za-z+/]{80,90}={0,2}")),
    Rule("bearer-token", ("bearer",), re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}"), folded=True),
    Rule("url-password", ("://",), re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?=@)")),
    Rule(
        "cookie-header",
        ("cookie",),
        # The value ends at a quote or backslash, except that a double-quoted
        # cookie value right after `=` is taken whole.
        re.compile(
            r"(?i)(\b(?:set-)?cookie\\?[\"']?\s{0,3}:\s{0,3}\\?[\"']?)"
            r"(?:[^\r\n\"'\\]|(?<==)\"[^\"\r\n\\]{0,400}\"){8,1000}"
        ),
        folded=True,
        valid=_cookie,
    ),
)


def _allowed(value: str) -> bool:
    """Documented example credentials, such as AWS's AKIAIOSFODNN7EXAMPLE."""

    return value.endswith("EXAMPLE") or "EXAMPLEKEY" in value


def _mask(rule: Rule, match: re.Match[str]) -> str:
    kept = match.group(1) if rule.pattern.groups else ""
    value = match.group(0)[len(kept) :]
    if _allowed(value) or (rule.valid is not None and not rule.valid(value)):
        return match.group(0)
    return f"{kept}[redacted:{rule.kind}]"


def applies(rule: Rule, text: str, folded: str) -> bool:
    return any(keyword in (folded if rule.folded else text) for keyword in rule.keywords)


def redact(text: str) -> str:
    folded = text.lower()
    for rule in RULES:
        if applies(rule, text, folded):
            text = rule.pattern.sub(lambda m, r=rule: _mask(r, m), text)
            folded = text.lower()
    return text
