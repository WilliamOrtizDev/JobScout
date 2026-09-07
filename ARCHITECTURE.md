# Architecture

## Cost and dependency boundary

The baseline system has no paid search, crawler, or job-data dependency. Its only expected operating costs are the machine running Hermes and the selected model subscription/provider. Optional services must remain removable shadow inputs, never required runtime components.

## Portable deployment boundary

The Git repository contains code, migrations, examples, templates, tests, and documentation. Candidate profiles, resumes, SQLite state, packets, browser sessions, credentials, and delivery targets remain private runtime inputs. Host paths are supplied through environment variables and rendered into the installed skill, cron prompt, and launcher; runtime code must not depend on `/home/hermes` or a fixed checkout location.

`scripts/bootstrap.sh` prepares a clean host, `scripts/install.sh` installs an idempotent private runtime, and `scripts/portable.py doctor` reports mandatory and optional readiness independently. `scripts/portable.py backup` uses SQLite's online backup API and creates a hashed allowlist-only archive. Restore rejects nonempty destinations, traversal, unsupported members, inventory drift, digest mismatch, and failed SQLite integrity. Hermes profile state is exported separately with the Hermes CLI.

The optional legacy Hermes source patch is not part of the normal dependency chain. Portable installations use upstream Hermes unless an operator explicitly enables a compatibility patch after reproducing the old defect.

## Current production flow

The live GPT-backed Hermes cron runs every 15 minutes around the clock. Its deterministic collector searches configured ATS feeds before Hermes expands discovery through general web sources, LinkedIn Jobs, Dice, and Indeed. Hermes verifies candidates, records application decisions, creates every packet it can finish within the run, and delivers each completed packet as a separate ordered plain-text iMessage followed by that job's paired PDFs. It returns `[SILENT]` when no verified match exists. Exact-job approval remains mandatory before submission.

Runs are serialized. A run longer than 15 minutes delays the next effective start rather than overlapping.

## Implemented SQLite-backed collector and lifecycle

The repository contains a deterministic collector that supplies prioritized discovery input to the live scout:

1. Fetch public Greenhouse, Lever, Ashby, and SmartRecruiters tenant feeds, paging large Lever and SmartRecruiters boards within explicit limits.
2. Reuse ETag/Last-Modified responses and cached JSON after HTTP 304 responses; prune the disk cache every five minutes to at most 5,000 entries and 30 days of age.
3. Fetch independent tenants concurrently with bounded workers.
4. Normalize every source into one `Candidate` schema.
5. Remove script/style content before descriptions can reach a model.
6. Deduplicate stable source IDs plus normalized canonical and application URLs within each poll.
7. Add a deterministic content hash for changed-record detection.
8. Apply cheap title, remote, contract, and explicit W-2-only filters.
9. Import old JSON application history once into a private, versioned SQLite database.
10. Remove URLs with completed application decisions. Collector-only history remains retryable until Hermes records a decision.
11. Write a replaceable model-input snapshot containing only model-relevant fields; it is not authoritative state.
12. Persist each run, source-health summary, and accepted candidate upsert in SQLite.
13. Preserve partial results when an individual source fails and report source health separately.

SQLite is the sole authoritative structured store. It owns candidates, collection runs, queue cursors, verification attempts, rejection and lifecycle events, application records, packet paths and fingerprints, exact approvals, submission attempts, and the notification outbox. Collector-only rows do not suppress delivery; a SQLite round-robin cursor rotates bounded model input so overflow leads cannot starve. Packet PDFs and source files remain filesystem artifacts whose canonical path and exact fingerprint are recorded in SQLite. `scripts/state_db.py export` creates an offline rollback snapshot only. The database uses WAL transactions and file mode `0600`; a process-level lock serializes collection and SQLite transactions protect lifecycle changes.

Approval is bound to both the immutable packet-directory fingerprint and exact application URL. A material revision must use a new revision-specific job ID and packet directory, so `APPROVE <JOB-ID>` cannot select a packet the user was never shown. Packet creation and its pending notification outbox row commit in one transaction. The renderer atomically claims pending rows with an expiring lease, validates the canonical confined packet path and fingerprint, durably materializes the exact snapshotted PDF bytes under a private content-addressed delivery directory, flushes output, and then records dispatch. Replacing the mutable working packet afterward cannot change the PDF bytes handed to Hermes. A render or snapshot failure releases the claim for retry. Hermes owns the separate transport-delivery database, so the application/transport boundary is at-least-once rather than falsely claiming exactly-once delivery; confirmed delivery or transport failure can be reconciled to `delivered` or `failed` without recreating a packet.

This adapts the useful Resumount ingestion patterns—normalization, bounded concurrency, partial success, source-level failures, and deterministic deduplication—without copying its scraper-first architecture. Employer ATS data is authoritative here; broad board scraping is discovery-only.

## Target two-speed flow

### Fast lane: every 15 minutes

- Poll known ATS tenants and public feeds.
- Revalidate pending application URLs.
- Use conditional requests and stable IDs.
- Send only new or changed plausible candidates to GPT.
- Stay silent when nothing changed.

### Discovery lane: hourly, rotated

- Discover new employer ATS tenants.
- Run self-hosted JobSpy searches for LinkedIn and Indeed as an optional local component.
- Run a separately maintained Dice collector only where access is allowed; never bypass login, CAPTCHA, rate limits, or other controls.
- Add free remote-job APIs/RSS feeds.
- Treat every board result as a lead until a current employer page or permitted board-native application surface verifies it.

JobSpy is not part of the 15-minute path. It is open source and self-hostable, but LinkedIn is rate-limited, it does not cover Dice, and the available wrapper image is stale. If adopted, pin the library in an isolated worker, use no proxy-bypass service by default, and allow per-source failure without failing the ATS lane.

## State progression

`discovered -> verification_pending -> verified/rejected -> packet_ready -> pending_approval -> approved -> submitted/blocked`

A successful submission atomically consumes its approval and is terminal. A blocked attempt may resume under the same approval only while its packet revision and application URL remain unchanged. Submission remains outside discovery and requires `APPROVE <JOB-ID>` for the exact packet and exact stored URL. A changed URL or materially changed listing requires re-approval.

## Migration gate

Do not replace the live scout until the shadow collector has run long enough to measure:

- unique verified candidates per source;
- stale/closed rate;
- bytes and latency per poll;
- source failure rate;
- browser escalation rate;
- model-bound candidate count;
- recall against current deterministic and browser-assisted discovery paths.
