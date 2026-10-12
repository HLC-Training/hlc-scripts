"""
Behavioral proof for the FIELD-LEVEL mirror conflict rule in sync_ap.py's
plan_mirror_writes (decision 2026-10-12-held-field-release-is-field-level,
refining 93113ee9 / e217604f). Fixtures are AP-0619-5 from the 2026-10-12
Mid-Month pilot: Jen held Current Finish 2026-10-30 -> 2026-11-30 at
14:08:53Z; the same save pushed Comments live, which bumped the row's
modifiedAt past orion_written_at while the sheet's Current Finish still read
2026-10-30. Pre-fix the planner read that as "Smartsheet changed after ORiON
write" and discarded the held date.

Run:  python tests/test_mirror_conflict_field_level.py   (no pytest dependency)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test')

from sync_ap import plan_mirror_writes, mirror_origin_values  # noqa: E402

ROW_ID     = 100596926709636
MIRROR_ID  = '76a2ee6f-666e-4452-b7d7-fc382c15ca6c'
WRITTEN_AT = '2026-10-12T14:08:53.283402+00:00'
PUSHED_AT  = '2026-10-12T14:08:55Z'           # comments push bumped the row
LATER      = '2026-10-12T15:54:15Z'           # a later sheet edit
SYNCED_AT  = '2026-10-11T12:30:37+00:00'      # last time the sync WROTE the row

COMMENT = '[2026-10-12 Jennifer Wright] Moved to November. Pilot session is in early Nov.'


def stored(**over):
    base = {
        'id': MIRROR_ID, 'smartsheet_row_id': ROW_ID, 'ap_number': 'AP-0619-5',
        'is_parent': False, 'is_child': True,
        'overall_status': 'Not Started', 'current_start': '2026-10-01',
        'current_finish': '2026-11-30', 'comments': COMMENT,
        'smartsheet_modified_at': '2026-02-24T19:37:51+00:00',
        'orion_dirty': True,
        'orion_written_value': {'comments': COMMENT, 'current_finish': '2026-11-30'},
        'orion_written_at': WRITTEN_AT, 'last_synced_at': SYNCED_AT,
    }
    base.update(over)
    return base


def sheet(**over):
    """The fresh sheet capture (mirror payload shape, no sync-state keys)."""
    base = {
        'smartsheet_row_id': ROW_ID, 'ap_number': 'AP-0619-5',
        'is_parent': False, 'is_child': True,
        'overall_status': 'Not Started', 'current_start': '2026-10-01',
        'current_finish': '2026-10-30', 'comments': COMMENT,
        'smartsheet_modified_at': PUSHED_AT,
    }
    base.update(over)
    return base


def plan(ex, payload, origins):
    return plan_mirror_writes({ROW_ID: ex}, {ROW_ID: {'mirror': payload}}, dry_run=False, origins=origins)


ORIGIN = {ROW_ID: {'current_finish': '2026-10-30'}}   # what mirror_origin_values derives


def test_case_a_held_finish_survives_an_unrelated_push_that_bumped_the_row():
    # Row modifiedAt > orion_written_at (the comments push), but the sheet's
    # Current Finish still equals its origin -> PROTECT, no conflict, no upsert.
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(stored(), sheet(), ORIGIN)
    assert conflicts == [] and ingested == 0 and conflicted == set(), conflicts
    assert protected == 1 and to_upsert == []


def test_case_b_sheet_finish_moved_to_a_third_value_is_a_real_conflict():
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(
        stored(), sheet(current_finish='2026-12-15', smartsheet_modified_at=LATER), ORIGIN)
    assert ingested == 1 and conflicted == {ROW_ID}
    assert [(c['field'], c['old_value'], c['new_value']) for c in conflicts] == [('current_finish', '2026-11-30', '2026-12-15')]
    assert len(to_upsert) == 1 and to_upsert[0]['current_finish'] == '2026-12-15'
    assert to_upsert[0]['orion_dirty'] is False and to_upsert[0]['orion_written_value'] is None


def test_case_c_sheet_finish_applied_to_the_held_value_is_an_echo():
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(
        stored(), sheet(current_finish='2026-11-30', smartsheet_modified_at=LATER), ORIGIN)
    assert echo == 1 and conflicts == [] and ingested == 0
    assert len(to_upsert) == 1 and to_upsert[0]['orion_dirty'] is False


def test_case_d_partial_conflict_keeps_the_untouched_held_key_dirty():
    # Someone else edited Comments in the sheet (moved) while Current Finish
    # is still the origin (untouched): conflict logged for comments ONLY,
    # the sheet's comments ingested, ORiON's finish kept and the row left
    # dirty for exactly that key.
    other = COMMENT + '\n13Oct26-Lead confirmed'
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(
        stored(), sheet(comments=other, smartsheet_modified_at=LATER), ORIGIN)
    assert [c['field'] for c in conflicts] == ['comments'], conflicts
    assert ingested == 1 and conflicted == {ROW_ID}
    p = to_upsert[0]
    assert p['comments'] == other
    assert p['current_finish'] == '2026-11-30'
    assert p['orion_dirty'] is True
    assert p['orion_written_value'] == {'current_finish': '2026-11-30'}
    assert p['orion_written_at'] == WRITTEN_AT


def test_no_origin_on_record_keeps_the_row_time_rule():
    # Legacy: no change-log entry behind the write -> unchanged 93113ee9 behavior.
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(stored(), sheet(), {ROW_ID: {}})
    assert ingested == 1 and [c['field'] for c in conflicts] == ['current_finish']


def test_origins_unavailable_protects_every_differing_dirty_row():
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = plan(stored(), sheet(), None)
    assert protected == 1 and ingested == 0 and to_upsert == [] and conflicts == []


def test_sheet_not_yet_caught_up_still_protects():
    # Sheet modifiedAt older than the write: unchanged PROTECT branch.
    to_upsert, *_rest = plan(stored(), sheet(current_finish='2026-12-15', smartsheet_modified_at='2026-02-24T19:37:51Z'), ORIGIN)
    assert to_upsert == [] and _rest[4] == 1   # protected_pending


def _entry(**over):
    base = {'item_id': 'x', 'module': 'pc', 'ap_number': 'AP-0619-5', 'field': 'target_end_date',
            'old_value': '2026-10-30', 'new_value': '2026-11-30', 'changed_by': 'jen', 'changed_at': WRITTEN_AT}
    base.update(over)
    return base


def test_origin_comes_from_the_earliest_person_entry_since_the_last_sync_write():
    ex = stored()
    entries = [
        _entry(changed_at='2026-09-20T10:00:00+00:00', old_value='2026-09-30', new_value='2026-10-30'),  # previous episode, before SYNCED_AT
        _entry(module='delivery', field='due_date'),                                                      # module rail, delivery shape
        _entry(changed_at='2026-10-12T14:20:00+00:00', old_value='2026-11-30', new_value='2026-12-15'),  # a second edit
        _entry(module='ops', item_id=MIRROR_ID, field='current_finish', changed_by=None,
               old_value='2026-11-30', new_value='2026-10-30'),                                           # sync-authored conflict row
    ]
    assert mirror_origin_values(ex, entries) == {'current_finish': '2026-10-30'}
    # Ops rail (master-only rows): same key, logged against the mirror id.
    ops_only = [_entry(module='ops', item_id=MIRROR_ID, field='current_finish', ap_number=None)]
    assert mirror_origin_values(ex, ops_only) == {'current_finish': '2026-10-30'}
    # Status never derives from a module entry (mapped vocabulary), only from ops.
    ex2 = stored(orion_written_value={'overall_status': 'In Progress'})
    assert mirror_origin_values(ex2, [_entry(field='status', old_value='approved', new_value='in_progress')]) == {}
    assert mirror_origin_values(ex2, [_entry(module='ops', item_id=MIRROR_ID, field='overall_status',
                                             old_value='Not Started', new_value='In Progress')]) == {'overall_status': 'Not Started'}
    # Another AP's entries never leak in.
    assert mirror_origin_values(ex, [_entry(ap_number='AP-0769')]) == {}
    # No written value -> nothing to derive.
    assert mirror_origin_values(stored(orion_written_value=None), entries) == {}


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"{len(tests)} passed")
