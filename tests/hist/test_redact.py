"""Redaction rules. Every token here is synthetic and assembled from parts,
so that no scanner mistakes this file for a leak."""

import unittest

from hist.redact import RULES, redact

ALNUM = "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zC"


def run(n: int) -> str:
    return (ALNUM * (n // len(ALNUM) + 1))[:n]


class RedactTest(unittest.TestCase):
    def test_vendor_shapes_are_masked(self) -> None:
        cases = {
            "google-oauth-secret": "GOCSPX" + "-" + run(28),
            "google-oauth-token": "ya29" + "." + run(40),
            "slack-app-token": "xapp" + "-1-A0B1C2D3-1234567890-" + run(30),
            "slack-webhook": "hooks.slack" + ".com/services/" + run(40),
            "npm-token": "npm" + "_" + run(36),
            "pypi-token": "pypi" + "-AgEIcHlwaS5vcmc" + run(60),
            "sendgrid-key": "SG" + "." + run(22) + "." + run(43),
            "digitalocean-token": "dop" + "_v1_" + "a1b2c3d4" * 8,
            "tailscale-key": "tskey" + "-auth-k1234-" + run(20),
            "databricks-token": "dapi" + "0123456789abcdef" * 2,
            "linear-key": "lin" + "_api_" + run(40),
            "age-secret-key": "AGE-SECRET" + "-KEY-1" + "QPZRY9X8GF" * 5 + "QPZRY9X8",
            "onepassword-token": "ops" + "_eyJ" + run(120),
        }
        for kind, token in cases.items():
            with self.subTest(kind):
                self.assertEqual(redact(f"key: {token} end"), f"key: [redacted:{kind}] end")

    def test_a_json_escape_counts_as_a_boundary(self) -> None:
        token = "GOCSPX" + "-" + run(28)
        self.assertEqual(redact(f'{{"output":"line\\n{token}\\tnext"}}'),
                         '{"output":"line\\n[redacted:google-oauth-secret]\\tnext"}')
        self.assertEqual(redact(f"x{token}"), f"x{token}")
        github = "ghp" + "_" + run(36)
        self.assertEqual(redact(f"a\\n{github}"), "a\\n[redacted:github-token]")

    def test_context_is_kept_and_examples_are_not_masked(self) -> None:
        key = run(86) + "=="
        self.assertEqual(redact(f"DefaultEndpointsProtocol=https;AccountKey={key};EndpointSuffix=x"),
                         "DefaultEndpointsProtocol=https;AccountKey=[redacted:azure-account-key];EndpointSuffix=x")
        self.assertEqual(redact("AK" + "IAIOSFODNN7EXAMPLE"), "AK" + "IAIOSFODNN7EXAMPLE")

    def test_cookie_headers_are_masked_only_when_they_look_real(self) -> None:
        self.assertEqual(redact(f"Cookie: session={run(24)}; theme=dark"), "Cookie: [redacted:cookie-header]")
        self.assertEqual(redact(f'-H "set-cookie: sid={run(16)}"'), '-H "set-cookie: [redacted:cookie-header]"')
        # A quoted value after `=` belongs to the header, as do the cookies after it.
        joined = f'Set-Cookie: gid="{run(20)}"; Max-Age=9, guest={run(18)}; Path=/'
        self.assertEqual(redact(joined), "Set-Cookie: [redacted:cookie-header]")
        self.assertEqual(redact(f"{{'Set-Cookie': 'gid=\"{run(20)}\"; Path=/'}}"),
                         "{'Set-Cookie': '[redacted:cookie-header]'}")
        for kept in ("Cookie: session=${token}", "cookie: the jar is empty today", "Cookie: a=aaaaaaaaaaaa"):
            with self.subTest(kept):
                self.assertEqual(redact(kept), kept)

    def test_every_rule_has_keywords_its_matches_contain(self) -> None:
        for rule in RULES:
            with self.subTest(rule.kind):
                self.assertTrue(rule.keywords)
                if rule.folded:
                    self.assertTrue(all(k == k.lower() for k in rule.keywords))
