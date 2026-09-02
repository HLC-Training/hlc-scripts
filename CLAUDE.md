# hlc-scripts

Last updated: 2026-09-02 — added the three-digest inventory and the
Vercel-owns-scheduling rule (weekly per-TPM digest build). Prior note:
2026-08-26 (second update — ORiON→Smartsheet live push landed in
orion-pll, not here; sync_ap.py placeholder note corrected)

Scheduled sync and automation scripts for the SAM COS and ORiON systems.

## What lives here

- `sync_ap.py` — Smartsheet AP tracker to ORiON Supabase (`czdkctjbejnwuopigxta`).
  Still one-way and Delivery/P&C-scoped, but since 2026-08-26 in-scope rows
  land FIRST in the `ap_tracker` mirror table (full-row shape, loop-prevention
  sync state) and the module tables (`action_items`/`pc_projects`) are
  projections of that landing zone (orion-pll decision 69ba45bd). The LIVE
  ORiON→Smartsheet write-back shipped 2026-08-26 in the orion-pll APP
  (`lib/smartsheet.ts` + `app/(protected)/operations/ap-actions.ts` — it
  fires from a user's save, so it cannot live in this cron script); this
  script's `push_row_to_smartsheet` remains a gate-demonstrating
  placeholder that never writes. This script stays strictly
  Smartsheet→ORiON; its loop-prevention echo handling is what absorbs the
  app's pushes (AP Manager flag checked in code on both sides because
  service_role bypasses RLS). Runs on GitHub Actions.
- `sync_repo_docs.py` — repo reasoning docs to SAM COS Supabase. Vendored
  identically into four repos; samcos is canonical.
- **Three digests, three separate everything.** `send_pll_digest.py` (daily,
  each PLL, Delivery `action_items`), `send_tpm_digest.py` (daily, **Michele
  only**, all-TPM P&C roll-up), `send_tpm_individual_digest.py` (**weekly
  Mondays, each TPM, their own P&C projects** — added 2026-09-02, decision
  `2026-09-02-tpm-individual-digest.md`). Each has its OWN state table
  (`pll_digest_state` / `tpm_digest_state` / `tpm_individual_digest_state`)
  and its OWN live flag (`PLL_DIGEST_LIVE` / `TPM_DIGEST_LIVE` /
  `TPM_INDIVIDUAL_DIGEST_LIVE`). Never share a watermark or alias a flag
  between them — enabling one must never enable another.

`sync_vault.py` does NOT live in this repo, despite this file previously
claiming otherwise. It lives in `samcos/scripts/sync_vault.py` and has run on
GitHub Actions since 2026-08-07 (decision `6558742d` / action item `e5f1ee5c`),
reading `open-actions.md` via the Drive API. ARGUS's Task Scheduler copy is
disabled, not deleted — its heartbeat (`health:argus:orion_vault_sync`) has
been frozen since 2026-08-07. Corrected 2026-08-20 during the vault-import
due_date pipeline build (action item `b1bfe79e`); this file had never been
updated for the 2026-08-07 migration.

## Scheduling: Vercel owns it, GitHub Actions only runs it

Since 2026-09-02 the digests are scheduled by **Vercel cron**, not GitHub
cron. GitHub's `schedule:` is the unreliable half on this account: the PLL
digest's `7 7 * * 1-5` has been observed firing at 12:04, 12:31, 15:01,
18:14 and 19:23. `pll-digest.yml`'s schedule is commented out (`4c0a9b2`) and
`tpm-individual-digest.yml` never had one.

`tpm-individual-digest.yml` is triggered by `repository_dispatch` (type
`tpm-individual-digest`), sent by the authenticated orion-pll endpoint
`/api/cron/tpm-individual-digest`, which the orion-pll `vercel.json` cron
(`9 11 * * 1`) calls. **Do not add a `schedule:` trigger to a workflow that
already has a Vercel cron** — that is the double-fire of bug `645438e0`. If
Vercel is ever retired as scheduler, the schedule goes back in the same
commit that removes the Vercel cron, never both at once.

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
