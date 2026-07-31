# Lessons — hlc-scripts

Append-only. Newest at the bottom.

## Exit 0 on a failed fetch is a lie the whole system believes

`sync_ap.py` exits 0 and writes its success heartbeat even when the Smartsheet
fetch fails. A run during a Smartsheet outage produces a green Actions run, a
fresh heartbeat, and zero synced rows. Observed live 2026-07-30: run
30549527981 concluded "success", produced no script output, wrote nothing.

Any sync script must exit non-zero when its source fetch fails. A green run
that did nothing is worse than a red one, because nobody investigates green.

## A heartbeat measures liveness, not staleness

A heartbeat proves a run started. It never proves a run was due and did not
happen, and it never proves the run accomplished anything. The AP sync went
31 hours without firing (2026-07-29 18:13Z to 2026-07-31 01:08Z) while the
health dashboard stayed green throughout.

Staleness needs an expected-interval check: a heartbeat older than roughly 3x
its nominal cron interval should alert. Liveness checks cannot catch absence.

## GitHub Actions cron contention is per-workflow, not per-account

AP sync on `5,35 * * * *` fired roughly one slot in seven, with 3-4 hour gaps.
`sync-xyleme` held ~17 minutes on an identical cron, in the same repo, on the
same account, through the entire AP outage.

So this is contention on the specific slot, not a rate limit. Minutes near
`:00` and `:30` are the most contended. Do not assume a cron expression
delivers its nominal cadence; measure the observed gaps before designing
anything around freshness.

## Writes to open-actions.md must use the marker, and the file looks fine either way

Vault writes to `open-actions.md` require `operation: insert_before_marker`
with `match_string: # ORION Sync Log`. A plain append writes below the marker,
where `sync_vault.py` skips it. The entry is in the file, the file is valid
markdown, and the content never reaches ORiON.

Entry headers require the leading `## ` prefix:
`## YYYY-MM-DD | Owner | Action | Status: Open`, exactly four pipe fields,
with Category and Tier as `>` metadata lines below. Without the `## ` the
Phase C validator rejects the queue row, and an entry written outside the
queue is silently skipped by sync.

Both failures are invisible on inspection. That is why they are here.

## Postgres and Supabase gotchas

Newlines in SQL content need PostgreSQL escape syntax: `E'line one\nline two'`.
A plain string with a literal newline will not do what you expect.

GE email lookups use `.ilike()`, never `.eq()`. GE Vernova addresses appear in
mixed case and an exact match silently returns nothing.

Multi-statement queries through Supabase MCP parse unreliably. Split them.

`vault_write_queue` inserts go one row per file at a time. Concurrent inserts
fire webhooks that clobber each other. Verify status before the next insert.
