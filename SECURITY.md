# Security

The repository contains only reusable code, examples, templates, and documentation. Candidate profiles, resumes, SQLite databases, application packets, browser sessions, credentials, MFA codes, government identifiers, private keys, OAuth tokens, API keys, delivery targets, and runtime caches must never be committed.

Provider credentials belong in Hermes credential storage or `~/.hermes/.env`. The committed `.env.example` contains names and placeholders only. Browser profiles are credential material and remain outside normal backups.

`python3 scripts/portable.py backup` intentionally includes personal profiles, the master resume, SQLite lifecycle state, and application packets. Its archive is mode `0600` and excludes credentials, but it is still sensitive personal data. Store and transfer it through an encrypted channel. Hermes profile exports exclude `auth.json` and `.env`, but still contain sessions, memories, skills, and other private material and require the same protection.

Before every push, run:

    python3 scripts/validate.py
    python3 -m unittest discover -s tests -q

If a secret is ever committed or exposed in logs, revoke it immediately and rewrite history; deleting it in a later commit is insufficient.

## Public-release gate

Before changing repository visibility or publishing a release:

1. Inspect every tracked and staged path and confirm ignored files were not force-added.
2. Run the repository validator, full tests, and portable clean-home installation smoke test.
3. Scan the complete Git history for credentials, candidate-private information, resumes, databases, application packets, browser state, backup archives, and generated runtime artifacts.
4. Confirm examples contain placeholders only and local documentation links resolve.
5. Verify deterministic and community collectors with disposable state, cache, and output paths.
6. Record browser-assisted sources as PASS, DEGRADED, BLOCKED-AUTH, or FAIL without bypassing login, MFA, CAPTCHA, or access controls.
7. Complete a migration backup/restore drill and inspect the resulting integrity report.
8. Obtain an independent fail-closed review of the exact release diff.
9. Create an immutable version tag and publish release notes that identify supported platforms and manual steps before changing visibility.

Never publish candidate profiles, resumes, SQLite/WAL/SHM files, locks, application packets or generated PDFs, browser state or cookies, Hermes profile exports, backup archives, `.env`, `auth.json`, credentials, tokens, MFA material, delivery targets, or raw job-data caches. If a history scan finds sensitive material, rotate affected credentials first and remove the material from history before publication.
