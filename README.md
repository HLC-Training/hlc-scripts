# hlc-scripts

GitHub Actions automation for HLC Training operations.
Replaces Windows Task Scheduler scripts previously running on ARGUS.

## Scripts

### sync_ap.py
**Workflow:** sync-ap.yml
**Schedule:** Every 30 minutes
**What it does:** Syncs active AP tasks from the OFS Training Action Plan Tracker
(Smartsheet) into ORION Supabase action_items. Writes a health heartbeat to
SAM COS context_store as `health:argus:orion_ap_sync`.

**Secrets required:**
- `SMARTSHEET_API_TOKEN`
- `ORION_SUPABASE_SERVICE_KEY`
- `SAMCOS_SERVICE_KEY`

## Adding Scripts

Drop the .py file in the repo root and create a corresponding workflow in
`.github/workflows/`. Follow the sync-ap.yml pattern.
