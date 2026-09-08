# hlc-scripts

GitHub Actions automation for HLC Training operations.
Replaces Windows Task Scheduler scripts previously running on ARGUS.

## Scripts

### sync_ap.py
**Workflow:** sync-ap.yml
**Schedule:** Every 30 minutes
**What it does:** Syncs active AP tasks from the OFS Training Action Plan Tracker
(Smartsheet) into ORION Supabase action_items. Writes a health heartbeat to
SAM COS context_store as `health:github:orion_ap_sync`.

**Secrets required:**
- `SMARTSHEET_API_TOKEN`
- `ORION_SUPABASE_SERVICE_KEY`
- `SAMCOS_SERVICE_KEY`

### sync_xyleme.py
**Workflow:** sync-xyleme.yml
**Schedule:** Every 30 minutes
**What it does:** Reads the Xyleme Training Modernization Tracker and Exams
Transfer Tracker from Smartsheet. Creates/updates one ORION action_item per
course (grouped by Course_Integration). PLLs see their full curriculum
modernization workload — active and backlog — in ORION alongside AP items.
Smartsheet is source of truth; no back-channel writes.
Writes health heartbeat to SAM COS context_store as `health:github:xyleme_sync`.

Exams are joined to courses via their parent course-group header in the Exams
tracker. The two trackers currently use different course taxonomies, so exam
summaries appear in notes only for course names that genuinely match — the
per-run log reports the joined/unjoined counts.

**Secrets required:**
- `SMARTSHEET_API_TOKEN`
- `ORION_SUPABASE_SERVICE_KEY`
- `SAMCOS_SERVICE_KEY`

**Sheet IDs:**
- Training Modernization Tracker: `3204043720576900`
- Exams Transfer Tracker: `8868469282918276`

### send_ap_pending_digest.py
**Workflow:** ap-pending-digest.yml
**Schedule:** Weekdays 12:00 UTC (7:00 AM Houston, CDT; Mon-Fri)
**What it does:** Reads action_items and pc_projects rows flagged
`ap_pending_update` (PLL/TPM edits in ORiON not yet applied in Smartsheet),
reconciles each logged field against the live `ap_tracker` row with the
shared `ap_pending.py` rule (matched or superseded-by-a-newer-tracker-edit
rows are omitted), and emails Jen Wright a digest via Resend, with a
callout on rows pending more than 14 days and a removed-from-tracker
section for rows still open in ORiON whose AP number left the sheet. Test
fixtures (AP-99xx, "TEST FIXTURE" titles) never render. Sends nothing when
nothing is left. Run with `--dry-run` to render without sending (set
`PYTHONIOENCODING=utf-8` on Windows), or `--to <email>` to override the
recipient. Decision: `knowledge/decisions/2026-09-08-ap-pending-clear-and-direction.md`.

**Secrets required:**
- `ORION_SUPABASE_SERVICE_KEY`
- `RESEND_API_KEY`

### keepalive.yml
Weekly no-op run (Mondays 12:00 UTC) to keep scheduled workflows from being
paused by GitHub's inactivity rules.

## Adding Scripts

Drop the .py file in the repo root and create a corresponding workflow in
`.github/workflows/`. Follow the sync-ap.yml pattern.
