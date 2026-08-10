#!/usr/bin/env python3
"""
sync_ap.py
─────────────────────────────────────────────────────────────────
Smartsheet Action Plan → ORiON sync. Runs on ARGUS on a schedule.

Flow:
  - Smartsheet is the SOURCE OF TRUTH. This script never writes to it.
  - Reads child-level AP tasks from the OFS Training Action Plan Tracker
    (active statuses drive the sync; inactive rows are read only to settle
    pending flags — see fetch_child_ap_tasks).
  - Each row's Lead cell is resolved to an email (linked contact value, then
    the ap_lead_aliases table, then a name match against users/portal_users —
    see resolve_lead_email), then that email is resolved against BOTH users
    (PLLs/admins) and portal_users (TPMs). PLL/admin leads route to
    action_items (Delivery); TPM leads route to pc_projects (P&C). A lead
    resolving in both, resolving only to a viewer, or resolving in neither,
    is skipped and logged rather than guessed.
  - Upserts into the appropriate table on ap_number (insert new, update changed).
  - Top-level parent/summary rows (Is Parent, AP# like "AP-0621") are read in
    the same fetch and their "Improvement" text and "Current Finish" date are
    upserted into ap_titles (new/changed only) — the AP-name source for
    orion-pll's grouping headers, plus the parent-level end date. A changed
    end date (vs the stored ap_titles.end_date) additionally inserts one
    event row into ap_end_date_changes — the trigger for orion-pll's
    owner-acknowledgment flow (1d039530). A stored NULL end_date is a silent
    baseline write, never an event, so the first run after ship flags nobody.
    Titles/events write AFTER the child sync; a write failure there exits
    non-zero at the very end but never blocks or aborts the child sync.
  - When a PLL changes status or due date in ORiON, ap_pending_update is raised
    (ap_pending_since records when) and the row is protected from sync
    overwrites. Jennifer Wright makes the matching change in Smartsheet; the
    first run that sees the row MATCH Smartsheet again clears both flag fields
    (Smartsheet caught up). These fields only exist on the Delivery
    (action_items) side.
  - Flagged rows still diverging after ESCALATION_DAYS are written to SAM COS
    context_store (orion:ap_pending:escalated, overwritten every run) and
    paged to Jim via the samcos notification_queue → Pushover drain — but only
    when the escalated set of AP numbers changes, never as a repeat page.

Schedule: GitHub Actions (.github/workflows/sync-ap.yml) — every 30 min.
─────────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import json
import uuid
import logging
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from supabase import create_client

# ─── CONFIG ─────────────────────────────────────────────────────
SUPABASE_URL         = "https://czdkctjbejnwuopigxta.supabase.co"
# Project-scoped name (closes bug 306cea89) — the generic "SUPABASE_SERVICE_KEY"
# risked resolving to the wrong project's key if ever set at Windows User scope
# on HERMES (machine-wide) for a local run, across a three-project Supabase
# estate (this ORiON project, SAM COS, GreenThumb). Matches the SAMCOS_SERVICE_KEY
# naming convention already used below for the SAM COS client.
ORION_SUPABASE_SERVICE_KEY = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
SMARTSHEET_TOKEN     = os.environ.get("SMARTSHEET_API_TOKEN", "")

SHEET_ID     = "1362792971980676"
JENNIFER_EMAIL = "jennifer.b.wright@gevernova.com"

# Pending-flag escalation: a flagged Delivery row still diverging from
# Smartsheet after this many days is recorded in SAM COS and paged.
ESCALATION_DAYS = 14
ESCALATION_KEY  = "orion:ap_pending:escalated"

SAMCOS_SUPABASE_URL = "https://hucrkbomqsxpmokgypxg.supabase.co"

LOG_FILE = Path(__file__).parent / "sync_ap.log"

if not ORION_SUPABASE_SERVICE_KEY:
    raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY environment variable is not set.")
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

# Top-level AP numbers only (AP-0621, never AP-0621-1) — ap_titles keys on
# these; mid-level summary rows (Is Parent set on e.g. AP-0621-1) are
# expected in the sheet and are not titles we capture.
TOP_LEVEL_AP_RE = re.compile(r"^AP-\d+$")

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


def fetch_child_ap_tasks() -> tuple[list[dict], list[dict]]:
    """
    Fetch all rows from the AP sheet and return
    (child_tasks, parent_titles):

    child_tasks — every child-level task, active AND inactive — regardless
    of who the Lead is or whether they resolve to anyone in ORiON. Owner
    resolution and routing (Delivery vs P&C vs skip) happens in main().

    parent_titles — one dict per TOP-LEVEL parent/summary row (Is Parent
    set, AP# like "AP-0621" with no sub-segments): the AP number, the
    primary "Improvement" text (the AP's real title), the parent-level
    "Current Finish" end date (None when blank/unparseable — the diff in
    main() keeps the stored value in that case), and the Smartsheet row id
    for traceability. Mid-level summary rows (Is Parent on e.g.
    AP-0621-1) are expected and silently skipped — ap_titles keys on
    top-level numbers only. A top-level parent with a blank title is
    logged and skipped, never captured as an empty string.

    Inactive rows (Complete / Cancelled / On Hold) are included ONLY so a
    Delivery row whose ap_pending_update flag is set can be recognized as
    caught-up once Jennifer applies the change in Smartsheet: e.g. ORiON
    "Done" vs Smartsheet "Complete" map to the same status and must clear
    the flag. main() restricts inactive rows to exactly that path — they
    never insert, never route to P&C, never touch the skip counters.

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

    child_tasks   = []
    parent_titles = []
    seen_title_aps = set()
    for row in all_rows:
        cells       = {c["columnId"]: c.get("displayValue") or c.get("value") for c in row.get("cells", [])}
        raw         = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        display_raw = {c["columnId"]: c.get("displayValue") for c in row.get("cells", [])}

        is_child  = str(cells.get(COL_IS_CHILD, "0")).strip()
        is_parent = str(cells.get(COL_IS_PARENT, "0")).strip()

        # Title capture from TOP-LEVEL parent rows — read-only side channel,
        # runs before the child filters and never short-circuits them, so
        # child_tasks comes out byte-identical to the pre-capture behavior.
        if is_parent == "1.0" or is_parent == "1":
            p_ap    = str(cells.get(COL_AP_NUM) or "").strip()
            p_title = str(cells.get(COL_IMPROVEMENT) or "").strip()
            if TOP_LEVEL_AP_RE.match(p_ap):
                if not p_title:
                    log.warning(f"Title capture: top-level parent {p_ap} has a blank Improvement cell — skipped, not captured as empty")
                elif p_ap in seen_title_aps:
                    log.warning(f"Title capture: duplicate top-level parent row for {p_ap} — keeping the first, skipping row {row.get('id')}")
                else:
                    seen_title_aps.add(p_ap)
                    parent_titles.append({
                        "ap_number":     p_ap,
                        "title":         p_title,
                        "end_date":      parse_due_date(cells.get(COL_FINISH)),
                        "source_row_id": row.get("id"),
                    })
            # Non-top-level parents (AP-0621-1 etc.) and blank AP# summary
            # rows are expected sheet structure — no log, no capture.

        # Child tasks only
        if is_child != "1.0" and is_child != "1":
            continue
        if is_parent == "1.0" or is_parent == "1":
            continue

        status_raw = cells.get(COL_STATUS, "")

        # Lead value/displayValue are kept separate (not merged) — resolve_lead_email()
        # needs both: the raw contact value (an email, when linked) and the typed
        # display text (a name, whether linked or free text), for alias/name lookups.
        lead_value   = (raw.get(COL_LEAD) or "").strip()
        lead_display = (display_raw.get(COL_LEAD) or "").strip()

        child_tasks.append({
            "active":       status_raw in ACTIVE_STATUSES,
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

    n_active = sum(1 for t in child_tasks if t["active"])
    log.info(f"Smartsheet: {len(child_tasks)} child-level tasks found ({n_active} active)")
    log.info(f"Smartsheet: {len(parent_titles)} top-level AP titles found on parent rows")
    return child_tasks, parent_titles


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


def days_since(iso_ts: str | None) -> float | None:
    """Days elapsed since a Supabase timestamptz ISO string; None if unset/unparseable."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400


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

    # ── SAM COS client + STARTED heartbeat (unconditional, top of routine) ──
    # Written before any Smartsheet/ORiON work is attempted, mirroring the
    # email-scan routine's last_run/last_success split (2026-07-17 lesson: a
    # cross-run signal that must fire "every run" belongs as close to the top
    # as possible, not after work that can fail or exit early). The completion
    # heartbeat (health:github:orion_ap_sync) at the bottom of this function is
    # unchanged and is the success-class signal the health dashboard keys on;
    # this is its started-class counterpart.
    sc = None
    SAMCOS_SERVICE_KEY = os.environ.get("SAMCOS_SERVICE_KEY", "")
    if SAMCOS_SERVICE_KEY and not dry_run:
        try:
            sc = create_client(SAMCOS_SUPABASE_URL, SAMCOS_SERVICE_KEY)
            sc.table('context_store').upsert({
                'key':    'health:github:orion_ap_sync:last_run',
                'value':  datetime.now(timezone.utc).isoformat(),
                'domain': 'system',
                'notes':  'Run started — written before Smartsheet fetch, unconditional (proves a run began, not that it finished).',
            }, on_conflict='key').execute()
        except Exception as _e:
            log.warning(f"SAM COS client init / started heartbeat failed (non-critical): {_e}")

    # Connect to Supabase
    try:
        db = create_client(SUPABASE_URL, ORION_SUPABASE_SERVICE_KEY)
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
    # id → name for escalation records — Delivery owners live in users,
    # P&C (TPM) owners live in portal_users; ids are table-scoped uuids so
    # merging by id carries no collision risk across the two tables.
    id_to_name = {u['id']: u.get('name') for u in users_resp.data}
    id_to_name.update({p['id']: p.get('name') for p in portal_resp.data})
    for u in users_resp.data:
        if u.get('name') and u.get('email'):
            name_to_email[u['name'].strip().lower()] = u['email'].strip().lower()
    for p in portal_resp.data:
        if p.get('name') and p.get('email'):
            name_to_email[p['name'].strip().lower()] = p['email'].strip().lower()

    # Fetch active AP tasks from Smartsheet
    try:
        tasks, parent_titles = fetch_child_ap_tasks()
    except Exception as e:
        # Exit non-zero (closes bug 306cea89 / lessons.md "exit 0 on a failed
        # fetch is a lie the whole system believes") — a plain `return` here
        # exits the process with code 0, so a Smartsheet outage produced a
        # green GitHub Actions run that synced nothing. No success heartbeat
        # is reachable from this path either way (it lives at the very end
        # of this function, well past this return), so the fix is the exit
        # code the CI run reports, not a heartbeat change.
        log.error(f"Smartsheet fetch failed: {e}")
        sys.exit(1)

    if not tasks:
        log.info("No active AP tasks found.")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | AP sync complete — no active tasks found.")
        return

    # Load existing Delivery (action_items) rows keyed by ap_number
    try:
        existing_resp = db.table('action_items') \
            .select('id, ap_number, owner_id, status, due_date, start_date, priority, ap_pending_update, ap_pending_since') \
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
            .select('id, ap_number, owner_id, title, description, status, category, start_date, target_end_date, target_date_moves, ap_pending_update, ap_pending_since') \
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

    # ── AP title diff (top-level parent rows → ap_titles) ───────
    # Diffed here (before the dry-run gate) so dry runs can report it;
    # the actual writes happen AFTER the child-row write phase below —
    # a display-title write must never precede or block the primary
    # child sync. Failure discipline (2026-08-10 decision doc): a failed
    # ap_titles read/write marks the run failed and exits non-zero at the
    # very end, but the child sync always completes first. Malformed
    # parent rows were already logged and skipped at fetch time.
    titles_to_write      = []
    date_events          = []  # parent-level end-date moves → ap_end_date_changes
    date_baselines       = 0   # stored end_date NULL → silent first capture, no event
    title_capture_failed = False
    try:
        titles_resp = db.table('ap_titles').select('ap_number, title, source_row_id, end_date').execute()
        existing_titles = {r['ap_number']: r for r in (titles_resp.data or []) if r.get('ap_number')}
        for p in parent_titles:
            ex         = existing_titles.get(p['ap_number'])
            stored_end = ex.get('end_date') if ex else None
            new_end    = p['end_date']

            if new_end is None:
                # Blank/unparseable Current Finish on a top-level parent
                # (none exist in the sheet today) — keep the stored value,
                # never null it out, never fire an event.
                if stored_end is not None:
                    log.warning(f"End-date capture: top-level parent {p['ap_number']} has a blank Current Finish — keeping stored {stored_end}")
                write_end = stored_end
            else:
                write_end = new_end
                if stored_end is None:
                    # First capture for this AP (or a brand-new AP row) —
                    # silent baseline. Historical moves never flag anyone.
                    date_baselines += 1
                elif stored_end != new_end:
                    # Genuine parent-level move (either direction — a pull-in
                    # matters to a child owner as much as a slip).
                    date_events.append({
                        'ap_number':    p['ap_number'],
                        'old_end_date': stored_end,
                        'new_end_date': new_end,
                    })

            if (ex is None or ex.get('title') != p['title']
                    or ex.get('source_row_id') != p['source_row_id']
                    or stored_end != write_end):
                titles_to_write.append({
                    'ap_number':     p['ap_number'],
                    'title':         p['title'],
                    'end_date':      write_end,
                    'source_row_id': p['source_row_id'],
                })
        log.info(
            f"AP titles: {len(existing_titles)} existing, {len(titles_to_write)} new/changed, "
            f"{date_baselines} end-date baseline(s), {len(date_events)} end-date change event(s)"
        )
    except Exception as e:
        title_capture_failed = True
        log.error(f"AP title capture: failed to load existing ap_titles — no titles/events will be written this run: {e}")

    to_insert_delivery = []
    to_update_delivery = []  # list of (id, fields_to_update, ap_number)
    to_clear_pending   = []  # list of (id, ap_number) — Smartsheet caught up, clear flag only
    to_insert_pc       = []
    to_update_pc       = []  # list of (id, fields_to_update, ap_number)
    to_clear_pending_pc = []  # list of (id, ap_number) — Smartsheet caught up, clear flag only
    escalations        = []  # pending > ESCALATION_DAYS and still diverging

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

        # Inactive rows exist ONLY to settle a pending Delivery or P&C flag.
        # Anything else — new rows, unflagged rows — is dropped here exactly
        # as if it had never been fetched (no counters, no logs), so behavior
        # for non-pending rows is byte-identical to the old active-only fetch.
        # Each destination branch below re-checks its own table's pending
        # flag before doing anything with an inactive row — this top-level
        # check only decides whether the row is worth resolving a lead for
        # at all; it must not be trusted alone to prevent an inactive insert,
        # since destination is resolved fresh per row and could in principle
        # land on the table that ISN'T the one holding the pending flag.
        if not task['active']:
            delivery_pending = bool(existing_delivery.get(ap_num, {}).get('ap_pending_update'))
            pc_pending = bool(existing_pc.get(ap_num, {}).get('ap_pending_update'))
            if not delivery_pending and not pc_pending:
                continue

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

                # Field diff runs BEFORE the pending check — the same fields
                # dict decides both "normal update" and "Smartsheet caught up",
                # so the reset condition and the update condition cannot drift.
                fields = {}
                if owner_id != ex.get('owner_id'):
                    fields['owner_id'] = owner_id
                if status != ex['status']:
                    fields['status'] = status
                if due_date != ex.get('due_date'):
                    fields['due_date'] = due_date
                if start_date != ex.get('start_date'):
                    fields['start_date'] = start_date
                if ex.get('priority') != 'Tier 2':
                    fields['priority'] = 'Tier 2'

                if ex.get('ap_pending_update'):
                    if not fields:
                        # Smartsheet caught up — the change the PLL flagged is
                        # now reflected here. Clear the flag, touch nothing else.
                        to_clear_pending.append((ex['id'], ap_num))
                        log.info(f"{ap_num}: Smartsheet caught up — clearing ap_pending_update")
                        if dry_run:
                            print(f"[DRY RUN] CLEAR    {ap_num:14} delivery — Smartsheet caught up, pending flag cleared")
                    else:
                        # Still diverging — keep protecting the PLL's change.
                        skipped_pending += 1
                        log.info(f"Skipping {ap_num} — pending update awaiting Smartsheet change")
                        if dry_run:
                            print(f"[DRY RUN] SKIP     {ap_num:14} delivery — pending update awaiting Smartsheet change")
                        pending_days = days_since(ex.get('ap_pending_since'))
                        if pending_days is not None and pending_days > ESCALATION_DAYS:
                            # ASCII arrow on purpose — this string is printed
                            # on Windows consoles (cp1252) as well as pushed.
                            divergence = ", ".join(
                                f"{k}: {ex.get(k)!r} -> {v!r}" for k, v in fields.items()
                            )
                            escalations.append({
                                'module':       'delivery',
                                'ap_number':    ap_num,
                                'owner':        id_to_name.get(ex.get('owner_id')) or lead_display_for_log,
                                'days_flagged': round(pending_days),
                                'divergence':   divergence,
                            })
                            log.warning(
                                f"ESCALATED: {ap_num} pending {round(pending_days)}d "
                                f"(> {ESCALATION_DAYS}d) and still diverging — {divergence}"
                            )
                            if dry_run:
                                print(f"[DRY RUN] ESCALATE {ap_num:14} delivery — pending {round(pending_days)}d, {divergence}")
                    continue

                if fields:
                    if 'owner_id' in fields:
                        log.info(f"{ap_num}: lead reassigned in Smartsheet — owner updated")
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
            # Inactive rows are fetched solely to settle a pending Delivery
            # or P&C flag (see the top-level filter above) — an inactive row
            # that isn't itself a pending P&C row must never create or
            # modify a P&C project, even if the Lead now resolves to a TPM.
            if not task['active'] and not existing_pc.get(ap_num, {}).get('ap_pending_update'):
                continue

            pc_status = PC_STATUS_MAP.get(task['status_raw'])
            title       = task['action_text']
            description = task['notes']

            if ap_num in existing_pc:
                ex = existing_pc[ap_num]

                # Field diff runs BEFORE the pending check — same discipline
                # as Delivery (572a1936): the reset condition and the update
                # condition share one fields dict so they can't drift apart.
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

                if ex.get('ap_pending_update'):
                    if not fields:
                        # Smartsheet caught up — clear the flag, touch nothing else.
                        to_clear_pending_pc.append((ex['id'], ap_num))
                        log.info(f"{ap_num}: Smartsheet caught up (P&C) — clearing ap_pending_update")
                        if dry_run:
                            print(f"[DRY RUN] CLEAR    {ap_num:14} P&C      — Smartsheet caught up, pending flag cleared")
                    else:
                        # Still diverging — keep protecting the TPM's change.
                        skipped_pending += 1
                        log.info(f"Skipping {ap_num} — P&C pending update awaiting Smartsheet change")
                        if dry_run:
                            print(f"[DRY RUN] SKIP     {ap_num:14} P&C      — pending update awaiting Smartsheet change")
                        pending_days = days_since(ex.get('ap_pending_since'))
                        if pending_days is not None and pending_days > ESCALATION_DAYS:
                            divergence = ", ".join(
                                f"{k}: {ex.get(k)!r} -> {v!r}" for k, v in fields.items()
                            )
                            escalations.append({
                                'module':       'pc',
                                'ap_number':    ap_num,
                                'owner':        id_to_name.get(ex.get('owner_id')) or lead_display_for_log,
                                'days_flagged': round(pending_days),
                                'divergence':   divergence,
                            })
                            log.warning(
                                f"ESCALATED: {ap_num} (P&C) pending {round(pending_days)}d "
                                f"(> {ESCALATION_DAYS}d) and still diverging — {divergence}"
                            )
                            if dry_run:
                                print(f"[DRY RUN] ESCALATE {ap_num:14} P&C      — pending {round(pending_days)}d, {divergence}")
                    continue

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
            f"Delivery — would insert: {len(to_insert_delivery)}, would update: {len(to_update_delivery)}, "
            f"would clear pending flag: {len(to_clear_pending)}"
        )
        pc_summary = (
            f"P&C      — would insert: {len(to_insert_pc)}, would update: {len(to_update_pc)}, "
            f"would clear pending flag: {len(to_clear_pending_pc)}"
        )
        skip_summary = (
            f"Skipped  — no AP#/text: {skipped_no_ap}, unmapped lead: {skipped_unmapped}, "
            f"ambiguous lead: {skipped_ambiguous}, owner is viewer: {skipped_viewer}, "
            f"pending Smartsheet update: {skipped_pending}, unchanged: {skipped_unchanged}"
        )
        titles_summary = (
            f"AP titles — captured from Smartsheet: {len(parent_titles)}, "
            f"would write (new/changed): {len(titles_to_write)}, "
            f"end-date baselines: {date_baselines}, "
            f"end-date change events: {len(date_events)}"
            + (" [CAPTURE FAILED — existing ap_titles unreadable]" if title_capture_failed else "")
        )
        print("-" * 72)
        print(delivery_summary)
        print(pc_summary)
        print(skip_summary)
        print(titles_summary)
        for t in titles_to_write:
            print(f"[DRY RUN] TITLE    {t['ap_number']:14} \"{t['title']}\" end {t['end_date']} (row {t['source_row_id']})")
        for ev in date_events:
            print(f"[DRY RUN] DATEMOVE {ev['ap_number']:14} {ev['old_end_date']} -> {ev['new_end_date']}")
        if unmapped_leads:
            print(f"Unmapped lead emails ({len(unmapped_leads)}): {sorted(unmapped_leads)}")
        if ambiguous_leads:
            print(f"AMBIGUOUS lead emails ({len(ambiguous_leads)}) — needs human review: {sorted(ambiguous_leads)}")
        if viewer_leads:
            print(f"VIEWER lead emails ({len(viewer_leads)}) — resolves to a real person, excluded by role: {sorted(viewer_leads)}")
        if escalations:
            print(f"Escalated ({len(escalations)}) — pending > {ESCALATION_DAYS} days and still diverging:")
            for e in escalations:
                print(f"  [{e['module']:8}] {e['ap_number']:14} {e['days_flagged']:>3}d  {e['owner']}  — {e['divergence']}")
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

    # Clear settled pending flags — Smartsheet caught up on these rows.
    # Deliberately does NOT touch last_updated (or anything else): the row's
    # content didn't change, only the flag lifecycle did.
    cleared_pending = 0
    for item_id, ap_num in to_clear_pending:
        try:
            db.table('action_items') \
                .update({'ap_pending_update': False, 'ap_pending_since': None}) \
                .eq('id', item_id) \
                .execute()
            cleared_pending += 1
        except Exception as e:
            log.error(f"Pending-flag clear failed for {ap_num}: {e}")

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

    # Clear settled P&C pending flags — same discipline as Delivery: only
    # the flag lifecycle changes, nothing else on the row.
    cleared_pending_pc = 0
    for item_id, ap_num in to_clear_pending_pc:
        try:
            db.table('pc_projects') \
                .update({'ap_pending_update': False, 'ap_pending_since': None}) \
                .eq('id', item_id) \
                .execute()
            cleared_pending_pc += 1
        except Exception as e:
            log.error(f"P&C pending-flag clear failed for {ap_num}: {e}")

    # ── End-date change events (diffed pre-gate; deliberately AFTER the
    # child sync, and BEFORE the ap_titles upsert: if the stored end_date
    # advanced but the event insert had failed, the event would be lost
    # forever — the next run's diff would see no change. The reverse
    # failure only produces a duplicate event on retry, which the app's
    # ack-all-outstanding-events-per-AP design absorbs. So: events first,
    # and on event failure skip the title writes so the stored dates
    # cannot advance past an unrecorded event. ──────────────────────
    date_events_written = 0
    if not title_capture_failed and date_events:
        try:
            db.table('ap_end_date_changes').insert(date_events).execute()
            date_events_written = len(date_events)
            for ev in date_events:
                log.info(f"End-date change event: {ev['ap_number']} {ev['old_end_date']} -> {ev['new_end_date']}")
        except Exception as e:
            title_capture_failed = True
            log.error(f"ap_end_date_changes insert failed — skipping ap_titles writes this run so stored dates don't advance past the unrecorded event(s): {e}")

    # ── AP title writes (diffed pre-gate; deliberately AFTER the child
    # sync so a title failure can never cost a child row) ───────────
    titles_written = 0
    if not title_capture_failed and titles_to_write:
        # Explicit updated_at on every write (2026-08-05 lesson class: no
        # trigger bumps this table — verified at migration time), and only
        # on new/changed rows, so updated_at stays meaningful as "the title
        # last actually changed", not "the sync last ran".
        now_iso = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(titles_to_write), 25):
            batch = [{**t, 'updated_at': now_iso} for t in titles_to_write[i:i + 25]]
            try:
                db.table('ap_titles').upsert(batch, on_conflict='ap_number').execute()
                titles_written += len(batch)
            except Exception as e:
                title_capture_failed = True
                log.error(f"AP title upsert batch failed: {e}")
    if titles_written:
        log.info(f"AP titles written: {titles_written} (of {len(parent_titles)} captured)")

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
        f"Delivery: inserted {inserted_delivery}, updated {updated_delivery}, "
        f"pending cleared {cleared_pending} | "
        f"P&C: inserted {inserted_pc}, updated {updated_pc}, "
        f"pending cleared {cleared_pending_pc} | "
        f"skipped: {skipped_total}, "
        f"pending Smartsheet updates: {len(pending)}, "
        f"escalated: {len(escalations)}, "
        f"WIP flags set: {wip_flagged} | "
        f"AP titles: captured {len(parent_titles)}, written {titles_written}, "
        f"end-date events: {len(date_events)} detected, {date_events_written} written"
        + (" [TITLE/DATE CAPTURE FAILED]" if title_capture_failed else "")
    )
    log.info(summary)
    print(summary)

    # `sc` (SAM COS client) was already created at the top of this function
    # for the started heartbeat — reused here for the escalation record and
    # completion heartbeat rather than recreated.

    # ── Escalation record + Pushover page → SAM COS ─────────────
    # context_store[ESCALATION_KEY] always mirrors the CURRENT escalated set —
    # overwritten every run, including down to an empty list, so it can be
    # trusted as durable state. The Pushover page (a notification_queue row;
    # samcos drains the queue to Pushover) fires ONLY when the set of AP
    # numbers differs from what context_store already held — a 30-minute cron
    # must not re-page about the same rows all day.
    # notification_queue is used instead of POST /api/notify because this
    # workflow holds SAMCOS_SERVICE_KEY but not CRON_SECRET, and a queued row
    # survives a samcos outage where a direct POST would be lost.
    if sc is not None:
        try:
            stored_aps = []
            stored_resp = sc.table('context_store').select('value').eq('key', ESCALATION_KEY).execute()
            if stored_resp.data and stored_resp.data[0].get('value'):
                try:
                    stored_state = json.loads(stored_resp.data[0]['value'])
                    stored_aps = sorted(e['ap_number'] for e in stored_state.get('escalated', []))
                except (ValueError, KeyError, TypeError):
                    stored_aps = []  # unreadable prior state — treat as empty
            current_aps = sorted(e['ap_number'] for e in escalations)

            sc.table('context_store').upsert({
                'key':    ESCALATION_KEY,
                'value':  json.dumps({
                    'escalated': escalations,
                    'as_of':     datetime.now(timezone.utc).isoformat(),
                }),
                'domain': 'work',
                'notes':  (
                    f'{len(escalations)} row(s) (Delivery action_items or P&C pc_projects) '
                    f'flagged ap_pending_update for > {ESCALATION_DAYS} days and still '
                    f'diverging from Smartsheet. Written by sync_ap.py every run.'
                ),
            }, on_conflict='key').execute()
            log.info(f"Escalation record written to SAM COS ({len(escalations)} row(s))")

            if current_aps != stored_aps:
                if escalations:
                    lines = [
                        f"[{e['module']}] {e['ap_number']} ({e['days_flagged']}d, {e['owner']}) — {e['divergence']}"
                        for e in escalations
                    ]
                    message = (
                        f"{len(escalations)} AP row(s) flagged > {ESCALATION_DAYS} days, "
                        f"Smartsheet still not updated:\n" + "\n".join(lines)
                    )
                else:
                    message = "All previously escalated AP pending rows have resolved."
                sc.table('notification_queue').insert({
                    'title':   'ORiON AP sync — pending escalation',
                    'message': message[:1000],
                }).execute()
                log.warning(f"Escalation page queued — set changed {stored_aps} -> {current_aps}")
        except Exception as _e:
            log.error(f"Escalation record/notify failed: {_e}")

    # ── Health heartbeat → SAM COS context_store ────────────────
    # Upserts last successful run timestamp so the health dashboard can monitor this script.
    if sc is not None:
        try:
            sc.table('context_store').upsert({
                'key':    'health:github:orion_ap_sync',
                'value':  datetime.utcnow().isoformat() + 'Z',
                'domain': 'system',
                'notes':  (
                    f'delivery_inserted={inserted_delivery} delivery_updated={updated_delivery} '
                    f'delivery_pending_cleared={cleared_pending} escalated={len(escalations)} '
                    f'pc_inserted={inserted_pc} pc_updated={updated_pc} '
                    f'pc_pending_cleared={cleared_pending_pc} skipped={skipped_total} '
                    f'titles_captured={len(parent_titles)} titles_written={titles_written} '
                    f'date_events_detected={len(date_events)} date_events_written={date_events_written} '
                    f'date_baselines={date_baselines} '
                    f'title_capture_failed={title_capture_failed}'
                ),
            }, on_conflict='key').execute()
            log.info("Health heartbeat sent to SAM COS")
        except Exception as _e:
            log.warning(f"Health heartbeat failed (non-critical): {_e}")

    # ── Title-capture failure → non-zero exit, LAST ─────────────────
    # Runs after every write phase and both heartbeats: the child sync (the
    # production purpose of this run) completed and its heartbeat reflects
    # that, but the run itself must not report green on a partial success —
    # a green run that silently dropped titles is the same lie as the
    # 2026-07-30 fetch-failure incident, just smaller. Decision doc:
    # knowledge/decisions/2026-08-10-ap-title-capture.md.
    if title_capture_failed:
        msg = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | AP title/end-date capture FAILED — exiting non-zero (child sync completed; see log)"
        log.error(msg)
        print(msg)
        sys.exit(1)


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
