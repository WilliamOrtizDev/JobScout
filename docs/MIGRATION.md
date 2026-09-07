# Migration and Rebuild

Use this procedure before rebuilding a homelab host or moving the scout to another Hermes AI host.

There are two independent state bundles:

1. Job-search runtime: SQLite lifecycle, profiles, resume, direct-source registry, and application packets.
2. Hermes profile: sessions, skills, memories, cron definitions, configuration, and other profile state. Hermes profile exports deliberately exclude `auth.json` and `.env`.

Treat both archives as sensitive personal data.

## Source host

### 1. Pause mutable activity

    HERMES_HOME="$HOME/.hermes/profiles/job-scout" hermes cron list --all
    HERMES_HOME="$HOME/.hermes/profiles/job-scout" hermes cron pause <JOB-ID>

Replace `job-scout` with the source profile name. Wait for its active run to finish. Do not let two hosts run the same scout concurrently.

### 2. Back up job-search state

Run these commands from the source checkout. The environment override keeps existing checkout locations valid:

    export JOB_SEARCH_AUTOMATION="${JOB_SEARCH_AUTOMATION:-$(git rev-parse --show-toplevel)}"
    cd "$JOB_SEARCH_AUTOMATION"
    python3 scripts/portable.py backup "$HOME/job-search-migration.tar.gz"

The command uses SQLite's online backup API, validates the snapshot, hashes every archived file, and excludes caches and credentials.

### 3. Back up Hermes

For a named profile:

    hermes profile export job-scout -o "$HOME/hermes-job-scout-profile.tar.gz"

Replace `job-scout` with the actual source profile name. When creating the cron on the target, set `JOB_SEARCH_HERMES_HOME` directly as shown below. As a convenience, `create-cron.sh` can instead derive that directory from `HERMES_PROFILE` when `JOB_SEARCH_HERMES_HOME` is unset.

Record the archive path printed by Hermes. If profile export does not include all host-level state needed by the installed Hermes version, follow the current Hermes migration documentation or preserve `~/.hermes` with a filesystem archive after stopping the gateway.

Before copying a raw `~/.hermes/state.db`, checkpoint its WAL:

    sqlite3 "$HOME/.hermes/state.db" 'PRAGMA wal_checkpoint(TRUNCATE);'

Hermes migration reference: https://hermes-agent.nousresearch.com/docs

### 4. Record versions and verify archives

    git -C "$JOB_SEARCH_AUTOMATION" rev-parse HEAD
    hermes --version
    sha256sum "$HOME/job-search-migration.tar.gz"

Transfer both archives through an encrypted channel. Do not upload them to a public repository.

## Target host

### 1. Install the base system and clone

Follow [GETTING_STARTED.md](GETTING_STARTED.md) through repository cloning. Install dependencies without creating destination files:

    ./scripts/bootstrap.sh --all --dependencies-only

Install the same or a newer Hermes version.

Do not run the normal scaffolding or full job-search installer yet. The restore command intentionally requires empty workspace and resume destinations and refuses to overwrite an existing private source registry.

### 2. Restore Hermes profile state

    hermes profile import /path/to/hermes-job-scout-profile.tar.gz --name job-scout-migrated
    hermes profile use job-scout-migrated

Inspect:

    hermes profile list
    hermes skills list
    hermes cron list --all

The imported cron definition still contains its source-host rendered prompt and workdir. Keep it paused, note its ID, and remove it rather than resuming it:

    HERMES_HOME="$HOME/.hermes/profiles/job-scout-migrated" hermes cron remove <IMPORTED-JOB-ID>

The imported profile cannot be named `default`, and importing over an existing profile is refused. `job-scout-migrated` is the example target name. Profile exports exclude provider and Photon credentials, so reauthenticate with `hermes model` and `hermes photon setup`. Browser sessions are not included in the normal job-search backup; follow [BROWSER_SETUP.md](BROWSER_SETUP.md) and log in manually.

### 3. Configure and export target paths

The first four values are normal defaults. A named Hermes profile is not inferred by the Python runtime, so export its home explicitly:

    export JOB_SEARCH_AUTOMATION="$HOME/JobScout"
    export JOB_SEARCH_WORKDIR="$HOME/job-search"
    export RESUME_DIR="$HOME/job-search-resume"
    export HERMES_HOME="$HOME/.hermes"
    export HERMES_PROFILE="job-scout-migrated"
    export JOB_SEARCH_HERMES_HOME="$HOME/.hermes/profiles/job-scout-migrated"

Keep these exports in the shell through restore, installation, cron creation, and verification. Adjust them before restoration when the new AI host uses different paths.

### 4. Restore job-search state

Ensure the configured workspace and resume directories are absent or empty. Then run:

    cd "$JOB_SEARCH_AUTOMATION"
    python3 scripts/portable.py restore /path/to/job-search-migration.tar.gz

The restore rejects absolute archive members, path traversal, unsupported members, file-inventory drift, digest mismatch, existing targets, and a failed SQLite integrity check. It also rewrites packet paths stored in SQLite from the archived applications root to the configured target applications root.

### 5. Regenerate host-specific runtime files

    export PRIVATE_RESUME_FILE="$RESUME_DIR/master-resume.tex"
    ./scripts/install.sh

The installer preserves restored profiles and resume while rebuilding the local virtual environment, skill, prompt, and launcher with target-host paths.

### 6. Verify before cutover

    python3 scripts/portable.py doctor
    python3 scripts/state_db.py --database "$JOB_SEARCH_WORKDIR/job-scout.db" status
    HERMES_HOME="$JOB_SEARCH_HERMES_HOME" hermes doctor
    HERMES_HOME="$JOB_SEARCH_HERMES_HOME" hermes gateway status
    HERMES_HOME="$JOB_SEARCH_HERMES_HOME" hermes photon status
    "$JOB_SEARCH_HERMES_HOME/scripts/job-scout-shadow.py"

Check a known historical job ID and confirm applications are visible. Test browser sources manually.

### 7. Cut over

Keep the old host paused. Recreate the target cron from the newly rendered paths rather than resuming the imported definition:

    HERMES_HOME="$JOB_SEARCH_HERMES_HOME" hermes gateway restart
    export PHOTON_TARGET='photon:+1XXXXXXXXXX'
    ./scripts/create-cron.sh

Observe one scheduled run and confirm its execution and delivery state. Only then retire the source host.

## Restore drill without cutover

Use disposable empty paths:

    export JOB_SEARCH_WORKDIR=/tmp/job-search-restore-drill/workspace
    export RESUME_DIR=/tmp/job-search-restore-drill/resume
    export JOB_SEARCH_AUTOMATION=$HOME/JobScout
    python3 scripts/portable.py restore "$HOME/job-search-migration.tar.gz"
    python3 scripts/state_db.py --database "$JOB_SEARCH_WORKDIR/job-scout.db" status

Remove the disposable drill only after verifying SQLite integrity and expected packet/profile files. A backup is not proven until this drill succeeds.

## Expected nonportable items

These must be recreated or verified on the new host:

- Active background processes and sockets
- Browser login sessions
- Photon sidecar dependencies and possibly Photon authentication
- Provider OAuth/API authentication when tokens are not portable
- Native Python wheels and Tectonic binary
- Systemd/gateway service state
- Reconstructible HTTP and community-dataset caches
