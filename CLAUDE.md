# hlc-scripts

Last updated: 2026-08-21 (added Session Protocol with forced doc-fix rule)

Scheduled sync and automation scripts for the SAM COS and ORiON systems.

## What lives here

- `sync_ap.py` — Smartsheet AP tracker to ORiON Supabase (`czdkctjbejnwuopigxta`).
  One-way, Delivery and P&C scoped. Runs on GitHub Actions.
- `sync_repo_docs.py` — repo reasoning docs to SAM COS Supabase. Vendored
  identically into four repos; samcos is canonical.

`sync_vault.py` does NOT live in this repo, despite this file previously
claiming otherwise. It lives in `samcos/scripts/sync_vault.py` and has run on
GitHub Actions since 2026-08-07 (decision `6558742d` / action item `e5f1ee5c`),
reading `open-actions.md` via the Drive API. ARGUS's Task Scheduler copy is
disabled, not deleted — its heartbeat (`health:argus:orion_vault_sync`) has
been frozen since 2026-08-07. Corrected 2026-08-20 during the vault-import
due_date pipeline build (action item `b1bfe79e`); this file had never been
updated for the 2026-08-07 migration.

## Databases

- SAM COS `hucrkbomqsxpmokgypxg` — `action_items`, `bugs`, `decisions`,
  `daily_log`, `context_store`, `vault_write_queue`, `repo_docs`
- ORiON `czdkctjbejnwuopigxta` — portal tables, prefixed `portal_`
- GreenThumb `xfzjywareudbvuubzfye` — never used by anything in this repo

## Machines

ARGUS is the always-on automation hub and runs the Task Scheduler jobs.
Python must be invoked by full path (`...\Python312\python.exe`). Bare
`python` does not resolve under Task Scheduler.

## Session Protocol
1. **Start:** `git remote -v` (must be HLC-Training/hlc-scripts) → `git pull origin main` → `/diff` + `git log`. In that order, every session.
2. Build in small commits with clear messages.
3. **Close:** commit → push. Close-step fact check (forced): if this session changed a fact any living reference doc states — a runtime, schema value, file path, URL, repo location, enum, or role list — fix that doc's claim in the same commit, citing its live source. Living docs = CLAUDE.md, `knowledge/reference/*`, `knowledge/contacts/*`, `knowledge/runbooks/*`; NOT the dated `decisions/*` or `learnings/*` history. Forced, not deferred — a logged doc-fix is the drift this prevents. A fact in another repo's doc you can't reach: `/add-dir` or tell Jim same-day.

## Before changing anything here

Read `tasks/lessons.md`. Most of the non-obvious failure modes in this repo
are silent ones, and the file exists because they were expensive to find.
