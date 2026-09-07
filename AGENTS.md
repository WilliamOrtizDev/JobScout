# AI Agent Setup Contract

This repository is safe to hand to an AI agent for installation only when the operator controls the host and reviews credential/account steps.

## Objective

Install or restore the contract-job scout without copying another candidate's identity, losing SQLite lifecycle state, exposing secrets, or enabling application submission without explicit approval.

## Required reading

1. `docs/GETTING_STARTED.md`
2. `docs/BROWSER_SETUP.md`
3. `docs/MIGRATION.md` when moving existing state
4. `ARCHITECTURE.md`
5. `skill/continuous-job-search/SKILL.example.md`

## Hard rules

- Never commit private profiles, resumes, databases, packets, browser state, credentials, tokens, or generated runtime files.
- Never copy another user's private configuration as a template. Use tracked `*.example.*` files.
- Never invent required candidate facts. Leave unknown values `null` and ask the operator.
- Never submit an application. Installation and smoke testing do not authorize submission.
- Never create job-board accounts, bypass CAPTCHA/MFA, or export browser cookies.
- Never enable the scheduler until doctor, tests, one deterministic collection, browser readiness, and delivery checks pass.
- Pause the source scheduler before migration and never run two hosts against the same SQLite lifecycle.
- Do not enable `APPLY_HERMES_PATCH=1` unless the current Hermes release still exhibits the documented compatibility problem and the patch applies cleanly.

## Setup sequence

On a clean Debian/Ubuntu host:

    ./scripts/bootstrap.sh --all

Then stop and ask the operator to complete:

- `~/job-search/search-profile.json`
- `~/job-search/application-profile.json`
- A private LaTeX master resume
- Provider authentication through `hermes model`
- Optional browser logins
- Optional Photon account and recipient number

After the operator supplies the resume path:

    export PRIVATE_RESUME_FILE=/private/path/master-resume.tex
    ./scripts/install.sh
    python3 scripts/portable.py doctor
    python3 scripts/validate.py
    .venv-community/bin/python -m unittest discover -s tests -q

Run one deterministic collector smoke test. Report exact source successes/failures and candidate count. Do not claim the browser, Photon, cron, or delivery path is working until each was checked directly.

Create the cron only after explicit operator confirmation of the delivery target:

    PHOTON_TARGET='photon:+1XXXXXXXXXX' ./scripts/create-cron.sh

## Completion report

Report:

- Repository commit
- Runtime paths
- Provider selected, without exposing credentials
- Required and optional doctor results
- Test count and status
- Collector source health
- Browser source readiness per board
- Photon/gateway status when configured
- Cron ID, schedule, enabled state, and next run
- Backup path and successful restore-drill result for migrations
- Any manual blockers
