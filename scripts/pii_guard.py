#!/usr/bin/env python3
"""Keep PII and secrets out of the repo -- automatically, with no name list.

Run it three ways:
  * pre-commit hook:  python3 scripts/pii_guard.py --staged   (staged changes)
  * CI:               python3 scripts/pii_guard.py --all        (every tracked file)
  * ad hoc:           python3 scripts/pii_guard.py <paths...>
  * seed ignores:     python3 scripts/pii_guard.py --baseline   (writes current
                      findings to .piiguardignore so the gate starts green)

Any unsuppressed finding prints why it was flagged plus a copy-pasteable
fingerprint, and the program exits non-zero so the commit (hook) or the CI run
fails.

WHAT IT CATCHES
  - Structured PII: US SSNs (dashed/dotted/spaced, plus bare 9-digit in a tax/SSN
    context), US and international phone numbers, street addresses and
    city/state/ZIP, dates of birth (labeled or a bare date in a birth context),
    credit-card numbers (Luhn-checked), and emails (minus an allowlist for the
    project's own contact address). Bare-number detectors are gated on a context
    cue so they do not flag every number.
  - Secrets: private-key blocks and AWS access-key ids, AWS 40-char secret keys
    (in an aws/secret context), and Stripe / GitHub / Google / Slack tokens.
  - Person names, detected STRUCTURALLY with no maintained list: >=2 adjacent
    Titlecase tokens (e.g. a foreign full name absent from any gazetteer) when a
    PII context cue is on the same line (name/patient/applicant/owner/author/
    reviewer/redact/an honorific/a possessive...). Shape + a cue are both
    required, so menu labels and product names ("Add Text", "Document AI") do not
    flag. Candidates whose every token also appears lowercased elsewhere in the
    repo are dropped as ordinary vocabulary -- a data-driven filter with nothing
    to maintain. Known blind spot: a name made of common English words ("Grace
    Brown") can be filtered out; the structural rule is tuned for the foreign /
    uncommon names that gazetteers miss.
  - Document files (.pdf/.doc/.docx/.xls/.xlsx/.csv) added outside tests/fixtures.

CLEARING A FALSE POSITIVE
  Either fix the line, or suppress the finding so the re-run passes:
    * person names only:  add a trailing  # piiguard:allow  comment on the line
      (or  # piiguard:allow-nextline  on the line above).
    * any finding:  add its printed fingerprint to .piiguardignore (committed; it
      holds only hashes, never plaintext) with a short reason after `#`.
  Secrets and structured PII can be suppressed ONLY through .piiguardignore, never
  inline, so a stray comment can never silently bless a real key.

It is a strong gate, not a guarantee: never put a real document in the repo and
keep every fixture synthetic.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

# The project's own contact address is intentional; everything else is flagged.
ALLOWLIST_EMAILS = {"edw.luko@gmail.com"}
ALLOWLIST_DOMAINS = {"example.com", "example.org", "example.net", "email.com"}
# "@2x.png" etc. are filenames, not emails -- their "TLD" is a file extension.
_FILE_EXT_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "icns",
                  "css", "js", "ts", "py", "md", "json", "html", "txt", "pdf",
                  "spec"}

# (rule_id, human label, compiled regex). rule_id is a stable slug used in
# fingerprints and the ignore file.
PATTERNS = [
    ("private-key", "private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-key", "AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("stripe-key", "Stripe secret key", re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("github-token", "GitHub token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b")),
    ("google-key", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", "Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    # SSN: dash, dot, or space separated (123-45-6789 / 078 05 1120).
    ("us-ssn", "US SSN", re.compile(r"\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b")),
    # Phone: (415) 555-0182, and 3-3-4 with dash/dot/space (212.555.0147).
    ("us-phone", "US phone", re.compile(
        r"\(\d{3}\)\s?\d{3}[-.\s]\d{4}\b|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")),
    # International: +<cc> then grouped digits (+44 20 7946 0958).
    ("intl-phone", "international phone", re.compile(
        r"\+\d{1,3}[\s.\-]\(?\d{2,4}\)?(?:[\s.\-]?\d{2,6}){1,5}")),
    # Street address: number + name words + a street suffix.
    ("address", "street address", re.compile(
        r"\b\d{1,6}\s+(?:[A-Z][A-Za-z'.\-]+\s+){1,4}"
        r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|"
        r"Court|Ct|Way|Place|Pl|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|"
        r"Hwy|Crescent|Cres|Square|Sq|Trail|Trl|Loop|Run|Row|Plaza)\b\.?")),
    # City/state/ZIP: a real US state abbreviation + ZIP (NM 87104).
    ("address", "city/state/ZIP", re.compile(
        r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
        r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
        r"VT|VA|WA|WV|WI|WY|DC)\s+\d{5}(?:-\d{4})?\b")),
]
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
EMAIL_RULE = "email"

# --- Context-gated detectors -------------------------------------------------
# A bare number is only PII in context, so these fire only when a cue word is on
# the line. Keeps the gate precise instead of flagging every 9/10-digit number.

# Date shapes for the date-of-birth detector (bare date in a birth context).
_DATE = re.compile(
    r"\b(?:"
    r"\d{4}-\d{2}-\d{2}"                                          # 1990-11-02
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"                         # 04/17/1986
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r")\b", re.I)


def _has(words, *cues):
    return any(c in words for c in cues)


# (rule_id, label, value_regex, line-cue predicate over the lowercased word set)
CONTEXT_PATTERNS = [
    ("us-ssn", "US SSN (unformatted)", re.compile(r"\b\d{9}\b"),
     lambda w: _has(w, "ssn", "ssns", "itin", "taxpayer")
               or (_has(w, "social") and _has(w, "security"))
               or (_has(w, "tax") and _has(w, "id"))),
    ("us-phone", "US phone (unformatted)", re.compile(r"\b\d{10}\b"),
     lambda w: _has(w, "phone", "telephone", "tel", "fax", "mobile", "cell")),
    ("aws-secret", "AWS secret key",
     re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
     lambda w: _has(w, "aws") and _has(w, "secret", "key")),
    ("dob", "date of birth", _DATE,
     lambda w: _has(w, "dob", "birth", "born", "birthdate", "birthday",
                    "natal", "dateofbirth")),
]

# --- Credit-card numbers (Luhn-checked, so random digit runs do not flag) -----
_CC_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0

DOC_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv")
ALLOWED_DOC_DIRS = ("tests/fixtures/",)
DOC_RULE = "document-file"

# Only low-severity findings may be cleared by an inline comment. Secrets and
# structured PII must go through the reviewable .piiguardignore file.
INLINE_ALLOWED_RULES = {"person-name"}

# This scanner names patterns and grammar words but never any individual; skip it
# so its own tables and regexes are not self-flagged.
SKIP_FILES = {"scripts/pii_guard.py"}

IGNORE_FILE = ".piiguardignore"

# ---------------------------------------------------------------------------
# Person-name heuristic. The sets below are closed English grammar / common
# nouns, NOT a list of people -- they never name an individual.
# ---------------------------------------------------------------------------

# Tokens that may sit BETWEEN two name parts ("Maria de Souza", "Robert van Dijk").
PARTICLES = frozenset({
    "de", "del", "della", "da", "di", "van", "von", "der", "den", "ten", "ter",
    "la", "le", "du", "dos", "das", "bin", "ibn", "al", "el", "mac", "mc", "st",
})

# A PII context cue anywhere on the line is required before a name candidate is
# flagged. Underscores/punctuation are split, so "guardian_name" yields both.
LINE_CUES = frozenset({
    "name", "named", "naming", "patient", "patients", "customer", "customers",
    "client", "clients", "applicant", "applicants", "owner", "owners",
    "guardian", "guardians", "author", "authors", "authored", "reviewer",
    "reviewers", "assignee", "assignees", "assigned", "recipient", "recipients",
    "sender", "employee", "employees", "beneficiary", "dependent", "dependents",
    "spouse", "nominee", "witness", "witnesses", "plaintiff", "defendant",
    "claimant", "payee", "signed", "redact", "redacted", "redaction", "dob",
    "ssn", "born", "dear", "attn", "attendee", "attendees", "guarantor",
    "cosigner",
})
HONORIFICS = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "professor", "rev", "sir", "madam",
    "esq", "hon",
})

# Common function / sentence words and generic UI verbs that are never names.
STOPWORDS = frozenset({
    "the", "this", "that", "these", "those", "a", "an", "and", "or", "but", "if",
    "for", "while", "return", "with", "from", "when", "where", "why", "how",
    "it", "we", "you", "they", "he", "she", "note", "see", "todo", "fixme",
    "warning", "error", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "at", "by", "as", "not", "all", "any", "each", "use", "used",
    "using", "add", "set", "get", "run", "save", "open", "close", "show", "hide",
    "edit", "view", "find", "sort", "load", "copy", "move", "make", "build",
    "test", "fix", "update", "delete", "remove", "insert", "export", "import",
    "select", "clear", "reset", "enable", "disable", "true", "false", "none",
    "null", "also", "then", "else", "via", "per", "yes", "no",
    # generic build / CI / packaging / license words -- never a person
    "install", "setup", "upload", "download", "checkout", "generate", "publish",
    "deploy", "configure", "compile", "package", "release", "bundle", "sign",
    "notarize", "scan", "copyright", "version", "artifact", "manage", "tools",
})
TECH_NOUNS = frozenset({
    "ai", "ml", "api", "sdk", "ui", "ux", "cli", "url", "http", "https", "json",
    "xml", "html", "css", "sql", "cloud", "studio", "native", "server", "hub",
    "kafka", "docker", "kubernetes", "python", "java", "javascript",
    "typescript", "react", "node", "vue", "angular", "django", "flask",
    "fastapi", "pyside", "qt", "postgres", "postgresql", "redis", "linux",
    "windows", "macos", "github", "gitlab", "bitbucket", "aws", "gcp", "azure",
    "stripe", "slack", "google", "microsoft", "apple", "amazon", "railway",
})
PLACE_PREFIXES = frozenset({
    "new", "san", "santa", "los", "las", "cape", "fort", "lake", "mount",
    "saint", "st", "port", "north", "south", "east", "west",
})
MONTHS_DAYS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue", "wed",
    "thu", "fri", "sat", "sun",
})

# A letter token: starts uppercase, allows accents and internal '/-/. marks.
_TOKEN = re.compile(r"[^\W\d_]+(?:['’.\-][^\W\d_]+)*", re.UNICODE)
_LOWER_WORD = re.compile(r"\b[a-z]{3,}\b")


def _is_title(tok: str) -> bool:
    """Titlecase-ish: >=2 chars, leading uppercase, not an ALL-CAPS acronym."""
    return len(tok) >= 2 and tok[0].isupper() and not tok.isupper()


def _name_runs(line: str):
    """Yield (start, end, title_tokens[]) for maximal whitespace-joined runs of
    Titlecase tokens (optionally bridged by a single particle)."""
    toks = list(_TOKEN.finditer(line))
    i, n = 0, len(toks)
    while i < n:
        if not _is_title(toks[i].group()):
            i += 1
            continue
        run = [toks[i]]
        j = i + 1
        while j < n:
            gap = line[toks[j - 1].end():toks[j].start()]
            if gap.strip():  # only whitespace may join name parts
                break
            cur = toks[j].group()
            if _is_title(cur):
                run.append(toks[j])
                j += 1
                continue
            # allow a lowercase particle if a Titlecase token follows it
            if (cur.lower() in PARTICLES and j + 1 < n
                    and not line[toks[j].end():toks[j + 1].start()].strip()
                    and _is_title(toks[j + 1].group())):
                run.append(toks[j])
                j += 1
                continue
            break
        titles = [r.group() for r in run if _is_title(r.group())]
        if 2 <= len(titles) <= 4:
            yield run[0].start(), run[-1].end(), titles
        i = max(j, i + 1)


def _has_line_cue(line: str) -> bool:
    words = set(re.split(r"[^a-z0-9]+", line.lower()))
    return bool(words & LINE_CUES)


def _suppressed(titles, common_lower) -> bool:
    low = [t.lower() for t in titles]
    if any(t in STOPWORDS or t in TECH_NOUNS or t in MONTHS_DAYS for t in low):
        return True
    if low and low[0] in PLACE_PREFIXES:
        return True
    # Data-driven: if every part is also used as an ordinary lowercase word in
    # the repo, it is vocabulary (e.g. "Text Editor"), not a person.
    if common_lower and all(t in common_lower for t in low):
        return True
    return False


def _name_signal(line: str, start: int, end: int, line_cue: bool):
    if line_cue:
        return "PII context cue on line"
    pre = line[:start]
    m = re.search(r"([^\W\d_]+)[\s.]*$", pre, re.UNICODE)
    if m:
        w = m.group(1).lower()
        if w in HONORIFICS:
            return "honorific before name"
        if w in {"by", "from"}:
            return "follows 'by/from'"
    if re.match(r"['’]s\b", line[end:]):
        return "possessive 's"
    return None


def scan_names(line: str, common_lower):
    line_cue = _has_line_cue(line)
    out = []
    for start, end, titles in _name_runs(line):
        if _suppressed(titles, common_lower):
            continue
        sig = _name_signal(line, start, end, line_cue)
        if not sig:
            continue
        out.append((line[start:end], sig))
    return out


# ---------------------------------------------------------------------------
# Findings, fingerprints, suppression
# ---------------------------------------------------------------------------

def fingerprint(rel_path: str, rule_id: str, value: str) -> str:
    """Stable across reformatting / line moves, re-flags if the value changes.
    Hashes only the value, never stores plaintext."""
    norm_path = rel_path.replace("\\", "/")
    norm_val = value.replace("\r", "").strip().casefold()
    inner = hashlib.sha256(norm_val.encode("utf-8")).hexdigest()
    raw = f"{norm_path}:{rule_id}:{inner}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def scan_text(path: str, text: str, common_lower=None):
    """Return a list of findings: (rule_id, label, line_no, idx, value, why)."""
    findings = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        line_no = idx + 1
        for rule_id, label, rx in PATTERNS:
            for m in rx.finditer(line):
                findings.append((rule_id, label, line_no, idx, m.group(0), label))
        words = set(re.split(r"[^a-z0-9]+", line.lower()))
        for rule_id, label, rx, cue in CONTEXT_PATTERNS:
            if cue(words):
                for m in rx.finditer(line):
                    findings.append((rule_id, label, line_no, idx, m.group(0), label))
        for m in _CC_CANDIDATE.finditer(line):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                findings.append(("credit-card", "credit card number", line_no,
                                 idx, m.group(0), "Luhn-valid 13-19 digit PAN"))
        for m in EMAIL.finditer(line):
            email, domain = m.group(0), m.group(1)
            tld = domain.rsplit(".", 1)[-1].lower()
            if (email.lower() in ALLOWLIST_EMAILS
                    or domain.lower() in ALLOWLIST_DOMAINS
                    or tld in _FILE_EXT_TLDS):
                continue
            findings.append((EMAIL_RULE, "email address", line_no, idx, email, "email address"))
        for value, sig in scan_names(line, common_lower):
            findings.append(("person-name", "possible person name", line_no, idx,
                             value, f">=2 Titlecase tokens; {sig}"))
    return findings, lines


def _inline_allowed(lines, idx, rule_id) -> bool:
    if rule_id not in INLINE_ALLOWED_RULES:
        return False
    cur = lines[idx] if 0 <= idx < len(lines) else ""
    prev = lines[idx - 1] if idx - 1 >= 0 else ""
    m = re.search(r"piiguard:allow(?!-nextline)(?:=([\w-]+))?", cur)
    if m and m.group(1) in (None, rule_id):
        return True
    m = re.search(r"piiguard:allow-nextline(?:=([\w-]+))?", prev)
    if m and m.group(1) in (None, rule_id):
        return True
    return False


def load_ignore():
    """Return (fingerprint -> reason, [fingerprints missing a reason])."""
    ignores, no_reason = {}, []
    if os.path.exists(IGNORE_FILE):
        with open(IGNORE_FILE, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                code, _, reason = line.partition("#")
                parts = code.split()
                if not parts:
                    continue
                fp = parts[0]
                ignores[fp] = reason.strip()
                if not reason.strip():
                    no_reason.append(fp)
    return ignores, no_reason


# ---------------------------------------------------------------------------
# File enumeration + repo vocabulary
# ---------------------------------------------------------------------------

def git_files(mode: str):
    if mode == "--staged":
        args = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:  # --all / --baseline
        args = ["git", "ls-files"]
    r = subprocess.run(args, capture_output=True, text=True)
    return [f for f in r.stdout.splitlines() if f]


def _read(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError,
            PermissionError, OSError):
        return None


def build_common_lower(texts) -> set:
    """Words that appear as ordinary lowercase tokens anywhere in the repo. A
    name candidate whose every part is in here is treated as vocabulary."""
    common = set()
    for text in texts:
        if text:
            # Words that are ALREADY lowercase in the source (do not lowercase
            # first, or every Titlecase name would count as common vocabulary).
            common.update(_LOWER_WORD.findall(text))
    return common


# ---------------------------------------------------------------------------
# Output / baseline
# ---------------------------------------------------------------------------

REDACTED = "[redacted -- value not printed]"


def _shown(rule_id: str, value: str) -> str:
    # Show the candidate name (the reviewer must judge it; it is already in the
    # diff); never echo a secret or structured identifier into the logs.
    return value if rule_id == "person-name" else REDACTED


def _ignore_line(fp: str, rule_id: str, path: str, line_no: int) -> str:
    return f"{fp}  {rule_id}  # {path}:{line_no} -- replace with the reason it is safe"


def write_baseline(findings, docs) -> int:
    existing, _ = load_ignore()
    seen = set(existing)
    new_lines = []
    for (path, rule_id, _label, line_no, _idx, _value, _why, fp) in findings:
        if fp not in seen:
            seen.add(fp)
            new_lines.append(_ignore_line(fp, rule_id, path, line_no))
    for (path, fp) in docs:
        if fp not in seen:
            seen.add(fp)
            new_lines.append(_ignore_line(fp, DOC_RULE, path, 0))
    if not new_lines:
        print("Nothing new to baseline; .piiguardignore already covers all findings.")
        return 0
    header = (f"# {IGNORE_FILE} -- suppressed findings (hashes only, no plaintext).\n"
              f"# Each line: <fingerprint>  <rule>  # reason. Replace the placeholder\n"
              f"# reason with why the finding is a false positive before committing.\n")
    write_header = not os.path.exists(IGNORE_FILE)
    with open(IGNORE_FILE, "a", encoding="utf-8") as fh:
        if write_header:
            fh.write(header)
        fh.write("\n".join(new_lines) + "\n")
    print(f"Wrote {len(new_lines)} entr{'y' if len(new_lines) == 1 else 'ies'} "
          f"to {IGNORE_FILE}. Edit in the real reasons, then commit.")
    return 0


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "--staged"
    baseline = arg == "--baseline"
    if baseline or arg in ("--staged", "--all"):
        files = git_files("--all" if baseline else arg)
    else:
        files = sys.argv[1:]

    # Build repo vocabulary from all tracked files (stable regardless of mode)
    # plus whatever we are scanning, reading each file once.
    corpus_files = set(files)
    if arg in ("--staged", "--all") or baseline:
        corpus_files.update(git_files("--all"))
    cache = {f: _read(f) for f in corpus_files}
    common_lower = build_common_lower(cache.values())

    ignores, no_reason = load_ignore()
    used = set()
    findings, docs = [], []

    for f in files:
        if f in SKIP_FILES:
            continue
        if (f.lower().endswith(DOC_EXTS)
                and not any(f.startswith(d) for d in ALLOWED_DOC_DIRS)):
            fp = fingerprint(f, DOC_RULE, "")
            if fp in ignores:
                used.add(fp)
            else:
                docs.append((f, fp))
        text = cache.get(f)
        if text is None:
            text = _read(f)
        if text is None:
            continue
        raw, lines = scan_text(f, text, common_lower)
        for (rule_id, label, line_no, idx, value, why) in raw:
            if _inline_allowed(lines, idx, rule_id):
                continue
            fp = fingerprint(f, rule_id, value)
            if fp in ignores:
                used.add(fp)
                continue
            findings.append((f, rule_id, label, line_no, idx, value, why, fp))

    if baseline:
        return write_baseline(findings, docs)

    total = len(findings) + len(docs)
    if total:
        plural = "s" if total != 1 else ""
        print(f"PII GUARD: {total} finding{plural} -- this gate blocks the merge.\n")
        for (path, rule_id, _label, line_no, _idx, value, why, fp) in findings:
            print(f"  {path}:{line_no}  [{rule_id}]  fingerprint={fp}")
            print(f"      why:  {why}")
            print(f"      text: {_shown(rule_id, value)}")
            if rule_id in INLINE_ALLOWED_RULES:
                print(f"      fix it, OR add `# piiguard:allow` on that line, OR add to {IGNORE_FILE}:")
            else:
                print(f"      fix it (remove/rotate), OR add to {IGNORE_FILE} (cannot be cleared inline):")
            print(f"          {_ignore_line(fp, rule_id, path, line_no)}")
            print()
        for (path, fp) in docs:
            print(f"  {path}  [{DOC_RULE}]  fingerprint={fp}")
            print("      why:  document outside tests/fixtures -- may contain PII")
            print(f"      remove it, OR add to {IGNORE_FILE}:")
            print(f"          {_ignore_line(fp, DOC_RULE, path, 0)}")
            print()
        print("  ----")
        print("  Fingerprints are stable across reformatting and re-flag if the value")
        print(f"  changes. Re-run after fixing or adding the entry to {IGNORE_FILE}.")
        print("  `git commit --no-verify` skips the local hook but NOT this CI gate.")

    # Non-fatal hygiene warnings (do not fail the gate). Stale detection only
    # makes sense on a full-tree scan; in --staged/paths mode most entries
    # legitimately do not match because their files are not in scope.
    stale = sorted(set(ignores) - used) if arg == "--all" else []
    warn = []
    if stale:
        warn.append(f"{len(stale)} {IGNORE_FILE} entr{'y' if len(stale) == 1 else 'ies'} "
                    f"matched nothing this run (stale -- consider removing): "
                    + ", ".join(stale))
    if no_reason:
        warn.append(f"{len(no_reason)} {IGNORE_FILE} entr{'y' if len(no_reason) == 1 else 'ies'} "
                    f"missing a reason comment: " + ", ".join(no_reason))
    for w in warn:
        print(f"\n  note: {w}", file=sys.stderr)

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
