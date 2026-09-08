"""
Behavioral proof for sync_ap.prune_stale_mirror_rows (action item 3ca6c010,
decision 2026-09-08-ap-tracker-prune-and-composite-guard.md).

Runs the REAL function against a stub db that records what it was asked to
delete — not a retyped copy — so the proof can't drift from what ships.

Run:  python tests/test_ap_tracker_prune.py     (no pytest dependency)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test')

from sync_ap import prune_stale_mirror_rows  # noqa: E402


class StubQuery:
    def __init__(self, table):
        self.table = table
        self.deleted_ids = None

    def delete(self):
        return self

    def in_(self, col, ids):
        assert col == 'smartsheet_row_id', "prune must delete by smartsheet_row_id, never ap_number"
        self.deleted_ids = list(ids)
        return self

    def execute(self):
        self.table.calls.append(sorted(self.deleted_ids))
        return self


class StubTable:
    def __init__(self):
        self.calls = []  # each entry: sorted list of smartsheet_row_ids passed to .delete().in_(...)

    def table(self, name):
        assert name == 'ap_tracker'
        return StubQuery(self)


def test_prune_removes_a_vanished_mirror_row_on_complete_fetch():
    db = StubTable()
    # AP-0293-3-2-shaped stale sibling: still in ap_tracker, its
    # smartsheet_row_id 404s on the sheet (absent from this fetch).
    existing_mirror = {111: {'id': 'a'}, 222: {'id': 'b'}}
    all_sheet_row_ids = {222}  # 111 vanished from the sheet

    pruned, skipped = prune_stale_mirror_rows(db, existing_mirror, all_sheet_row_ids,
                                               fetch_complete=True, mirror_load_failed=False)

    assert skipped is False
    assert pruned == 1
    assert db.calls == [[111]], f"expected exactly row 111 deleted, got {db.calls}"


def test_prune_skips_on_incomplete_fetch_even_with_a_vanished_row_present():
    db = StubTable()
    existing_mirror = {111: {'id': 'a'}, 222: {'id': 'b'}}
    all_sheet_row_ids = {222}  # 111 LOOKS vanished, but the fetch was short

    pruned, skipped = prune_stale_mirror_rows(db, existing_mirror, all_sheet_row_ids,
                                               fetch_complete=False, mirror_load_failed=False)

    assert skipped is True
    assert pruned == 0
    assert db.calls == [], "an incomplete fetch must never issue a delete"


def test_prune_skips_when_the_mirror_load_itself_failed():
    db = StubTable()
    # mirror_load_failed=True means existing_mirror is untrustworthy (empty
    # or partial) regardless of what it contains — the flag alone must gate.
    pruned, skipped = prune_stale_mirror_rows(db, {111: {'id': 'a'}}, set(),
                                               fetch_complete=True, mirror_load_failed=True)

    assert skipped is True
    assert pruned == 0
    assert db.calls == []


def test_prune_leaves_twins_alone_when_both_sheet_rows_still_exist():
    # AP-036: one is_parent row (smartsheet_row_id 6299779155595140) + one
    # is_child row (1796179528224644), both live in the sheet — same
    # ap_number, distinct row ids. A bare ap_number-keyed prune would see
    # "two rows, one ap_number" and be tempted to collapse them; this
    # function only ever looks at smartsheet_row_id, so both survive.
    db = StubTable()
    existing_mirror = {
        6299779155595140: {'id': 'parent'},
        1796179528224644: {'id': 'child'},
    }
    all_sheet_row_ids = {6299779155595140, 1796179528224644}

    pruned, skipped = prune_stale_mirror_rows(db, existing_mirror, all_sheet_row_ids,
                                               fetch_complete=True, mirror_load_failed=False)

    assert skipped is False
    assert pruned == 0
    assert db.calls == [], "both twin rows are present in the sheet — neither is a prune candidate"


def test_prune_noop_when_nothing_vanished():
    db = StubTable()
    existing_mirror = {111: {'id': 'a'}}
    all_sheet_row_ids = {111}

    pruned, skipped = prune_stale_mirror_rows(db, existing_mirror, all_sheet_row_ids,
                                               fetch_complete=True, mirror_load_failed=False)

    assert skipped is False
    assert pruned == 0
    assert db.calls == []


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"{len(tests)} passed")
