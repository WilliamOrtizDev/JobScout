# Browser Source Setup

LinkedIn Jobs, Dice, and Indeed are broad discovery sources. They are not authoritative application state, and they are not required for direct ATS or community-feed collection.

## Rules

- Use a dedicated browser profile for job searching.
- Complete login, CAPTCHA, MFA, email verification, and account recovery manually.
- Do not export cookies into this repository or an application packet.
- Do not bypass rate limits, bot protection, or access controls.
- Never treat a search snippet or cached page as proof that a vacancy is live.
- Prefer the employer's current careers or ATS listing before producing a packet.

## Hermes browser tool

Configure browser tools through:

    hermes setup tools

Or:

    hermes tools

Enable the `browser` and `web` toolsets, then start a new session or restart the gateway. Hermes may use a local persistent browser or a configured cloud-browser provider depending on the user's setup.

Open a controlled Hermes session and load the job-search skill before testing:

    hermes --skills continuous-job-search

Use the browser tool to visit a harmless jobs-search page for each source. If login is required, take over manually. Confirm that a later Hermes session can still access the signed-in page before calling that source ready.

## Per-source readiness

LinkedIn Jobs:

- A LinkedIn account may be required.
- Searches and job pages may be rate-limited or login-gated.
- Do not automate connection requests, messages, or unrelated account activity.

Dice:

- A Dice account may be required for some application routes.
- Verify whether a listing resolves to an employer or staffing-company application page.
- Explicit W-2-only or no-C2C/no-1099 language must be handled by the configured policy.

Indeed:

- An Indeed account may be required for board-native applications.
- Prefer employer-hosted application routes when available.
- Account creation or verification remains a human action unless an exact approved application specifically authorizes one employer ATS account.

## Optional Playwright adapter

This repository does not currently require Playwright for deterministic ATS collection. If a local adapter is added, isolate it in its own virtual environment and install Chromium as the same operating-system user that runs the scheduler:

    python3 -m venv "$HOME/.local/share/job-search-playwright"
    "$HOME/.local/share/job-search-playwright/bin/pip" install playwright
    "$HOME/.local/share/job-search-playwright/bin/python" -m playwright install chromium

On supported Debian/Ubuntu hosts, browser system libraries can be installed with:

    sudo "$HOME/.local/share/job-search-playwright/bin/python" -m playwright install-deps chromium

Official reference: https://playwright.dev/python/docs/browsers

Hermes browser reference: https://hermes-agent.nousresearch.com/docs/user-guide/features/browser

Use a persistent browser-data directory outside the repository. Treat it as credential material. Do not include it in normal job-search backups; manually reauthenticate on a new host or store a separate encrypted backup only if the account owner accepts the risk.

## Failure behavior

A blocked board must not stop other sources. Leave it disabled or mark it unavailable, continue direct ATS/community discovery, and report the source failure separately. Re-enable it only after a manual browser smoke test succeeds.
