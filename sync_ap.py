#!/usr/bin/env python3
"""
sync_ap.py
─────────────────────────────────────────────────────────────────
Smartsheet Action Plan → ORiON sync. Runs on ARGUS on a schedule.

Flow:
  - Smartsheet is the SOURCE OF TRUTH. This script never writes to it.
  - Reads EVERY AP-numbered row from the OFS Training Action Plan Tracker —
    children, mid-nodes, umbrella parents, standalones, all statuses (see
    fetch_ap_rows; widened 2026-08-27, action item 37bc44dd / bug 3e1cdc00 —
    before that only is_child rows were fetched, so childless top-level APs
    never reached ORiON).
  - Each row's Lead cell is resolved to an email (linked contact value, then
    the ap_lead_aliases table, then a name match against users/portal_users —
    see resolve_lead_email), then that email is resolved against BOTH users
    (PLLs/admins) and portal_users (TPMs). PLL/admin leads route to
    action_items (Delivery); TPM leads route to pc_projects (P&C). A lead
    resolving in both, resolving only to a viewer, or resolving in neither,
    is skipped as an OWNER and logged rather than guessed.
  - Membership is FAMILY-SCOPED (2026-08-27 ruling): a family (all rows
    sharing a top-level AP-#### prefix) or a standalone is in-scope iff at
    least one member's Lead routes to a module. Every row of an in-scope
    family mirrors and projects — a member whose own lead is unresolved or
    out-of-scope projects with owner_id NULL plus the raw Smartsheet lead
    display text (ap_lead_display). Pure parents (is_parent AND NOT
    is_child) project as family headers into EVERY module their family
    occupies (split families get the header in both tables). Families with
    zero routed members (Customer/Internal/Ops-led) stay out — that
    population's widening is deferred.
  - ALL statuses project, Complete/Cancelled included — ORiON is becoming
    the system of record and holds terminal history.
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
from collections import defaultdict
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

# Family key: the top-level AP prefix of any ap_number ("AP-0621-3-2" ->
# "AP-0621"). Family-scoped membership (2026-08-27 ruling, 37bc44dd): a
# family — every row sharing a family key — or a standalone is in-scope iff
# at least one member's Lead routes to a module. The per-row Lead rule still
# decides each row's OWNER; the family decides MEMBERSHIP. Matches the
# TypeScript side's TOP_LEVEL_AP in orion-pll lib/ap-grouping.ts.
FAMILY_RE = re.compile(r"^(AP-\d+)")


def family_key(ap_number: str | None) -> str | None:
    m = FAMILY_RE.match((ap_number or "").strip())
    return m.group(1) if m else None

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


def fetch_ap_rows() -> tuple[list[dict], list[dict]]:
    """
    Fetch all rows from the AP sheet and return
    (tasks, parent_titles):

    tasks — EVERY row that carries an AP number (children, mid-nodes,
    umbrella parents, standalones), active AND inactive — regardless of
    who the Lead is or whether they resolve to anyone in ORiON. Widened
    from is_child-only rows 2026-08-27 (action item 37bc44dd, bug
    3e1cdc00): a childless top-level AP used to be invisible to ORiON.
    Each task carries its shape flags (is_child / is_parent) and family
    key (top-level AP-#### prefix). Blank-AP rows are kept only when
    is_child is set — so main()'s skipped_no_ap accounting stays identical
    to the pre-widening behavior — and other blank-AP rows (structural
    summary lines) are skipped here with a count in the log. Owner
    resolution, family scoping, and routing happen in main().

    parent_titles — one dict per TOP-LEVEL parent/summary row (Is Parent
    set, AP# like "AP-0621" with no sub-segments): the AP number, the
    primary "Improvement" text (the AP's real title), the parent-level
    "Current Finish" end date (None when blank/unparseable — the diff in
    main() keeps the stored value in that case), and the Smartsheet row id
    for traceability. Mid-level summary rows (Is Parent on e.g.
    AP-0621-1) are expected and silently skipped — ap_titles keys on
    top-level numbers only. A top-level parent with a blank title is
    logged and skipped, never captured as an empty string.

    Inactive rows (Complete / Cancelled / On Hold) are all captured. For
    IN-SCOPE families they now project fully — all statuses, terminal
    history included (2026-08-27 ruling). For out-of-scope rows the
    pre-widening restrictions still hold in main(): (a) a row whose
    ap_pending_update flag is set can be recognized as caught-up once
    Jennifer applies the change in Smartsheet, and (b) an existing
    non-pending row whose Smartsheet status went inactive gets a
    status-only close write (bug e6b35596) — they never insert and never
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

    tasks         = []
    parent_titles = []
    seen_title_aps = set()
    skipped_structural = 0
    for row in all_rows:
        cells       = {c["columnId"]: c.get("displayValue") or c.get("value") for c in row.get("cells", [])}
        raw         = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        display_raw = {c["columnId"]: c.get("displayValue") for c in row.get("cells", [])}

        is_child  = str(cells.get(COL_IS_CHILD, "0")).strip()
        is_parent = str(cells.get(COL_IS_PARENT, "0")).strip()

        # Title capture from TOP-LEVEL parent rows — read-only side channel,
        # runs before the child filters and never short-circuits them, so
        # the task capture below is unaffected by it.
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

        # EVERY AP-numbered row is a task now (2026-08-27 widening,
        # 37bc44dd): children, mid-nodes (both flags — bug 59cd7d7b),
        # umbrella parents, standalones. Blank-AP rows are kept only when
        # is_child is set (so main()'s skipped_no_ap counting matches the
        # pre-widening accounting identity); other blank-AP rows are
        # structural summary lines, skipped here with one counter.
        is_child_flag  = is_child in ("1", "1.0")
        is_parent_flag = is_parent in ("1", "1.0")
        ap_txt = str(cells.get(COL_AP_NUM) or "").strip()
        if not ap_txt and not is_child_flag:
            skipped_structural += 1
            continue

        status_raw = cells.get(COL_STATUS, "")

        # Lead value/displayValue are kept separate (not merged) — resolve_lead_email()
        # needs both: the raw contact value (an email, when linked) and the typed
        # display text (a name, whether linked or free text), for alias/name lookups.
        lead_value   = (raw.get(COL_LEAD) or "").strip()
        lead_display = (display_raw.get(COL_LEAD) or "").strip()

        tasks.append({
            "active":       status_raw in ACTIVE_STATUSES,
            "ap_number":    cells.get(COL_AP_NUM, ""),
            "is_child":     is_child_flag,
            "is_parent":    is_parent_flag,
            "family":       family_key(ap_txt),
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

    n_active = sum(1 for t in tasks if t["active"])
    n_parent = sum(1 for t in tasks if t["is_parent"] and not t["is_child"])
    n_mid    = sum(1 for t in tasks if t["is_parent"] and t["is_child"])
    n_stand  = sum(1 for t in tasks if not t["is_parent"] and not t["is_child"])
    log.info(
        f"Smartsheet: {len(tasks)} AP rows captured ({n_active} active; "
        f"{n_parent} pure parents, {n_mid} mid-nodes, {n_stand} standalones, "
        f"{len(tasks) - n_parent - n_mid - n_stand} children; "
        f"{skipped_structural} structural blank-AP rows skipped)"
    )
    log.info(f"Smartsheet: {len(parent_titles)} top-level AP titles found on parent rows")
    return tasks, parent_titles


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
    (tasks) capture — that projection is built separately."""
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
        # Pure family parents (ap_is_parent AND NOT ap_is_child) are
        # headers, not work — excluded from capacity math (Task 5 of the
        # 2026-08-27 widening). Mid-nodes (both flags), standalones, and
        # leaves all count.
        resp = db.table('action_items') \
            .select('id, owner_id, start_date, due_date') \
            .in_('owner_id', list(owner_ids)) \
            .eq('priority', 'Tier 2') \
            .in_('status', ['Open', 'In Progress']) \
            .or_('ap_is_parent.eq.false,ap_is_child.eq.true') \
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


# ─── SHARED PROJECTION PATH (Task 3, 2026-08-27 family-scope widening) ──
# One parameterized mirror→module path. A module is: a table, a status
# vocab, field builders, and (Delivery only, expressed outside this file's
# planner via the WIP check) a workload concept. Both modules flow through
# plan_module_projection(); the per-module differences live in the spec
# dicts built in main().

def is_pure_parent(task: dict) -> bool:
    """Umbrella/summary row: Is Parent set, Is Child not. Mid-nodes (both
    flags) are real tasks and are treated as leaves everywhere except the
    UI's indentation."""
    return bool(task['is_parent']) and not bool(task['is_child'])


def module_row_key(ap_number: str, pure_parent: bool) -> tuple:
    """Module rows key on (ap_number, pure_parent) — NOT bare ap_number.
    Three sheet families (AP-036/AP-093/AP-174) have a parent row AND a
    child row sharing one flat AP number; bare-ap keying would make one
    twin invisible to the diff and re-insert it every run. Legacy module
    rows are all leaves (ap_is_parent false), so they key (ap, False) and
    keep matching — including AP-175, the pre-existing flat is_child row
    the backfill must not double-insert."""
    return (ap_number, pure_parent)


def map_module_status(status_map: dict, status_raw, default: str, ap_num: str, label: str) -> str:
    """Map a Smartsheet Overall Status to a module status, tolerating case
    drift ('Not started' exists in the sheet) and blank cells. All statuses
    project now, so an unmappable value must still land somewhere legal —
    default with a warning rather than dropping the row or writing an
    illegal CHECK value."""
    if status_raw:
        s = str(status_raw).strip()
        if s in status_map:
            return status_map[s]
        for k, v in status_map.items():
            if k.lower() == s.lower():
                return v
    log.warning(f"{ap_num}: unmapped Overall Status {status_raw!r} — defaulting to {default!r} ({label})")
    return default


def compute_shared_fields(task: dict) -> dict:
    """Module-independent computed fields for one row's projection."""
    return {
        'due_date':      parse_due_date(task['finish_raw']),
        'start_date':    parse_due_date(task['start_raw']),
        'category':      map_category(task['sqdcg_raw']),
        'no_report_out': map_no_report_out(task['no_report_out_raw']),
        # Raw Smartsheet lead text — the owner fallback for rows whose lead
        # resolves to no portal user (owner_id null). Stored on every row;
        # the UI only surfaces it when owner_id is null.
        'lead_display':  (task['lead_display'] or task['lead_value'] or '').strip() or None,
    }


def _shape_meta_diff(task: dict, ex: dict, shared: dict) -> dict:
    """Projection-metadata diff, shared by both modules: shape flags + lead
    display text. Kept SEPARATE from the content diff on purpose — the
    pending-settle logic judges divergence on content fields only, and the
    first widened run flips ap_is_child on every legacy row; that flip must
    never hold a pending flag open or escalate as a PLL's diverging change."""
    meta = {}
    if bool(task['is_parent']) != bool(ex.get('ap_is_parent')):
        meta['ap_is_parent'] = bool(task['is_parent'])
    if bool(task['is_child']) != bool(ex.get('ap_is_child')):
        meta['ap_is_child'] = bool(task['is_child'])
    if shared['lead_display'] != (ex.get('ap_lead_display') or None):
        meta['ap_lead_display'] = shared['lead_display']
    return meta


def build_delivery_insert(task: dict, owner_id, shared: dict, status: str) -> dict:
    return {
        'id':                str(uuid.uuid4()),
        'owner_id':          owner_id,
        'action_text':       task['action_text'],
        'notes':             build_delivery_notes(task),
        'status':            status,
        'start_date':        shared['start_date'],
        'due_date':          shared['due_date'],
        'category':          shared['category'],
        'priority':          'Tier 2',
        'source':            'ap_import',
        'ap_number':         task['ap_number'],
        'no_report_out':     shared['no_report_out'],
        'ap_pending_update': False,
        'vault_synced':      True,
        'ap_is_parent':      bool(task['is_parent']),
        'ap_is_child':       bool(task['is_child']),
        'ap_lead_display':   shared['lead_display'],
        'created_date':      datetime.now().strftime('%Y-%m-%d'),
        'last_updated':      datetime.now(timezone.utc).isoformat(),
    }


def build_delivery_diff(task: dict, ex: dict, owner_id, shared: dict, status: str) -> tuple[dict, dict]:
    """(content_fields, meta_fields) for a Delivery row. Content rules are
    byte-identical to the pre-widening diff: dates guarded against blank
    clobber (bug 74ebd314), no_report_out unguarded (real boolean), priority
    never re-forced (bug aaaa96ea), action_text/notes/category deliberately
    not synced on update (pre-existing module asymmetry, preserved)."""
    fields = {}
    if owner_id != ex.get('owner_id'):
        fields['owner_id'] = owner_id
    if status != ex['status']:
        fields['status'] = status
    if shared['due_date'] is not None and shared['due_date'] != ex.get('due_date'):
        fields['due_date'] = shared['due_date']
    if shared['start_date'] is not None and shared['start_date'] != ex.get('start_date'):
        fields['start_date'] = shared['start_date']
    if shared['no_report_out'] != ex.get('no_report_out'):
        fields['no_report_out'] = shared['no_report_out']
    return fields, _shape_meta_diff(task, ex, shared)


def build_pc_insert(task: dict, owner_id, shared: dict, status: str) -> dict:
    return {
        'id':                   str(uuid.uuid4()),
        'title':                task['action_text'],
        'description':          task['notes'],
        'owner_id':             owner_id,
        'status':               status,
        'source':               'ap_synced',
        'ap_number':            task['ap_number'],
        'priority':             'Tier 3',
        'category':             shared['category'],
        'start_date':           shared['start_date'],
        'target_end_date':      shared['due_date'],
        'original_target_date': shared['due_date'],
        'no_report_out':        shared['no_report_out'],
        'ap_is_parent':         bool(task['is_parent']),
        'ap_is_child':          bool(task['is_child']),
        'ap_lead_display':      shared['lead_display'],
    }


def build_pc_diff(task: dict, ex: dict, owner_id, shared: dict, status: str) -> tuple[dict, dict]:
    """(content_fields, meta_fields) for a P&C row. Content rules preserved:
    category guarded against None-clobber (bug b75a59f6), dates guarded
    (bug 6f43d355) with the target_date_moves increment on real moves,
    title/description synced (cleared description is intent, ruled 8/12)."""
    fields = {}
    if owner_id != ex.get('owner_id'):
        fields['owner_id'] = owner_id
    if task['action_text'] != ex.get('title'):
        fields['title'] = task['action_text']
    if task['notes'] != ex.get('description'):
        fields['description'] = task['notes']
    if status != ex.get('status'):
        fields['status'] = status
    if shared['category'] is not None and shared['category'] != ex.get('category'):
        fields['category'] = shared['category']
    if shared['start_date'] is not None and shared['start_date'] != ex.get('start_date'):
        fields['start_date'] = shared['start_date']
    if shared['due_date'] is not None and shared['due_date'] != ex.get('target_end_date'):
        fields['target_end_date'] = shared['due_date']
        fields['target_date_moves'] = (ex.get('target_date_moves') or 0) + 1
    if shared['no_report_out'] != ex.get('no_report_out'):
        fields['no_report_out'] = shared['no_report_out']
    return fields, _shape_meta_diff(task, ex, shared)


def plan_module_projection(task: dict, mod: dict, owner_id, shared: dict,
                           dry_run: bool, escalations: list, id_to_name: dict,
                           lead_display_for_log: str, secondary: bool) -> str:
    """Plan one row's projection into one module. Returns the disposition:
    'insert' | 'update' | 'unchanged' | 'clear_pending' | 'skip_pending'.

    Queue entries carry the `secondary` flag (a pure parent projecting into
    the second module of a split family) so the write phase can attribute
    each op to the primary row-disposition stream (the accounting identity
    counts ROWS once) or the split_projection overlay stream.

    Pending-settle semantics are the pre-widening ones, verbatim: content
    diff empty -> clear the flag and touch nothing else this run (metadata
    lands on the next run's normal update); content diff non-empty -> skip,
    protect, escalate past ESCALATION_DAYS."""
    ap_num = task['ap_number']
    ex = mod['existing'].get(module_row_key(ap_num, is_pure_parent(task)))
    status = map_module_status(mod['status_map'], task['status_raw'], mod['default_status'], ap_num, mod['label'])

    if ex is None:
        payload = mod['build_insert'](task, owner_id, shared, status)
        payload['_secondary'] = secondary
        mod['to_insert'].append(payload)
        if dry_run:
            print(f"[DRY RUN] INSERT   {ap_num:14} {mod['plabel']} — owner {owner_id or shared['lead_display'] or '(none)'}, status {status}")
        return 'insert'

    content, meta = mod['build_diff'](task, ex, owner_id, shared, status)

    if ex.get('ap_pending_update'):
        if not content:
            mod['to_clear_pending'].append((ex['id'], ap_num, secondary))
            log.info(f"{ap_num}: Smartsheet caught up ({mod['label']}) — clearing ap_pending_update")
            if dry_run:
                print(f"[DRY RUN] CLEAR    {ap_num:14} {mod['plabel']} — Smartsheet caught up, pending flag cleared")
            return 'clear_pending'
        log.info(f"Skipping {ap_num} — {mod['label']} pending update awaiting Smartsheet change")
        if dry_run:
            print(f"[DRY RUN] SKIP     {ap_num:14} {mod['plabel']} — pending update awaiting Smartsheet change")
        pending_days = days_since(ex.get('ap_pending_since'))
        if pending_days is not None and pending_days > ESCALATION_DAYS:
            divergence = ", ".join(
                f"{k}: {ex.get(k)!r} -> {v!r}" for k, v in content.items()
            )
            escalations.append({
                'module':       mod['esc_module'],
                'ap_number':    ap_num,
                'owner':        id_to_name.get(ex.get('owner_id')) or lead_display_for_log,
                'days_flagged': round(pending_days),
                'divergence':   divergence,
            })
            log.warning(
                f"ESCALATED: {ap_num} ({mod['label']}) pending {round(pending_days)}d "
                f"(> {ESCALATION_DAYS}d) and still diverging — {divergence}"
            )
            if dry_run:
                print(f"[DRY RUN] ESCALATE {ap_num:14} {mod['plabel']} — pending {round(pending_days)}d, {divergence}")
        return 'skip_pending'

    fields = {**content, **meta}
    if fields:
        if 'owner_id' in content:
            log.info(f"{ap_num}: lead reassigned in Smartsheet — owner updated ({mod['label']})")
        fields[mod['ts_field']] = datetime.now(timezone.utc).isoformat()
        mod['to_update'].append((ex['id'], fields, ap_num, secondary))
        if dry_run:
            print(f"[DRY RUN] UPDATE   {ap_num:14} {mod['plabel']} — {', '.join(k for k in fields if k != mod['ts_field'])}")
        return 'update'
    if dry_run:
        print(f"[DRY RUN] SKIP     {ap_num:14} {mod['plabel']} — no changes")
    return 'unchanged'


# ─── MAIN ───────────────────────────────────────────────────────
def build_sync_accounting(*, child_tasks_total, inserted_delivery, updated_delivery,
                          inserted_pc, updated_pc, skipped_unchanged, cleared_pending,
                          cleared_pending_pc, closed_delivery, closed_pc, skipped_pending,
                          skipped_no_ap, skipped_ambiguous, skipped_viewer, skipped_unmapped,
                          skipped_inactive_noop, skipped_inactive_wrongtable,
                          skipped_split_unresolved,
                          failed_inserts_delivery, failed_inserts_pc,
                          split_projection,
                          parent_titles_total, titles_new_or_changed, titles_written,
                          date_baselines, date_events_detected, date_events_written,
                          title_capture_failed,
                          mirror_candidates_total, mirror_upserted, mirror_echo_cleared,
                          mirror_conflicts_ingested, mirror_protected_pending,
                          mirror_conflict_rows_logged, mirror_failed):
    """
    Machine-readable per-run accounting — Phase 1 of the reconciliation
    monitor (decision 2026-08-25-ap-sync-reconciliation-monitor-design.md,
    action item 9278f68e). Separate identities, never folded together:
    the child stream must sum to child_tasks_total (since the 2026-08-27
    widening, "child" is a historical name — the stream covers EVERY
    captured AP row, all shapes, each counted ONCE by its primary-module
    disposition); the parent-titles stream is reported alongside with its
    own numbers. child_identity_residual is emitted, not acted on — the
    Phase 2 monitor interprets it. Keyword-only on purpose: both call
    sites (dry-run prediction, live actuals) are forced to supply every
    field, so the emitted shape cannot drift between them.
    Overlays that must never appear as child terms: `escalations` (an
    overlay on skipped_pending) and `split_projection` (a pure parent's
    projection into the SECOND module of a split family — its own stream,
    because the row already counted once under its primary module).
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
        'skipped_split_unresolved':    skipped_split_unresolved,
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
        # Split-family stream (2026-08-27 widening): a pure parent's
        # projection ops into the SECOND module its family occupies. Not
        # part of the child identity — those rows already counted once.
        'split_projection': split_projection,
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

    # Fetch all AP rows from Smartsheet
    try:
        tasks, parent_titles = fetch_ap_rows()
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
        log.info("No AP rows found.")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | AP sync complete — no AP rows found.")
        return

    # Load existing Delivery (action_items) rows keyed by
    # (ap_number, pure_parent) — see module_row_key for why bare ap_number
    # stopped being a safe key when parents started projecting.
    try:
        existing_resp = db.table('action_items') \
            .select('id, ap_number, owner_id, status, due_date, start_date, priority, ap_pending_update, ap_pending_since, ap_orphaned, no_report_out, ap_is_parent, ap_is_child, ap_lead_display') \
            .eq('source', 'ap_import') \
            .execute()
        existing_delivery = {
            module_row_key(r['ap_number'], bool(r.get('ap_is_parent')) and not bool(r.get('ap_is_child'))): r
            for r in (existing_resp.data or [])
            if r.get('ap_number')
        }
        existing_delivery_aps = {r['ap_number'] for r in (existing_resp.data or []) if r.get('ap_number')}
        log.info(f"Existing Delivery AP items in Supabase: {len(existing_delivery)}")
    except Exception as e:
        log.error(f"Failed to load existing Delivery AP items: {e}")
        sys.exit(1)

    # Load existing P&C (pc_projects) rows — same keying.
    try:
        existing_pc_resp = db.table('pc_projects') \
            .select('id, ap_number, owner_id, title, description, status, category, start_date, target_end_date, target_date_moves, ap_pending_update, ap_pending_since, ap_orphaned, no_report_out, ap_is_parent, ap_is_child, ap_lead_display') \
            .eq('source', 'ap_synced') \
            .execute()
        existing_pc = {
            module_row_key(r['ap_number'], bool(r.get('ap_is_parent')) and not bool(r.get('ap_is_child'))): r
            for r in (existing_pc_resp.data or [])
            if r.get('ap_number')
        }
        existing_pc_aps = {r['ap_number'] for r in (existing_pc_resp.data or []) if r.get('ap_number')}
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
    to_update_delivery = []  # list of (id, fields_to_update, ap_number, secondary)
    to_clear_pending   = []  # list of (id, ap_number, secondary) — Smartsheet caught up, clear flag only
    to_close_delivery  = []  # list of (id, new_status, ap_number) — out-of-scope row went inactive in Smartsheet
    to_insert_pc       = []
    to_update_pc       = []  # list of (id, fields_to_update, ap_number, secondary)
    to_clear_pending_pc = []  # list of (id, ap_number, secondary) — Smartsheet caught up, clear flag only
    to_close_pc        = []  # list of (id, new_status, ap_number) — out-of-scope row went inactive in Smartsheet
    escalations        = []  # pending > ESCALATION_DAYS and still diverging

    skipped_no_ap        = 0
    skipped_unmapped     = 0
    skipped_ambiguous    = 0
    skipped_viewer       = 0
    skipped_unchanged    = 0
    skipped_pending      = 0
    cleared_pending_planned    = 0  # primary clear_pending dispositions (accounting)
    cleared_pending_pc_planned = 0
    # Reconciliation-monitor Phase 1 (decision 2026-08-25): the two
    # previously-uncounted loop exits, counted so the identity
    # len(tasks) == sum(all dispositions) can close (action 9278f68e).
    skipped_inactive_noop       = 0  # out-of-scope inactive, non-pending, no close queued
    skipped_inactive_wrongtable = 0  # preserved counter; the in-scope path no longer produces it
    # Family-scope widening (2026-08-27): an unresolved-lead LEAF inside a
    # family whose members span BOTH modules has no ruled projection target
    # — mirrored but not projected, loudly. Zero such rows exist today.
    skipped_split_unresolved    = 0
    # Primary-row dispositions planned per module (the accounting identity
    # counts each ROW exactly once; a pure parent's projection into the
    # second module of a split family is tracked in the secondary stream).
    planned_primary_insert = {'delivery': 0, 'pc': 0}
    planned_primary_update = {'delivery': 0, 'pc': 0}
    sec_planned = {'insert': 0, 'update': 0, 'unchanged': 0, 'clear_pending': 0, 'skip_pending': 0}
    # ap_tracker landing set: every row of every in-scope family/standalone
    # (2026-08-27 widening — the mirror holds the complete in-scope tree),
    # plus any out-of-scope row that still holds a module row (legacy
    # close/settle set). Keyed by smartsheet_row_id.
    mirror_candidates = {}
    unmapped_leads       = set()
    ambiguous_leads      = set()
    viewer_leads         = set()

    # ── Routing pre-pass + family scoping (Task 2) ──────────────
    # Route every row once, then decide family membership: a family (or
    # standalone) is in-scope iff >=1 member routes to a module. fam_modules
    # holds the set of modules each in-scope family occupies — a pure parent
    # projects into every one of them (split families get the header in
    # both tables, ruling b).
    fam_modules = defaultdict(set)
    for task in tasks:
        task['lead_email'] = resolve_lead_email(task['lead_value'], task['lead_display'], alias_map, name_to_email)
        destination, owner_id = resolve_owner(task['lead_email'] or "", email_to_id, portal_email_to_id, viewer_emails)
        task['destination'] = destination
        task['owner_id'] = owner_id
        if task['family'] and destination in ('delivery', 'pc'):
            fam_modules[task['family']].add(destination)
    in_scope_families = set(fam_modules)
    n_split = sum(1 for mods in fam_modules.values() if len(mods) > 1)
    log.info(
        f"Family scoping: {len(in_scope_families)} in-scope families/standalones "
        f"({n_split} split across both modules); "
        f"{len({t['family'] for t in tasks if t['family']}) - len(in_scope_families)} families with zero routed members stay out"
    )

    module_specs = {
        'delivery': {
            'label': 'delivery', 'plabel': 'delivery', 'esc_module': 'delivery',
            'existing': existing_delivery, 'status_map': STATUS_MAP, 'default_status': 'Open',
            'build_insert': build_delivery_insert, 'build_diff': build_delivery_diff,
            'ts_field': 'last_updated',
            'to_insert': to_insert_delivery, 'to_update': to_update_delivery,
            'to_clear_pending': to_clear_pending,
        },
        'pc': {
            'label': 'P&C', 'plabel': 'P&C     ', 'esc_module': 'pc',
            'existing': existing_pc, 'status_map': PC_STATUS_MAP, 'default_status': 'approved',
            'build_insert': build_pc_insert, 'build_diff': build_pc_diff,
            'ts_field': 'updated_at',
            'to_insert': to_insert_pc, 'to_update': to_update_pc,
            'to_clear_pending': to_clear_pending_pc,
        },
    }

    for task in tasks:
        ap_num = task['ap_number']
        in_scope = task['family'] in in_scope_families

        if in_scope:
            # ── In-scope family: every member row mirrors and projects
            # (Task 2/3). All statuses project — terminal history included.
            mirror_candidates[task['mirror']['smartsheet_row_id']] = task
            if not ap_num or not task['action_text']:
                # Both module tables have NOT NULL text/title; a blank-text
                # row still mirrors but cannot project.
                skipped_no_ap += 1
                continue

            lead_display_for_log = task['lead_email'] or task['lead_display'] or task['lead_value'] or '(blank)'
            if task['destination'] == 'ambiguous':
                ambiguous_leads.add(task['lead_email'])
                log.error(
                    f"AMBIGUOUS LEAD: {ap_num} — {task['lead_email']} resolves in BOTH "
                    f"users and portal_users(tpm). Projecting with owner_id=null + fallback; needs human review."
                )

            pure_parent = is_pure_parent(task)
            if task['destination'] in ('delivery', 'pc'):
                row_modules = sorted(fam_modules[task['family']]) if pure_parent else [task['destination']]
                owner_id = task['owner_id']
                primary_module = task['destination']
            else:
                # Unresolved / viewer / ambiguous lead on an in-scope-family
                # row: owner_id null + display-text fallback (ruling a).
                owner_id = None
                fam_mods = sorted(fam_modules[task['family']])
                if pure_parent or len(fam_mods) == 1:
                    row_modules = fam_mods if pure_parent else fam_mods[:1]
                else:
                    skipped_split_unresolved += 1
                    log.warning(
                        f"{ap_num}: unresolved-lead leaf in split family {task['family']} "
                        f"({fam_mods}) — no ruling covers this shape; mirrored, not projected"
                    )
                    continue
                primary_module = row_modules[0]

            shared = compute_shared_fields(task)
            for m in row_modules:
                secondary = (m != primary_module)
                disposition = plan_module_projection(
                    task, module_specs[m], owner_id, shared, dry_run,
                    escalations, id_to_name, lead_display_for_log, secondary)
                if secondary:
                    sec_planned[disposition] += 1
                elif disposition == 'insert':
                    planned_primary_insert[m] += 1
                elif disposition == 'update':
                    planned_primary_update[m] += 1
                elif disposition == 'unchanged':
                    skipped_unchanged += 1
                elif disposition == 'clear_pending':
                    if m == 'delivery':
                        cleared_pending_planned += 1
                    else:
                        cleared_pending_pc_planned += 1
                elif disposition == 'skip_pending':
                    skipped_pending += 1
            continue

        # ── Out-of-scope row (family has zero routed members, or blank-AP
        # is_child row): the PRE-WIDENING behavior, preserved verbatim —
        # inactive rows only settle/close, active rows only hit the skip
        # counters, nothing inserts.

        # Inactive out-of-scope rows exist ONLY to settle a pending flag or
        # (non-pending) to close an existing row whose Smartsheet status
        # went inactive. New/unknown rows are still dropped exactly as if
        # never fetched (no counters, no logs). Pending rows continue past
        # this block to the skip counters, exactly as before the widening.
        if not task['active']:
            # Mirror landing (legacy set): an out-of-scope inactive row
            # still mirrors iff a module row already exists for it (the
            # close/pending-settle set).
            if ap_num and (ap_num in existing_delivery_aps or ap_num in existing_pc_aps):
                mirror_candidates[task['mirror']['smartsheet_row_id']] = task
            row_k = module_row_key(ap_num, is_pure_parent(task))
            delivery_pending = bool(existing_delivery.get(row_k, {}).get('ap_pending_update'))
            pc_pending = bool(existing_pc.get(row_k, {}).get('ap_pending_update'))
            if not delivery_pending and not pc_pending:
                # A close queued below already counts the row (closed_delivery /
                # closed_pc); skipped_inactive_noop must cover only the rows that
                # queue nothing here, or closed rows would count twice.
                _closes_before = len(to_close_delivery) + len(to_close_pc)
                # Status-only close (bug e6b35596 + its Delivery sibling):
                # Complete/Cancelled/On Hold set in Smartsheet must reach an
                # existing ORiON row even though out-of-scope inactive rows
                # never otherwise sync. Update-only (never insert), and
                # deliberately independent of lead resolution — a cleared
                # Lead cell must not leave a finished row showing active
                # forever. Only status moves (bug b75a59f6 reasoning).
                d_ex = existing_delivery.get(row_k)
                d_status = STATUS_MAP.get(task['status_raw'])
                if d_ex and d_status and d_ex.get('status') != d_status:
                    to_close_delivery.append((d_ex['id'], d_status, ap_num))
                    if dry_run:
                        print(f"[DRY RUN] CLOSE    {ap_num:14} delivery — {d_ex.get('status')} -> {d_status} (Smartsheet: {task['status_raw']})")
                pc_ex = existing_pc.get(row_k)
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

        lead_email = task['lead_email']
        # For logging when nothing resolved, fall back to whatever raw text the
        # Lead cell actually had, so "TDM TBD" / free text is still visible.
        lead_display_for_log = lead_email or task['lead_display'] or task['lead_value'] or '(blank)'

        # A routed destination is impossible here — a delivery/pc route
        # would have put this row's family in scope. Only the skip-class
        # destinations remain, preserved verbatim from the pre-widening
        # behavior.
        destination = task['destination']

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

        skipped_unmapped += 1
        if lead_email:
            unmapped_leads.add(lead_email)
        if dry_run:
            print(f"[DRY RUN] SKIP     {ap_num:14} lead does not resolve in users or portal_users(tpm) ({lead_display_for_log})")
        continue

    # ── Orphan detection (bug c4494694) ─────────────────────────
    # A row whose ap_number is absent from the FULL fetch (active AND
    # inactive, EVERY shape — the fetch now captures parents and
    # standalones too, so their module rows can never be mis-orphaned by
    # the detector; Task 6 of the 2026-08-27 widening) was deleted from
    # the tracker — distinct from Complete/Cancelled/On Hold, which remain
    # in the sheet and take a status/close path. Detection FLAGS
    # (ap_orphaned + ap_orphaned_since), never deletes and never
    # auto-closes (Jim's ruling, 2026-08-20). The sheet set is scope-blind
    # on purpose: a family dropping out of scope must not orphan its
    # existing module rows — the rows still exist in the sheet.
    sheet_ap_numbers = {t['ap_number'] for t in tasks if t['ap_number']}
    to_orphan_delivery   = []  # (id, ap_number)
    to_unorphan_delivery = []  # (id, ap_number)
    to_orphan_pc         = []
    to_unorphan_pc       = []
    if not sheet_ap_numbers:
        # tasks is non-empty here (checked at fetch), so an empty AP-number
        # set means every row lost its AP# — sheet damage, not mass
        # deletion. Never mass-flag on that.
        log.error("Orphan detection skipped — fetch produced zero AP numbers")
    else:
        for r in existing_delivery.values():
            ap = r['ap_number']
            if ap not in sheet_ap_numbers and not r.get('ap_orphaned'):
                to_orphan_delivery.append((r['id'], ap))
            elif ap in sheet_ap_numbers and r.get('ap_orphaned'):
                to_unorphan_delivery.append((r['id'], ap))
        for r in existing_pc.values():
            ap = r['ap_number']
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
        scope_summary = (
            f"Scope    — in-scope families/standalones: {len(in_scope_families)} "
            f"({n_split} split across both modules); secondary parent projections: "
            f"{sum(sec_planned.values())} ({sec_planned})"
        )
        skip_summary = (
            f"Skipped  — no AP#/text: {skipped_no_ap}, unmapped lead: {skipped_unmapped}, "
            f"ambiguous lead: {skipped_ambiguous}, owner is viewer: {skipped_viewer}, "
            f"pending Smartsheet update: {skipped_pending}, unchanged: {skipped_unchanged}, "
            f"split-family unresolved leaf: {skipped_split_unresolved}"
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
        print(scope_summary)
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
            inserted_delivery=planned_primary_insert['delivery'],
            updated_delivery=planned_primary_update['delivery'],
            inserted_pc=planned_primary_insert['pc'],
            updated_pc=planned_primary_update['pc'],
            skipped_unchanged=skipped_unchanged,
            cleared_pending=cleared_pending_planned,
            cleared_pending_pc=cleared_pending_pc_planned,
            closed_delivery=len(to_close_delivery),
            closed_pc=len(to_close_pc),
            skipped_pending=skipped_pending,
            skipped_no_ap=skipped_no_ap,
            skipped_ambiguous=skipped_ambiguous,
            skipped_viewer=skipped_viewer,
            skipped_unmapped=skipped_unmapped,
            skipped_inactive_noop=skipped_inactive_noop,
            skipped_inactive_wrongtable=skipped_inactive_wrongtable,
            skipped_split_unresolved=skipped_split_unresolved,
            failed_inserts_delivery=0,
            failed_inserts_pc=0,
            split_projection={'planned': dict(sec_planned), 'inserted': 0, 'updated': 0,
                              'cleared_pending': 0, 'failed': 0},
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
    # Ops carry the `_secondary` tag (a pure parent landing in the second
    # module of a split family) so counts attribute to the primary child
    # identity vs the split_projection overlay stream.
    inserted_delivery = 0
    failed_inserts_delivery = 0
    inserted_pc = 0
    failed_inserts_pc = 0
    sec_written = {'inserted': 0, 'updated': 0, 'cleared_pending': 0, 'failed': 0}
    inserted_delivery_items = []   # rows that actually landed — WIP check keys on these

    def _count_insert(item, module):
        nonlocal inserted_delivery, inserted_pc
        if item.get('_secondary'):
            sec_written['inserted'] += 1
        elif module == 'delivery':
            inserted_delivery += 1
        else:
            inserted_pc += 1

    def _count_insert_failure(item, module):
        nonlocal failed_inserts_delivery, failed_inserts_pc
        if item.get('_secondary'):
            sec_written['failed'] += 1
        elif module == 'delivery':
            failed_inserts_delivery += 1
        else:
            failed_inserts_pc += 1

    def _strip(payload):
        return {k: v for k, v in payload.items() if k != '_secondary'}

    for i in range(0, len(to_insert_delivery), 25):
        batch = to_insert_delivery[i:i + 25]
        try:
            db.table('action_items').insert([_strip(p) for p in batch]).execute()
            for item in batch:
                _count_insert(item, 'delivery')
                inserted_delivery_items.append(item)
        except Exception as e:
            log.error(f"Delivery insert batch failed ({len(batch)} rows) — retrying per row: {e}")
            for item in batch:
                try:
                    db.table('action_items').insert(_strip(item)).execute()
                    _count_insert(item, 'delivery')
                    inserted_delivery_items.append(item)
                except Exception as row_e:
                    _count_insert_failure(item, 'delivery')
                    log.error(f"Delivery insert failed for {item['ap_number']}: {row_e}")

    # Apply Delivery updates
    updated_delivery = 0
    for item_id, fields, ap_num, secondary in to_update_delivery:
        try:
            db.table('action_items').update(fields).eq('id', item_id).execute()
            if secondary:
                sec_written['updated'] += 1
            else:
                updated_delivery += 1
        except Exception as e:
            log.error(f"Delivery update failed for {ap_num}: {e}")

    # Clear settled pending flags — Smartsheet caught up on these rows.
    # Deliberately does NOT touch last_updated (or anything else): the row's
    # content didn't change, only the flag lifecycle did.
    cleared_pending = 0
    for item_id, ap_num, secondary in to_clear_pending:
        try:
            db.table('action_items') \
                .update({'ap_pending_update': False, 'ap_pending_since': None}) \
                .eq('id', item_id) \
                .execute()
            if secondary:
                sec_written['cleared_pending'] += 1
            else:
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

    # Insert new P&C items in batches of 25 — same per-row fallback and
    # secondary-attribution discipline as the Delivery inserts above.
    for i in range(0, len(to_insert_pc), 25):
        batch = to_insert_pc[i:i + 25]
        try:
            db.table('pc_projects').insert([_strip(p) for p in batch]).execute()
            for item in batch:
                _count_insert(item, 'pc')
        except Exception as e:
            log.error(f"P&C insert batch failed ({len(batch)} rows) — retrying per row: {e}")
            for item in batch:
                try:
                    db.table('pc_projects').insert(_strip(item)).execute()
                    _count_insert(item, 'pc')
                except Exception as row_e:
                    _count_insert_failure(item, 'pc')
                    log.error(f"P&C insert failed for {item['ap_number']}: {row_e}")

    # Apply P&C updates
    updated_pc = 0
    for item_id, fields, ap_num, secondary in to_update_pc:
        try:
            db.table('pc_projects').update(fields).eq('id', item_id).execute()
            if secondary:
                sec_written['updated'] += 1
            else:
                updated_pc += 1
        except Exception as e:
            log.error(f"P&C update failed for {ap_num}: {e}")

    # Clear settled P&C pending flags — same discipline as Delivery: only
    # the flag lifecycle changes, nothing else on the row.
    cleared_pending_pc = 0
    for item_id, ap_num, secondary in to_clear_pending_pc:
        try:
            db.table('pc_projects') \
                .update({'ap_pending_update': False, 'ap_pending_since': None}) \
                .eq('id', item_id) \
                .execute()
            if secondary:
                sec_written['cleared_pending'] += 1
            else:
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
        # Null owners (unresolved-lead fallback rows) have no WIP to check.
        new_owner_ids = {item['owner_id'] for item in inserted_delivery_items if item.get('owner_id')}
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
        f"families in scope: {len(in_scope_families)} ({n_split} split; "
        f"secondary parent ops: {sec_written['inserted']}i/{sec_written['updated']}u, "
        f"{sec_written['failed']} failed) | "
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
                skipped_split_unresolved=skipped_split_unresolved,
                failed_inserts_delivery=failed_inserts_delivery,
                failed_inserts_pc=failed_inserts_pc,
                split_projection={'planned': dict(sec_planned), **sec_written},
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
