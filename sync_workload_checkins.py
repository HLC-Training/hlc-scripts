"""
sync_workload_checkins.py — ORiON workload check-in answers → SAM COS
pll_capacity_log (source = 'self_report').

Hub-and-spoke: ORiON (orion-pll) never writes to SAM COS. This job is the ONE
writer of self_report rows. Jim's own reads (source meeting/eod) are written
by the Meetings project and the EOD skill and are never touched here.

Decision: orion-pll knowledge/decisions/2026-09-24-workload-checkin-v1.md
(SAM COS action item ed6e818e, grill 5327c471).

Mapping to capacity_read — Q1 sets the level, Q2 only moves the middle band:

    Q1 answer                           Heavier        About the same  Lighter
    room  (Nothing, I have room for it) has_headroom   has_headroom    has_headroom
    small_slide (Something small …)     load_growing   has_headroom    has_headroom
    matters_slip (Something that …)     near_ceiling   near_ceiling    near_ceiling
    already_slipping (Things are …)     near_ceiling   near_ceiling    near_ceiling

'unclear' is never produced by a self-report. Raw answers stay in ORiON; only
the mapped read, the free text (note) and the date go to SAM COS.
session_date = the submission instant in America/Chicago.

Idempotent: pll_capacity_log.orion_checkin_id is UNIQUE (migration
migrations/2026-09-24_pll_capacity_log_self_report.sql); every run upserts
with on_conflict=orion_checkin_id, ignore_duplicates=True, so running twice
adds nothing the second time and never rewrites an existing row.

Runs on GitHub Actions daily (.github/workflows/sync-workload-checkins.yml).
Env: ORION_SUPABASE_SERVICE_KEY, SAMCOS_SERVICE_KEY.
Usage: python sync_workload_checkins.py [--dry-run] [--lookback-days N]
Tests (no DB): python tests/test_workload_checkin_mapping.py
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ORION_SUPABASE_URL = "https://czdkctjbejnwuopigxta.supabase.co"
SAMCOS_SUPABASE_URL = "https://hucrkbomqsxpmokgypxg.supabase.co"
CHICAGO = ZoneInfo("America/Chicago")
SOURCE = "self_report"
DEFAULT_LOOKBACK_DAYS = 120  # every run is a reconcile over this window; upsert makes it safe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_workload_checkins")

Q1_KEYS = ("room", "small_slide", "matters_slip", "already_slipping")
Q2_KEYS = ("heavier", "same", "lighter")


def map_capacity_read(q1: str, q2: str) -> str:
    """The section-4 table, exactly. Raises on an unknown key rather than
    guessing — an unmapped answer must fail loudly, never land as 'unclear'."""
    if q2 not in Q2_KEYS:
        raise ValueError(f"unknown q2 key {q2!r}")
    if q1 == "room":
        return "has_headroom"
    if q1 == "small_slide":
        return "load_growing" if q2 == "heavier" else "has_headroom"
    if q1 in ("matters_slip", "already_slipping"):
        return "near_ceiling"
    raise ValueError(f"unknown q1 key {q1!r}")


def session_date(submitted_at_iso: str) -> str:
    """Submission instant → America/Chicago calendar date (YYYY-MM-DD)."""
    ts = datetime.fromisoformat(submitted_at_iso.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(CHICAGO).date().isoformat()


def build_rows(checkins: list[dict], names_by_user_id: dict[str, str]) -> list[dict]:
    """ORiON workload_checkins rows → pll_capacity_log payloads. A check-in
    whose submitter has no portal_users name is skipped and logged, never
    written under a blank name."""
    rows = []
    for c in checkins:
        name = names_by_user_id.get(c["user_id"])
        if not name:
            log.warning("skip %s: no portal_users name for user %s", c["id"], c["user_id"])
            continue
        note = (c.get("slip_text") or "").strip() or None
        rows.append({
            "orion_checkin_id": c["id"],
            "pll_name": name,
            "capacity_read": map_capacity_read(c["q1_slip"], c["q2_trend"]),
            "note": note,
            "source": SOURCE,
            "session_date": session_date(c["submitted_at"]),
        })
    return rows


def fetch_checkins(orion, since_iso: str) -> tuple[list[dict], dict[str, str]]:
    res = (
        orion.table("workload_checkins")
        .select("id, user_id, month, q1_slip, q2_trend, slip_text, submitted_at")
        .gte("submitted_at", since_iso)
        .order("submitted_at")
        .execute()
    )
    checkins = res.data or []
    user_ids = sorted({c["user_id"] for c in checkins})
    names: dict[str, str] = {}
    if user_ids:
        ures = orion.table("portal_users").select("id, name").in_("id", user_ids).execute()
        names = {u["id"]: (u.get("name") or "").strip() for u in (ures.data or [])}
    return checkins, names


def count_self_reports(sc) -> int:
    res = sc.table("pll_capacity_log").select("id", count="exact").eq("source", SOURCE).execute()
    return res.count or 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render what would be written; no SAM COS write")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = ap.parse_args()

    orion_key = os.environ.get("ORION_SUPABASE_SERVICE_KEY", "")
    samcos_key = os.environ.get("SAMCOS_SERVICE_KEY", "")
    if not orion_key:
        raise SystemExit("ERROR: ORION_SUPABASE_SERVICE_KEY is not set.")
    if not samcos_key and not args.dry_run:
        raise SystemExit("ERROR: SAMCOS_SERVICE_KEY is not set.")

    from supabase import create_client  # imported late so the tests never need it

    orion = create_client(ORION_SUPABASE_URL, orion_key)
    since = (datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).isoformat()
    checkins, names = fetch_checkins(orion, since)
    rows = build_rows(checkins, names)
    log.info("ORiON: %d check-in(s) since %s → %d row(s) to reconcile", len(checkins), since[:10], len(rows))
    for r in rows:
        log.info("  %s | %s | %s | %s | note=%s", r["orion_checkin_id"], r["pll_name"], r["capacity_read"], r["session_date"], "yes" if r["note"] else "no")

    if args.dry_run:
        log.info("DRY RUN — nothing written to SAM COS.")
        return 0
    if not rows:
        log.info("Nothing to write.")
        return 0

    sc = create_client(SAMCOS_SUPABASE_URL, samcos_key)
    before = count_self_reports(sc)
    sc.table("pll_capacity_log").upsert(rows, on_conflict="orion_checkin_id", ignore_duplicates=True).execute()
    after = count_self_reports(sc)
    log.info("SAM COS pll_capacity_log self_report rows: %d before → %d after (+%d new)", before, after, after - before)
    return 0


if __name__ == "__main__":
    sys.exit(main())
