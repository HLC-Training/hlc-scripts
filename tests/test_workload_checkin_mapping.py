"""
Behavioral proof for sync_workload_checkins.py — the Q1×Q2 → capacity_read
mapping (decision orion-pll 2026-09-24-workload-checkin-v1, section 4 table),
the America/Chicago session_date, and the row shape written to SAM COS.

Run:  python tests/test_workload_checkin_mapping.py     (no pytest, no DB)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sync_workload_checkins import (  # noqa: E402
    build_rows, map_capacity_read, session_date, SOURCE,
)

# The section-4 table, verbatim, one cell per element. Never edit this to
# match the code — if a cell fails, the code is wrong.
EXPECTED = {
    ("room", "heavier"): "has_headroom",
    ("room", "same"): "has_headroom",
    ("room", "lighter"): "has_headroom",
    ("small_slide", "heavier"): "load_growing",
    ("small_slide", "same"): "has_headroom",
    ("small_slide", "lighter"): "has_headroom",
    ("matters_slip", "heavier"): "near_ceiling",
    ("matters_slip", "same"): "near_ceiling",
    ("matters_slip", "lighter"): "near_ceiling",
    ("already_slipping", "heavier"): "near_ceiling",
    ("already_slipping", "same"): "near_ceiling",
    ("already_slipping", "lighter"): "near_ceiling",
}

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label:<48} got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


for (q1, q2), want in EXPECTED.items():
    check(f"map {q1} x {q2}", map_capacity_read(q1, q2), want)

# 'unclear' is never produced.
check("no combination yields unclear", any(v == "unclear" for v in (map_capacity_read(a, b) for (a, b) in EXPECTED)), False)

# Unknown keys fail loudly rather than mapping to anything.
for bad in (("Nothing, I have room for it", "heavier"), ("room", "Heavier"), ("", "same")):
    try:
        map_capacity_read(*bad)
        check(f"raises on {bad}", "no raise", "ValueError")
    except ValueError:
        check(f"raises on {bad}", "ValueError", "ValueError")

# session_date is the Chicago date, not UTC's: 03:30Z on the 25th is still
# 22:30 CDT on the 24th.
check("session_date CDT rollback", session_date("2026-09-25T03:30:00+00:00"), "2026-09-24")
check("session_date Z suffix", session_date("2026-09-25T03:30:00Z"), "2026-09-24")
check("session_date CST (January)", session_date("2026-01-15T05:59:59+00:00"), "2026-01-14")
check("session_date same day", session_date("2026-09-24T18:00:00+00:00"), "2026-09-24")

# Row shape: only the mapped read, the free text and the date cross; raw
# answers do not.
rows = build_rows(
    [
        {"id": "aaaaaaaa-0000-0000-0000-000000000001", "user_id": "u1", "month": "2026-09-01",
         "q1_slip": "small_slide", "q2_trend": "heavier", "slip_text": "  the exam refresh  ",
         "submitted_at": "2026-09-24T18:00:00+00:00"},
        {"id": "aaaaaaaa-0000-0000-0000-000000000002", "user_id": "u1", "month": "2026-08-01",
         "q1_slip": "room", "q2_trend": "same", "slip_text": None,
         "submitted_at": "2026-08-03T12:00:00+00:00"},
        {"id": "aaaaaaaa-0000-0000-0000-000000000003", "user_id": "ghost", "month": "2026-09-01",
         "q1_slip": "room", "q2_trend": "same", "slip_text": None,
         "submitted_at": "2026-09-03T12:00:00+00:00"},
    ],
    {"u1": "Pablo Schibli"},
)
check("rows: unnamed submitter skipped", len(rows), 2)
check("rows: shape", sorted(rows[0].keys()),
      ["capacity_read", "note", "orion_checkin_id", "pll_name", "session_date", "source"])
check("rows: source", rows[0]["source"], SOURCE)
check("rows: note trimmed", rows[0]["note"], "the exam refresh")
check("rows: empty note -> None", rows[1]["note"], None)
check("rows: mapped read", rows[0]["capacity_read"], "load_growing")
check("rows: pll_name from portal_users", rows[0]["pll_name"], "Pablo Schibli")
check("rows: no raw answers cross", any(k in rows[0] for k in ("q1_slip", "q2_trend")), False)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASS")
