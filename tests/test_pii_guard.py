#!/usr/bin/env python3
"""Regression tests for scripts/pii_guard.py.

Run directly:  python tests/test_pii_guard.py

PII/secret literals here are assembled at runtime from fragments so that the
scanner finds nothing when it scans THIS file, and the person-name cases use
invented synthetic tokens (never a real individual).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import pii_guard as pg  # noqa: E402


def _mk(*parts):
    """Join fragments at runtime; keeps the source line un-flaggable."""
    return "".join(parts)


def _rules(line, common=None):
    findings, _ = pg.scan_text("sample.py", line, common)
    return {f[0] for f in findings}


def test_structured_and_secrets():
    cases = [
        (_mk("ssn = ", "123-45-", "6789"), "us-ssn"),
        (_mk("call ", "415-555", "-0182 today"), "us-phone"),
        (_mk("DOB", ": 04/17", "/1986"), "dob"),
        (_mk("-----BEGIN ", "RSA PRIVATE KEY", "-----"), "private-key"),
        (_mk("key=", "AKIA", "IOSFODNN7EXAMPLE"), "aws-key"),
        (_mk("k=", "sk_live_", "51HQ8aZK7xPq3mNvR2dLwYbT0"), "stripe-key"),
        (_mk("t=", "ghp_", "R8nQ2vWmKx7TzLpA4cYf6BdJ1eHsG0uViXoN"), "github-token"),
        (_mk("g=", "AIza", "SyB7x9Qf2KmNpR4tVwL3zXcD8aH1jE0sGuY"), "google-key"),
        (_mk("s=", "xoxb-", "2488291043792-2491038475610-Hq7Pn3RkLm9TvZ"), "slack-token"),
        (_mk("e = ", "qa", "@", "proton", ".me"), "email"),
    ]
    for line, rule in cases:
        assert rule in _rules(line), f"{rule} not detected in: {line!r}"


def test_broadened_structured_formats():
    cases = [
        (_mk("ssn ", "078", " 05", " 1120"), "us-ssn"),          # space-separated
        (_mk("ssn ", "123", ".45", ".6789"), "us-ssn"),          # dot-separated
        (_mk("tax", "_id=", "456789", "012"), "us-ssn"),         # bare 9-digit + cue
        (_mk("call ", "212.555", ".0147"), "us-phone"),          # dotted phone
        (_mk("fax", "_line: ", "8005", "550133"), "us-phone"),   # bare 10-digit + cue
        (_mk("contact ", "+44 20", " 7946 0958"), "intl-phone"),  # international
        (_mk("home ", "2210 Birchwood", " Ln"), "address"),      # street suffix
        (_mk("ship ", "Albuquerque, ", "NM ", "87104"), "address"),  # state/ZIP
        (_mk("card ", "4111 1111 ", "1111 1111"), "credit-card"),  # Luhn-valid PAN
        (_mk("aws_secret_access_key = ", "wJalrXUtnFEMI/K7MDENG/",
             "bPxRfiCYEXAMPLEKEY"), "aws-secret"),               # 40-char secret + cue
        (_mk("patient", "_dob = ", "22 March", " 1974"), "dob"),  # bare date + cue
    ]
    for line, rule in cases:
        assert rule in _rules(line), f"{rule} not detected in: {line!r}"


def test_context_gating_keeps_precision():
    # Bare numbers and dates must NOT flag without their context cue.
    assert "us-ssn" not in _rules(_mk("build ", "123456", "789 items"))
    assert "us-phone" not in _rules(_mk("id = ", "12345", "67890"))
    assert "dob" not in _rules(_mk("released ", "2020-01", "-15 to prod"))
    # A 16-digit run that fails the Luhn check is not a card number.
    assert "credit-card" not in _rules(_mk("ref ", "4111 1111 ", "1111 1112"))
    # A 40-char base64 blob without an aws/secret cue does not flag.
    assert "aws-secret" not in _rules(_mk("hash=", "abcdefghij0123456789",
                                          "abcdefghij0123456789"))


def test_allowlisted_email_not_flagged():
    line = _mk("contact ", "edw.luko", "@", "gmail.com")
    assert "email" not in _rules(line), "allowlisted email should not flag"


def test_foreign_names_caught():
    # Invented synthetic names: structure + a context cue must flag, no list.
    cases = [
        _mk("reviewer assigned: ", "Vextor", " ", "Kalim"),
        _mk("applicant ", "name", ": ", "Soraya", " ", "Quenel"),
        _mk("guardian_", "name", " = ", "Drazen", " ", "Malovic"),
        _mk("redact ", "Aigera", " ", "Toktarova", " from the export"),
    ]
    for line in cases:
        assert "person-name" in _rules(line), f"name not detected in: {line!r}"


def test_name_needs_a_cue():
    # Same name shape with NO cue must NOT flag (keeps menu labels quiet).
    line = _mk("title = ", "Quber", " ", "Naxen")
    assert "person-name" not in _rules(line), "name without a cue should not flag"


def test_no_false_positives():
    # None of these has a PII cue; none should flag.
    quiet = [
        "class DocumentProcessor(QWidget):",
        "from fastapi import FastAPI, Depends, HTTPException",
        "We use Google Document AI for OCR on uploaded pages.",
        "CI runs on GitHub Actions for every push to dev.",
        "## Configuration Options For Production Deployment",
        'WINDOW_TITLE = "PDF Text Editor - Clay"',
        '@router.get("/health", tags=["System Health"])',
        "model = LinearRegression().fit(X_train, y_train)",
        "class NewYorkTimeZoneAdapter(BaseAdapter):",
        "- name: Install Inno Setup (Windows)",
    ]
    for line in quiet:
        assert not _rules(line), f"unexpected finding in: {line!r}"


def test_data_driven_vocabulary_filter():
    # A cue + a Titlecase bigram whose parts are ordinary repo words is dropped.
    line = _mk("owner", ": ", "Text", " ", "Editor")
    assert "person-name" in _rules(line, common=None), "no vocab => should flag"
    assert "person-name" not in _rules(line, common={"text", "editor"}), \
        "both parts common => should be filtered as vocabulary"


def test_fingerprint_stability():
    ssn = _mk("123-45", "-6789")   # fragments so this file does not self-flag
    ssn2 = _mk("123-45", "-6780")
    a = pg.fingerprint("a/b.py", "us-ssn", ssn)
    # reformatting the value (whitespace / case) does not change the fingerprint
    assert a == pg.fingerprint("a/b.py", "us-ssn", "  " + ssn + "  ")
    # a different value re-flags (new fingerprint)
    assert a != pg.fingerprint("a/b.py", "us-ssn", ssn2)
    # a different rule or path is scoped separately
    assert a != pg.fingerprint("a/b.py", "person-name", ssn)
    assert a != pg.fingerprint("c/d.py", "us-ssn", ssn)
    # windows backslashes normalize to posix so fingerprints are portable
    assert (pg.fingerprint("a/b.py", "us-ssn", ssn)
            == pg.fingerprint("a\\b.py", "us-ssn", ssn))


def test_inline_allow_clears_name_not_secret():
    # scan_text always reports the finding; main() decides suppression via
    # _inline_allowed, which is where the secret-vs-name policy lives.
    name_line = _mk("owner ", "Soraya", " ", "Quenel", "  # piiguard:allow")
    assert "person-name" in _rules(name_line), "name should still be detected"
    assert pg._inline_allowed([name_line], 0, "person-name"), \
        "inline allow should clear a person-name"

    # An inline comment must NEVER clear a secret -- only the ignore file can.
    secret_line = _mk("key = ", "AKIA", "IOSFODNN7EXAMPLE", "  # piiguard:allow")
    assert not pg._inline_allowed([secret_line], 0, "aws-key"), \
        "inline allow must not suppress a secret"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} pii_guard tests passed.")


if __name__ == "__main__":
    main()
