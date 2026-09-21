"""
Render proof for send_ap_pending_digest.py's orphaned-wins rule (decision
2026-09-21-pc-renumber-zombies, bug 4dfe07fd): a row flagged BOTH
ap_pending_update and ap_orphaned is never rendered as an "apply in
Smartsheet" ask — it appears only in the removed-from-tracker section — while
a genuinely pending non-orphan row still renders, and the per-field
reconciliation rule (decision 2026-09-08: matched / tracker_newer drop out)
is untouched.

Runs the REAL fetch / suppress / reconcile / enrich / render functions
against a stub db (hermetic — `supabase` is stubbed in sys.modules).
Fixture rows reuse the live AP-0785 shapes from the 2026-09-21 incident.

Run:  python tests/test_ap_pending_digest_orphan_suppression.py
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

GLORIA = 'f7b39978-857e-4681-b2c4-1c2502389d4e'
IMPORT_TITLE = 'Import ICS Level 2 Competency Map Structure in Kahuna'
EDIT_TEXT = 'Use Import file from Program Manager to load Map structure into Kahuna'
SYSTEM_REASON = 'Edited in ORiON (no reason required for this field)'


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
        return _Resp([dict(r) for r in self._rows if all(f(r) for f in self._filters)])


class StubDb:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def pc_row(rid, ap, title, *, pending=True, orphaned=False, since='2026-09-21T11:18:10.811289+00:00', status='approved'):
    return {'id': rid, 'ap_number': ap, 'title': title, 'status': status, 'source': 'ap_synced',
            'target_end_date': '2026-10-31', 'original_target_date': '2026-10-31',
            'ap_pending_update': pending, 'ap_pending_since': since if pending else None,
            'ap_orphaned': orphaned, 'ap_orphaned_since': '2026-09-21T15:00:00+00:00' if orphaned else None,
            'owner_id': GLORIA, 'ap_is_parent': False, 'ap_is_child': True}


def log_entry(item_id, ap, new_value, changed_at='2026-09-21T11:18:10.811289+00:00', old_value=None):
    return {'id': f'log-{item_id}', 'module': 'pc', 'item_id': item_id, 'ap_number': ap,
            'field': 'description', 'old_value': old_value, 'new_value': new_value,
            'reason': SYSTEM_REASON, 'changed_by': GLORIA, 'changed_at': changed_at}


def tracker_row(ap, improvement, row_id, description=None, modified='2026-09-01T20:27:19+00:00'):
    return {'id': f'trk-{row_id}', 'ap_number': ap, 'is_parent': False, 'is_child': True,
            'smartsheet_row_id': row_id, 'smartsheet_modified_at': modified,
            'last_synced_at': '2026-09-21T12:00:00+00:00', 'improvement': improvement,
            'description': description, 'overall_status': 'Not Started', 'sqdcgp': None,
            'start_date_only': None, 'current_finish': '2026-10-31',
            'orion_dirty': False, 'orion_written_value': None}


def run_pipeline(pc_rows, log_rows, tracker_rows):
    """The real main() path up to the rendered text body, minus the send."""
    db = StubDb({
        'pc_projects': pc_rows, 'action_items': [],
        'ap_change_log': log_rows, 'ap_tracker': tracker_rows,
        'portal_users': [{'id': GLORIA, 'name': 'Gloria Norris'}], 'users': [],
    })
    pending_raw = digest.fetch_pending_delivery_rows(db) + digest.fetch_pending_pc_rows(db)
    pending_raw, suppressed = digest.suppress_orphaned_pending(pending_raw)
    orphan_raw = digest.fetch_orphaned_delivery_rows(db) + digest.fetch_orphaned_pc_rows(db)
    reasons = digest.fetch_change_log_reasons(db, pending_raw)
    tracker_by_ap = digest.fetch_tracker_rows(db, {r['ap_number'] for r in pending_raw})
    rows, reconciled = digest.reconcile_rows(pending_raw, reasons, tracker_by_ap)
    orphan_rows = [r for r in orphan_raw if r.get('status') not in digest.TERMINAL_STATUSES[r['module']]]
    names = digest.fetch_owner_names(db, set(), {GLORIA})
    enriched = digest.enrich_rows(rows, names, reasons)
    enriched_orphans = digest.enrich_orphans(orphan_rows, names)
    text = digest.build_text_body(enriched, enriched_orphans)
    html = digest.build_html_body(enriched, enriched_orphans)
    return {'pending': enriched, 'suppressed': suppressed, 'reconciled': reconciled,
            'orphans': enriched_orphans, 'text': text, 'html': html}


def pending_section(text: str) -> str:
    return text.split('REMOVED FROM THE TRACKER')[0]


# ── the incident row: orphaned AND pending -> not an ask, listed as removed ──
def test_orphaned_pending_zombie_is_not_rendered_as_pending_but_is_listed_as_removed():
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True, orphaned=True)
    out = run_pipeline(
        [zombie],
        [log_entry('8e5e4fd1', 'AP-0785-3-3', EDIT_TEXT)],
        [tracker_row('AP-0785-3-3', 'ICS Level 2 Storyboard & Content Gathering', 7016220100001668)],
    )
    assert [r['id'] for r in out['suppressed']] == ['8e5e4fd1'], out['suppressed']
    assert out['pending'] == [], out['pending']
    assert 'AP-0785-3-3' not in pending_section(out['text']), out['text']
    assert 'waiting for the matching update' not in out['text'], out['text']
    assert [r['id'] for r in out['orphans']] == ['8e5e4fd1']
    assert 'REMOVED FROM THE TRACKER' in out['text'] and 'AP-0785-3-3' in out['text']
    assert 'now belongs' in out['text'] and 'to a different action after a renumber' in out['text'], out['text']
    assert 'now belongs to a' in out['html']


# ── suppression is scoped to orphaned: a real pending row still renders ──
def test_genuinely_pending_non_orphan_row_still_renders_as_pending():
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE, pending=True, orphaned=False)
    out = run_pipeline(
        [live],
        [log_entry('5b58e7a2', 'AP-0785-2-3', EDIT_TEXT)],
        [tracker_row('AP-0785-2-3', IMPORT_TITLE, 5355638976282500, description=None)],
    )
    assert out['suppressed'] == []
    assert [r['id'] for r in out['pending']] == ['5b58e7a2'], out['pending']
    assert out['pending'][0]['field_states'] == {'description': 'pending'}, out['pending'][0]['field_states']
    assert 'AP-0785-2-3' in pending_section(out['text'])
    assert f'description: — → {EDIT_TEXT}' in out['text'], out['text']
    assert out['orphans'] == []


def test_zombie_and_live_twin_together_render_only_the_twin_as_pending():
    zombie = pc_row('8e5e4fd1', 'AP-0785-3-3', IMPORT_TITLE, pending=True, orphaned=True)
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE, pending=True)
    out = run_pipeline(
        [zombie, live],
        [log_entry('8e5e4fd1', 'AP-0785-3-3', EDIT_TEXT), log_entry('5b58e7a2', 'AP-0785-2-3', EDIT_TEXT)],
        [tracker_row('AP-0785-3-3', 'ICS Level 2 Storyboard & Content Gathering', 7016220100001668),
         tracker_row('AP-0785-2-3', IMPORT_TITLE, 5355638976282500)],
    )
    assert [r['id'] for r in out['pending']] == ['5b58e7a2']
    assert [r['id'] for r in out['suppressed']] == ['8e5e4fd1']
    section = pending_section(out['text'])
    assert 'AP-0785-2-3' in section and 'AP-0785-3-3' not in section, section


# ── reconciliation rule intact (decision 2026-09-08) ──
def test_matched_field_still_reconciles_and_drops_out():
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE, pending=True)
    out = run_pipeline(
        [live],
        [log_entry('5b58e7a2', 'AP-0785-2-3', EDIT_TEXT)],
        [tracker_row('AP-0785-2-3', IMPORT_TITLE, 5355638976282500, description=EDIT_TEXT)],  # Jen applied it
    )
    assert [r['id'] for r in out['reconciled']] == ['5b58e7a2']
    assert out['reconciled'][0]['field_states'] == {'description': 'matched'}
    assert out['pending'] == [] and out['text'] == ''


def test_tracker_newer_field_still_supersedes_and_drops_out():
    live = pc_row('5b58e7a2', 'AP-0785-2-3', IMPORT_TITLE, pending=True)
    out = run_pipeline(
        [live],
        [log_entry('5b58e7a2', 'AP-0785-2-3', EDIT_TEXT)],
        [tracker_row('AP-0785-2-3', IMPORT_TITLE, 5355638976282500,
                     description='Jen rewrote this afterwards', modified='2026-09-21T14:00:00+00:00')],
    )
    assert [r['id'] for r in out['reconciled']] == ['5b58e7a2']
    assert out['reconciled'][0]['field_states'] == {'description': 'tracker_newer'}
    assert out['pending'] == []


def test_orphaned_row_that_is_closed_in_orion_is_not_listed_anywhere():
    closed = pc_row('e7902c20', 'AP-0785-3-5', 'Configure Kahuna …', pending=False, orphaned=True, status='cancelled')
    out = run_pipeline([closed], [], [])
    assert out['pending'] == [] and out['orphans'] == [] and out['text'] == ''


def test_suppress_is_a_pure_partition():
    rows = [{'id': 'a', 'module': 'pc', 'ap_number': 'AP-1', 'ap_orphaned': True},
            {'id': 'b', 'module': 'pc', 'ap_number': 'AP-2', 'ap_orphaned': False},
            {'id': 'c', 'module': 'delivery', 'ap_number': 'AP-3'}]   # key absent -> not orphaned
    pending, suppressed = digest.suppress_orphaned_pending(rows)
    assert [r['id'] for r in pending] == ['b', 'c'] and [r['id'] for r in suppressed] == ['a']


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
