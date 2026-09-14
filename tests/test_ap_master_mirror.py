"""
Behavioral proof for the Phase 4 master-AP changes to sync_ap.py
(decision doc orion-pll/knowledge/decisions/2026-09-14-master-ap-table-phase4.md):

  1. build_mirror_capture carries Smartsheet's permanent parentId as
     smartsheet_parent_row_id (the nesting key; NULL on top-level rows).
  2. plan_mirror_writes skips a stored row whose sheet facts are unchanged
     (no write — the idempotency gate; bug adee6b56's every-row-rewrite
     class), writes a changed row, writes a new row, and treats '...Z' vs
     '...+00:00' timestamp spellings as the same instant.
  3. A dirty row still takes the echo/conflict/protect path — the
     unchanged-skip never bypasses loop prevention.

Runs the REAL functions, not retyped copies, so the proof can't drift from
what ships.

Run:  python tests/test_ap_master_mirror.py     (no pytest dependency)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test')

from sync_ap import (  # noqa: E402
    build_mirror_capture, plan_mirror_writes, mirror_row_changed,
    COL_AP_NUM, COL_IMPROVEMENT, COL_IS_PARENT, COL_IS_CHILD, COL_STATUS,
)


def _row(row_id, parent_id, ap, is_parent, is_child, modified='2026-09-14T12:00:00Z'):
    cells = [
        {'columnId': COL_AP_NUM, 'value': ap, 'displayValue': ap},
        {'columnId': COL_IMPROVEMENT, 'value': f'Improve {ap}', 'displayValue': f'Improve {ap}'},
        {'columnId': COL_IS_PARENT, 'value': is_parent, 'displayValue': '1' if is_parent else '0'},
        {'columnId': COL_IS_CHILD, 'value': is_child, 'displayValue': '1' if is_child else '0'},
        {'columnId': COL_STATUS, 'value': 'In Progress', 'displayValue': 'In Progress'},
    ]
    row = {'id': row_id, 'rowNumber': 1, 'createdAt': '2026-01-01T00:00:00Z',
           'modifiedAt': modified, 'cells': cells}
    if parent_id is not None:
        row['parentId'] = parent_id
    return row


def _capture(row):
    cells = {c['columnId']: c.get('displayValue') or c.get('value') for c in row['cells']}
    raw = {c['columnId']: c.get('value') for c in row['cells']}
    disp = {c['columnId']: c.get('displayValue') for c in row['cells']}
    return build_mirror_capture(row, cells, raw, disp)


def test_capture_carries_smartsheet_parent_row_id():
    # AP-174 twins: parent row 5882484337905540 (top level), child row
    # 7335015124012932 whose API parentId is the parent's permanent id —
    # the live shape verified 2026-09-14 (Gate 1).
    parent = _capture(_row(5882484337905540, None, 'AP-174', True, False))
    child = _capture(_row(7335015124012932, 5882484337905540, 'AP-174', False, True))
    assert parent['smartsheet_parent_row_id'] is None
    assert child['smartsheet_parent_row_id'] == 5882484337905540
    assert parent['ap_number'] == child['ap_number'] == 'AP-174'
    assert parent['smartsheet_row_id'] != child['smartsheet_row_id']


def _stored_from(payload, **overrides):
    """What Postgres hands back for a row the sync wrote: same facts, sync
    state cleared, timestamptz re-spelled the PostgREST way."""
    stored = dict(payload)
    stored['id'] = 'uuid-' + str(payload['smartsheet_row_id'])
    stored['orion_dirty'] = False
    stored['orion_written_value'] = None
    stored['orion_written_at'] = None
    stored['last_synced_at'] = '2026-09-14T10:00:00+00:00'
    stored['orion_at_risk'] = False
    for k in ('smartsheet_created_at', 'smartsheet_modified_at'):
        if stored.get(k) and stored[k].endswith('Z'):
            stored[k] = stored[k][:-1] + '+00:00'
    stored.update(overrides)
    return stored


def test_unchanged_row_is_not_written_and_changed_or_new_rows_are():
    unchanged_row = _row(111, None, 'AP-0700', False, False)
    changed_row = _row(222, None, 'AP-0701', False, False)
    new_row = _row(333, None, 'AP-0702', False, False)
    cands = {r['id']: {'mirror': _capture(r)} for r in (unchanged_row, changed_row, new_row)}

    existing = {
        111: _stored_from(cands[111]['mirror']),
        # Stored copy holds yesterday's title; the sheet moved on.
        222: _stored_from(cands[222]['mirror'], improvement='old title'),
    }

    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = \
        plan_mirror_writes(existing, cands, dry_run=False)

    assert unchanged == 1, unchanged
    assert sorted(p['smartsheet_row_id'] for p in to_upsert) == [222, 333]
    assert echo == ingested == protected == 0 and not conflicts and not conflicted
    # Identity the accounting stream asserts on: candidates = upserted + unchanged + protected
    assert len(cands) == len(to_upsert) + unchanged + protected


def test_timestamp_spelling_alone_is_not_a_change():
    payload = _capture(_row(444, None, 'AP-0703', False, False, modified='2026-09-14T12:00:00Z'))
    stored = _stored_from(payload)
    assert stored['smartsheet_modified_at'] == '2026-09-14T12:00:00+00:00'
    assert mirror_row_changed(stored, payload) is False
    # ...but a genuinely later sheet edit is.
    later = dict(payload, smartsheet_modified_at='2026-09-14T12:00:01Z')
    assert mirror_row_changed(stored, later) is True


def test_second_run_after_first_run_writes_nothing():
    """The literal idempotency gate: run 1 lands N rows; run 2, same sheet,
    plans 0 upserts and N unchanged."""
    rows = [_row(i, None, f'AP-08{i:02d}', False, False) for i in range(1, 6)]
    cands = {r['id']: {'mirror': _capture(r)} for r in rows}

    run1 = plan_mirror_writes({}, cands, dry_run=False)
    assert len(run1[0]) == 5 and run1[6] == 0

    existing_after_run1 = {p['smartsheet_row_id']: _stored_from(p) for p in run1[0]}
    run2 = plan_mirror_writes(existing_after_run1, cands, dry_run=False)
    assert len(run2[0]) == 0, [p['ap_number'] for p in run2[0]]
    assert run2[6] == 5


def test_dirty_row_still_takes_the_loop_prevention_path():
    row = _row(555, None, 'AP-0704', False, False, modified='2026-09-14T12:00:00Z')
    payload = _capture(row)
    # ORiON wrote overall_status='Complete' at 11:00; the sheet still says
    # 'In Progress' and was last modified at 12:00 (> written_at) -> a REAL
    # post-write change: ingest + conflict row. The unchanged-skip must not
    # swallow this even though every non-state fact matches the stored row.
    stored = _stored_from(payload, orion_dirty=True,
                          orion_written_value={'overall_status': 'Complete'},
                          orion_written_at='2026-09-14T11:00:00+00:00')
    to_upsert, conflicts, conflicted, echo, ingested, protected, unchanged = \
        plan_mirror_writes({555: stored}, {555: {'mirror': payload}}, dry_run=False)
    assert unchanged == 0
    assert ingested == 1 and conflicted == {555} and len(conflicts) == 1
    assert conflicts[0]['field'] == 'overall_status'
    assert len(to_upsert) == 1 and to_upsert[0]['orion_dirty'] is False

    # Echo: the sheet now matches what ORiON wrote -> flag cleared via a write.
    echo_payload = dict(payload, overall_status='Complete')
    stored_echo = _stored_from(payload, orion_dirty=True,
                               orion_written_value={'overall_status': 'Complete'},
                               orion_written_at='2026-09-14T11:00:00+00:00')
    res = plan_mirror_writes({555: stored_echo}, {555: {'mirror': echo_payload}}, dry_run=False)
    assert res[3] == 1 and len(res[0]) == 1 and res[6] == 0


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
