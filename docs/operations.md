# Production operations

Flight Review runs Python 3.12 and SQLite on Sevalla. Build from the repository-root
Dockerfile; Docker Compose and CI use the same image definition. The runtime dependency
lock includes hashes; development tools and notebooks are separate.

## Service configuration

Use one web instance with the existing persistent disk mounted at
`/workspace/source/data`. Set `STORAGE_PATH` to that path and
`FLIGHT_REVIEW_ENV=production`. SQLite, uploaded logs and analysis caches belong on
this volume. Do not enable horizontal scaling until the database and uploads are
migrated to services designed for concurrent instances.

The container runs as UID/GID 10001. Existing volume files must be owned by that user
before upgrading. For a new Compose install: `mkdir -p data && sudo chown 10001:10001 data`.
Set a random `COOKIE_SECRET` (at least 32 characters) in the hosting secret store;
`python -c "import secrets; print(secrets.token_urlsafe(48))"` generates one.
Rotation signs all existing users out. Never put the value in Git.

Probes: port 8080, HTTP `/healthz` for liveness, `/readyz` for readiness.
Use initial delay 30 seconds, interval 10 seconds, timeout 3 seconds and three
failures. Readiness verifies the database schema and writable log storage.
`/opsz` returns 503 if free disk space is at or below 10%, or a configured backup
is older than 48 hours. It is monitored separately and must not be used as a
restart probe. GitHub's Production health workflow checks all three every 15 minutes;
GitHub schedule timing is best effort. Enable Actions failure notifications in the
maintainer account. Expand the disk before the alert threshold; do not prune original
logs as an automatic response.

## Deployment and rollback

Validate and deploy runs regression tests, pylint, dependency auditing, the production
Docker build, startup/restart smoke tests, and a fixed high/critical vulnerability scan.
Only successful main runs may deploy. CI creates an immutable `deploy/<commit-sha>`
branch, deploys it through the Sevalla v3 API, checks the reported commit, and verifies
the live probes. Superseded main runs skip deployment. Sevalla automatic deploy must
stay disabled; the old unverified deploy hook workflow has been removed.

Repository secret `SEVALLA_API_KEY` uses Flight Review-scoped APP:READ/APP:UPDATE
capabilities. The initial key expires September 8, 2027; rotate it before that date.
APP_REVISION is set by CI at runtime and independently checked against Sevalla's commit.
Deployment logs must never print environment-variable responses or credentials.

For rollback, use Sevalla's previous successful deployment after checking schema
compatibility. This release adds an AnalysisJobs table and indexes and does not remove
existing tables or columns. Keep the persistent disk attached. A rollback to the old
code reintroduces its security/operational limitations; prefer a forward fix.
Redeploy a tested revision by dispatching Validate and deploy on main.

## Backups and recovery

Configure a private S3-compatible bucket with runtime-only BACKUP_BUCKET,
BACKUP_ENDPOINT (HTTPS), BACKUP_ACCESS_KEY and BACKUP_SECRET_KEY. The scheduler runs
at startup and checks hourly; a successful backup is due every 24 hours. Failures are
logged as BACKUP_FAILED and retried. Backups stream each original log and AI cache
file, take a consistent SQLite snapshot, and upload a manifest only after its objects.
Content-addressed objects avoid uploading unchanged logs repeatedly. Each run reads
back and verifies the remote manifest and database checksum.

Backups currently retain all snapshots and objects. There is no automatic deletion or
bucket expiration policy: deleting shared objects can corrupt multiple snapshots.
Review bucket growth monthly and introduce reference-aware retention only after the
required historical retention window is decided. Platform-owned disaster recovery
does not replace these application backups.

Run manually from the container:
```sh
python ops_backup.py backup
python ops_backup.py restore --snapshot snapshots/<timestamp>.json --target /tmp/restore-drill
```
Restore requires an empty destination, checks every file's SHA-256 and size, rejects
path traversal, and checks SQLite integrity. Do not restore over live storage.
For full recovery, stop writes, restore into a new empty mounted volume, verify log
and user counts and a representative plot, then switch the service to that volume.
Keep the old volume until recovery is confirmed. Database snapshots and originals
are recoverable; derived plotting caches can be regenerated.

## Expensive work

Upload parsing uses a fresh child process (default one parser, 1536 MiB address-space
limit, 240-second deadline). Timeout/cancellation kills and reaps it before another
parse enters. AI extraction/model calls also run outside the web process, with a
2048 MiB address-space limit and 3900-second deadline. These are resource bounds,
not an untrusted-code security sandbox.

AI requests return 202 with an opaque job ID. Polling/cancellation requires the
submitting, approved account. The durable queue deduplicates active requests and
limits each account to two active jobs and 12 submissions/hour, with 16 active jobs
globally and one AI worker. Stale interrupted jobs fail explicitly rather than
silently repeating a paid request. Completed queue records expire after seven days;
cached analyses remain available. Chat history is scoped to each account and log;
the old shared chat cache is not imported. Approval and log access are checked at
submission and execution.

Use the page's analysis action after a refresh to resume a pending request. Cancel
analysis stops its worker; an upstream model service may already have incurred cost.

## Development

Clone this fork with submodules:
```sh
git clone --recursive https://github.com/ARK-Electronics/flight_review.git
cd flight_review
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r app/requirements.txt
pip install -r app/requirements-dev.txt
PYTHONPATH=app:app/plot_app:app/plot_app/libevents/libs/python pytest app/tests -q
bash run_pylint.sh
```
For notebooks install `app/requirements-notebook.in` separately.
To update runtime dependencies with review:
```sh
pip-compile --generate-hashes --output-file app/requirements.txt app/requirements.in
pip-audit -r app/requirements.txt --no-deps --disable-pip
```
Commit the input and lock together and let the production image checks run.
