#!/usr/bin/env python3
"""
backfill_ap_category_people.py
─────────────────────────────────────────────────────────────────
One-time reconciliation for the SQDCGP `P` → `People` map gap
(decision 2026-09-01-sync-sqdcgp-people-map-and-backfill.md, action
item 1dcbfeaa). NOT wired into the scheduled sync — invoke explicitly.

sync_ap.py's SQDCG_MAP lacked a `P` key from whenever the sheet's SQDCG
column grew a sixth option (now `SQDCGP`, options S/Q/D/C/G/P) until the
map fix landed 2026-09-01. Every row categorized `P` on the sheet since
then synced into ORiON with category=NULL. The map fix (sync_ap.py)
stops the gap going forward; this script backfills the rows already
nulled by it.

Scope, exactly:
  - pc_projects rows: category IS NULL, status NOT IN ('complete','cancelled')
  - action_items rows: source='ap_import', category IS NULL, status != 'Done'
    ('Done' is action_items' only terminal status — STATUS_MAP maps both
    Smartsheet Complete and Cancelled to it, so pc_projects' two-value
    exclusion has no literal action_items equivalent; confirmed with Jim
    2026-09-01 rather than guessed)

For each candidate row: read its SQDCGP cell from the live sheet by
ap_number, map through sync_ap.map_category() (the real production
map — imported, not reimplemented, so this can't drift from what the
scheduled sync does), and write category ONLY when the map returns a
non-null value. A genuinely blank sheet cell leaves the row NULL — no
invented default (2026-08-11 ruling). A sheet value that isn't a known
SQDCGP letter logs as unmapped rather than silently skipping, so a
future map gap surfaces loudly instead of as another silent null
(tasks/lessons.md, 2026-09-01 entry).

Every already-categorized row is untouched — the IS NULL filter is in
the read query, not just the write, and each write additionally scopes
its own WHERE on id AND category IS NULL so a row categorized by
another process between the read and the write is not clobbered.

Usage:
    python scripts/backfill_ap_category_people.py --dry-run
    python scripts/backfill_ap_category_people.py
─────────────────────────────────────────────────────────────────
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from supabase import create_client
import sync_ap  # noqa: E402  (path insert must precede this import)

DELIVERY_TERMINAL_STATUS = "Done"
PC_TERMINAL_STATUSES = ("complete", "cancelled")


def fetch_sqdcg_by_ap_number() -> dict:
    """ap_number -> raw SQDCGP cell text, for every row that carries one.
    Reuses sync_ap's own paginated fetch (bug 0644145e pagination fix)
    and column IDs so this can't drift from what the scheduled sync reads."""
    page_size = 500
    page = 1
    all_rows = []
    total_row_count = None

    while True:
        data = sync_ap.ss_get(
            f"/sheets/{sync_ap.SHEET_ID}",
            params={"include": "cells", "pageSize": page_size, "page": page},
        )
        rows = data.get("rows", [])
        all_rows.extend(rows)
        total_row_count = data.get("totalRowCount", total_row_count)
        if len(rows) < page_size:
            break
        if total_row_count is not None and len(all_rows) >= total_row_count:
            break
        page += 1

    if total_row_count is not None and len(all_rows) != total_row_count:
        print(
            f"WARNING: fetched {len(all_rows)} rows but totalRowCount="
            f"{total_row_count} — sheet may have changed mid-fetch"
        )

    by_ap = {}
    for row in rows_cells(all_rows):
        ap_num, sqdcg_raw = row
        if ap_num:
            by_ap[ap_num] = sqdcg_raw
    return by_ap


def rows_cells(rows):
    for row in rows:
        cells = {c["columnId"]: c.get("value") for c in row.get("cells", [])}
        ap_num = str(cells.get(sync_ap.COL_AP_NUM) or "").strip()
        sqdcg_raw = cells.get(sync_ap.COL_SQDCG)
        yield ap_num, sqdcg_raw


def log_mapping_result(ap_number: str, sqdcg_raw, mapped: str | None):
    if sqdcg_raw and mapped is None:
        print(f"  UNMAPPED  {ap_number:16} SQDCGP={sqdcg_raw!r} -> no known letter, left NULL")


def backfill_pc_projects(db, sqdcg_by_ap: dict, dry_run: bool) -> dict:
    resp = (
        db.table("pc_projects")
        .select("id, ap_number, category, status")
        .is_("category", "null")
        .not_.in_("status", list(PC_TERMINAL_STATUSES))
        .execute()
    )
    rows = resp.data or []
    print(f"pc_projects candidates: {len(rows)}")

    written = 0
    left_null = 0
    for row in rows:
        ap_number = row["ap_number"]
        sqdcg_raw = sqdcg_by_ap.get(ap_number)
        mapped = sync_ap.map_category(sqdcg_raw)
        log_mapping_result(ap_number, sqdcg_raw, mapped)
        if mapped is None:
            left_null += 1
            continue
        print(f"  WRITE     {ap_number:16} SQDCGP={sqdcg_raw!r} -> category={mapped!r}")
        if not dry_run:
            db.table("pc_projects") \
                .update({"category": mapped}) \
                .eq("id", row["id"]) \
                .is_("category", "null") \
                .execute()
        written += 1

    return {"candidates": len(rows), "written": written, "left_null": left_null}


def backfill_action_items(db, sqdcg_by_ap: dict, dry_run: bool) -> dict:
    resp = (
        db.table("action_items")
        .select("id, ap_number, category, status")
        .eq("source", "ap_import")
        .is_("category", "null")
        .neq("status", DELIVERY_TERMINAL_STATUS)
        .execute()
    )
    rows = resp.data or []
    print(f"action_items (Delivery, ap_import) candidates: {len(rows)}")

    written = 0
    left_null = 0
    for row in rows:
        ap_number = row["ap_number"]
        sqdcg_raw = sqdcg_by_ap.get(ap_number)
        mapped = sync_ap.map_category(sqdcg_raw)
        log_mapping_result(ap_number, sqdcg_raw, mapped)
        if mapped is None:
            left_null += 1
            continue
        print(f"  WRITE     {ap_number:16} SQDCGP={sqdcg_raw!r} -> category={mapped!r}")
        if not dry_run:
            db.table("action_items") \
                .update({"category": mapped}) \
                .eq("id", row["id"]) \
                .is_("category", "null") \
                .execute()
        written += 1

    return {"candidates": len(rows), "written": written, "left_null": left_null}


def parse_args():
    parser = argparse.ArgumentParser(description="One-time SQDCGP category backfill")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written without writing")
    return parser.parse_args()


def main():
    args = parse_args()
    db = create_client(sync_ap.SUPABASE_URL, sync_ap.ORION_SUPABASE_SERVICE_KEY)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Fetching live SQDCGP cells from Smartsheet...")
    sqdcg_by_ap = fetch_sqdcg_by_ap_number()
    print(f"Fetched SQDCGP for {len(sqdcg_by_ap)} AP-numbered rows.\n")

    pc_result = backfill_pc_projects(db, sqdcg_by_ap, args.dry_run)
    print()
    ai_result = backfill_action_items(db, sqdcg_by_ap, args.dry_run)

    print("\n--- Summary ---")
    print(f"pc_projects:   {pc_result['candidates']} candidates, {pc_result['written']} written, {pc_result['left_null']} left NULL (blank sheet cell)")
    print(f"action_items:  {ai_result['candidates']} candidates, {ai_result['written']} written, {ai_result['left_null']} left NULL (blank sheet cell)")
    if pc_result["candidates"] == 0 and ai_result["candidates"] == 0:
        print("Zero rows to write — nothing was nulled by the gap (or a prior pass already reconciled it).")
    if args.dry_run:
        print("\nDRY RUN — no writes were made. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
