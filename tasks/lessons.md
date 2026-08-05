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

## 2026-08-03 — Generic Supabase key names are a repo-wide pattern, not a one-script bug

Fixed `sync_ap.py`'s Smartsheet-fetch-failure exit code (`sys.exit(1)`
instead of a silent `return`, closing the "exit 0 on a failed fetch" lesson
above) and renamed its `SUPABASE_SERVICE_KEY` read to
`ORION_SUPABASE_SERVICE_KEY` (bug 306cea89) — the generic name risked
resolving to the wrong project's key if ever set at Windows User scope on
HERMES across the three-project Supabase estate (this ORiON project, SAM
COS, GreenThumb). `sync-ap.yml`'s secret was already named
`ORION_SUPABASE_SERVICE_KEY` at the repo level; only the env var mapping
inside the workflow needed to stop renaming it on the way in.

While checking for a naming convention to match, found `sync_xyleme.py` and
`send_ap_pending_digest.py` read the exact same generic
`SUPABASE_SERVICE_KEY` name for this same ORiON project — same collision
risk, not fixed this session (out of the scoped bug). Worth its own
follow-up rather than assuming a fix in one script covers the pattern.

Also added a started-class heartbeat (`health:github:orion_ap_sync:last_run`,
written unconditionally before the Smartsheet fetch) alongside the existing
completion heartbeat (`health:github:orion_ap_sync`, unchanged, written only
at the end of a fully successful run) — mirrors the email-scan routine's
`last_run`/`last_success` split so "started" and "succeeded" stay two
different signals here too.

## 2026-08-05 — last_updated is not an insert timestamp, and zoneinfo needs tzdata on Windows

Two from the PLL digest build (`send_pll_digest.py`):

`action_items.last_updated` defaults to `now()` at insert but a
`BEFORE UPDATE` trigger bumps it on every later edit — live vault rows
showed lags up to 55 days between `created_date` and `last_updated`. Any
"new since X" logic keyed on `last_updated` silently reports old,
recently-edited rows as new. This table has NO immutable insert
timestamp; the digest uses `created_date` (the vault header date) plus a
reported-ids exclusion list instead. Check for an update trigger before
trusting any timestamp column to mean "created."

Python's `zoneinfo` raises `ZoneInfoNotFoundError` on Windows unless the
`tzdata` pip package is installed — there is no OS tz database to fall
back on. Ubuntu runners mask this. Any script using `ZoneInfo(...)` must
list `tzdata` in its install line or it works in CI and dies on HERMES/
ARGUS.
