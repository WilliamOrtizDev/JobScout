# Daily Use

## Safety model

Discovery does not authorize application submission. Every application requires the exact approval command shown in its packet notification:

    APPROVE <JOB-ID>

Approval is bound to one immutable packet fingerprint and one exact application URL. A material change requires a new packet revision and new approval.

Never put credentials, browser cookies, government identifiers, or secrets in profiles, packet files, prompts, logs, or Git.

## Health checks

Application runtime:

    cd "$HOME/JobScout"
    python3 scripts/portable.py doctor
    python3 scripts/state_db.py --database "$HOME/job-search/job-scout.db" status

Hermes and schedule:

    hermes doctor
    hermes gateway status
    hermes cron status
    hermes cron list --all

Photon when enabled:

    hermes photon status

The important operational states are:

- Healthy silence: scheduler ran successfully and returned `[SILENT]`.
- Active work: a current run has started and has not exceeded its normal window.
- Collector degradation: one source failed but other sources and the run succeeded.
- Execution failure: the run timed out, crashed, or was interrupted.
- Delivery failure: a packet exists in the SQLite outbox but transport did not confirm delivery.

## Run collection manually

    cd "$HOME/JobScout"
    .venv-community/bin/python scripts/collect.py \
      --profile "$HOME/job-search/search-profile.json" \
      --database "$HOME/job-search/job-scout.db"

This records collector history and candidates but does not submit applications.

Run the installed pre-run launcher exactly as Hermes does:

    "$HOME/.hermes/scripts/job-scout-shadow.py"

Run a scheduled job on its next scheduler tick:

    hermes cron run <JOB-ID>

Pause and resume:

    hermes cron pause <JOB-ID>
    hermes cron resume <JOB-ID>

## Candidate decisions

Inspect a record:

    python3 scripts/state_db.py --database "$HOME/job-search/job-scout.db" show <JOB-ID>

Approve only after reviewing the displayed job, URL, resume, and cover letter:

    APPROVE <JOB-ID>

Other commands handled by the skill:

    SKIP <JOB-ID>
    REVISE <JOB-ID> <instructions>

Do not run low-level SQLite approval or submission commands to bypass the review flow.

## Updating configuration

Edit private files only:

    $HOME/job-search/search-profile.json
    $HOME/job-search/application-profile.json
    $HOME/JobScout/config/sources.json

After changing paths, the candidate name, or the repository location, render and reinstall runtime files:

    export PRIVATE_RESUME_FILE="$HOME/job-search-resume/master-resume.tex"
    ./scripts/install.sh

Existing profiles and the master resume are preserved.

## Updating software

Before updating:

    hermes cron pause <JOB-ID>
    python3 scripts/portable.py backup "$HOME/job-search-backup-$(date +%Y%m%d).tar.gz"

Then update Hermes and this repository separately. Run all checks before resuming:

    hermes update
    git pull --ff-only
    ./scripts/install.sh
    python3 scripts/portable.py doctor
    python3 scripts/validate.py
    .venv-community/bin/python -m unittest discover -s tests -q

The optional compatibility patch is disabled by default. Do not enable it unless the current Hermes release still needs the repository-specific fixes and the patch applies cleanly.

## Backups

Create a consistent application backup while the database is live:

    python3 scripts/portable.py backup "$HOME/job-search-backup-$(date +%Y%m%d-%H%M).tar.gz"

The archive includes a SQLite backup snapshot, search/application profiles, master resume, private direct-ATS registry, and application packet files. It excludes credentials and reconstructible caches. The archive is mode `0600` but still contains personal data; store it encrypted.

The portable archive is intentionally bounded to 10,000 runtime files, 256 MiB per file, 500 MiB of runtime payload, and a 512 MiB compressed archive so an untrusted or accidental oversized archive cannot exhaust the host. If a mature installation exceeds those limits, pause the scheduler and use an encrypted filesystem-level backup for packet files while still using SQLite's online backup API for the database.

Hermes profile state is separate:

    hermes profile export default

Protect the resulting profile archive because it contains sessions, memories, skills, and cron definitions. Hermes deliberately excludes `auth.json` and `.env`; provider and messaging credentials must be configured again on the target. See [MIGRATION.md](MIGRATION.md).

## Troubleshooting sequence

1. Run `python3 scripts/portable.py doctor`.
2. Run `hermes doctor` and `hermes gateway status`.
3. Inspect `hermes cron list --all` for the last status and next run.
4. Run the installed `job-scout-shadow.py` launcher manually.
5. Check browser and Photon readiness separately.
6. Keep the scheduler paused until a failing dependency is repaired and verified.
