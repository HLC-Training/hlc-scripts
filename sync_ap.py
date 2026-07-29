#!/usr/bin/env python3
"""
sync_ap.py
─────────────────────────────────────────────────────────────────
Smartsheet Action Plan → ORiON sync. Runs on ARGUS on a schedule.

Flow:
  - Smartsheet is the SOURCE OF TRUTH. This script never writes to it.
  - Reads active child-level AP tasks from the OFS Training Action Plan Tracker.
  - Each row's Lead cell is resolved to an email (linked contact value, then
    the ap_lead_aliases table, then a name match against users/portal_users —
    see resolve_lead_email), then that email is resolved against BOTH users
    (PLLs/admins) and portal_users (TPMs). PLL/admin leads route to
    action_items (Delivery); TPM leads route to pc_projects (P&C). A lead
    resolving in both, resolving only to a viewer, or resolving in neither,
    is skipped and logged rather than guessed.
  - Upserts into the appropriate table on ap_number (insert new, update changed).
  - Resets ap_pending_update = false after a successful pull (Smartsheet is now
    current). This field only exists on the Delivery (action_items) side.
  - When a PLL changes status or due date in ORiON, ap_pending_update is raised
    in Supabase. Jennifer Wright is notified to make the actual change in Smartsheet.
    The next sync run pulls the confirmed Smartsheet change back into ORiON.

Schedule: Windows Task Scheduler — same cadence as sync_vault.py (every 30 min).
─────────────────────────────────────────────────────────────────
"""

import os
import uuid
import logging
import argparse
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
# Smartsheet Overall Status → ORiON (Delivery / action_items) status
STATUS_MAP = {
    "Not Started": "Open",
    "In Progress":  "In Progress",
    "On Hold":      "Deferred",
    "Complete":     "Done",
    "Cancelled":    "Done",
}

# Smartsheet Overall Status → P&C (pc_projects) status.
# Only Not Started / In Progress ever reach this map — see ACTIVE_STATUSES.
PC_STATUS_MAP = {
    "Not Started": "approved",
    "In Progress": "active",
}

# Active statuses — only import these (both Delivery and P&C)
ACTIVE_STATUSES = {"Not Started", "In Progress"}

# SQDCG → ORiON category (first letter wins; G = Growth = Strategy)
SQDCG_MAP = {
    "S": "Safety",
    "Q": "Quality",
    "D": "Delivery",
    "C": "Cost",
    "G": "Strategy",
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
    Fetch all rows from the AP sheet and return every active child-level task,
    regardless of who the Lead is or whether they resolve to anyone in ORiON.
    Owner resolution and routing (Delivery vs P&C vs skip) happens in main().
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
    for row in all_rows:
        cells       = {c["columnId"]: c.get("displayValue") or c.get("value") for c in row.get("cells", [])}
        raw         = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        display_raw = {c["columnId"]: c.get("displayValue") for c in row.get("cells", [])}

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

        # Lead value/displayValue are kept separate (not merged) — resolve_lead_email()
        # needs both: the raw contact value (an email, when linked) and the typed
        # display text (a name, whether linked or free text), for alias/name lookups.
        lead_value   = (raw.get(COL_LEAD) or "").strip()
        lead_display = (display_raw.get(COL_LEAD) or "").strip()

        active_tasks.append({
            "ap_number":    cells.get(COL_AP_NUM, ""),
            "action_text":  cells.get(COL_IMPROVEMENT, ""),
            "notes":        cells.get(COL_DESCRIPTION),
            "lead_value":   lead_value,
            "lead_display": lead_display,
            "status_raw":   status_raw,
            "start_raw":    cells.get(COL_START),
            "finish_raw":   cells.get(COL_FINISH),
            "sqdcg_raw":    cells.get(COL_SQDCG),
            "bucket":       cells.get(COL_BUCKET),
            "source_raw":   cells.get(COL_SOURCE),
        })

    log.info(f"Smartsheet: {len(active_tasks)} active child-level tasks found")
    return active_tasks


def resolve_lead_email(lead_value: str, lead_display: str, alias_map: dict, name_to_email: dict) -> str | None:
    """
    Resolve a Lead cell to an email, trying in order:
      1. the raw contact value, if it's a linked contact's email (as today) —
         but an alias override (ap_lead_aliases) always wins first, so a
         linked-but-wrong address (e.g. ben.smith@) still gets corrected.
      2. the typed display text, looked up in ap_lead_aliases (case-insensitive,
         trimmed) — covers free-text Leads like "Tamara Biediger" where value
         and displayValue are the same untyped string.
      3. the typed display text, matched by exact name against users.name or
         portal_users.name (any role — the role filter is enforced later by
         resolve_owner(), not here).
      4. unresolved (None) — e.g. "TDM TBD" or a blank Lead cell.
    Every branch returns an email or None; it never returns a user id directly,
    so the role/table filters in resolve_owner() can't be bypassed.
    """
    value = (lead_value or "").strip().lower()
    if value:
        if value in alias_map:
            return alias_map[value]
        if "@" in value:
            return value

    display = (lead_display or "").strip().lower()
    if display:
        if display in alias_map:
            return alias_map[display]
        if display in name_to_email:
            return name_to_email[display]

    return None


def resolve_owner(email: str, email_to_id: dict, portal_email_to_id: dict, viewer_emails: set) -> tuple[str, str | None]:
    """
    Resolve an already-identified email to a routing decision.
    Returns (destination, owner_id) where destination is one of:
      'delivery'  — resolves in users, role != viewer (a PLL/admin)  → action_items
      'pc'        — resolves in portal_users, role tpm                → pc_projects
      'ambiguous' — resolves in BOTH; needs a human, not a guess
      'viewer'    — resolves in users, but role is viewer — a real person,
                    excluded on purpose (mirrors item-actions.ts, which
                    rejects viewers on owner assignment). Distinct from
                    'none' so this doesn't get buried in the unmapped bucket.
      'none'      — resolves in neither (or no email was ever identified)
    """
    delivery_id = email_to_id.get(email)
    pc_id       = portal_email_to_id.get(email)

    if delivery_id and pc_id:
        return "ambiguous", None
    if delivery_id:
        return "delivery", delivery_id
    if pc_id:
        return "pc", pc_id
    if email in viewer_emails:
        return "viewer", None
    return "none", None


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


# ─── WIP CHECK (Delivery only — pc_projects has no WIP concept) ─
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


def build_delivery_notes(task: dict) -> str | None:
    """Combine description and bucket context for an action_items row."""
    notes_parts = []
    if task['notes']:
        notes_parts.append(task['notes'])
    if task['bucket']:
        notes_parts.append(f"Bucket: {task['bucket']}")
    return " | ".join(notes_parts) if notes_parts else None


# ─── MAIN ───────────────────────────────────────────────────────
def main(dry_run: bool = False):
    log.info("=" * 56)
    log.info(f"ORiON AP sync starting{' (DRY RUN)' if dry_run else ''}")
    if dry_run:
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | DRY RUN — no Supabase writes will be made.")

    # Connect to Supabase
    try:
        db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        log.info("Supabase connected")
    except Exception as e:
        log.error(f"Supabase connection failed: {e}")
        return

    # Load PLL (users) and TPM (portal_users) email → id maps.
    # Both sides are matched by lowercased email — GE addresses are mixed case.
    # Delivery excludes role='viewer' — mirrors item-actions.ts, which rejects
    # viewers on owner assignment. != 'viewer' (not == 'pll') deliberately,
    # since admins (e.g. Jim) are legitimate Delivery owners too.
    try:
        users_resp = db.table('users').select('id, name, email, role').execute()
        email_to_id = {
            u['email'].strip().lower(): u['id']
            for u in users_resp.data if u.get('email') and u.get('role') != 'viewer'
        }
        viewer_emails = {
            u['email'].strip().lower()
            for u in users_resp.data if u.get('email') and u.get('role') == 'viewer'
        }
        log.info(f"Users loaded: {len(email_to_id)} eligible (non-viewer), {len(viewer_emails)} viewer")
    except Exception as e:
        log.error(f"Failed to load users: {e}")
        return

    # portal_users is fetched WITHOUT a role filter: portal_email_to_id (the
    # actual P&C routing map) stays narrowed to role=tpm, but name_to_email
    # (built below) needs every portal_users row so a name match can find e.g.
    # a director too — resolve_owner() enforces the role filter afterward.
    try:
        portal_resp = db.table('portal_users').select('id, name, email, role').execute()
        portal_email_to_id = {
            p['email'].strip().lower(): p['id']
            for p in portal_resp.data if p.get('email') and p.get('role') == 'tpm'
        }
        log.info(f"Portal users loaded: {len(portal_email_to_id)} eligible (tpm) of {len(portal_resp.data)} total")
    except Exception as e:
        log.error(f"Failed to load portal_users: {e}")
        return

    # ap_lead_aliases: Smartsheet Lead text (a name or a wrong-but-linked email)
    # → the correct email. Loaded once per run, not queried per row.
    try:
        aliases_resp = db.table('ap_lead_aliases').select('smartsheet_value, resolved_email').execute()
        alias_map = {
            a['smartsheet_value'].strip().lower(): a['resolved_email'].strip().lower()
            for a in aliases_resp.data if a.get('smartsheet_value') and a.get('resolved_email')
        }
        log.info(f"Lead aliases loaded: {len(alias_map)}")
    except Exception as e:
        log.error(f"Failed to load ap_lead_aliases: {e}")
        return

    # Name → email fallback for free-text Leads with no alias row (any role,
    # in either table — resolve_owner() is what actually enforces role/table).
    # users is loaded first and portal_users second, so on a name collision
    # portal_users wins. Harmless today — the only people who exist in both
    # tables (Jim, Michele, Cal) use the same address in each — but if a
    # future name collision involves two DIFFERENT people, this silently
    # picks the portal_users one.
    name_to_email = {}
    for u in users_resp.data:
        if u.get('name') and u.get('email'):
            name_to_email[u['name'].strip().lower()] = u['email'].strip().lower()
    for p in portal_resp.data:
        if p.get('name') and p.get('email'):
            name_to_email[p['name'].strip().lower()] = p['email'].strip().lower()

    # Fetch active AP tasks from Smartsheet
    try:
        tasks = fetch_active_ap_tasks()
    except Exception as e:
        log.error(f"Smartsheet fetch failed: {e}")
        return

    if not tasks:
        log.info("No active AP tasks found.")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | AP sync complete — no active tasks found.")
        return

    # Load existing Delivery (action_items) rows keyed by ap_number
    try:
        existing_resp = db.table('action_items') \
            .select('id, ap_number, owner_id, status, due_date, start_date, priority, ap_pending_update') \
            .eq('source', 'ap_import') \
            .execute()
        existing_delivery = {
            r['ap_number']: r
            for r in (existing_resp.data or [])
            if r.get('ap_number')
        }
        log.info(f"Existing Delivery AP items in Supabase: {len(existing_delivery)}")
    except Exception as e:
        log.error(f"Failed to load existing Delivery AP items: {e}")
        return

    # Load existing P&C (pc_projects) rows keyed by ap_number
    try:
        existing_pc_resp = db.table('pc_projects') \
            .select('id, ap_number, owner_id, title, description, status, category, start_date, target_end_date, target_date_moves') \
            .eq('source', 'ap_synced') \
            .execute()
        existing_pc = {
            r['ap_number']: r
            for r in (existing_pc_resp.data or [])
            if r.get('ap_number')
        }
        log.info(f"Existing P&C AP items in Supabase: {len(existing_pc)}")
    except Exception as e:
        log.error(f"Failed to load existing P&C AP items: {e}")
        return

    to_insert_delivery = []
    to_update_delivery = []  # list of (id, fields_to_update, ap_number)
    to_insert_pc       = []
    to_update_pc       = []  # list of (id, fields_to_update, ap_number)

    skipped_no_ap        = 0
    skipped_unmapped     = 0
    skipped_ambiguous    = 0
    skipped_viewer       = 0
    skipped_unchanged    = 0
    skipped_pending      = 0
    unmapped_leads       = set()
    ambiguous_leads      = set()
    viewer_leads         = set()

    for task in tasks:
        ap_num = task['ap_number']
        if not ap_num or not task['action_text']:
            skipped_no_ap += 1
            continue

        lead_email = resolve_lead_email(task['lead_value'], task['lead_display'], alias_map, name_to_email)
        # For logging when nothing resolved, fall back to whatever raw text the
        # Lead cell actually had, so "TDM TBD" / free text is still visible.
        lead_display_for_log = lead_email or task['lead_display'] or task['lead_value'] or '(blank)'

        destination, owner_id = resolve_owner(lead_email or "", email_to_id, portal_email_to_id, viewer_emails)

        if destination == 'ambiguous':
            ambiguous_leads.add(lead_email)
            skipped_ambiguous += 1
            log.error(
                f"AMBIGUOUS LEAD: {ap_num} — {lead_email} resolves in BOTH "
                f"users and portal_users(tpm). Skipping; needs human review, not a guess."
            )
            if dry_run:
                print(f"[DRY RUN] SKIP     {ap_num:14} ambiguous lead ({lead_email}) — resolves in both users and portal_users(tpm)")
            continue

        if destination == 'viewer':
            viewer_leads.add(lead_email)
            skipped_viewer += 1
            log.warning(
                f"SKIPPED (owner is viewer): {ap_num} — {lead_email} resolves in users "
                f"but role is viewer, not a valid Delivery owner."
            )
            if dry_run:
                print(f"[DRY RUN] SKIP     {ap_num:14} owner is viewer ({lead_email}) — not a valid Delivery owner")
            continue

        if destination == 'none':
            skipped_unmapped += 1
            if lead_email:
                unmapped_leads.add(lead_email)
            if dry_run:
                print(f"[DRY RUN] SKIP     {ap_num:14} lead does not resolve in users or portal_users(tpm) ({lead_display_for_log})")
            continue

        due_date   = parse_due_date(task['finish_raw'])
        start_date = parse_due_date(task['start_raw'])
        category   = map_category(task['sqdcg_raw'])

        if destination == 'delivery':
            status = STATUS_MAP.get(task['status_raw'], 'Open')
            notes  = build_delivery_notes(task)

            if ap_num in existing_delivery:
                ex = existing_delivery[ap_num]

                # Skip if PLL has a pending update — Smartsheet hasn't been updated yet.
                if ex.get('ap_pending_update'):
                    skipped_pending += 1
                    log.info(f"Skipping {ap_num} — pending update awaiting Smartsheet change")
                    if dry_run:
                        print(f"[DRY RUN] SKIP     {ap_num:14} delivery — pending update awaiting Smartsheet change")
                    continue

                fields = {}
                if owner_id != ex.get('owner_id'):
                    fields['owner_id'] = owner_id
                    log.info(f"{ap_num}: lead reassigned in Smartsheet — owner updated")
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
                    to_update_delivery.append((ex['id'], fields, ap_num))
                    if dry_run:
                        print(f"[DRY RUN] UPDATE   {ap_num:14} delivery — {', '.join(k for k in fields if k != 'last_updated')}")
                else:
                    skipped_unchanged += 1
                    if dry_run:
                        print(f"[DRY RUN] SKIP     {ap_num:14} delivery — no changes")
            else:
                to_insert_delivery.append({
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
                if dry_run:
                    print(f"[DRY RUN] INSERT   {ap_num:14} delivery — owner {owner_id}, status {status}")

        elif destination == 'pc':
            pc_status = PC_STATUS_MAP.get(task['status_raw'])
            title       = task['action_text']
            description = task['notes']

            if ap_num in existing_pc:
                ex = existing_pc[ap_num]
                fields = {}
                if owner_id != ex.get('owner_id'):
                    fields['owner_id'] = owner_id
                if title != ex.get('title'):
                    fields['title'] = title
                if description != ex.get('description'):
                    fields['description'] = description
                if pc_status != ex.get('status'):
                    fields['status'] = pc_status
                if category != ex.get('category'):
                    fields['category'] = category
                if start_date != ex.get('start_date'):
                    fields['start_date'] = start_date
                if due_date != ex.get('target_end_date'):
                    fields['target_end_date'] = due_date
                    # target_date_moves feeds the P&C timeline's drift view —
                    # increment the CURRENT value on every real target_end_date
                    # change, never on insert, and never touch original_target_date
                    # again after insert (that baseline is app-write-only).
                    fields['target_date_moves'] = (ex.get('target_date_moves') or 0) + 1
                # priority is intentionally NOT re-forced here — Michele re-tiers
                # P&C items in the UI and that choice should stick between syncs.
                if fields:
                    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
                    to_update_pc.append((ex['id'], fields, ap_num))
                    if dry_run:
                        print(f"[DRY RUN] UPDATE   {ap_num:14} P&C      — {', '.join(k for k in fields if k != 'updated_at')}")
                else:
                    skipped_unchanged += 1
                    if dry_run:
                        print(f"[DRY RUN] SKIP     {ap_num:14} P&C      — no changes")
            else:
                to_insert_pc.append({
                    'id':                    str(uuid.uuid4()),
                    'title':                 title,
                    'description':           description,
                    'owner_id':              owner_id,
                    'status':                pc_status,
                    'source':                'ap_synced',
                    'ap_number':             ap_num,
                    'priority':              'Tier 3',
                    'category':              category,
                    'start_date':            start_date,
                    'target_end_date':       due_date,
                    'original_target_date':  due_date,
                })
                if dry_run:
                    print(f"[DRY RUN] INSERT   {ap_num:14} P&C      — owner {owner_id}, status {pc_status}")

    if unmapped_leads:
        log.info(f"Lead emails that don't resolve in users or portal_users(tpm) (skipped): {sorted(unmapped_leads)}")
    if ambiguous_leads:
        log.error(f"Lead emails resolving in BOTH users and portal_users(tpm) (skipped): {sorted(ambiguous_leads)}")
    if viewer_leads:
        log.warning(f"Lead emails resolving to a viewer, not a valid Delivery owner (skipped): {sorted(viewer_leads)}")

    if dry_run:
        delivery_summary = (
            f"Delivery — would insert: {len(to_insert_delivery)}, would update: {len(to_update_delivery)}"
        )
        pc_summary = (
            f"P&C      — would insert: {len(to_insert_pc)}, would update: {len(to_update_pc)}"
        )
        skip_summary = (
            f"Skipped  — no AP#/text: {skipped_no_ap}, unmapped lead: {skipped_unmapped}, "
            f"ambiguous lead: {skipped_ambiguous}, owner is viewer: {skipped_viewer}, "
            f"pending Smartsheet update: {skipped_pending}, unchanged: {skipped_unchanged}"
        )
        print("-" * 72)
        print(delivery_summary)
        print(pc_summary)
        print(skip_summary)
        if unmapped_leads:
            print(f"Unmapped lead emails ({len(unmapped_leads)}): {sorted(unmapped_leads)}")
        if ambiguous_leads:
            print(f"AMBIGUOUS lead emails ({len(ambiguous_leads)}) — needs human review: {sorted(ambiguous_leads)}")
        if viewer_leads:
            print(f"VIEWER lead emails ({len(viewer_leads)}) — resolves to a real person, excluded by role: {sorted(viewer_leads)}")
        print("-" * 72)
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | DRY RUN complete — no writes were made.")
        return

    # Insert new Delivery items in batches of 25
    inserted_delivery = 0
    for i in range(0, len(to_insert_delivery), 25):
        batch = to_insert_delivery[i:i + 25]
        try:
            db.table('action_items').insert(batch).execute()
            inserted_delivery += len(batch)
        except Exception as e:
            log.error(f"Delivery insert batch failed: {e}")

    # Apply Delivery updates
    updated_delivery = 0
    for item_id, fields, ap_num in to_update_delivery:
        try:
            db.table('action_items').update(fields).eq('id', item_id).execute()
            updated_delivery += 1
        except Exception as e:
            log.error(f"Delivery update failed for {ap_num}: {e}")

    # Insert new P&C items in batches of 25
    inserted_pc = 0
    for i in range(0, len(to_insert_pc), 25):
        batch = to_insert_pc[i:i + 25]
        try:
            db.table('pc_projects').insert(batch).execute()
            inserted_pc += len(batch)
        except Exception as e:
            log.error(f"P&C insert batch failed: {e}")

    # Apply P&C updates
    updated_pc = 0
    for item_id, fields, ap_num in to_update_pc:
        try:
            db.table('pc_projects').update(fields).eq('id', item_id).execute()
            updated_pc += 1
        except Exception as e:
            log.error(f"P&C update failed for {ap_num}: {e}")

    # Check for WIP overages caused by new Delivery inserts (Delivery only)
    if inserted_delivery > 0:
        new_owner_ids = {item['owner_id'] for item in to_insert_delivery[:inserted_delivery]}
        wip_flagged = check_and_flag_wip_overages(db, new_owner_ids, log)
        if wip_flagged:
            log.warning(f"{wip_flagged} PLL(s) flagged for WIP review on next login")
    else:
        wip_flagged = 0

    # Check for pending updates that need Jennifer's attention (Delivery only)
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

    skipped_total = (
        skipped_no_ap + skipped_unmapped + skipped_ambiguous + skipped_viewer
        + skipped_pending + skipped_unchanged
    )

    # Summary
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    summary = (
        f"{now} | AP sync complete — "
        f"Delivery: inserted {inserted_delivery}, updated {updated_delivery} | "
        f"P&C: inserted {inserted_pc}, updated {updated_pc} | "
        f"skipped: {skipped_total}, "
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
                'notes':  (
                    f'delivery_inserted={inserted_delivery} delivery_updated={updated_delivery} '
                    f'pc_inserted={inserted_pc} pc_updated={updated_pc} skipped={skipped_total}'
                ),
            }, on_conflict='key').execute()
            log.info("Health heartbeat sent to SAM COS")
    except Exception as _e:
        log.warning(f"Health heartbeat failed (non-critical): {_e}")


def parse_args():
    parser = argparse.ArgumentParser(description="Smartsheet AP → ORiON sync")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full sync logic without writing to Supabase (no inserts/updates, no WIP flags, no health heartbeat).",
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    main(dry_run=args.dry_run)
