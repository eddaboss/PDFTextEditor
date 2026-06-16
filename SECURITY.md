# Security & privacy

## Keeping PII out of this repo

No personal data belongs in this repository: not in code, comments, tests,
fixtures, commit messages, or committed files. Editing happens on the user's own
machine; real documents stay there and are never added here.

Two automatic gates enforce this, plus one habit:

1. **Pre-commit hook** (`.githooks/pre-commit` -> `scripts/pii_guard.py`) scans
   your staged changes and blocks the commit if it finds PII or secrets. Enable
   it once per clone:

   ```bash
   git config core.hooksPath .githooks
   ```

2. **CI scan** (the `PII / secret scan` job in `.github/workflows/ci.yml`) runs
   the same scanner on every push and pull request and fails the run on any
   finding. It is the authoritative gate; the build matrix only runs after it
   passes. Protect `master` by requiring this check before merge.

3. **Habit:** never add a real document to the repo, and keep every test fixture
   synthetic (invented placeholder names, not real people or clients).

### What the scanner catches

Detection is fully automatic. There is **no name list to maintain.**

- Structured PII: US SSNs, US phone numbers, "date of birth" lines, and email
  addresses (minus the project's own contact address).
- Secrets: private-key blocks and AWS / Stripe / GitHub / Google / Slack token
  formats.
- **Person names, detected structurally:** two or more adjacent capitalized
  words (so a foreign full name absent from any name database still flags) when
  a PII context word is on the same line (`name`, `patient`, `applicant`,
  `owner`, `author`, `redact`, an honorific, a possessive...). Requiring both
  the shape and a cue is what keeps menu labels and product names ("Add Text",
  "Document AI") quiet. A candidate whose every word is also used as ordinary
  lowercase text elsewhere in the repo is treated as vocabulary, not a name.
  Blind spot worth knowing: a name made only of common English words ("Grace
  Brown") may be filtered out; the rule is tuned for the uncommon/foreign names
  that name databases miss.
- Document files (`.pdf`, `.doc(x)`, `.xls(x)`, `.csv`) added outside
  `tests/fixtures/`.

### Clearing a false positive

Every finding prints a stable `fingerprint=`. To make a re-run pass, either fix
the line or suppress the finding:

- **A flagged name only** — add a trailing `# piiguard:allow` comment on that
  line (or `# piiguard:allow-nextline` on the line above).
- **Any finding** — add its fingerprint to `.piiguardignore` with a short reason:

  ```
  a1b2c3d4e5f60718  person-name  # tests/foo.py:12 -- synthetic test persona
  ```

  `.piiguardignore` is **committed** (it holds only hashes and a reason, never
  plaintext) so CI and every clone share the same suppressions. The fingerprint
  is stable across reformatting and line moves but re-flags if the value itself
  changes.

Secrets and structured PII can be cleared **only** through `.piiguardignore`,
never with an inline comment, so a stray comment can never silently bless a real
key. To seed the ignore file for an existing tree in one shot:

```bash
python scripts/pii_guard.py --baseline   # writes current findings; edit in reasons
```

The scanner also warns (without failing) about ignore entries that match nothing
this run (stale) or are missing a reason, so the file stays auditable.

### If the scanner flags something you cannot resolve

Adjust the rules in `scripts/pii_guard.py` if it is a systematic false positive.
As a last resort, `git commit --no-verify` skips the local hook, but the CI scan
still runs, so do not rely on it.

### If PII ever does land in history

Scrubbing a file in a new commit is not enough; the data stays in history.
Rewrite history with `git filter-repo`, force-push every ref, and ask GitHub
Support to purge cached pull-request refs. Preventing it with the gates above is
far cheaper.
