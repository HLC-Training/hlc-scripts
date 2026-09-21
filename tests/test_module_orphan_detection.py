"""
Behavioral proof for sync_ap.plan_orphan_flags — module-row (action_items /
pc_projects) orphan detection keyed on liveness, not AP-number existence
(decision 2026-09-21-pc-renumber-zombies, action item 35988c9e, bug 4dfe07fd).

One fixture per orphan shape, each judged individually (the gate forbids an
aggregate "catches some zombie" proof):
  (a) number fully gone                       -> orphan, reason 'deleted'
  (b) number reassigned to a different action -> orphan, reason 'reassigned'
      (legacy un-stamped row, AND a stamped row whose sheet row id vanished)
  (c) genuinely live row                      -> not flagged
      (visited by the projection; stamped and still in the fetch; legacy
       un-stamped row whose number carries the SAME action -> stamped, live)
plus the guards: true positives stay flagged, a live row that was flagged is
unflagged, an incomplete fetch never adds a flag, a still-pending orphan gets
its flag-clear planned, and the column-missing fallback stamps nothing.

Runs the REAL function (no retyped copy). Row ids and titles are the live
AP-0785 shapes from ORiON on 2026-09-21, so the proof reads against the
incident.

Run:  python tests/test_module_orphan_detection.py     (no pytest dependency)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test')

from sync_ap import plan_orphan_flags  # noqa: E402


def task(ap, text, row_id, is_parent=False, is_child=True):
    return {'ap_number': ap, 'action_text': text, 'is_parent': is_parent, 'is_child': is_child,
            'mirror': {'smartsheet_row_id': row_id}}


def pc_row(rid, ap, title, *, orphaned=False, pending=False, stamp=None, is_parent=False, is_child=True):
    return {'id': rid, 'ap_number': ap, 'title': title, 'ap_orphaned': orphaned,
            'ap_pending_update': pending, 'smartsheet_row_id': stamp,
            'ap_is_parent': is_parent, 'ap_is_child': is_child}


# The live AP-0785 sheet after Jen's renumber (tracker rows as of 2026-09-21).
IMPORT_TITLE = 'Import ICS Level 2 Competency Map Structure in Kahuna'
SHEET = [
    task('AP-0785-2-3', IMPORT_TITLE, 5355638976282500),                       # the action's live number
    task('AP-0785-3-3', 'ICS Level 2 Storyboard & Content Gathering', 7016220100001668),  # reassigned number
    task('AP-0785-2-5', 'Configure Kahuna for ICS Level 2 Competency Map', 7607438789967748),
    task('AP-0845', 'Visual Management Certification Program', 1657173549318020, is_parent=True, is_child=False),
]
TASKS_BY_AP = {}
for t in SHEET:
    TASKS_BY_AP.setdefault(t['ap_number'], []).append(t)
ALL_SHEET_ROW_IDS = {t['mirror']['smartsheet_row_id'] for t in SHEET}


def plan(rows, visited=None, fetch_complete=True, row_id_available=True, tasks_by_ap=None, sheet_ids=None):
    return plan_orphan_flags(rows, visited or {}, tasks_by_ap if tasks_by_ap is not None else TASKS_BY_AP,
                             sheet_ids if sheet_ids is not None else ALL_SHEET_ROW_IDS,
                             fetch_complete, 'title', row_id_available)


def ids(pairs):
    return {p[0] for p in pairs}


# ── (a) number fully gone ───────────────────────────────────────
def test_a_number_fully_gone_flags_as_deleted():
    # AP-0785-3-5 no longer exists under any number in the sheet.
    row = pc_row('e7902c20', 'AP-0785-3-5', 'Configure Kahuna for ICS Level 2 Competency Map')
    out = plan([row])
    assert out['to_orphan'] == [('e7902c20', 'AP-0785-3-5', 'deleted')], out
    assert out['to_unorphan'] == [] and out['to_stamp'] == [] and out['to_clear_pending'] == []


def test_a_true_positive_already_flagged_stays_flagged():
    # The -3-5/-3-6/-4-4 class: already ap_orphaned=true, number still gone -> no change either way.
    rows = [pc_row('e7902c20', 'AP-0785-3-5', 'Configure Kahuna …', orphaned=True),
            pc_row('2968447e', 'AP-0785-3-6', 'Rollout ICS Level 2 Comms Plan', orphaned=True),
            pc_row('06048833', 'AP-0785-4-4', 'Build ICS Level 2 Media', orphaned=True)]
    out = plan(rows)
    assert out['to_orphan'] == [], out
    assert out['to_unorphan'] == [], out   # nothing brings them back


# ── (b) number reassigned to a different action ─────────────────
def test_b_reassigned_number_legacy_row_flags_as_reassigned_and_clears_its_pending_flag():
    # 8e5e4fd1: the zombie behind Jen's 2026-09-21 notice. Its number AP-0785-3-3
    # is present in the sheet — but carried by the Storyboard action.
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True)
    out = plan([zombie])
    assert out['to_orphan'] == [('8e5e4fd1', 'AP-0785-3-3', 'reassigned')], out
    assert out['to_clear_pending'] == [('8e5e4fd1', 'AP-0785-3-3')], out
    assert out['to_stamp'] == [] and out['to_unorphan'] == []


def test_b_reassigned_number_stamped_row_flags_when_its_sheet_row_vanished():
    # Post-migration shape: the row carries the OLD sheet row id (deleted by
    # the renumber) while its number lives on under a different action.
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, stamp=1111111111111111)
    out = plan([zombie])
    assert out['to_orphan'] == [('8e5e4fd1', 'AP-0785-3-3', 'deleted')], out
    assert out['to_clear_pending'] == []   # not pending -> nothing to clear


def test_b_pre_fix_rule_would_have_missed_it():
    # Documents the false negative being fixed: the number IS in the sheet.
    sheet_ap_numbers = set(TASKS_BY_AP)
    assert 'AP-0785-3-3' in sheet_ap_numbers   # ap_number-existence says "live" — wrong


# ── (c) genuinely live rows ─────────────────────────────────────
def test_c_visited_row_is_live_and_gets_stamped():
    # 5b58e7a2 = the live AP-0785-2-3 twin; this run's projection matched it.
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE)
    out = plan([live], visited={'5b58e7a2': 5355638976282500})
    assert out['to_orphan'] == [] and out['to_clear_pending'] == [], out
    assert out['to_stamp'] == [('5b58e7a2', 'AP-0785-2-3', 5355638976282500)], out


def test_c_stamped_row_still_in_fetch_is_live_without_a_visit():
    # Out-of-scope-family shape: not projected this run, but its sheet row exists.
    live = pc_row('5b58e7a2', 'AP-0785-2-3', 'renamed in the sheet since', stamp=5355638976282500)
    out = plan([live])
    assert out == {'to_orphan': [], 'to_unorphan': [], 'to_stamp': [], 'to_clear_pending': []}, out


def test_c_legacy_unvisited_row_with_same_action_under_its_number_is_live_and_backfilled():
    # AP-0845: a legacy leaf-shaped duplicate of a live pure-parent sheet row,
    # never visited (its key differs) — same title => live, stamped with the
    # matching sheet row id, NOT an orphan.
    dup = pc_row('8cf0bcd4', 'AP-0845', 'Visual Management Certification Program', is_parent=False, is_child=False)
    out = plan([dup])
    assert out['to_orphan'] == [], out
    assert out['to_stamp'] == [('8cf0bcd4', 'AP-0845', 1657173549318020)], out


def test_c_title_match_is_whitespace_and_case_insensitive():
    dup = pc_row('x', 'AP-0845', '  visual management   CERTIFICATION program ', is_parent=False, is_child=False)
    out = plan([dup])
    assert out['to_orphan'] == [] and out['to_stamp'] == [('x', 'AP-0845', 1657173549318020)], out


def test_c_live_2_subtree_never_flags_alongside_the_3_subtree_zombies():
    # Per-element proof over the incident's own rows: 2-x live, 3-x zombies.
    rows = [
        pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE),
        pc_row('79dc8297', 'AP-0785-2-5', 'Configure Kahuna for ICS Level 2 Competency Map'),
        pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True),
    ]
    visited = {'5b58e7a2': 5355638976282500, '79dc8297': 7607438789967748}
    out = plan(rows, visited=visited)
    assert ids(out['to_orphan']) == {'8e5e4fd1'}, out
    assert ids(out['to_stamp']) == {'5b58e7a2', '79dc8297'}, out


# ── unflag, gates, fallbacks ────────────────────────────────────
def test_flagged_row_that_is_live_again_is_unflagged():
    restored = pc_row('e7902c20', 'AP-0785-2-5', 'Configure Kahuna for ICS Level 2 Competency Map', orphaned=True)
    out = plan([restored], visited={'e7902c20': 7607438789967748})
    assert out['to_unorphan'] == [('e7902c20', 'AP-0785-2-5')], out
    assert out['to_orphan'] == []


def test_incomplete_fetch_adds_no_flag_and_clears_nothing_but_still_unflags_and_stamps():
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True)
    gone = pc_row('e7902c20', 'AP-0785-3-5', 'Configure Kahuna …')
    restored = pc_row('r', 'AP-0785-2-5', 'Configure Kahuna for ICS Level 2 Competency Map', orphaned=True)
    out = plan([zombie, gone, restored], visited={'r': 7607438789967748}, fetch_complete=False)
    assert out['to_orphan'] == [] and out['to_clear_pending'] == [], out
    assert out['to_unorphan'] == [('r', 'AP-0785-2-5')] and out['to_stamp'] == [('r', 'AP-0785-2-5', 7607438789967748)], out


def test_column_missing_fallback_ignores_stamps_and_plans_none():
    # Pre-migration database: stored stamp must be ignored (it cannot exist),
    # verdicts fall back to visited/legacy rules, and nothing is stamped.
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True)
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE)
    out = plan([zombie, live], visited={'5b58e7a2': 5355638976282500}, row_id_available=False)
    assert out['to_orphan'] == [('8e5e4fd1', 'AP-0785-3-3', 'reassigned')], out
    assert out['to_clear_pending'] == [('8e5e4fd1', 'AP-0785-3-3')], out
    assert out['to_stamp'] == [], out


def test_already_orphaned_row_still_pending_gets_its_clear_planned():
    # A zombie flagged on an earlier run that a user edited again afterwards.
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, orphaned=True, pending=True)
    out = plan([zombie])
    assert out['to_orphan'] == [] and out['to_clear_pending'] == [('8e5e4fd1', 'AP-0785-3-3')], out


def test_non_orphan_pending_row_is_never_touched_by_the_orphan_clear():
    pending_live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE, pending=True)
    out = plan([pending_live], visited={'5b58e7a2': 5355638976282500})
    assert out['to_clear_pending'] == [] and out['to_orphan'] == [], out


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
