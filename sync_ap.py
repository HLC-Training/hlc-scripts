#!/usr/bin/env python3
"""
sync_ap.py
─────────────────────────────────────────────────────────────────
Smartsheet Action Plan → ORiON sync. Runs on ARGUS on a schedule.

Flow:
  - Smartsheet is the SOURCE OF TRUTH. This script never writes to it.
  - Reads active child-level AP tasks from the OFS Training Action Plan Tracker.
  - Upserts into Supabase action_items on ap_number (insert new, update changed).
  - Resets ap_pending_update = false after a successful pull (Smartsheet is now current).
  - When a PLL changes status or due date in ORiON, ap_pending_update is raised
    in Supabase. Jennifer Wright is notified to make the actual change in Smartsheet.
    The next sync run pulls the confirmed Smartsheet change back into ORiON.

Schedule: Windows Task Scheduler — same cadence as sync_vault.py (every 30 min).
─────────────────────────────────────────────────────────────────
"""

import os
import uuid
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL         = "https://czdkctjbejnwuopigxta.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SMARTSHEET_TOKEN     = os.environ.get("SMARTSHEET_API_TOKEN", "")

SHEET_ID     = "1362792971980676"
JENNIFER_EMAIL = "jennifer.b.wright@gevernova.com"

LOG_FILE = Path(__file__).parent / "sync_ap.log"

if not SUPABASE_SERVICE_KEY:
    raise SystemExit("ERROR: SUPABASE_SERVICE_KEY environment variable is not set.")
if not SMARTSHEET_TOKEN:
    raise SystemExit("ERROR: SMARTSHEET_API_TOKEN environment variable is not set.")

# ─── COLUMN IDs (from OFS Training Action Plan Tracker) ─────────
COL_AP_NUM      = 2655828149751684   # AP#
COL_IMPROVEMENT = 3705298878680964   # Improvement (primary = action_text)
COL_DESCRIPTION = 3234169554685828   # Description  → notes
COL_LEAD        = 8208898506051460   # Lead (email) → owner lookup
COL_STATUS      = 890549111574404    # Overall Status
COL_FINISH      = 3142348925259652   # Current Finish → due_date
COL_START       = 6841156930670468   # Start Date (date only) → start_date
COL_SQDCG       = 5718885853253508   # SQDCG → category
COL_BUCKET      = 982369741000580    # Bucket
COL_SOURCE      = 3288805485006724   # Source (AP origin context)
COL_IS_CHILD    = 4351693839093636   # Is Child (1 = task-level row)
COL_IS_PARENT   = 7602491737984900   # Is Parent

# ─── MAPPINGS ────────────────────────────────────────────────────
# Smartsheet Overall Status → ORiON status
STATUS_MAP = {
    "Not Started": "Open",
    "In Progress":  "In Progress",
    "On Hold":      "Deferred",
    "Complete":     "Done",
    "Cancelled":    "Done",
}

# Active statuses — only import these
ACTIVE_STATUSES = {"Not Started", "In Progress"}

# SQDCG → ORiON category (first letter wins; G = Growth = Strategy)
SQDCG_MAP = {
    "S": "Safety",
    "Q": "Quality",
    "D": "Delivery",
    "C": "Cost",
    "G": "Strategy",
}

# Lead email → ORiON user name (must match users.name exactly).
# Keys must be lowercase — lookups are normalized with .lower().
LEAD_EMAIL_MAP = {
    "jim.rosen@gevernova.com":        "Jim Rosen",
    "james.rosen@gevernova.com":      "Jim Rosen",
    "gregoryd.walker@gevernova.com":  "Greg Walker",
    "harry.hanson@gevernova.com":     "Harry Hanson",
    "mohammed.nizami@gevernova.com":  "Mohammed Nizami",
    "pablo.schibli@gevernova.com":    "Pablo Schibli",
    "sherif.khalifa@gevernova.com":   "Sherif Khalifa",
    "linda.nelson@gevernova.com":     "Linda Nelson",
    "ben.smith@gevernova.com":        "Ben Smith",
    "ben.smith1@gevernova.com":       "Ben Smith",
}
# ────────────────────────────────────────────────────────────────


# ─── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ─── SMARTSHEET HELPERS ─────────────────────────────────────────
def ss_get(path: str, params: dict = None) -> dict:
    """GET from Smartsheet API. Raises on HTTP error."""
    resp = requests.get(
        f"https://api.smartsheet.com/2.0{path}",
        headers={"Authorization": f"Bearer {SMARTSHEET_TOKEN}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_active_ap_tasks() -> list[dict]:
    """
    Fetch all rows from the AP sheet and return only active child-level tasks
    where the Lead maps to an ORiON PLL.
    Smartsheet rows are retrieved via GET /sheets/{id} with pagination.
    """
    page_size = 500
    page      = 1
    all_rows  = []

    while True:
        data = ss_get(
            f"/sheets/{SHEET_ID}",
            params={
                "include":  "cells",
                "pageSize": page_size,
                "page":     page,
            },
        )
        rows = data.get("rows", [])
        all_rows.extend(rows)
        total_pages = data.get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    log.info(f"Smartsheet: fetched {len(all_rows)} total rows")

    active_tasks = []
    unmapped_leads = set()
    for row in all_rows:
        cells = {c["columnId"]: c.get("displayValue") or c.get("value") for c in row.get("cells", [])}
        raw   = {c["columnId"]: c.get("value") for c in row.get("cells", [])}

        # Child tasks only
        is_child  = str(cells.get(COL_IS_CHILD, "0")).strip()
        is_parent = str(cells.get(COL_IS_PARENT, "0")).strip()
        if is_child != "1.0" and is_child != "1":
            continue
        if is_parent == "1.0" or is_parent == "1":
            continue

        # Active statuses only
        status_raw = cells.get(COL_STATUS, "")
        if status_raw not in ACTIVE_STATUSES:
            continue

        # Lead must map to a PLL in ORiON. Contact cells store the email in
        # `value` while `displayValue` is the person's name — prefer value.
        lead_email = ((raw.get(COL_LEAD) or cells.get(COL_LEAD) or "")).strip().lower()
        if lead_email not in LEAD_EMAIL_MAP:
            if lead_email and "@" in lead_email:
                unmapped_leads.add(lead_email)
            continue

        active_tasks.append({
            "ap_number":    cells.get(COL_AP_NUM, ""),
            "action_text":  cells.get(COL_IMPROVEMENT, ""),
            "notes":        cells.get(COL_DESCRIPTION),
            "lead_email":   lead_email,
            "status_raw":   status_raw,
            "start_raw":    cells.get(COL_START),
            "finish_raw":   cells.get(COL_FINISH),
            "sqdcg_raw":    cells.get(COL_SQDCG),
            "bucket":       cells.get(COL_BUCKET),
            "source_raw":   cells.get(COL_SOURCE),
        })

    log.info(f"Smartsheet: {len(active_tasks)} active PLL-led tasks found")
    if unmapped_leads:
        log.info(f"Active tasks with leads not in LEAD_EMAIL_MAP (not imported): {sorted(unmapped_leads)}")
    return active_tasks


def parse_due_date(finish_raw: str | None) -> str | None:
    """Extract YYYY-MM-DD from Smartsheet ISO datetime string."""
    if not finish_raw:
        return None
    try:
        return finish_raw[:10]
    except Exception:
        return None


def map_category(sqdcg_raw: str | None) -> str | None:
    """Map first SQDCG letter to ORiON category."""
    if not sqdcg_raw:
        return None
    for char in sqdcg_raw.upper():
        if char in SQDCG_MAP:
            return SQDCG_MAP[char]
    return None


# ─── WIP CHECK ──────────────────────────────────────────────────
def check_and_flag_wip_overages(db, owner_ids: set, log) -> int:
    """
    For each owner, count Tier 2 items 'currently working':
    start_date <= today <= due_date (nulls = unbounded).
    If count > 5, set pending_wip_review = true.
    Returns number of users flagged.
    """
    today   = datetime.now().date()
    flagged = 0
    for owner_id in owner_ids:
        try:
            resp = db.table('action_items') \
                .select('id, start_date, due_date') \
                .eq('owner_id', owner_id) \
                .eq('priority', 'Tier 2') \
                .in_('status', ['Open', 'In Progress']) \
                .execute()
            rows  = resp.data or []
            count = sum(
                1 for r in rows
                if (r.get('start_date') is None or r['start_date'] <= str(today))
                and (r.get('due_date')   is None or r['due_date']   >= str(today))
            )
            if count > 5:
                db.table('users') \
                    .update({'pending_wip_review': True}) \
                    .eq('id', owner_id) \
                    .execute()
                log.warning(f"WIP overage: owner {owner_id} has {count} active Tier 2 items — pending_wip_review flagged")
                flagged += 1
        except Exception as e:
            log.error(f"WIP check failed for {owner_id}: {e}")
    return flagged


# ─── MAIN ───────────────────────────────────────────────────────
def main():
    log.info("=" * 56)
    log.info("ORiON AP sync starting")

    # Connect to Supabase
    try:
        db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        log.info("Supabase connected")
    except Exception as e:
        log.error(f"Supabase connection failed: {e}")
        return

    # Load user name → ID map
    try:
        users_resp = db.table('users').select('id, name, email').execute()
        name_to_id = {u['name']: u['id'] for u in users_resp.data}
        log.info(f"Users loaded: {len(name_to_id)}")
    except Exception as e:
        log.error(f"Failed to load users: {e}")
        return

    # Fetch active AP tasks from Smartsheet
    try:
        tasks = fetch_active_ap_tasks()
    except Exception as e:
        log.error(f"Smartsheet fetch failed: {e}")
        return

    if not tasks:
        log.info("No active AP tasks found for ORiON PLLs.")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | AP sync complete — no active tasks found.")
        return

    # Load existing AP items from Supabase keyed by ap_number
    try:
        existing_resp = db.table('action_items') \
            .select('id, ap_number, owner_id, status, due_date, start_date, priority, ap_pending_update') \
            .eq('source', 'ap_import') \
            .execute()
        existing = {
            r['ap_number']: r
            for r in (existing_resp.data or [])
            if r.get('ap_number')
        }
        log.info(f"Existing AP items in Supabase: {len(existing)}")
    except Exception as e:
        log.error(f"Failed to load existing AP items: {e}")
        return

    to_insert  = []
    to_update  = []  # list of (id, fields_to_update)
    skipped    = 0

    for task in tasks:
        ap_num = task['ap_number']
        if not ap_num or not task['action_text']:
            skipped += 1
            continue

        owner_name = LEAD_EMAIL_MAP.get(task['lead_email'])
        owner_id   = name_to_id.get(owner_name)
        if not owner_id:
            log.warning(f"No ORiON user ID for lead {task['lead_email']} — skipping {ap_num}")
            skipped += 1
            continue

        status   = STATUS_MAP.get(task['status_raw'], 'Open')
        due_date = parse_due_date(task['finish_raw'])
        start_date = parse_due_date(task['start_raw'])
        category = map_category(task['sqdcg_raw'])

        # Build notes: combine description and bucket context
        notes_parts = []
        if task['notes']:
            notes_parts.append(task['notes'])
        if task['bucket']:
            notes_parts.append(f"Bucket: {task['bucket']}")
        notes = " | ".join(notes_parts) if notes_parts else None

        if ap_num in existing:
            ex = existing[ap_num]

            # Skip if PLL has a pending update — Smartsheet hasn't been updated yet.
            # Don't overwrite their change; wait for Jennifer to update Smartsheet first.
            if ex.get('ap_pending_update'):
                log.info(f"Skipping {ap_num} — pending update awaiting Smartsheet change")
                continue

            # Check if anything changed
            fields = {}
            if owner_id != ex.get('owner_id'):
                fields['owner_id'] = owner_id
                log.info(f"{ap_num}: lead reassigned in Smartsheet — owner updated to {owner_name}")
            if status != ex['status']:
                fields['status'] = status
            if due_date != ex.get('due_date'):
                fields['due_date'] = due_date
            if start_date != ex.get('start_date'):
                fields['start_date'] = start_date
            if ex.get('priority') != 'Tier 2':
                fields['priority'] = 'Tier 2'
            if fields:
                fields['last_updated'] = datetime.now(timezone.utc).isoformat()
                to_update.append((ex['id'], fields))
        else:
            to_insert.append({
                'id':                  str(uuid.uuid4()),
                'owner_id':            owner_id,
                'action_text':         task['action_text'],
                'notes':               notes,
                'status':              status,
                'start_date':          start_date,
                'due_date':            due_date,
                'category':            category,
                'priority':            'Tier 2',
                'source':              'ap_import',
                'ap_number':           ap_num,
                'ap_pending_update':   False,
                'vault_synced':        True,
                'created_date':        datetime.now().strftime('%Y-%m-%d'),
                'last_updated':        datetime.now(timezone.utc).isoformat(),
            })

    # Insert new items in batches of 25
    inserted = 0
    for i in range(0, len(to_insert), 25):
        batch = to_insert[i:i+25]
        try:
            db.table('action_items').insert(batch).execute()
            inserted += len(batch)
        except Exception as e:
            log.error(f"Insert batch failed: {e}")

    # Apply updates
    updated = 0
    for item_id, fields in to_update:
        try:
            db.table('action_items').update(fields).eq('id', item_id).execute()
            updated += 1
        except Exception as e:
            log.error(f"Update failed for {item_id}: {e}")

    # Check for WIP overages caused by new inserts
    if inserted > 0:
        new_owner_ids = {item['owner_id'] for item in to_insert[:inserted]}
        wip_flagged = check_and_flag_wip_overages(db, new_owner_ids, log)
        if wip_flagged:
            log.warning(f"{wip_flagged} PLL(s) flagged for WIP review on next login")
    else:
        wip_flagged = 0

    # Check for pending updates that need Jennifer's attention
    try:
        pending_resp = db.table('action_items') \
            .select('ap_number, action_text, status, due_date') \
            .eq('source', 'ap_import') \
            .eq('ap_pending_update', True) \
            .execute()
        pending = pending_resp.data or []
    except Exception as e:
        log.error(f"Failed to check pending updates: {e}")
        pending = []

    if pending:
        log.warning(f"{len(pending)} AP items have pending updates awaiting Smartsheet change:")
        for p in pending:
            log.warning(f"  {p['ap_number']} | {p['action_text'][:60]} | Status: {p['status']} | Due: {p['due_date']}")

    # Summary
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    summary = (
        f"{now} | AP sync complete — "
        f"inserted: {inserted}, updated: {updated}, skipped: {skipped}, "
        f"pending Smartsheet updates: {len(pending)}, "
        f"WIP flags set: {wip_flagged}"
    )
    log.info(summary)
    print(summary)

    # ── Health heartbeat → SAM COS context_store ────────────────
    # Upserts last successful run timestamp so the health dashboard can monitor this script.
    try:
        import requests as _requests
        SAMCOS_SUPABASE_URL = "https://hucrkbomqsxpmokgypxg.supabase.co"
        SAMCOS_SERVICE_KEY  = os.environ.get("SAMCOS_SERVICE_KEY", "")
        if SAMCOS_SERVICE_KEY:
            from supabase import create_client as _create_client
            sc = _create_client(SAMCOS_SUPABASE_URL, SAMCOS_SERVICE_KEY)
            sc.table('context_store').upsert({
                'key':    'health:github:orion_ap_sync',
                'value':  datetime.utcnow().isoformat() + 'Z',
                'domain': 'system',
                'notes':  f'inserted={inserted} updated={updated} skipped={skipped}',
            }, on_conflict='key').execute()
            log.info("Health heartbeat sent to SAM COS")
    except Exception as _e:
        log.warning(f"Health heartbeat failed (non-critical): {_e}")


if __name__ == '__main__':
    main()
