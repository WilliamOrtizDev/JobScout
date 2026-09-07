# Getting Started From a Clean Machine

This guide assumes a new Debian 12 or Ubuntu 22.04/24.04 Linux host and no installed project dependencies. macOS and other Linux distributions can work, but their system-package installation is manual.

The system has four layers:

1. Hermes Agent runs the scheduled AI workflow and browser tools.
2. This repository provides deterministic collection, SQLite state, safety policy, setup, and migration utilities.
3. Tectonic compiles tailored LaTeX resumes and cover letters.
4. Photon optionally delivers approval-ready packets through iMessage.

No job application is submitted without the exact command `APPROVE <JOB-ID>` for the exact packet and application URL.

## Accounts and external projects

Required:

- GitHub repository: https://github.com/WilliamOrtizDev/JobScout
- Hermes Agent: https://github.com/NousResearch/hermes-agent
- Hermes documentation: https://hermes-agent.nousresearch.com/docs
- One supported Hermes inference provider.

A convenient provider option is OpenAI Codex through a ChatGPT or Codex subscription. Configure it with `hermes model`, then choose `ChatGPT or Codex Subscription` and complete device-code authentication. Hermes documents authentication but does not currently document which ChatGPT tiers are eligible or exactly how usage counts against plan limits. See https://hermes-agent.nousresearch.com/docs/integrations/providers and https://chatgpt.com/pricing before purchasing a plan.

Alternatives include Nous Portal, an OpenAI API key, Anthropic API access, OpenRouter, GitHub Copilot, or another provider supported by Hermes. A ChatGPT subscription is therefore convenient, not a hard dependency.

Optional delivery:

- Photon account: https://app.photon.codes
- Photon/Hermes setup guide: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/photon
- Node.js 18.17 or newer and npm are required by the Photon sidecar.
- Node.js downloads and supported releases: https://nodejs.org/en/download

Community discovery data:

- ats-scrapers: https://github.com/kalil0321/ats-scrapers
- OpenRoles: https://github.com/datascry/openroles

PDF rendering:

- Tectonic: https://github.com/tectonic-typesetting/tectonic
- Installation reference: https://tectonic-typesetting.github.io/book/latest/installation

Browser reference:

- Playwright browser installation: https://playwright.dev/python/docs/browsers
- Hermes browser automation: https://hermes-agent.nousresearch.com/docs/user-guide/features/browser

The current workflow uses Hermes' browser tool. Playwright is not required by the deterministic collector itself. Install it only for a local browser adapter that explicitly requires it.

## 1. Install enough tools to clone

On Debian or Ubuntu:

    sudo apt-get update
    sudo apt-get install -y ca-certificates curl git

Clone the repository:

    git clone https://github.com/WilliamOrtizDev/JobScout.git ~/JobScout
    cd ~/JobScout

Use the branch or release documented by the repository owner. Do not copy another person's private profiles, database, browser session, or application packets.

## 2. Bootstrap dependencies and private templates

For a supported Debian/Ubuntu host:

    ./scripts/bootstrap.sh --all

This installs system packages, downloads the official Hermes installer to a temporary file, installs a pinned checksum-verified Tectonic binary, and creates private configuration files without overwriting existing ones.

If you prefer to control each step:

    ./scripts/bootstrap.sh --install-system
    ./scripts/bootstrap.sh --install-hermes
    ./scripts/bootstrap.sh --install-tectonic

The bootstrap intentionally does not configure provider credentials, create browser accounts, or enable scheduling.

Distribution repositories may provide an older Node.js build, especially on Ubuntu 22.04. The project doctor parses `node --version` and marks Photon unavailable below 18.17. Install a current Node.js LTS release from the official Node.js site before Photon setup when that warning appears; the rest of the scout can operate without Photon.

## 3. Configure the candidate

Edit:

    ~/job-search/search-profile.json
    ~/job-search/application-profile.json

Replace every placeholder and explicitly configure:

- Identity and contact details
- Target roles and geography
- Remote and engagement requirements
- Minimum compensation
- Industry/employer exclusions
- Work authorization and sponsorship answers
- C2C, 1099, or W-2 preferences
- Education status and other required application facts

Unknown facts should remain `null`. Do not guess.

Add optional direct ATS tenants in:

    ~/JobScout/config/sources.json

The community feeds work without adding direct tenants, but employer-specific Greenhouse, Lever, Ashby, and SmartRecruiters tenants provide better first-party coverage.

## 4. Supply the master resume and install the runtime

The resume must be LaTeX source. Keep it outside Git.

    export PRIVATE_RESUME_FILE="$HOME/private-resume/master-resume.tex"
    ./scripts/install.sh

The installer:

- Preserves existing private profiles and resume.
- Initializes or migrates the SQLite database.
- Installs the isolated DuckDB collector environment.
- Renders an environment-specific Hermes skill and cron prompt.
- Installs a launcher containing the actual repository and workspace paths.
- Does not enable the optional legacy Hermes compatibility patch unless `APPLY_HERMES_PATCH=1` is explicitly set.

If the repository, workspace, or Hermes profile is elsewhere, export the path variables from `.env.example` before running installation.

## 5. Configure Hermes inference and tools

Run:

    hermes setup

Or select only the model/provider:

    hermes model

For ChatGPT/Codex subscription authentication, choose `ChatGPT or Codex Subscription`. No Codex CLI installation is required.

Enable the toolsets needed by the scout:

- web
- browser
- terminal
- file
- code_execution
- cronjob
- skills

Use `hermes tools` or the setup wizard. Tool changes apply to a new session or gateway restart.

Run:

    hermes doctor
    hermes gateway install
    hermes gateway start

## 6. Configure browser sources

Follow [BROWSER_SETUP.md](BROWSER_SETUP.md). LinkedIn, Dice, and Indeed may require manual login, CAPTCHA, MFA, or account verification. Those steps must remain human-operated. The ATS/community collectors continue working if a board is unavailable.

## 7. Configure Photon iMessage delivery

Photon is optional; another Hermes delivery platform can be substituted by creating the cron manually.

Prerequisites:

    node --version
    npm --version

Node must be 18.17 or newer. Then run:

    hermes photon setup --phone +15551234567
    hermes photon status
    hermes gateway restart

Use the actual recipient number when creating the scheduled scout:

    export PHOTON_TARGET='photon:+15551234567'
    ./scripts/create-cron.sh

The script refuses to create a duplicate job with the same name.

## 8. Verify before enabling unattended operation

Run:

    python3 scripts/portable.py doctor
    python3 scripts/validate.py
    .venv-community/bin/python -m unittest discover -s tests -q

Run one deterministic collection without application submission:

    .venv-community/bin/python scripts/collect.py \
      --profile "$HOME/job-search/search-profile.json" \
      --database "$HOME/job-search/job-scout.db"

Review its source failures and candidate counts. Then inspect scheduling:

    hermes cron list --all
    hermes cron status

Trigger one controlled run only after the profiles, browser, delivery target, and skill are correct:

    hermes cron run <JOB-ID>

A healthy no-match run produces `[SILENT]`. See [USAGE.md](USAGE.md) for daily operation and [MIGRATION.md](MIGRATION.md) before rebuilding a host.
