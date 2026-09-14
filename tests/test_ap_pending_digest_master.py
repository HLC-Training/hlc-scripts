"""
Render proof for send_ap_pending_digest.py's master-tracker rail (2026-09-14,
October full-AP review pilot — orion-pll decision
2026-09-14-october-full-ap-review-pilot.md): a dirty ap_tracker row whose
held Current Finish was logged against the mirror itself (module 'ops')
renders in the digest with the app's workflow reason; an ordinary push
awaiting its echo does not; fixtures never do.

Runs the REAL fetch/enrich/render functions against a stub db (hermetic —
no network, no key; `supabase` is stubbed in sys.modules so the test runs
on any machine).

Run:  python tests/test_ap_pending_digest_master.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'stub-key-for-render-test')
_stub = types.ModuleType('supabase')
_stub.create_client = lambda *a, **k: None
sys.modules.setdefault('supabase', _stub)

import send_ap_pending_digest as digest  # noqa: E402
from ap_pending import is_fixture  # noqa: E402

HELD_REASON = ("Apply in Smartsheet — Current Finish is a dependency-controlled column on the "
               "AP tracker; ORiON holds the date until it is entered in the sheet")
JEN = 'c1494071-97a7-445b-a373-16a9d2f335ed'


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._filters = []

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self._filters.append(lambda r: r.get(col) in set(vals))
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        return _Resp([r for r in self._rows if all(f(r) for f in self._filters)])


class StubDb:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _db(tracker_rows, log_rows):
    return StubDb({
        'ap_tracker': tracker_rows,
        'ap_change_log': log_rows,
        'portal_users': [{'id': JEN, 'name': 'Jennifer Wright'}],
        'users': [],
    })


def test_held_finish_on_ops_only_row_renders_with_workflow_reason():
    tracker = [{
        'id': 'trk-1', 'ap_number': 'AP-0756', 'improvement': 'Tooling refresh',
        'overall_status': 'In Progress', 'current_finish': '2026-10-31', 'original_finish': '2026-10-15',
        'owner_user_id': JEN, 'lead_display': 'Jen Wright', 'smartsheet_row_id': 4660350745706372,
        'orion_dirty': True, 'orion_written_value': {'current_finish': '2026-10-31'},
        'orion_written_at': '2026-09-14T15:00:00+00:00',
    }]
    log = [{
        'module': 'ops', 'item_id': 'trk-1', 'ap_number': 'AP-0756', 'field': 'current_finish',
        'old_value': '2026-10-15', 'new_value': '2026-10-31', 'reason': HELD_REASON,
        'changed_by': JEN, 'changed_at': '2026-09-14T15:00:00.100+00:00',
    }]
    rows = digest.fetch_master_pending_rows(_db(tracker, log))
    assert [r['ap_number'] for r in rows] == ['AP-0756']
    assert rows[0]['module'] == 'master'
    assert [e['field'] for e in rows[0]['reasons']] == ['current_finish']

    enriched = digest.enrich_rows(rows, {JEN: 'Jennifer Wright'}, {})
    text = digest.build_text_body(enriched)
    html = digest.build_html_body(enriched)
    assert '[Master tracker] AP-0756 — Tooling refresh' in text
    assert 'Owner: Jennifer Wright' in text
    assert 'Reason: current_finish: 2026-10-15 → 2026-10-31 — Apply in Smartsheet' in text
    assert 'Master tracker' in html and 'Apply in Smartsheet' in html
    # Never error-flavored — Jen's own edit is workflow, not a failure.
    assert 'fail' not in text.lower() and '1080' not in text


def test_ordinary_push_awaiting_echo_is_not_listed():
    tracker = [{
        'id': 'trk-2', 'ap_number': 'AP-0759', 'improvement': 'Status only',
        'overall_status': 'Complete', 'current_finish': '2026-10-20', 'original_finish': None,
        'owner_user_id': None, 'lead_display': 'Siaw Wei Tan', 'smartsheet_row_id': 4660350745706373,
        'orion_dirty': True, 'orion_written_value': {'overall_status': 'Complete'},
        'orion_written_at': '2026-09-14T15:00:00+00:00',
    }]
    # No ops entry: the status reached the sheet and is just waiting for the sync's echo.
    assert digest.fetch_master_pending_rows(_db(tracker, [])) == []
    # A sync-authored conflict row (changed_by null) never qualifies either.
    conflict = [{'module': 'ops', 'item_id': 'trk-2', 'field': 'overall_status', 'old_value': 'In Progress',
                 'new_value': 'Complete', 'reason': 'conflict: …', 'changed_by': None,
                 'changed_at': '2026-09-14T15:01:00+00:00'}]
    assert digest.fetch_master_pending_rows(_db(tracker, conflict)) == []


def test_stale_ops_entry_for_a_different_value_does_not_qualify():
    tracker = [{
        'id': 'trk-3', 'ap_number': 'AP-0770', 'improvement': 'Re-edited', 'overall_status': 'Not Started',
        'current_finish': '2026-10-31', 'original_finish': None, 'owner_user_id': None,
        'lead_display': 'Alyssa Mays', 'smartsheet_row_id': 4660350745706374,
        'orion_dirty': True, 'orion_written_value': {'current_finish': '2026-10-31'},
        'orion_written_at': '2026-09-14T16:00:00+00:00',
    }]
    log = [
        {'module': 'ops', 'item_id': 'trk-3', 'field': 'current_finish', 'old_value': '2026-10-01',
         'new_value': '2026-10-15', 'reason': HELD_REASON, 'changed_by': JEN, 'changed_at': '2026-09-14T15:00:00+00:00'},
        {'module': 'ops', 'item_id': 'trk-3', 'field': 'current_finish', 'old_value': '2026-10-15',
         'new_value': '2026-10-31', 'reason': HELD_REASON, 'changed_by': JEN, 'changed_at': '2026-09-14T16:00:00+00:00'},
    ]
    rows = digest.fetch_master_pending_rows(_db(tracker, log))
    assert [(e['old_value'], e['new_value']) for e in rows[0]['reasons']] == [('2026-10-15', '2026-10-31')]
    enriched = digest.enrich_rows(rows, {}, {})
    assert 'Owner: Alyssa Mays' in digest.build_text_body(enriched)   # unattributed lead keeps the sheet text


def test_fixture_rows_are_excluded_by_the_shared_rule():
    row = {'ap_number': 'AP-9904', 'action_text': 'TEST FIXTURE — Ops-only', 'smartsheet_row_id': 999999990104}
    assert is_fixture(row['ap_number'], row['action_text'], row['smartsheet_row_id'])
    assert is_fixture('AP-0756', 'Real title', 999999990104)


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"{len(tests)} passed")
