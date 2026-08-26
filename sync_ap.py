#!/usr/bin/env python3
"""
sync_ap.py
─────────────────────────────────────────────────────────────────
Smartsheet Action Plan → ORiON sync. Runs on ARGUS on a schedule.

Flow:
  - Smartsheet is the SOURCE OF TRUTH. This script never writes to it.
  - Reads child-level AP tasks from the OFS Training Action Plan Tracker
    (active statuses drive the sync; inactive rows are read to settle
    pending flags and to close out existing rows whose Smartsheet status
    went On Hold / Complete / Cancelled — see fetch_child_ap_tasks).
  - Each row's Lead cell is resolved to an email (linked contact value, then
    the ap_lead_aliases table, then a name match against users/portal_users —
    see resolve_lead_email), then that email is resolved against BOTH users
    (PLLs/admins) and portal_users (TPMs). PLL/admin leads route to
    action_items (Delivery); TPM leads route to pc_projects (P&C). A lead
    resolving in both, resolving only to a viewer, or resolving in neither,
    is skipped and logged rather than guessed.
  - In-scope rows (the ones that route to a module, or that already hold a
    module row) land FIRST in the ap_tracker mirror table (full-row shape,
    keyed on smartsheet_row_id — decision 69ba45bd, 2026-08-26); the module
    tables below are projections of that landing zone. The mirror carries
    the loop-prevention state (orion_written_value/orion_written_at/
    orion_dirty + the row-level Smartsheet modifiedAt) for the future
    ORiON→Smartsheet write-back: an ORiON write stamps the row dirty; the
    next sync clears the flag on an echo (sheet == written value, nothing
    logged), ingests + conflict-logs a genuinely newer Smartsheet change
    (last-write-wins, decision 93113ee9), and protects the row when
    Smartsheet hasn't caught up yet. The live write-back is NOT here and
    never will be: it shipped 2026-08-26 in the orion-pll app
    (lib/smartsheet.ts + app/(protected)/operations/ap-actions.ts), which
    stamps the dirty state and pushes at save time — this script's job is
    only to recognize those pushes as echoes on the next run.
    push_row_to_smartsheet() below stays a gate-demonstrating placeholder.
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
    (Smartsheet caught up). Both tables carry the flag fields; the
    Jennifer-reconcile narrative above is the Delivery flow, and pc_projects
    has the same settle mechanics.
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
COL_NO_REPORT_OUT = 5308689965338500 # No report-out at this level (CHECKBOX) → no_report_out

# ─── MIRROR COLUMN IDs (ap_tracker full-row capture, Ops Phase 2) ───
# The remaining sheet columns, captured only into the ap_tracker mirror
# (decision 69ba45bd). The module sync above never reads these.
COL_AP_ID          = 6603493652778884  # AP ID#
COL_AUTONUM        = 8566302296985476  # AutoNum
COL_UNIQUE_ID      = 403527972376452   # UniqueID
COL_PARENT_ID      = 3240146395942788  # Parent ID
COL_PARENTID       = 2510806431518596  # ParentID (distinct sheet column)
COL_PARENT_LEVEL   = 5248404384075652  # Parent_Level
COL_CHILD_NUM      = 7014406058889092  # Child#
COL_ASSIST         = 7737769182056324  # Assist
COL_TDM_AMB        = 6762592903401348  # TDM Ambassador (CONTACT_LIST)
COL_OWNER          = 8300719135477636  # Owner (CONTACT_LIST)
COL_CURRENT_STATUS = 8412869321510788  # Current Status (sheet picklist)
COL_CURRENT_START  = 5394148738944900  # Current Start
COL_PREV_FINISH    = 7800627084873604  # Previous Finish Date
COL_MOVE_CANCEL    = 7174133144833924  # Move/Cancel Pending Discussion (CHECKBOX)
COL_ORIG_START     = 2108269647843204  # Original Start
COL_ORIG_FINISH    = 5899404933549956  # Original Finish
COL_HOURS_SAVED    = 5079137159253892  # Hours Saved per Year
COL_IMPACTED_TRNG  = 5286202654805892  # Impacted Trng Roles
COL_IMPACTED_NON   = 3034402841120644  # Impacted Non Training Roles
COL_KPI            = 4437592226090884  # KPI Connection
COL_ORIG_REQUESTER = 6843756061609860  # Original Requester
COL_COMMENTS       = 3647605119864708  # Comments
COL_ONE_PAGER      = 8151204747235204  # One-pager Link
COL_DURATION       = 7645948552630148  # Duration
COL_PREDECESSORS   = 2016449018417028  # Predecessors
COL_STARTING_MONTH = 3679394665287556  # is starting this month (CHECKBOX)
COL_DUE_THIS_MONTH = 3206058314256260  # is due this month (CHECKBOX)
COL_DUE_NEXT_MONTH = 7709657941626756  # Is due next month (CHECKBOX)
COL_DUE_LAST_MONTH = 6983934885973892  # Is due last month (CHECKBOX)
COL_AT_RISK        = 6520048645787524  # At Risk (CHECKBOX)
COL_PLL_LEAD       = 6296898674921348  # PLL Lead?
COL_FINISH_DATE_ONLY = 1211657396457348  # Finish Date (date only)
COL_CREATED_CELL   = 1831243593502596  # Created (DATETIME)
COL_MODIFIED_CELL  = 604393216184196   # Modified (DATETIME)
COL_CREATED_BY     = 4844025737334660  # Created By (CONTACT_LIST)

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
# Active statuses flow through the normal update path; inactive ones
# (On Hold / Complete / Cancelled) reach an existing row only via the
# status-only close pass in main() or the pending-settle diff — never as
# inserts. Before the inactive mappings existed (bug e6b35596), a pending
# P&C row that went Complete in Smartsheet diffed status against None and
# could never settle its flag.
PC_STATUS_MAP = {
    "Not Started": "approved",
    "In Progress": "active",
    "On Hold":     "on_hold",
    "Complete":    "complete",
    "Cancelled":   "cancelled",
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

    Inactive rows (Complete / Cancelled / On Hold) are included for two
    purposes only: (a) a row whose ap_pending_update flag is set can be
    recognized as caught-up once Jennifer applies the change in Smartsheet
    (e.g. ORiON "Done" vs Smartsheet "Complete" map to the same status and
    must clear the flag), and (b) an existing non-pending row whose
    Smartsheet status went inactive gets a status-only close write, so
    Complete/Cancelled/On Hold actually reaches ORiON (bug e6b35596).
    main() restricts inactive rows to exactly those paths — they never
    insert, never touch any field beyond status on a close, and never
    touch the skip counters.

    Smartsheet rows are retrieved via GET /sheets/{id} with pagination.
    """
    page_size = 500
    page      = 1
    all_rows  = []
    total_row_count = None

    # Termination keys on what the GET /sheets/{id} response actually
    # contains: a short page (fewer rows than pageSize) means the sheet is
    # exhausted, and totalRowCount — the field the response really returns —
    # is cross-checked as a secondary stop and a post-loop audit. The old
    # loop broke on `totalPages`, a field this response does NOT return, so
    # .get()'s default of 1 always won and no run ever read past row 500 of
    # the 709-row tracker (bug 0644145e — ~78 active rows silently never
    # evaluated, the bulk of the 108-row gap and the ap_titles gap).
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
        total_row_count = data.get("totalRowCount", total_row_count)
        log.info(
            f"Smartsheet: page {page} returned {len(rows)} row(s) — "
            f"{len(all_rows)} fetched so far, totalRowCount={total_row_count}"
        )
        if len(rows) < page_size:
            break
        if total_row_count is not None and len(all_rows) >= total_row_count:
            break
        page += 1

    if total_row_count is not None and len(all_rows) != total_row_count:
        # Loud, not fatal: rows added/deleted between page fetches can
        # legitimately move the count; orphan detection downstream is the
        # reason a silent shortfall here would be dangerous.
        log.warning(
            f"Smartsheet: fetched {len(all_rows)} rows but the response "
            f"reported totalRowCount={total_row_count} — sheet may have "
            f"changed mid-fetch"
        )
    log.info(f"Smartsheet: fetched {len(all_rows)} total rows across {page} page(s)")

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

        # Child tasks only. Is Child alone qualifies a row as a syncable
        # task — a row that is BOTH Is Child and Is Parent (a middle node
        # in a 3-level hierarchy, e.g. AP-214-7 under AP-214) is still a
        # real task with its own Lead/status/dates, and dropping it here
        # silently excluded 15 active rows across 7 AP families (bug
        # 59cd7d7b). Is Parent gates ONLY the title-capture branch above,
        # where distinguishing top-level parents is legitimate.
        if is_child != "1.0" and is_child != "1":
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
            "no_report_out_raw": cells.get(COL_NO_REPORT_OUT),
            # Full-row capture for the ap_tracker mirror — same fetch, same
            # pass; main() decides which rows actually land (in-scope only).
            "mirror":       build_mirror_capture(row, cells, raw, display_raw),
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
    """Extract YYYY-MM-DD from Smartsheet ISO datetime string.

    The slice is validated as a real calendar date before it's returned.
    Smartsheet surfaces formula errors as literal cell text — DATEONLY() on a
    blank Current Start yields "#INVALID DATA TYPE" — and passing the sliced
    literal ("#INVALID D") through to Postgres poisoned an entire 25-row
    insert batch on the first live run of 2026-08-24 (25 of 31 Delivery
    inserts silently dropped). An unusable date means the row has no date,
    not that the row is unsyncable: return None, warn with the raw value so
    the sheet-side data problem stays visible in the log.
    """
    if not finish_raw:
        return None
    candidate = str(finish_raw)[:10]
    try:
        datetime.strptime(candidate, '%Y-%m-%d')
    except ValueError:
        log.warning(f"Unparseable date cell value {finish_raw!r} — treating as unset")
        return None
    return candidate


def parse_iso_ts(iso_ts: str | None) -> datetime | None:
    """Parse a Smartsheet/Supabase ISO timestamp ('Z' or offset form) to an
    aware datetime; None if unset/unparseable. Used by the loop-prevention
    comparison, where a string compare would be wrong across the two
    formats ('...Z' vs '...+00:00')."""
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(str(iso_ts).replace('Z', '+00:00'))
    except ValueError:
        return None


def build_mirror_capture(row: dict, cells: dict, raw: dict, display_raw: dict) -> dict:
    """Full-row capture of one Smartsheet child row for the ap_tracker
    mirror (decision 69ba45bd — the mirror holds the FULL sheet shape;
    which rows get landed stays scoped in main()). Keys match ap_tracker
    columns 1:1 so the dict IS the upsert payload (minus sync-state, which
    the write planner appends). Contact columns keep raw value + display,
    same discipline as the Lead handling above. Never touches the module
    (child_tasks) capture — that projection stays byte-identical."""
    def txt(col):
        v = cells.get(col)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    def contact(col):
        v = (str(raw.get(col)).strip() if raw.get(col) is not None else None) or None
        d = (str(display_raw.get(col)).strip() if display_raw.get(col) is not None else None) or None
        return v, d

    def flag01(col):
        return str(cells.get(col, "0")).strip() in ("1", "1.0")

    lead_v, lead_d = contact(COL_LEAD)
    tdm_v, tdm_d = contact(COL_TDM_AMB)
    owner_v, owner_d = contact(COL_OWNER)
    creator_v, creator_d = contact(COL_CREATED_BY)

    return {
        'smartsheet_row_id':     row.get('id'),
        'smartsheet_row_number': row.get('rowNumber'),
        'smartsheet_created_at': row.get('createdAt'),
        # Row-level modifiedAt — the loop-prevention timestamp (decision
        # e217604f at row grain; cell-level modified time is not in the
        # GET /sheets fetch).
        'smartsheet_modified_at': row.get('modifiedAt'),
        'ap_number':      txt(COL_AP_NUM),
        'ap_id_number':   txt(COL_AP_ID),
        'autonum':        txt(COL_AUTONUM),
        'unique_id':      txt(COL_UNIQUE_ID),
        'parent_id':      txt(COL_PARENT_ID),
        'parentid':       txt(COL_PARENTID),
        'parent_level':   txt(COL_PARENT_LEVEL),
        'child_number':   txt(COL_CHILD_NUM),
        'is_parent':      flag01(COL_IS_PARENT),
        'is_child':       flag01(COL_IS_CHILD),
        'lead_value':     lead_v,
        'lead_display':   lead_d,
        'assist':         txt(COL_ASSIST),
        'tdm_ambassador_value':   tdm_v,
        'tdm_ambassador_display': tdm_d,
        'owner_value':    owner_v,
        'owner_display':  owner_d,
        'created_by_value':   creator_v,
        'created_by_display': creator_d,
        'improvement':    txt(COL_IMPROVEMENT),
        'description':    txt(COL_DESCRIPTION),
        'sqdcgp':         txt(COL_SQDCG),
        'bucket':         txt(COL_BUCKET),
        'source':         txt(COL_SOURCE),
        'overall_status': txt(COL_STATUS),
        'no_report_out':  map_no_report_out(cells.get(COL_NO_REPORT_OUT)),
        'current_status_sheet': txt(COL_CURRENT_STATUS),
        'current_start':  parse_due_date(cells.get(COL_CURRENT_START)),
        'current_finish': parse_due_date(cells.get(COL_FINISH)),
        'previous_finish_date': parse_due_date(cells.get(COL_PREV_FINISH)),
        'original_start':  parse_due_date(cells.get(COL_ORIG_START)),
        'original_finish': parse_due_date(cells.get(COL_ORIG_FINISH)),
        'start_date_only':  parse_due_date(cells.get(COL_START)),
        'finish_date_only': parse_due_date(cells.get(COL_FINISH_DATE_ONLY)),
        'duration':       txt(COL_DURATION),
        'predecessors':   txt(COL_PREDECESSORS),
        'created_at_cell':  parse_iso_ts(cells.get(COL_CREATED_CELL)).isoformat() if parse_iso_ts(cells.get(COL_CREATED_CELL)) else None,
        'modified_at_cell': parse_iso_ts(cells.get(COL_MODIFIED_CELL)).isoformat() if parse_iso_ts(cells.get(COL_MODIFIED_CELL)) else None,
        'move_cancel_pending':    bool(raw.get(COL_MOVE_CANCEL)),
        'is_starting_this_month': bool(raw.get(COL_STARTING_MONTH)),
        'is_due_this_month':      bool(raw.get(COL_DUE_THIS_MONTH)),
        'is_due_next_month':      bool(raw.get(COL_DUE_NEXT_MONTH)),
        'is_due_last_month':      bool(raw.get(COL_DUE_LAST_MONTH)),
        'at_risk':        bool(raw.get(COL_AT_RISK)),
        'pll_lead':       txt(COL_PLL_LEAD),
        'hours_saved_per_year':        txt(COL_HOURS_SAVED),
        'impacted_trng_roles':         txt(COL_IMPACTED_TRNG),
        'impacted_non_training_roles': txt(COL_IMPACTED_NON),
        'kpi_connection':     txt(COL_KPI),
        'original_requester': txt(COL_ORIG_REQUESTER),
        'comments':       txt(COL_COMMENTS),
        'one_pager_link': txt(COL_ONE_PAGER),
    }


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


def map_no_report_out(raw) -> bool:
    """Map the 'No report-out at this level' CHECKBOX cell to a boolean.

    Inverse of map_category's blank rule (decision
    2026-08-25-sync-no-report-out-flag.md): this is a checkbox, not a
    picklist, so Smartsheet reports "unchecked" as a blank/absent cell, not
    as an explicit False — and blank IS the answer here, not a data gap.
    checked (True) -> True; blank/absent/False -> False. Never None: a
    no_report_out column that could land NULL would make the Report-Out
    view unable to tell "reports out" from "unknown."
    """
    return bool(raw)


# ─── WIP CHECK (Delivery only — pc_projects has no WIP concept) ─
def check_and_flag_wip_overages(db, owner_ids: set, log) -> int:
    """
    For each owner, count Tier 2 items 'currently working':
    start_date <= today <= due_date (nulls = unbounded).
    If count > 5, set pending_wip_review = true.
    Returns number of users flagged.

    One batched SELECT across all owner_ids (grouped in memory) plus one
    batched UPDATE for whichever owners are over, instead of a per-owner
    query pair — mirrors the batch-insert/batch-update discipline this
    script's main reconcile loop already uses (bug 0514aaac).
    """
    if not owner_ids:
        return 0

    today = datetime.now().date()
    try:
        resp = db.table('action_items') \
            .select('id, owner_id, start_date, due_date') \
            .in_('owner_id', list(owner_ids)) \
            .eq('priority', 'Tier 2') \
            .in_('status', ['Open', 'In Progress']) \
            .execute()
    except Exception as e:
        log.error(f"WIP check failed for owners {sorted(owner_ids)}: {e}")
        return 0

    counts = {}
    for r in (resp.data or []):
        if (r.get('start_date') is None or r['start_date'] <= str(today)) \
                and (r.get('due_date') is None or r['due_date'] >= str(today)):
            counts[r['owner_id']] = counts.get(r['owner_id'], 0) + 1

    over_ids = [owner_id for owner_id, count in counts.items() if count > 5]
    if not over_ids:
        log.info(f"WIP check: {len(owner_ids)} owner(s) checked via 1 query, 0 over threshold")
        return 0

    try:
        db.table('users') \
            .update({'pending_wip_review': True}) \
            .in_('id', over_ids) \
            .execute()
    except Exception as e:
        log.error(f"WIP flag update failed for owners {sorted(over_ids)}: {e}")
        return 0

    for owner_id in over_ids:
        log.warning(f"WIP overage: owner {owner_id} has {counts[owner_id]} active Tier 2 items — pending_wip_review flagged")
    log.info(f"WIP check: {len(owner_ids)} owner(s) checked via 1 query, {len(over_ids)} flagged via 1 update")
    return len(over_ids)


# ─── AP TRACKER MIRROR (Ops Phase 2 foundation) ─────────────────
def plan_mirror_writes(existing_mirror: dict, mirror_candidates: dict, dry_run: bool):
    """
    Loop-prevention planner for the ap_tracker mirror (decision e217604f:
    (a) per-row sync state + (c) timestamp cursor; conflict rule 93113ee9).

    For each candidate row (keyed by smartsheet_row_id):
      - existing row with orion_dirty set:
          * every field in orion_written_value matches the fresh sheet
            value  -> ECHO: our own (future) write came back; clear the
            flag on the upsert, log NOTHING to ap_change_log.
          * fields differ AND the row's Smartsheet modifiedAt is AFTER
            orion_written_at -> REAL post-write change: ingest the sheet
            values (last-write-wins), clear the flag, and record one
            ap_change_log conflict row per differing field (old = what
            ORiON wrote / the losing edit, new = the Smartsheet value).
            Surfacing these to Jen's review queue is second-brief UI.
          * fields differ but Smartsheet is NOT newer -> Smartsheet hasn't
            seen the ORiON write yet: PROTECT — skip the upsert entirely,
            keep the dirty flag.
      - anything else -> plain upsert; the payload carries
        orion_dirty=false / orion_written_* = null, which is a no-op for
        rows that were never dirty. (Known Phase 2 limit: a dirty stamp
        landing between this run's read and write would be cleared — the
        live ORiON write path arrives in the second brief, which owns
        closing that race.)

    Returns (to_upsert, conflict_rows, conflicted_row_ids, echo_cleared,
    conflicts_ingested, protected_pending). conflicted_row_ids maps the
    conflict rows back to their mirror payloads: the write phase inserts
    conflict rows BEFORE the upserts, and on a failed conflict insert it
    withholds those rows' upserts so the dirty state (and therefore the
    conflict evidence) survives to the next run — same events-before-titles
    ordering discipline as the end-date capture below (a cleared flag with
    no logged conflict is a lost record; a duplicate conflict row on retry
    is absorbable).
    """
    to_upsert       = []
    conflict_rows   = []
    conflicted_row_ids = set()
    echo_cleared    = 0
    conflicts_ingested = 0
    protected_pending  = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for row_id in sorted(mirror_candidates):
        task    = mirror_candidates[row_id]
        payload = dict(task['mirror'])
        ap_num  = payload.get('ap_number')
        ex      = existing_mirror.get(row_id)

        if ex and ex.get('orion_dirty'):
            written = ex.get('orion_written_value') or {}
            diffs = {
                k: payload.get(k)
                for k in written
                if payload.get(k) != written.get(k)
            }
            if not diffs:
                echo_cleared += 1
                log.info(f"{ap_num}: mirror echo — Smartsheet reflects the ORiON write; clearing dirty flag, logging nothing")
                if dry_run:
                    print(f"[DRY RUN] ECHO     {str(ap_num):14} mirror — ORiON write reflected, would clear dirty flag")
            else:
                sheet_mod  = parse_iso_ts(payload.get('smartsheet_modified_at'))
                written_at = parse_iso_ts(ex.get('orion_written_at'))
                if sheet_mod and written_at and sheet_mod > written_at:
                    conflicts_ingested += 1
                    conflicted_row_ids.add(row_id)
                    for k, newv in diffs.items():
                        conflict_rows.append({
                            'module':    'ops',
                            'item_id':   ex['id'],
                            'ap_number': ap_num,
                            'field':     k,
                            'old_value': None if written.get(k) is None else str(written.get(k)),
                            'new_value': None if newv is None else str(newv),
                            'reason':    'conflict: Smartsheet changed after ORiON write — last-write-wins (Smartsheet kept), losing ORiON edit recorded (decision 93113ee9)',
                            'changed_by': None,
                        })
                    log.warning(f"{ap_num}: mirror conflict — Smartsheet changed after ORiON write ({sorted(diffs)}); ingesting Smartsheet values, conflict logged")
                    if dry_run:
                        print(f"[DRY RUN] CONFLICT {str(ap_num):14} mirror — Smartsheet newer on {sorted(diffs)}, would ingest + log")
                else:
                    protected_pending += 1
                    log.info(f"{ap_num}: mirror pending — Smartsheet not yet caught up to ORiON write; protecting row, no upsert")
                    if dry_run:
                        print(f"[DRY RUN] PROTECT  {str(ap_num):14} mirror — ORiON write pending mirror-out, row skipped")
                    continue

        payload['orion_dirty']         = False
        payload['orion_written_value'] = None
        payload['orion_written_at']    = None
        payload['last_synced_at']      = now_iso
        to_upsert.append(payload)

    return to_upsert, conflict_rows, conflicted_row_ids, echo_cleared, conflicts_ingested, protected_pending


def user_is_ap_manager(db, user_id: str) -> bool:
    """True iff portal_users.is_ap_manager is set for user_id. The push
    gate below depends on this being checked HERE, in code: the sync and
    any future push run as service_role, which BYPASSES RLS, so the
    is_ap_manager() RLS predicate on ap_tracker cannot gate an external
    write on its own (decision 46da36f0, layer 3)."""
    try:
        resp = db.table('portal_users').select('is_ap_manager').eq('id', user_id).execute()
    except Exception as e:
        log.error(f"AP Manager flag lookup failed for {user_id}: {e}")
        return False
    rows = resp.data or []
    return bool(rows and rows[0].get('is_ap_manager'))


def push_row_to_smartsheet(db, acting_user_id: str, ap_number: str, fields: dict):
    """ORiON → Smartsheet push path — GATE-ONLY PLACEHOLDER, permanently.

    The LIVE immediate-write shipped 2026-08-26 in the orion-pll app
    (lib/smartsheet.ts + app/(protected)/operations/ap-actions.ts): the
    push fires from a user's save in the Ops tab, so it cannot live in
    this cron script. This placeholder is kept only as the executable
    statement of the gate rule for any future push added HERE: service_role
    bypasses RLS, so portal_users.is_ap_manager must be enforced in code
    at the push step (decision 46da36f0) — exactly as the app's server
    action does.
    """
    if not user_is_ap_manager(db, acting_user_id):
        raise PermissionError(
            f"user {acting_user_id} is not an AP Manager — ORiON→Smartsheet "
            f"writes are gated on portal_users.is_ap_manager (decision 46da36f0)"
        )
    raise NotImplementedError(
        "The live ORiON→Smartsheet write-back lives in the orion-pll app "
        "(lib/smartsheet.ts), not in this script. sync_ap.py is strictly "
        "Smartsheet→ORiON; do not wire a sheet write here."
    )


def build_delivery_notes(task: dict) -> str | None:
    """Combine description and bucket context for an action_items row."""
    notes_parts = []
    if task['notes']:
        notes_parts.append(task['notes'])
    if task['bucket']:
        notes_parts.append(f"Bucket: {task['bucket']}")
    return " | ".join(notes_parts) if notes_parts else None


# ─── MAIN ───────────────────────────────────────────────────────
def build_sync_accounting(*, child_tasks_total, inserted_delivery, updated_delivery,
                          inserted_pc, updated_pc, skipped_unchanged, cleared_pending,
                          cleared_pending_pc, closed_delivery, closed_pc, skipped_pending,
                          skipped_no_ap, skipped_ambiguous, skipped_viewer, skipped_unmapped,
                          skipped_inactive_noop, skipped_inactive_wrongtable,
                          failed_inserts_delivery, failed_inserts_pc,
                          parent_titles_total, titles_new_or_changed, titles_written,
                          date_baselines, date_events_detected, date_events_written,
                          title_capture_failed,
                          mirror_candidates_total, mirror_upserted, mirror_echo_cleared,
                          mirror_conflicts_ingested, mirror_protected_pending,
                          mirror_conflict_rows_logged, mirror_failed):
    """
    Machine-readable per-run accounting — Phase 1 of the reconciliation
    monitor (decision 2026-08-25-ap-sync-reconciliation-monitor-design.md,
    action item 9278f68e). Two SEPARATE identities, never folded together:
    the child stream must sum to child_tasks_total; the parent-titles stream
    is reported alongside with its own numbers. child_identity_residual is
    emitted, not acted on — the Phase 2 monitor interprets it. Keyword-only
    on purpose: both call sites (dry-run prediction, live actuals) are forced
    to supply every field, so the emitted shape cannot drift between them.
    Note `escalations` is an overlay on skipped_pending, not a disposition —
    it must never appear as a term here.
    """
    child = {
        'child_tasks_total':           child_tasks_total,
        'inserted_delivery':           inserted_delivery,
        'updated_delivery':            updated_delivery,
        'inserted_pc':                 inserted_pc,
        'updated_pc':                  updated_pc,
        'skipped_unchanged':           skipped_unchanged,
        'cleared_pending':             cleared_pending,
        'cleared_pending_pc':          cleared_pending_pc,
        'closed_delivery':             closed_delivery,
        'closed_pc':                   closed_pc,
        'skipped_pending':             skipped_pending,
        'skipped_no_ap':               skipped_no_ap,
        'skipped_ambiguous':           skipped_ambiguous,
        'skipped_viewer':              skipped_viewer,
        'skipped_unmapped':            skipped_unmapped,
        'skipped_inactive_noop':       skipped_inactive_noop,
        'skipped_inactive_wrongtable': skipped_inactive_wrongtable,
        'failed_inserts_delivery':     failed_inserts_delivery,
        'failed_inserts_pc':           failed_inserts_pc,
    }
    child['child_identity_residual'] = child_tasks_total - sum(
        v for k, v in child.items() if k != 'child_tasks_total'
    )
    return {
        'as_of': datetime.now(timezone.utc).isoformat(),
        'child': child,
        'parent_titles': {
            'parent_titles_total':   parent_titles_total,
            'titles_new_or_changed': titles_new_or_changed,
            'titles_written':        titles_written,
            'date_baselines':        date_baselines,
            'date_events_detected':  date_events_detected,
            'date_events_written':   date_events_written,
            'title_capture_failed':  title_capture_failed,
        },
        # Third stream (Ops Phase 2): the ap_tracker mirror landing. Its own
        # identity — candidates = upserted + protected_pending (+ failed);
        # echo/conflict counts are overlays on upserted rows, never terms.
        'mirror': {
            'mirror_candidates_total':     mirror_candidates_total,
            'mirror_upserted':             mirror_upserted,
            'mirror_echo_cleared':         mirror_echo_cleared,
            'mirror_conflicts_ingested':   mirror_conflicts_ingested,
            'mirror_protected_pending':    mirror_protected_pending,
            'mirror_conflict_rows_logged': mirror_conflict_rows_logged,
            'mirror_failed':               mirror_failed,
        },
    }


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
        sys.exit(1)

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
        sys.exit(1)

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
        sys.exit(1)

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
        sys.exit(1)

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
            .select('id, ap_number, owner_id, status, due_date, start_date, priority, ap_pending_update, ap_pending_since, ap_orphaned, no_report_out') \
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
        sys.exit(1)

    # Load existing P&C (pc_projects) rows keyed by ap_number
    try:
        existing_pc_resp = db.table('pc_projects') \
            .select('id, ap_number, owner_id, title, description, status, category, start_date, target_end_date, target_date_moves, ap_pending_update, ap_pending_since, ap_orphaned, no_report_out') \
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
        sys.exit(1)

    # Load existing ap_tracker mirror rows (Ops Phase 2, decision 69ba45bd)
    # keyed by smartsheet_row_id — the loop-prevention planner needs each
    # row's dirty state. A failed load must NOT block the child sync (same
    # discipline as ap_titles): the mirror phase is skipped, mirror_failed
    # marks the run, and the process exits non-zero at the very end —
    # writing the mirror blind could clobber a pending ORiON write's dirty
    # flag, which is worse than skipping a cycle.
    mirror_load_failed = False
    existing_mirror = {}
    try:
        mirror_resp = db.table('ap_tracker') \
            .select('id, smartsheet_row_id, ap_number, orion_dirty, orion_written_value, orion_written_at') \
            .execute()
        existing_mirror = {
            r['smartsheet_row_id']: r
            for r in (mirror_resp.data or [])
            if r.get('smartsheet_row_id') is not None
        }
        log.info(f"Existing ap_tracker mirror rows: {len(existing_mirror)}")
    except Exception as e:
        mirror_load_failed = True
        log.error(f"Failed to load ap_tracker mirror — mirror landing skipped this run: {e}")

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
    to_close_delivery  = []  # list of (id, new_status, ap_number) — row went inactive in Smartsheet
    to_insert_pc       = []
    to_update_pc       = []  # list of (id, fields_to_update, ap_number)
    to_clear_pending_pc = []  # list of (id, ap_number) — Smartsheet caught up, clear flag only
    to_close_pc        = []  # list of (id, new_status, ap_number) — row went inactive in Smartsheet
    escalations        = []  # pending > ESCALATION_DAYS and still diverging

    skipped_no_ap        = 0
    skipped_unmapped     = 0
    skipped_ambiguous    = 0
    skipped_viewer       = 0
    skipped_unchanged    = 0
    skipped_pending      = 0
    # Reconciliation-monitor Phase 1 (decision 2026-08-25): the two
    # previously-uncounted child-loop exits, counted so the identity
    # len(child_tasks) == sum(all dispositions) can close (action 9278f68e).
    skipped_inactive_noop       = 0  # inactive, non-pending, no close queued
    skipped_inactive_wrongtable = 0  # inactive, pending on one table, resolved to the other
    # ap_tracker landing set (Ops Phase 2) — the SAME in-scope rows the
    # module sync handles today, nothing wider (STOP rule: ingest scope is
    # not widened in this brief; the mirror table is merely shaped for the
    # full sheet). Keyed by smartsheet_row_id so the two collection points
    # below can't double-count a row.
    mirror_candidates = {}
    unmapped_leads       = set()
    ambiguous_leads      = set()
    viewer_leads         = set()

    for task in tasks:
        ap_num = task['ap_number']

        # Inactive rows exist ONLY to settle a pending Delivery or P&C flag,
        # or (non-pending) to close an existing row whose Smartsheet status
        # went inactive. New/unknown rows are still dropped exactly as if
        # never fetched (no counters, no logs). Pending rows continue into
        # the main loop so the settle diff keeps sole authority over the
        # flag; each destination branch below re-checks its own table's
        # pending flag before doing anything with an inactive row — this
        # top-level check must not be trusted alone to prevent an inactive
        # insert, since destination is resolved fresh per row and could in
        # principle land on the table that ISN'T the one holding the flag.
        if not task['active']:
            # Mirror landing: an inactive row is in-scope iff a module row
            # already exists for it (the close/pending-settle set) — the
            # same rows that reach the modules today, nothing wider.
            if ap_num and (ap_num in existing_delivery or ap_num in existing_pc):
                mirror_candidates[task['mirror']['smartsheet_row_id']] = task
            delivery_pending = bool(existing_delivery.get(ap_num, {}).get('ap_pending_update'))
            pc_pending = bool(existing_pc.get(ap_num, {}).get('ap_pending_update'))
            if not delivery_pending and not pc_pending:
                # A close queued below already counts the row (closed_delivery /
                # closed_pc); skipped_inactive_noop must cover only the rows that
                # queue nothing here, or closed rows would count twice.
                _closes_before = len(to_close_delivery) + len(to_close_pc)
                # Status-only close (bug e6b35596 + its Delivery sibling):
                # Complete/Cancelled/On Hold set in Smartsheet must reach an
                # existing ORiON row even though inactive rows never
                # otherwise sync. Update-only (never insert), and deliberately
                # independent of lead resolution — a cleared Lead cell must
                # not leave a finished row showing active forever. Only
                # status moves: the row is inactive in the source, so nothing
                # else should change on the way out (in particular, a
                # cleared SQDCG must not null category — see bug b75a59f6).
                d_ex = existing_delivery.get(ap_num)
                d_status = STATUS_MAP.get(task['status_raw'])
                if d_ex and d_status and d_ex.get('status') != d_status:
                    to_close_delivery.append((d_ex['id'], d_status, ap_num))
                    if dry_run:
                        print(f"[DRY RUN] CLOSE    {ap_num:14} delivery — {d_ex.get('status')} -> {d_status} (Smartsheet: {task['status_raw']})")
                pc_ex = existing_pc.get(ap_num)
                pc_close_status = PC_STATUS_MAP.get(task['status_raw'])
                if pc_ex and pc_close_status and pc_ex.get('status') != pc_close_status:
                    to_close_pc.append((pc_ex['id'], pc_close_status, ap_num))
                    if dry_run:
                        print(f"[DRY RUN] CLOSE    {ap_num:14} P&C      — {pc_ex.get('status')} -> {pc_close_status} (Smartsheet: {task['status_raw']})")
                if len(to_close_delivery) + len(to_close_pc) == _closes_before:
                    skipped_inactive_noop += 1
                continue

        if not ap_num or not task['action_text']:
            skipped_no_ap += 1
            continue

        lead_email = resolve_lead_email(task['lead_value'], task['lead_display'], alias_map, name_to_email)
        # For logging when nothing resolved, fall back to whatever raw text the
        # Lead cell actually had, so "TDM TBD" / free text is still visible.
        lead_display_for_log = lead_email or task['lead_display'] or task['lead_value'] or '(blank)'

        destination, owner_id = resolve_owner(lead_email or "", email_to_id, portal_email_to_id, viewer_emails)

        # Mirror landing: an active row is in-scope iff it routes to a
        # module (delivery/pc) — ambiguous/viewer/none skips stay out of
        # the mirror exactly as they stay out of the module tables.
        if task['active'] and destination in ('delivery', 'pc'):
            mirror_candidates[task['mirror']['smartsheet_row_id']] = task

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
        no_report_out = map_no_report_out(task['no_report_out_raw'])

        if destination == 'delivery':
            # Mirror of the P&C guard below: an inactive row let through the
            # top-level filter by the OTHER table's pending flag must never
            # create or modify a Delivery row, even if the Lead now resolves
            # to a PLL.
            if not task['active'] and not existing_delivery.get(ap_num, {}).get('ap_pending_update'):
                skipped_inactive_wrongtable += 1
                continue

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
                # Guard (bug 74ebd314): a blank/cleared Smartsheet date cell must
                # not null a stored Delivery date. Same accident-when-blank ruling
                # as the P&C side (6f43d355) — a blank date reads as accident, not
                # intent, and the failure is silent unrecoverable data loss. Note:
                # `fields` also drives the ap_pending_update caught-up/divergence
                # logic below, so guarding here also (correctly) stops a blanked
                # cell from registering as a diverging change the PLL flagged.
                if due_date is not None and due_date != ex.get('due_date'):
                    fields['due_date'] = due_date
                if start_date is not None and start_date != ex.get('start_date'):
                    fields['start_date'] = start_date
                # No guard here (decision 2026-08-25-sync-no-report-out-flag.md):
                # this is a real computed boolean, never None, so the
                # None-clobber class of guard above doesn't apply — and an
                # unchecked box is a genuine user action that must propagate
                # both ways (true->false included), unlike a cleared date.
                if no_report_out != ex.get('no_report_out'):
                    fields['no_report_out'] = no_report_out
                # priority is intentionally NOT re-forced here (bug aaaa96ea,
                # 2026-08-20) — mirrors the P&C shape below. A PLL may re-tier
                # an AP item deliberately (ORiON-side reason gate covers it);
                # only the initial Tier 2 import assumption is forced, once,
                # on insert. Re-forcing on every sync silently discarded that
                # choice within 30 minutes with no error and no record.

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
                    'no_report_out':       no_report_out,
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
                skipped_inactive_wrongtable += 1
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
                # Guard (bug b75a59f6): never write a computed None over an
                # existing non-null category — SQDCG is sparsely populated in
                # the sheet, and a blank/cleared cell must not silently null
                # a hand-set value. A real (non-null) change still syncs.
                if category is not None and category != ex.get('category'):
                    fields['category'] = category
                # Guard (bug 6f43d355): a blank/cleared Smartsheet cell must not
                # null a stored date. Dates read as accident when blank, unlike
                # category (sparse source) and unlike description (mirrored:
                # a cleared description is intent, ruled 8/12). The target_end_date
                # guard also stops the phantom target_date_moves increment that a
                # None-write would record as a schedule slip that never happened.
                if start_date is not None and start_date != ex.get('start_date'):
                    fields['start_date'] = start_date
                if due_date is not None and due_date != ex.get('target_end_date'):
                    fields['target_end_date'] = due_date
                    # target_date_moves feeds the P&C timeline's drift view —
                    # increment the CURRENT value on every real target_end_date
                    # change, never on insert, and never touch original_target_date
                    # again after insert (that baseline is app-write-only).
                    fields['target_date_moves'] = (ex.get('target_date_moves') or 0) + 1
                # No guard here — same reasoning as the Delivery side above:
                # a real computed boolean, never None, and a toggle in either
                # direction is a genuine Smartsheet edit that must propagate.
                if no_report_out != ex.get('no_report_out'):
                    fields['no_report_out'] = no_report_out
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
                    'no_report_out':         no_report_out,
                })
                if dry_run:
                    print(f"[DRY RUN] INSERT   {ap_num:14} P&C      — owner {owner_id}, status {pc_status}")

    # ── Orphan detection (bug c4494694) ─────────────────────────
    # A row whose ap_number is absent from the FULL child fetch (active AND
    # inactive) was deleted from the tracker — distinct from Complete/
    # Cancelled/On Hold, which remain in the sheet and take the status-only
    # close path above. Detection FLAGS (ap_orphaned + ap_orphaned_since),
    # never deletes and never auto-closes (Jim's ruling, 2026-08-20): the
    # row carries notes, history, and ap_change_log references, and the
    # source being wrong is at least as likely as the row being wrong. A
    # reappearing ap_number (row restored, or a Smartsheet restructure that
    # briefly dropped the Is Child flag) clears the flag automatically.
    # Detection keys on the CHILD row's own AP number only — a deleted
    # child whose parent still exists is an orphan; the parent is not the
    # row this table mirrors.
    sheet_ap_numbers = {t['ap_number'] for t in tasks if t['ap_number']}
    to_orphan_delivery   = []  # (id, ap_number)
    to_unorphan_delivery = []  # (id, ap_number)
    to_orphan_pc         = []
    to_unorphan_pc       = []
    if not sheet_ap_numbers:
        # tasks is non-empty here (checked at fetch), so an empty AP-number
        # set means every child row lost its AP# — sheet damage, not mass
        # deletion. Never mass-flag on that.
        log.error("Orphan detection skipped — child fetch produced zero AP numbers")
    else:
        for ap, r in existing_delivery.items():
            if ap not in sheet_ap_numbers and not r.get('ap_orphaned'):
                to_orphan_delivery.append((r['id'], ap))
            elif ap in sheet_ap_numbers and r.get('ap_orphaned'):
                to_unorphan_delivery.append((r['id'], ap))
        for ap, r in existing_pc.items():
            if ap not in sheet_ap_numbers and not r.get('ap_orphaned'):
                to_orphan_pc.append((r['id'], ap))
            elif ap in sheet_ap_numbers and r.get('ap_orphaned'):
                to_unorphan_pc.append((r['id'], ap))
        if dry_run:
            for item_id, ap in to_orphan_delivery:
                print(f"[DRY RUN] ORPHAN   {ap:14} delivery — not in tracker, would flag ap_orphaned")
            for item_id, ap in to_orphan_pc:
                print(f"[DRY RUN] ORPHAN   {ap:14} P&C      — not in tracker, would flag ap_orphaned")
            for item_id, ap in to_unorphan_delivery:
                print(f"[DRY RUN] UNORPHAN {ap:14} delivery — back in tracker, would clear ap_orphaned")
            for item_id, ap in to_unorphan_pc:
                print(f"[DRY RUN] UNORPHAN {ap:14} P&C      — back in tracker, would clear ap_orphaned")

    # ── Mirror landing plan (Ops Phase 2, decisions 69ba45bd/e217604f) ──
    # Planned pre-gate so dry runs report it; the actual writes run FIRST
    # in the write phase below — the mirror is the landing zone, the module
    # tables are its projections.
    if mirror_load_failed:
        to_upsert_mirror, mirror_conflict_rows = [], []
        mirror_conflicted_row_ids = set()
        mirror_echo_cleared = mirror_conflicts_ingested = mirror_protected = 0
    else:
        (to_upsert_mirror, mirror_conflict_rows, mirror_conflicted_row_ids,
         mirror_echo_cleared, mirror_conflicts_ingested,
         mirror_protected) = plan_mirror_writes(
            existing_mirror, mirror_candidates, dry_run)

    if unmapped_leads:
        log.info(f"Lead emails that don't resolve in users or portal_users(tpm) (skipped): {sorted(unmapped_leads)}")
    if ambiguous_leads:
        log.error(f"Lead emails resolving in BOTH users and portal_users(tpm) (skipped): {sorted(ambiguous_leads)}")
    if viewer_leads:
        log.warning(f"Lead emails resolving to a viewer, not a valid Delivery owner (skipped): {sorted(viewer_leads)}")

    if dry_run:
        delivery_summary = (
            f"Delivery — would insert: {len(to_insert_delivery)}, would update: {len(to_update_delivery)}, "
            f"would close: {len(to_close_delivery)}, would clear pending flag: {len(to_clear_pending)}"
        )
        pc_summary = (
            f"P&C      — would insert: {len(to_insert_pc)}, would update: {len(to_update_pc)}, "
            f"would close: {len(to_close_pc)}, would clear pending flag: {len(to_clear_pending_pc)}"
        )
        skip_summary = (
            f"Skipped  — no AP#/text: {skipped_no_ap}, unmapped lead: {skipped_unmapped}, "
            f"ambiguous lead: {skipped_ambiguous}, owner is viewer: {skipped_viewer}, "
            f"pending Smartsheet update: {skipped_pending}, unchanged: {skipped_unchanged}"
        )
        orphan_summary = (
            f"Orphans  — would flag: {len(to_orphan_delivery)} delivery + {len(to_orphan_pc)} P&C, "
            f"would clear: {len(to_unorphan_delivery)} delivery + {len(to_unorphan_pc)} P&C"
        )
        mirror_summary = (
            f"Mirror   — candidates: {len(mirror_candidates)}, would upsert: {len(to_upsert_mirror)}, "
            f"echo-cleared: {mirror_echo_cleared}, conflicts ingested: {mirror_conflicts_ingested}, "
            f"protected pending: {mirror_protected}, conflict rows to log: {len(mirror_conflict_rows)}"
            + (" [MIRROR LOAD FAILED — landing skipped]" if mirror_load_failed else "")
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
        print(orphan_summary)
        print(mirror_summary)
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
        # Dry-run accounting: predicted dispositions (queue lengths; nothing
        # is written, so written/failed counters are their would-be values).
        accounting = build_sync_accounting(
            child_tasks_total=len(tasks),
            inserted_delivery=len(to_insert_delivery),
            updated_delivery=len(to_update_delivery),
            inserted_pc=len(to_insert_pc),
            updated_pc=len(to_update_pc),
            skipped_unchanged=skipped_unchanged,
            cleared_pending=len(to_clear_pending),
            cleared_pending_pc=len(to_clear_pending_pc),
            closed_delivery=len(to_close_delivery),
            closed_pc=len(to_close_pc),
            skipped_pending=skipped_pending,
            skipped_no_ap=skipped_no_ap,
            skipped_ambiguous=skipped_ambiguous,
            skipped_viewer=skipped_viewer,
            skipped_unmapped=skipped_unmapped,
            skipped_inactive_noop=skipped_inactive_noop,
            skipped_inactive_wrongtable=skipped_inactive_wrongtable,
            failed_inserts_delivery=0,
            failed_inserts_pc=0,
            parent_titles_total=len(parent_titles),
            titles_new_or_changed=len(titles_to_write),
            titles_written=len(titles_to_write) if not title_capture_failed else 0,
            date_baselines=date_baselines,
            date_events_detected=len(date_events),
            date_events_written=len(date_events) if not title_capture_failed else 0,
            title_capture_failed=title_capture_failed,
            mirror_candidates_total=len(mirror_candidates),
            mirror_upserted=len(to_upsert_mirror),
            mirror_echo_cleared=mirror_echo_cleared,
            mirror_conflicts_ingested=mirror_conflicts_ingested,
            mirror_protected_pending=mirror_protected,
            mirror_conflict_rows_logged=len(mirror_conflict_rows),
            mirror_failed=1 if mirror_load_failed else 0,
        )
        print(f"ACCOUNTING {json.dumps(accounting)}")
        print("-" * 72)
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | DRY RUN complete — no writes were made.")
        return

    # ── Mirror write phase — FIRST (Ops Phase 2, decision 69ba45bd): the
    # ap_tracker mirror is the landing zone AP data enters ORiON through;
    # the module writes below are its projections. Batches of 25 with the
    # same per-row fallback discipline as the module inserts. A mirror
    # failure never blocks the module sync (regression bar: today's rows
    # must keep reaching the modules) — it marks the run and exits
    # non-zero at the very end instead.
    mirror_upserted = 0
    mirror_failed_writes = 0
    mirror_conflict_rows_logged = 0
    # Conflict rows FIRST (see plan_mirror_writes docstring): if this insert
    # fails, the conflicted rows' upserts are withheld so their dirty state
    # — the only evidence of the losing edit — survives to the next run.
    if mirror_conflict_rows:
        try:
            db.table('ap_change_log').insert(mirror_conflict_rows).execute()
            mirror_conflict_rows_logged = len(mirror_conflict_rows)
        except Exception as e:
            mirror_failed_writes += 1
            log.error(f"Mirror conflict-log insert failed ({len(mirror_conflict_rows)} rows) — withholding {len(mirror_conflicted_row_ids)} conflicted row upsert(s) so the dirty state survives: {e}")
            to_upsert_mirror = [
                p for p in to_upsert_mirror
                if p.get('smartsheet_row_id') not in mirror_conflicted_row_ids
            ]
    for i in range(0, len(to_upsert_mirror), 25):
        batch = to_upsert_mirror[i:i + 25]
        try:
            db.table('ap_tracker').upsert(batch, on_conflict='smartsheet_row_id').execute()
            mirror_upserted += len(batch)
        except Exception as e:
            log.error(f"Mirror upsert batch failed ({len(batch)} rows) — retrying per row: {e}")
            for item in batch:
                try:
                    db.table('ap_tracker').upsert(item, on_conflict='smartsheet_row_id').execute()
                    mirror_upserted += 1
                except Exception as row_e:
                    mirror_failed_writes += 1
                    log.error(f"Mirror upsert failed for {item.get('ap_number')} (row {item.get('smartsheet_row_id')}): {row_e}")
    if to_upsert_mirror or mirror_conflict_rows:
        log.info(
            f"Mirror: {mirror_upserted} upserted of {len(mirror_candidates)} candidate(s), "
            f"{mirror_echo_cleared} echo-cleared, {mirror_conflicts_ingested} conflict(s) ingested, "
            f"{mirror_protected} protected pending, {mirror_conflict_rows_logged} conflict row(s) logged"
        )

    # Insert new Delivery items in batches of 25. A failed batch falls back
    # to per-row inserts: on 2026-08-24 one row with a junk date killed its
    # 24 batch-mates wholesale (batch INSERT is all-or-nothing at the API),
    # and the swallowed error left the run reporting green. The fallback
    # bounds the blast radius of a bad row to itself and names it in the log.
    inserted_delivery = 0
    failed_inserts_delivery = 0
    inserted_delivery_items = []   # rows that actually landed — WIP check keys on these
    for i in range(0, len(to_insert_delivery), 25):
        batch = to_insert_delivery[i:i + 25]
        try:
            db.table('action_items').insert(batch).execute()
            inserted_delivery += len(batch)
            inserted_delivery_items.extend(batch)
        except Exception as e:
            log.error(f"Delivery insert batch failed ({len(batch)} rows) — retrying per row: {e}")
            for item in batch:
                try:
                    db.table('action_items').insert(item).execute()
                    inserted_delivery += 1
                    inserted_delivery_items.append(item)
                except Exception as row_e:
                    failed_inserts_delivery += 1
                    log.error(f"Delivery insert failed for {item['ap_number']}: {row_e}")

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

    # Close Delivery rows that went inactive in Smartsheet — status only,
    # plus last_updated to match the normal update path's discipline.
    closed_delivery = 0
    for item_id, new_status, ap_num in to_close_delivery:
        try:
            db.table('action_items') \
                .update({'status': new_status, 'last_updated': datetime.now(timezone.utc).isoformat()}) \
                .eq('id', item_id) \
                .execute()
            closed_delivery += 1
            log.info(f"{ap_num}: closed (Delivery) — status -> {new_status}")
        except Exception as e:
            log.error(f"Delivery close failed for {ap_num}: {e}")

    # Insert new P&C items in batches of 25 — same per-row fallback
    # discipline as the Delivery inserts above.
    inserted_pc = 0
    failed_inserts_pc = 0
    for i in range(0, len(to_insert_pc), 25):
        batch = to_insert_pc[i:i + 25]
        try:
            db.table('pc_projects').insert(batch).execute()
            inserted_pc += len(batch)
        except Exception as e:
            log.error(f"P&C insert batch failed ({len(batch)} rows) — retrying per row: {e}")
            for item in batch:
                try:
                    db.table('pc_projects').insert(item).execute()
                    inserted_pc += 1
                except Exception as row_e:
                    failed_inserts_pc += 1
                    log.error(f"P&C insert failed for {item['ap_number']}: {row_e}")

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

    # Close P&C rows that went inactive in Smartsheet — status only, plus
    # updated_at to match the normal P&C update path's discipline.
    closed_pc = 0
    for item_id, new_status, ap_num in to_close_pc:
        try:
            db.table('pc_projects') \
                .update({'status': new_status, 'updated_at': datetime.now(timezone.utc).isoformat()}) \
                .eq('id', item_id) \
                .execute()
            closed_pc += 1
            log.info(f"{ap_num}: closed (P&C) — status -> {new_status}")
        except Exception as e:
            log.error(f"P&C close failed for {ap_num}: {e}")

    # ── Orphan flag writes (bug c4494694) — flag/clear ONLY. Never a
    # status write, never a delete; the row's own trigger will bump
    # last_updated/updated_at (unavoidable at the app layer), but no field
    # this script owns changes besides the two orphan columns. ───────
    orphaned_delivery = 0
    orphaned_pc       = 0
    unorphaned        = 0
    _orphan_now = datetime.now(timezone.utc).isoformat()
    for item_id, ap_num in to_orphan_delivery:
        try:
            db.table('action_items') \
                .update({'ap_orphaned': True, 'ap_orphaned_since': _orphan_now}) \
                .eq('id', item_id) \
                .execute()
            orphaned_delivery += 1
            log.warning(f"{ap_num}: ORPHANED (Delivery) — ap_number no longer in the tracker; flagged, not touched")
        except Exception as e:
            log.error(f"Orphan flag failed for {ap_num} (Delivery): {e}")
    for item_id, ap_num in to_orphan_pc:
        try:
            db.table('pc_projects') \
                .update({'ap_orphaned': True, 'ap_orphaned_since': _orphan_now}) \
                .eq('id', item_id) \
                .execute()
            orphaned_pc += 1
            log.warning(f"{ap_num}: ORPHANED (P&C) — ap_number no longer in the tracker; flagged, not touched")
        except Exception as e:
            log.error(f"Orphan flag failed for {ap_num} (P&C): {e}")
    for table, pairs in (('action_items', to_unorphan_delivery), ('pc_projects', to_unorphan_pc)):
        for item_id, ap_num in pairs:
            try:
                db.table(table) \
                    .update({'ap_orphaned': False, 'ap_orphaned_since': None}) \
                    .eq('id', item_id) \
                    .execute()
                unorphaned += 1
                log.info(f"{ap_num}: back in the tracker — ap_orphaned cleared ({table})")
            except Exception as e:
                log.error(f"Orphan clear failed for {ap_num} ({table}): {e}")

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

    # Check for WIP overages caused by new Delivery inserts (Delivery only).
    # Keys on the rows that actually landed, not a positional slice of the
    # insert list — with a failed batch those two diverge, and on 2026-08-24
    # the slice flagged an owner whose rows were ALL in the failed batch.
    if inserted_delivery_items:
        new_owner_ids = {item['owner_id'] for item in inserted_delivery_items}
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
        f"closed {closed_delivery}, pending cleared {cleared_pending} | "
        f"P&C: inserted {inserted_pc}, updated {updated_pc}, "
        f"closed {closed_pc}, pending cleared {cleared_pending_pc} | "
        f"skipped: {skipped_total}, "
        f"pending Smartsheet updates: {len(pending)}, "
        f"escalated: {len(escalations)}, "
        f"orphans flagged: {orphaned_delivery + orphaned_pc}, orphans cleared: {unorphaned}, "
        f"WIP flags set: {wip_flagged} | "
        f"AP titles: captured {len(parent_titles)}, written {titles_written}, "
        f"end-date events: {len(date_events)} detected, {date_events_written} written | "
        f"mirror: {mirror_upserted}/{len(mirror_candidates)} upserted, "
        f"{mirror_echo_cleared} echo, {mirror_conflicts_ingested} conflict, "
        f"{mirror_protected} protected"
        + (f" [INSERT FAILURES: {failed_inserts_delivery} Delivery, {failed_inserts_pc} P&C]"
           if (failed_inserts_delivery or failed_inserts_pc) else "")
        + (" [TITLE/DATE CAPTURE FAILED]" if title_capture_failed else "")
        + (" [MIRROR FAILED]" if (mirror_load_failed or mirror_failed_writes) else "")
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
                    f'delivery_closed={closed_delivery} '
                    f'delivery_pending_cleared={cleared_pending} escalated={len(escalations)} '
                    f'pc_inserted={inserted_pc} pc_updated={updated_pc} '
                    f'pc_closed={closed_pc} '
                    f'pc_pending_cleared={cleared_pending_pc} skipped={skipped_total} '
                    f'orphans_flagged={orphaned_delivery + orphaned_pc} orphans_cleared={unorphaned} '
                    f'titles_captured={len(parent_titles)} titles_written={titles_written} '
                    f'date_events_detected={len(date_events)} date_events_written={date_events_written} '
                    f'date_baselines={date_baselines} '
                    f'insert_failures={failed_inserts_delivery + failed_inserts_pc} '
                    f'title_capture_failed={title_capture_failed} '
                    f'mirror_upserted={mirror_upserted} mirror_echo={mirror_echo_cleared} '
                    f'mirror_conflicts={mirror_conflicts_ingested} mirror_protected={mirror_protected} '
                    f'mirror_failed={int(mirror_load_failed) + mirror_failed_writes}'
                ),
            }, on_conflict='key').execute()
            log.info("Health heartbeat sent to SAM COS")
        except Exception as _e:
            log.warning(f"Health heartbeat failed (non-critical): {_e}")

        # ── Structured accounting → sibling context_store key ───────
        # Phase 1 of the reconciliation monitor (decision doc 2026-08-25,
        # action item 9278f68e): the sync emits its own books durably so the
        # Phase 2 monitor can audit len(child_tasks) == sum(dispositions)
        # without re-deriving the inclusion rule. A SIBLING key on purpose —
        # the main heartbeat's value is a bare timestamp the health dashboard
        # parses for staleness, and must stay that way.
        try:
            accounting = build_sync_accounting(
                child_tasks_total=len(tasks),
                inserted_delivery=inserted_delivery,
                updated_delivery=updated_delivery,
                inserted_pc=inserted_pc,
                updated_pc=updated_pc,
                skipped_unchanged=skipped_unchanged,
                cleared_pending=cleared_pending,
                cleared_pending_pc=cleared_pending_pc,
                closed_delivery=closed_delivery,
                closed_pc=closed_pc,
                skipped_pending=skipped_pending,
                skipped_no_ap=skipped_no_ap,
                skipped_ambiguous=skipped_ambiguous,
                skipped_viewer=skipped_viewer,
                skipped_unmapped=skipped_unmapped,
                skipped_inactive_noop=skipped_inactive_noop,
                skipped_inactive_wrongtable=skipped_inactive_wrongtable,
                failed_inserts_delivery=failed_inserts_delivery,
                failed_inserts_pc=failed_inserts_pc,
                parent_titles_total=len(parent_titles),
                titles_new_or_changed=len(titles_to_write),
                titles_written=titles_written,
                date_baselines=date_baselines,
                date_events_detected=len(date_events),
                date_events_written=date_events_written,
                title_capture_failed=title_capture_failed,
                mirror_candidates_total=len(mirror_candidates),
                mirror_upserted=mirror_upserted,
                mirror_echo_cleared=mirror_echo_cleared,
                mirror_conflicts_ingested=mirror_conflicts_ingested,
                mirror_protected_pending=mirror_protected,
                mirror_conflict_rows_logged=mirror_conflict_rows_logged,
                mirror_failed=int(mirror_load_failed) + mirror_failed_writes,
            )
            sc.table('context_store').upsert({
                'key':    'health:github:orion_ap_sync:accounting',
                'value':  json.dumps(accounting),
                'domain': 'system',
                'notes':  (
                    'Structured per-run accounting for the AP sync (Phase 1, '
                    'decision 2026-08-25-ap-sync-reconciliation-monitor-design.md). '
                    'child_identity_residual is emitted, not acted on — the Phase 2 '
                    'monitor interprets it.'
                ),
            }, on_conflict='key').execute()
            log.info(f"Accounting emitted to SAM COS (child_identity_residual={accounting['child']['child_identity_residual']})")
        except Exception as _e:
            log.warning(f"Accounting emit failed (non-critical): {_e}")

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

    # ── Insert failures → non-zero exit, same discipline ────────────
    # Same shape as the title-capture block above, for the same reason: the
    # 2026-08-24 live run dropped a whole insert batch, reported green, and
    # sent a fresh heartbeat — the exact exit-0-lie the hardened paths exist
    # to prevent. Runs after both heartbeats so the heartbeat still carries
    # the honest insert_failures count for the health dashboard.
    if failed_inserts_delivery or failed_inserts_pc:
        msg = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"{failed_inserts_delivery + failed_inserts_pc} insert(s) FAILED "
            f"({failed_inserts_delivery} Delivery, {failed_inserts_pc} P&C) — "
            f"exiting non-zero (rest of sync completed; failed rows named in log)"
        )
        log.error(msg)
        print(msg)
        sys.exit(1)

    # ── Mirror failures → non-zero exit, same discipline ────────────
    # The module sync completed (its heartbeat reflects that), but a run
    # that failed to land the mirror must not report green — the mirror is
    # the landing zone the Ops module reads (decision 69ba45bd), and a
    # silently stale mirror is the same exit-0 lie as the 2026-08-24
    # insert-batch incident.
    if mirror_load_failed or mirror_failed_writes:
        msg = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"ap_tracker mirror FAILED "
            f"({'load failed' if mirror_load_failed else f'{mirror_failed_writes} write failure(s)'}) — "
            f"exiting non-zero (module sync completed; see log)"
        )
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
