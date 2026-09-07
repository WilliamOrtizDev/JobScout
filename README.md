# JobScout

A portable, deterministic job-discovery and approval-gated application workflow for Hermes Agent.

## Portable quick start

Start with [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md). On a clean Debian or Ubuntu host, after installing Git and cloning this repository:

    ./scripts/bootstrap.sh --all

The bootstrap installs supported system prerequisites, Hermes Agent from its official installer, and a pinned checksum-verified Tectonic binary. It creates private profile templates without overwriting existing state. After filling those profiles and supplying a private LaTeX resume:

    export PRIVATE_RESUME_FILE=/private/path/master-resume.tex
    ./scripts/install.sh
    python3 scripts/portable.py doctor

Additional guides:

- [Daily operation](docs/USAGE.md)
- [LinkedIn, Dice, Indeed, and browser setup](docs/BROWSER_SETUP.md)
- [Host migration, backup, restore, and restore drills](docs/MIGRATION.md)
- [Security and public-release safeguards](SECURITY.md)
- [Third-party licenses and service notices](THIRD_PARTY_NOTICES.md)
- [Instructions for an AI agent performing setup](AGENTS.md)

## License and supported release scope

JobScout source code is available under the [MIT License](LICENSE). Third-party software, public datasets, websites, and managed services retain their own licenses and terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The supported automated bootstrap target is Debian 12 or Ubuntu 22.04/24.04 Linux. Other Linux distributions and macOS may work with manual dependency installation but are not release-gated platforms. Provider authentication, candidate profiles, a private resume, optional browser logins, CAPTCHA/MFA, and optional messaging setup remain manual operator steps. Installation and testing never authorize an application submission or scheduler enablement.

All runtime locations can be overridden with `HERMES_HOME`, `JOB_SEARCH_HERMES_HOME`, `JOB_SEARCH_AUTOMATION`, `JOB_SEARCH_WORKDIR`, `RESUME_DIR`, `MASTER_RESUME_FILE`, and `TECTONIC`. See `.env.example`.

## What is versioned

- Sanitized example profiles
- Sanitized cron prompt and job template examples
- Deployment and validation scripts
- Local Hermes patches for Photon attachment accounting and systemd 249 scope compatibility
- Deterministic Greenhouse, Lever, Ashby, and SmartRecruiters collectors
- Broad community discovery from the ats-scrapers hosted Parquet dataset and OpenRoles slim index, with pinned upstream revisions and independent failure isolation
- Transactional SQLite lifecycle state with versioned migrations, one-time legacy JSON import, exact-packet approvals, submission auditing, and a notification outbox
- Conditional HTTP caching, bounded concurrency, bounded ATS pagination, source-health reporting, and atomic output
- Public-data discovery with no paid job-data vendor dependency

Candidate-specific profiles, state, prompts, resumes, skills, application packets, generated search pages, logs, databases, credentials, and runtime output are intentionally ignored. They remain local and can be backed up separately.

## Reference production behavior

- Schedule: every 15 minutes around the clock, serialized, with cron conversational continuity disabled because SQLite owns application state and the collector cursor
- Orchestrator/model: current Hermes default (GPT/OpenAI Codex)
- Sources each run: general web, LinkedIn Jobs, Dice, Indeed
- Delivery: one readable plain-text Photon/iMessage per verified job, ordered by fit, recency, and compensation; each message is immediately followed by that job's resume PDF and cover-letter PDF
- Healthy no-match runs: `[SILENT]`
- Submission: requires `APPROVE <JOB-ID>` for the exact stored packet and URL

## Validate

    python3 scripts/validate.py

## Deterministic shadow collector

The direct ATS collector is standard-library Python and has no paid data dependency. Broad discovery additionally uses a pinned DuckDB runtime to query ats-scrapers' public Parquet dataset efficiently and consumes integrity-checked, size-bounded OpenRoles slim-index chunks. Create the ignored tenant registry, install that isolated runtime, then run the collector:

    cp config/sources.example.json config/sources.json
    # Add employer ATS tenant IDs to config/sources.json.
    ./scripts/install_community_sources.sh
    .venv-community/bin/python scripts/collect.py \
      --database /path/to/private/job-search/job-scout.db

The collector creates/migrates the SQLite database automatically, deduplicates against application decisions already committed there, then records each run and candidate history. Candidate history alone does not suppress a lead: a candidate remains retryable until the orchestrator records a verified decision, preventing interrupted or oversized batches from silently losing jobs. SQLite is the only authoritative structured state store.

`./scripts/install.sh` safely imports and archives a legacy `state.json` only when the SQLite lifecycle is empty; it refuses to overwrite populated lifecycle history. The installer atomically stages one exact legacy file, writes and fsyncs a content-addressed private archive, imports those same bytes, and supports idempotent recovery if interruption occurs after the SQLite commit. A replacement file at the original path is preserved and causes a fail-closed stop. The installer also installs `scripts/live_scout_input.py` as `~/.hermes/scripts/job-scout-shadow.py`. Attach it to the live cron with `hermes cron edit <JOB-ID> --script job-scout-shadow.py`; its bounded output is injected as prioritized discovery leads while the agent still independently verifies every listing under the normal scout rules.

The legacy import is a one-time migration command. Export is for offline rollback inspection only; runtime code never reads the export:

    python3 scripts/state_db.py --database /path/to/job-scout.db import /path/to/state.json
    python3 scripts/state_db.py --database /path/to/job-scout.db status
    python3 scripts/state_db.py --database /path/to/job-scout.db export /path/to/state.rollback.json

Lifecycle commands record decisions, verification evidence, exact-packet approvals, and verified submission outcomes:

    python3 scripts/state_db.py --database /path/to/job-scout.db record JOB-ID --record /path/to/decision.json --reason 'verified decision' --notify --verification /path/to/verification.json
    python3 scripts/state_db.py --database /path/to/job-scout.db verify JOB-ID --outcome verified --source-url https://example.test/job --evidence /path/to/evidence.json
    python3 scripts/state_db.py --database /path/to/job-scout.db approve JOB-ID
    python3 scripts/state_db.py --database /path/to/job-scout.db submit JOB-ID --outcome submitted --confirmation /path/to/confirmation.json

Packet documents remain private filesystem artifacts confined beneath the applications root. SQLite stores their canonical directory, approval-relevant content fingerprint, lifecycle status, events, approval binding, submission attempts, queue cursor, and delivery-outbox bookkeeping. Approval-ready packet revisions are immutable: material changes use a new revision-specific job ID and generate a new notification. The renderer copies the exact snapshotted resume and cover-letter bytes into a private content-addressed delivery directory before emitting `MEDIA:` paths, preventing later working-packet edits from changing what Hermes opens. Post-approval confirmation evidence may be added without changing the approved revision. A successful submission atomically consumes its approval and submitted state cannot regress; a blocked attempt may resume only with the same active packet-and-URL approval. The outbox uses atomic expiring claims and render-failure recovery. Hermes records downstream Photon delivery separately, so this boundary is intentionally at-least-once rather than an unsupported exactly-once claim.

The atomic model-ready output is written to `runtime/candidates.json`; conditional-response bodies and metadata stay under ignored `runtime/http-cache/`. A tenant failure is reported without discarding successful sources. This shadow command does not submit applications or change the durable application state.

Community upstreams are configured in `config/community-sources.json`. The configuration records the exact inspected Git commits, repository URLs, and licenses. The ats-scrapers integration downloads only an allowlisted set of manifest-hashed ATS partitions under strict per-file and aggregate byte caps, refreshes its verified local cache at most every six hours, and queries those local files with DuckDB extension loading and external access disabled. OpenRoles chunks are likewise size-, row-, freshness-, and digest-verified before caching. Runtime records from both projects remain unverified discovery leads: the scout must reopen the exact employer or permitted fallback page and verify remote status, contract eligibility, compensation, conflict restrictions, and the application route before generating a packet. ats-scrapers code is MIT-licensed; its hosted dataset does not currently state separate data terms. OpenRoles code is MIT-licensed and its generated data is CC BY-SA 4.0.

Run its tests and repository validation with:

    python3 -m unittest discover -s tests -v
    python3 scripts/validate.py

## Existing-host installation

Review the script before running it, then:

1. Run `./scripts/bootstrap.sh` to create missing private templates without installing system packages.
2. Populate the private search and application profiles.
3. Set `PRIVATE_RESUME_FILE` to the private LaTeX resume.
4. Run:

    ./scripts/install.sh

Create the cron job only after setting the delivery target in the shell:

    PHOTON_TARGET='photon:+1XXXXXXXXXX' ./scripts/create-cron.sh

Provider credentials are never restored from this repository. Configure them with `hermes auth` or `hermes model`, then restart the gateway.

Create a consistent private application-state backup with:

    python3 scripts/portable.py backup /encrypted/location/job-search-backup.tar.gz

The backup excludes credentials and caches. Hermes profile state is exported separately with `hermes profile export`. Follow [docs/MIGRATION.md](docs/MIGRATION.md) before moving or rebuilding a host.

## Local Hermes patch

`patches/hermes-agent-local-fixes.patch` captures historical deployment-specific compatibility fixes. It is disabled by default. Set `APPLY_HERMES_PATCH=1` only after confirming that the installed Hermes release still needs it and the patch applies cleanly. Normal portable installations use unmodified upstream Hermes.
