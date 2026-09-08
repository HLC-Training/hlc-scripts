"""
Behavioral proof for ap_pending.py — the reconciliation rule shared by
sync_ap.py and send_ap_pending_digest.py (decision
2026-09-08-ap-pending-clear-and-direction.md, bug 424c1637).

Run:  python tests/test_ap_pending.py     (no pytest dependency)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ap_pending import (  # noqa: E402
    classify_field, classify_pending, latest_episode_entries, row_is_reconciled,
    is_fixture, pick_tracker_row, project_tracker_field, NOT_COMPARABLE,
    in_episode, EPISODE_SKEW_SECONDS,
)

EDIT_AT = "2026-09-01T14:37:28.512976+00:00"
BEFORE  = "2026-08-30T10:00:00+00:00"
AFTER   = "2026-09-03T15:40:44+00:00"


def tracker(**over):
    base = {
        'ap_number': 'AP-0001-1', 'is_parent': False, 'is_child': True,
        'improvement': 'Title in sheet', 'description': 'Desc in sheet',
        'overall_status': 'Not Started', 'sqdcgp': 'Q',
        'start_date_only': '2027-01-01', 'current_finish': '2027-01-30',
        'smartsheet_modified_at': BEFORE, 'last_synced_at': AFTER,
    }
    base.update(over)
    return base


def entry(field, new_value, changed_at=EDIT_AT, old_value=None):
    return {'field': field, 'old_value': old_value, 'new_value': new_value, 'changed_at': changed_at}


def test_projection_uses_the_syncs_columns():
    t = tracker()
    assert project_tracker_field('pc', 'start_date', t) == '2027-01-01'      # start_date_only, NOT current_start
    assert project_tracker_field('pc', 'target_end_date', t) == '2027-01-30'  # current_finish
    assert project_tracker_field('delivery', 'due_date', t) == '2027-01-30'
    assert project_tracker_field('delivery', 'status', t) == 'Open'
    assert project_tracker_field('pc', 'status', t) == 'approved'
    assert project_tracker_field('pc', 'category', t) == 'Quality'
    assert project_tracker_field('pc', 'status', tracker(overall_status='not started')) == 'approved'
    assert project_tracker_field('pc', 'owner_id', t) is NOT_COMPARABLE
    assert project_tracker_field('delivery', 'priority', t) is NOT_COMPARABLE


def test_matched_when_tracker_holds_the_orion_value():
    # Defect 1 (AP-0293-3): tracker description == ORiON new_value -> clear.
    assert classify_field('pc', 'description', 'Desc in sheet', 'Desc in sheet', BEFORE, EDIT_AT) == 'matched'
    # Whitespace-only differences (modal newlines vs a sheet cell) are matches.
    assert classify_field('pc', 'description', 'Line one.\nLine two.', 'Line one. Line two.', BEFORE, EDIT_AT) == 'matched'


def test_pending_when_tracker_differs_and_is_not_newer():
    assert classify_field('pc', 'description', 'ORiON text', 'Desc in sheet', BEFORE, EDIT_AT) == 'pending'
    # Same-second edit: tracker is not strictly newer -> still pending.
    assert classify_field('pc', 'start_date', '2027-02-01', '2027-01-01', EDIT_AT, EDIT_AT) == 'pending'
    # Unknown tracker timestamp: cannot claim the tracker is newer.
    assert classify_field('pc', 'start_date', '2027-02-01', '2027-01-01', None, EDIT_AT) == 'pending'


def test_tracker_newer_supersedes():
    # Defect 2 (AP-0547-1-2): tracker dates differ but the row was edited after the ORiON edit.
    assert classify_field('pc', 'start_date', '2027-01-01', '2027-02-01', AFTER, EDIT_AT) == 'tracker_newer'
    assert classify_field('delivery', 'status', 'Deferred', 'Open', AFTER, EDIT_AT) == 'tracker_newer'


def test_blank_tracker_cell_rules():
    # ac29261e (wont_fix): a blanked date releases the hold as caught up — even when not newer.
    assert classify_field('delivery', 'due_date', '2026-09-30', None, BEFORE, EDIT_AT) == 'matched'
    assert classify_field('pc', 'category', 'Quality', None, BEFORE, EDIT_AT) == 'matched'
    # A cleared P&C description is intent (8/12 ruling): it is a real divergence.
    assert classify_field('pc', 'description', 'ORiON text', None, BEFORE, EDIT_AT) == 'pending'
    assert classify_field('pc', 'description', 'ORiON text', None, AFTER, EDIT_AT) == 'tracker_newer'


def test_row_settles_only_when_every_episode_field_is_settled():
    t = tracker(description='ORiON text')
    episode = latest_episode_entries('pc', [
        entry('description', 'ORiON text'),
        entry('start_date', '2027-02-01'),
    ], EDIT_AT)
    states = classify_pending('pc', t, episode, lambda _f, e: e['new_value'])
    assert states == {'description': 'matched', 'start_date': 'pending'}
    assert not row_is_reconciled(states)

    t2 = tracker(description='ORiON text', smartsheet_modified_at=AFTER)
    states2 = classify_pending('pc', t2, episode, lambda _f, e: e['new_value'])
    assert states2 == {'description': 'matched', 'start_date': 'tracker_newer'}
    assert row_is_reconciled(states2)

    # An empty episode never settles by this rule (legacy whole-row rule owns it).
    assert not row_is_reconciled({})


def test_no_tracker_row_means_pending():
    episode = latest_episode_entries('pc', [entry('description', 'x')], EDIT_AT)
    assert classify_pending('pc', None, episode, lambda _f, e: e['new_value']) == {'description': 'pending'}


def test_episode_filters_to_mirrored_fields_and_since():
    entries = [
        entry('priority', 'Tier 1'),                                   # never mirrored — ignored
        entry('description', 'old', changed_at='2026-08-01T00:00:00+00:00'),  # previous episode
        entry('description', 'first', changed_at='2026-09-01T14:00:00+00:00'),
        entry('description', 'latest', changed_at='2026-09-01T15:00:00+00:00'),
    ]
    ep = latest_episode_entries('pc', entries, EDIT_AT)
    assert set(ep) == {'description'}
    assert ep['description']['new_value'] == 'latest'
    # Delivery mirrors only status/due_date/start_date.
    assert latest_episode_entries('delivery', [entry('title', 'x'), entry('status', 'Done')], None).keys() == {'status'}


def test_episode_tolerates_the_two_insert_skew():
    # Live 2026-09-08: the app stamped since from its SECOND insert, ~80 ms after the first.
    since = "2026-09-01T14:37:28.512976+00:00"
    first_insert = entry('target_end_date', '2027-01-30', changed_at="2026-09-01T14:37:28.430051+00:00")
    assert in_episode(first_insert, since)
    assert 'target_end_date' in latest_episode_entries('pc', [first_insert], since)
    # But a genuinely older entry (previous episode) stays out.
    old = entry('target_end_date', '2026-07-31', changed_at="2026-09-01T14:30:00+00:00")
    assert not in_episode(old, since)
    assert EPISODE_SKEW_SECONDS < 60


def test_fixture_rule():
    assert is_fixture('AP-9902', 'TEST FIXTURE — do not action — AP-9902')
    assert is_fixture('AP-9901-1', 'Closeout Probe — synthetic AP child')      # reserved range alone
    assert is_fixture('AP-0123', '[TEST FIXTURE — safe to delete, ap-parity-c] AP-synced project')
    assert is_fixture('AP-0123', 'Real title', smartsheet_row_id=999999990901)
    assert not is_fixture('AP-0993-1', 'Real title')                          # AP-09xx is real
    assert not is_fixture('AP-990', 'Real title')                             # three digits, not the range
    assert not is_fixture('AP-0293-3', 'Finalize Electrician Map in Systems', smartsheet_row_id=4660350745706372)


def test_pick_tracker_row_prefers_shape_then_freshness():
    parent = tracker(is_parent=True, is_child=False, last_synced_at='2026-09-08T12:30:00+00:00')
    child_stale = tracker(last_synced_at='2026-09-01T19:46:00+00:00', start_date_only='2025-09-01')
    child_live  = tracker(last_synced_at='2026-09-08T12:30:00+00:00', start_date_only='2026-11-01')
    assert pick_tracker_row([parent, child_stale, child_live], pure_parent=False) is child_live   # AP-214-8-1 case
    assert pick_tracker_row([parent, child_stale, child_live], pure_parent=True) is parent        # AP-036 twins
    assert pick_tracker_row([], pure_parent=False) is None


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"{len(tests)} passed")
