#!/usr/bin/env python3
"""
sync_xyleme.py
─────────────────────────────────────────────────────────────────
Xyleme Modernization → ORION sync. Runs on GitHub Actions schedule.

Flow:
  - Reads Training Modernization Tracker and Exams Transfer Tracker
    from Smartsheet (read-only — Smartsheet is SOURCE OF TRUTH).
  - Aggregates at the Course_Integration level (one ORION task per course).
  - Upserts into ORION Supabase action_items on action_text.
  - Status rollup: any active modules → In Progress; all on hold → Deferred;
    all complete → Done; all pending/blank → Open.
  - Smartsheet is read-only. PLLs update progress in Smartsheet directly.
    This script pulls that state into ORION for visibility.
  - No back-channel (no ap_pending_update equivalent).

Exam join (verified against live data, Jul 2026):
  Exams are joined to modernization courses via their PARENT row in the
  Exams Transfer Tracker (the level-0 course grouping header), matched
  by normalized name against Course_Integration values. NOTE: today the
  two trackers use disjoint course taxonomies (115 exam course groups vs
  6 modernization courses), so few or no exams join — the notes field
  simply omits the exam line until the taxonomies align. Unjoined counts
  are logged every run.

Schedule: GitHub Actions — every 30 minutes (same as sync_ap.py).
─────────────────────────────────────────────────────────────────
"""

import os
import re
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
SAMCOS_SERVICE_KEY   = os.environ.get("SAMCOS_SERVICE_KEY", "")

MODERNIZATION_SHEET_ID = "3204043720576900"
EXAMS_SHEET_ID         = "8868469282918276"

LOG_FILE = Path(__file__).parent / "sync_xyleme.log"

if not SUPABASE_SERVICE_KEY:
    raise SystemExit("ERROR: SUPABASE_SERVICE_KEY not set.")
if not SMARTSHEET_TOKEN:
    raise SystemExit("ERROR: SMARTSHEET_API_TOKEN not set.")

# ─── COLUMN INDICES ──────────────────────────────────────────────
# Training Modernization Tracker
MOD_COL_PROJECT    = 0   # Module/task name
MOD_COL_STATUS     = 8   # Status
MOD_COL_TARGET_END = 20  # Target End Date
MOD_COL_COURSE     = 24  # Course_Intergration (sic — typo in sheet)
MOD_COL_PRODUCT    = 25  # Product Line

# Exams Transfer Tracker
EXAM_COL_TITLE  = 4   # Course_Exam_Title
EXAM_COL_STATUS = 14  # Status

# Hierarchy note: the sheets' formula-based Hierarchy/Level columns render
# as "1" (displayValue) vs 1.0 (value) depending on how they're read, so
# row depth is computed from the API's native parentId links instead —
# verified identical to the Hierarchy column across all rows (Jul 2026).

# ─── STATUS SETS ────────────────────────────────────────────────
# Module statuses
MOD_ACTIVE   = {"In Analysis", "In Development", "In Review", "In Progress"}
MOD_HOLD     = {"On Hold"}
MOD_COMPLETE = {"Completed"}
# blank/None/Not Started = pending/backlog

# Exam status buckets
EXAM_LIVE      = {"Published"}
EXAM_IN_REVIEW = {
    "In Progress", "Sent for SME Review", "SME Review Completed",
    "Sent for Internal Review", "Internal Review Completed"
}
EXAM_HOLD      = {"On Hold"}
# Not Received, Not Started, N/A = pending

# ─── COURSE → PLL MAPPING ───────────────────────────────────────
# Course_Integration value → ORION user name (must match users.name exactly)
COURSE_PLL_MAP = {
    "EX2100e":          "Ben Smith",
    "GT - Maintenance": "Sherif Khalifa",
    "GT - Operation":   "Sherif Khalifa",
    "ELT Gas Turbine":  "Sherif Khalifa",
    "ST - Maintenance": "Pablo Schibli",
    "ST - Operation":   "Pablo Schibli",
}

# Fallback: Product Line → PLL (for any new courses not yet in COURSE_PLL_MAP)
PRODUCT_PLL_MAP = {
    "Gas":        "Sherif Khalifa",
    "Steam":      "Pablo Schibli",
    "Excitation": "Ben Smith",
    "Generator":  "Ben Smith",
    "Controls":   "Mohammed Nizami",
    "Aero":       "Greg Walker",
}

# Source tag — distinguishes from ap_import rows
SOURCE = "xyleme_import"

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
    resp = requests.get(
        f"https://api.smartsheet.com/2.0{path}",
        headers={"Authorization": f"Bearer {SMARTSHEET_TOKEN}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_sheet(sheet_id: str) -> tuple[dict, list[dict]]:
    """
    Fetch all rows from a Smartsheet sheet with pagination.
    Returns (col_index_map, rows) where col_index_map maps columnId → column
    index. The REST API identifies cells by columnId only (cells carry no
    columnIndex field), so this map is required to address cells by index.
    """
    page_size = 500
    page      = 1
    all_rows  = []
    col_map   = {}
    while True:
        data = ss_get(f"/sheets/{sheet_id}", params={
            "include":  "cells",
            "pageSize": page_size,
            "page":     page,
        })
        if not col_map:
            col_map = {c["id"]: c["index"] for c in data.get("columns", [])}
        rows = data.get("rows", [])
        all_rows.extend(rows)
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"Sheet {sheet_id}: fetched {len(all_rows)} rows")
    return col_map, all_rows


def cell_value(row: dict, col_index: int, col_map: dict):
    """
    Get a cell's raw value by column index from a Smartsheet row.
    Prefers `value` (ISO dates, raw numbers) over `displayValue`
    (locale/format-dependent strings).
    """
    for cell in row.get("cells", []):
        if col_map.get(cell.get("columnId")) == col_index:
            v = cell.get("value")
            return v if v is not None else cell.get("displayValue")
    return None


def compute_depths(rows: list[dict]) -> dict:
    """
    Row hierarchy depth from parentId links (0 = top level, 1 = child, …).
    Parents always precede children in the API's row order.
    """
    depths = {}
    for row in rows:
        parent = row.get("parentId")
        depths[row["id"]] = depths.get(parent, -1) + 1 if parent else 0
    return depths


def first_token(raw) -> str:
    """First value of a (possibly multi-select) cell — split on newline/comma."""
    return re.split(r"[\n,]", str(raw or ""))[0].strip()


# ─── MODULE AGGREGATION ─────────────────────────────────────────
def aggregate_modules(col_map: dict, rows: list[dict]) -> dict[str, dict]:
    """
    Aggregate module-level rows (depth 1) by Course_Integration.
    Returns dict keyed by course name with counts and earliest target date.
    Includes ALL courses — even those with no active work (backlog visibility).
    """
    depths  = compute_depths(rows)
    courses = {}

    for row in rows:
        if depths[row["id"]] != 1:
            continue

        # Multi-select cells arrive newline-joined — attribute the module to
        # the first listed course so counts never double.
        course = first_token(cell_value(row, MOD_COL_COURSE, col_map))
        if not course:
            continue

        status     = str(cell_value(row, MOD_COL_STATUS, col_map) or "").strip()
        target_end = cell_value(row, MOD_COL_TARGET_END, col_map)
        product    = str(cell_value(row, MOD_COL_PRODUCT, col_map) or "").strip()

        if course not in courses:
            courses[course] = {
                "product_line": product,
                "total":        0,
                "complete":     0,
                "active":       0,
                "on_hold":      0,
                "pending":      0,
                "target_dates": [],
            }

        c = courses[course]
        c["total"] += 1

        if status in MOD_COMPLETE:
            c["complete"] += 1
        elif status in MOD_ACTIVE:
            c["active"] += 1
        elif status in MOD_HOLD:
            c["on_hold"] += 1
        else:
            c["pending"] += 1  # blank, Not Started, etc.

        if target_end:
            c["target_dates"].append(str(target_end))

    return courses


# ─── EXAM AGGREGATION ───────────────────────────────────────────
def aggregate_exams(col_map: dict, rows: list[dict]) -> dict[str, dict]:
    """
    Aggregate exam-level rows (depth 1) by their PARENT course grouping
    header (the depth-0 row's Course_Exam_Title). This replaces the earlier
    fuzzy title-substring join — verified against live data: every exam row
    has a resolvable header parent.
    Returns dict keyed by course-group header with exam status counts.
    """
    depths  = compute_depths(rows)
    headers = {
        row["id"]: str(cell_value(row, EXAM_COL_TITLE, col_map) or "").strip()
        for row in rows if depths[row["id"]] == 0
    }
    exams = {}

    for row in rows:
        if depths[row["id"]] != 1:
            continue

        group  = headers.get(row.get("parentId"), "").strip()
        status = str(cell_value(row, EXAM_COL_STATUS, col_map) or "").strip()
        if not group:
            continue

        if group not in exams:
            exams[group] = {"live": 0, "in_review": 0, "on_hold": 0, "pending": 0}

        e = exams[group]
        if status in EXAM_LIVE:
            e["live"] += 1
        elif status in EXAM_IN_REVIEW:
            e["in_review"] += 1
        elif status in EXAM_HOLD:
            e["on_hold"] += 1
        else:
            e["pending"] += 1

    return exams


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def match_exam_groups(course: str, exams: dict[str, dict]) -> tuple[int, int, set]:
    """
    Join exam course-groups to a modernization course by normalized name:
    exact match, or one name containing the other. Conservative on purpose —
    no false positives; unjoined groups are reported by the caller.
    Returns (live, in_review, matched_group_names).
    """
    c = _norm(course)
    live = review = 0
    matched = set()
    for group, e in exams.items():
        g = _norm(group)
        if g == c or c in g or g in c:
            live   += e["live"]
            review += e["in_review"]
            matched.add(group)
    return live, review, matched


# ─── STATUS ROLLUP ──────────────────────────────────────────────
def rollup_status(c: dict) -> str:
    """Derive ORION status from module counts."""
    if c["complete"] == c["total"] and c["total"] > 0:
        return "Done"
    if c["active"] > 0:
        return "In Progress"
    if c["on_hold"] > 0 and c["active"] == 0:
        return "Deferred"
    return "Open"


# ─── BUILD ORION TASKS ──────────────────────────────────────────
def build_tasks(
    courses: dict[str, dict],
    exams: dict[str, dict],
    name_to_id: dict[str, str],
) -> list[dict]:
    """
    Build one ORION action_item dict per course.
    Includes ALL courses for full backlog visibility.
    """
    tasks = []
    unmapped = set()
    joined_groups = set()

    for course, c in courses.items():
        # Resolve PLL
        pll_name = COURSE_PLL_MAP.get(course)
        if not pll_name:
            # Fallback: try first product line token
            first_pl = first_token(c["product_line"])
            pll_name = PRODUCT_PLL_MAP.get(first_pl)
        if not pll_name:
            unmapped.add(course)
            log.warning(f"No PLL mapping for course '{course}' (Product Line: {c['product_line']}) — skipping")
            continue

        owner_id = name_to_id.get(pll_name)
        if not owner_id:
            log.warning(f"No ORION user ID for '{pll_name}' — skipping {course}")
            continue

        # Status rollup
        status = rollup_status(c)

        # Earliest upcoming target date (raw values are ISO, so sortable)
        upcoming = sorted([d for d in c["target_dates"] if d >= str(datetime.now().date())])
        due_date = upcoming[0] if upcoming else None

        # Exam summary — join via exam course-group headers
        exam_live, exam_review, matched = match_exam_groups(course, exams)
        joined_groups |= matched

        # Build notes summary
        notes_parts = [
            f"Modules: {c['complete']}/{c['total']} complete",
        ]
        if c["active"]:
            notes_parts.append(f"{c['active']} in dev/review")
        if c["on_hold"]:
            notes_parts.append(f"{c['on_hold']} on hold")
        if c["pending"]:
            notes_parts.append(f"{c['pending']} pending")
        if exam_live or exam_review:
            notes_parts.append(f"Exams: {exam_live} live | {exam_review} in review")

        tasks.append({
            "course":     course,
            "owner_id":   owner_id,
            "pll_name":   pll_name,
            "status":     status,
            "due_date":   due_date,
            "notes":      " | ".join(notes_parts),
            "action_text": f"Xyleme Modernization: {course}",
        })

    if unmapped:
        log.info(f"Courses with no PLL mapping (not imported): {sorted(unmapped)}")

    unjoined = len(exams) - len(joined_groups)
    log.info(
        f"Exam join: {len(joined_groups)}/{len(exams)} exam course-groups matched "
        f"a modernization course ({unjoined} unjoined — expected while the two "
        f"trackers use different course taxonomies)"
    )

    return tasks


# ─── MAIN ───────────────────────────────────────────────────────
def main():
    log.info("=" * 56)
    log.info("Xyleme sync starting")

    # Connect to ORION Supabase
    try:
        db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        log.info("Supabase connected")
    except Exception as e:
        log.error(f"Supabase connection failed: {e}")
        return

    # Load user name → ID map
    try:
        users_resp = db.table('users').select('id, name').execute()
        name_to_id = {u['name']: u['id'] for u in users_resp.data}
        log.info(f"Users loaded: {len(name_to_id)}")
    except Exception as e:
        log.error(f"Failed to load users: {e}")
        return

    # Fetch and aggregate Smartsheet data
    try:
        mod_map, mod_rows   = fetch_sheet(MODERNIZATION_SHEET_ID)
        exam_map, exam_rows = fetch_sheet(EXAMS_SHEET_ID)
    except Exception as e:
        log.error(f"Smartsheet fetch failed: {e}")
        return

    courses = aggregate_modules(mod_map, mod_rows)
    exams   = aggregate_exams(exam_map, exam_rows)
    tasks   = build_tasks(courses, exams, name_to_id)

    log.info(f"Courses found: {len(courses)} | Tasks to sync: {len(tasks)}")

    # Load existing xyleme_import rows keyed by action_text
    try:
        existing_resp = db.table('action_items') \
            .select('id, action_text, status, due_date, notes') \
            .eq('source', SOURCE) \
            .execute()
        existing = {r['action_text']: r for r in (existing_resp.data or [])}
        log.info(f"Existing xyleme items in Supabase: {len(existing)}")
    except Exception as e:
        log.error(f"Failed to load existing items: {e}")
        return

    inserted = updated = skipped = 0

    for task in tasks:
        action_text = task["action_text"]

        if action_text in existing:
            ex = existing[action_text]
            fields = {}
            if task["status"]   != ex["status"]:
                fields["status"]   = task["status"]
            if task["due_date"] != ex.get("due_date"):
                fields["due_date"] = task["due_date"]
            if task["notes"]    != ex.get("notes"):
                fields["notes"]    = task["notes"]
            if task["owner_id"]:
                # Always keep owner current in case PLL mapping changes
                fields["owner_id"] = task["owner_id"]

            if fields:
                fields["last_updated"] = datetime.now(timezone.utc).isoformat()
                try:
                    db.table('action_items').update(fields).eq('id', ex['id']).execute()
                    updated += 1
                    log.info(f"Updated: {action_text} → {fields}")
                except Exception as e:
                    log.error(f"Update failed for {action_text}: {e}")
            else:
                skipped += 1
        else:
            new_row = {
                'id':           str(uuid.uuid4()),
                'owner_id':     task["owner_id"],
                'action_text':  action_text,
                'status':       task["status"],
                'due_date':     task["due_date"],
                'notes':        task["notes"],
                'priority':     'Tier 2',
                'source':       SOURCE,
                'category':     'Content Development',
                'vault_synced': True,
                'created_date': datetime.now().strftime('%Y-%m-%d'),
                'last_updated': datetime.now(timezone.utc).isoformat(),
                'escalation_needed': False,
            }
            try:
                db.table('action_items').insert(new_row).execute()
                inserted += 1
                log.info(f"Inserted: {action_text}")
            except Exception as e:
                log.error(f"Insert failed for {action_text}: {e}")

    # Summary
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    summary = (
        f"{now} | Xyleme sync complete — "
        f"inserted: {inserted}, updated: {updated}, skipped: {skipped}"
    )
    log.info(summary)
    print(summary)

    # ── Health heartbeat → SAM COS context_store ─────────────────
    try:
        if SAMCOS_SERVICE_KEY:
            from supabase import create_client as _cc
            sc = _cc("https://hucrkbomqsxpmokgypxg.supabase.co", SAMCOS_SERVICE_KEY)
            sc.table('context_store').upsert({
                'key':    'health:github:xyleme_sync',
                'value':  datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'domain': 'system',
                'notes':  f'inserted={inserted} updated={updated} skipped={skipped}',
            }, on_conflict='key').execute()
            log.info("Health heartbeat sent to SAM COS")
    except Exception as _e:
        log.warning(f"Health heartbeat failed (non-critical): {_e}")


if __name__ == '__main__':
    main()
