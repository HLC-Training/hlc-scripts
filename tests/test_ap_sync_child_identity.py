#!/usr/bin/env python3
"""
Child-identity exit map (bug 6a9e3545, decision 2026-10-14-ap-sync-residual-and-repage.md).

Every child row sync_ap.py fetches must land in exactly one counted
disposition. This harness proves it against the REAL planning code, not a
retyped copy: it extracts the counter-init + routing pre-pass + child loop
block from sync_ap.py's own source text (the 2026-08-12 extract-and-exec
pattern), dedents it into a function and runs it on synthetic rows.

The same harness runs against any git revision's source, so the control is
the pre-fix code (e299023) reproducing residual 1 on the AP-0775 shape — a
gate you never saw fail has told you nothing.

Run: python tests/test_ap_sync_child_identity.py
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test-key')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test-token')

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import sync_ap  # noqa: E402

PRE_FIX_REV = 'e299023'
START = "    to_insert_delivery = []\n"
END = "    # ── Orphan detection"

# Every primary-disposition counter the extracted block can produce. The
# post-fix block adds projected_secondary_only; the pre-fix block has no such
# local, so it is read with a default of 0 (that absence IS the bug).
PRIMARY_NAMES = (
    'skipped_no_ap', 'skipped_unmapped', 'skipped_ambiguous', 'skipped_viewer',
    'skipped_unchanged', 'skipped_pending', 'cleared_pending_planned',
    'cleared_pending_pc_planned', 'skipped_inactive_noop',
    'skipped_inactive_wrongtable', 'projected_secondary_only',
)


def extract_block(source: str) -> str:
    a = source.index(START)
    b = source.index(END, a)
    return source[a:b]


def run_block(source: str, tasks: list, *, existing_delivery=None, existing_pc=None,
              email_to_id=None, portal_email_to_id=None, director_email_to_id=None,
              viewer_emails=None, dry_run=True) -> dict:
    """Exec the extracted block over `tasks`; return its locals."""
    body = textwrap.indent(textwrap.dedent(extract_block(source)), '    ')
    code = (
        "def _run(tasks, alias_map, name_to_email, email_to_id, portal_email_to_id,\n"
        "         viewer_emails, director_email_to_id, portal_any_email_to_id,\n"
        "         existing_delivery, existing_pc, pending_log_by_item,\n"
        "         pending_log_load_failed, dry_run, id_to_name):\n"
        + body + "\n    return dict(locals())\n"
    )
    ns = dict(vars(sync_ap))
    exec(compile(code, '<extracted sync_ap child loop>', 'exec'), ns)
    return ns['_run'](
        tasks=tasks, alias_map={}, name_to_email={},
        email_to_id=email_to_id or {}, portal_email_to_id=portal_email_to_id or {},
        viewer_emails=viewer_emails or set(),
        director_email_to_id=director_email_to_id or {},
        portal_any_email_to_id={}, existing_delivery=existing_delivery or {},
        existing_pc=existing_pc or {}, pending_log_by_item={},
        pending_log_load_failed=False, dry_run=dry_run, id_to_name={},
    )


def task(ap, *, lead, status='Complete', is_parent=False, is_child=True, text=None, row_id=None):
    family = sync_ap.family_key(ap)
    return {
        'active': status in sync_ap.ACTIVE_STATUSES, 'ap_number': ap,
        'is_child': is_child, 'is_parent': is_parent, 'family': family,
        'action_text': text or f'{ap} title', 'notes': None,
        'lead_value': lead, 'lead_display': '', 'status_raw': status,
        'start_raw': None, 'finish_raw': None, 'sqdcg_raw': None,
        'bucket': None, 'source_raw': None, 'no_report_out_raw': None,
        'mirror': {'smartsheet_row_id': row_id or abs(hash(ap)) % 10**15},
    }


def residual(result: dict, tasks: list) -> int:
    """child_tasks_total minus every PRIMARY disposition the block produced
    (the identity's own arithmetic — closes are counted from the queues)."""
    counted = sum(result.get(n, 0) for n in PRIMARY_NAMES)
    counted += sum(result['planned_primary_insert'].values())
    counted += sum(result['planned_primary_update'].values())
    counted += len(result['to_close_delivery']) + len(result['to_close_pc'])
    return len(tasks) - counted


# Leads: a delivery PLL, a tpm, a director (director-only -> via_director).
PLL, TPM, DIRECTOR = 'pll@ge.com', 'tpm@ge.com', 'director@ge.com'
MAPS = dict(
    email_to_id={PLL: 'u-pll'},
    portal_email_to_id={TPM: 'u-tpm'},
    director_email_to_id={DIRECTOR: 'u-dir'},
)


def ap0775_shape():
    """The live row: a Complete pure parent led by a director, in a family
    whose only routed member is a delivery PLL leaf (AP-0775 / AP-0775-2)."""
    return [
        task('AP-0775', lead=DIRECTOR, is_parent=True, is_child=False, row_id=1),
        task('AP-0775-2', lead=PLL, row_id=2),
    ]


def existing_for_ap0775():
    """Delivery already holds both rows, unchanged (as live)."""
    parent = task('AP-0775', lead=DIRECTOR, is_parent=True, is_child=False)
    leaf = task('AP-0775-2', lead=PLL)
    shared = lambda t: sync_ap.compute_shared_fields(t)  # noqa: E731
    mk = lambda t, owner: {  # noqa: E731
        'id': f"id-{t['ap_number']}", 'owner_id': owner, 'action_text': t['action_text'],
        'status': 'Done', 'start_date': None, 'due_date': None, 'no_report_out': False,
        'ap_is_parent': bool(t['is_parent']), 'ap_is_child': bool(t['is_child']),
        'ap_lead_display': shared(t)['lead_display'], 'ap_pending_update': False,
    }
    return {
        ('AP-0775', True): mk(parent, 'u-dir'),
        ('AP-0775-2', False): mk(leaf, 'u-pll'),
    }


def test_control_prefix_code_leaves_residual_one():
    """CONTROL: the pre-fix source, same rows -> residual 1 (AP-0775 uncounted)."""
    pre = subprocess.run(['git', 'show', f'{PRE_FIX_REV}:sync_ap.py'], cwd=REPO,
                         capture_output=True, text=True, encoding='utf-8')
    if pre.returncode != 0:
        print(f"[SKIP] control: git show {PRE_FIX_REV} unavailable ({pre.stderr.strip()[:80]})")
        return
    tasks = ap0775_shape()
    r = run_block(pre.stdout, tasks, existing_delivery=existing_for_ap0775(), **MAPS)
    res = residual(r, tasks)
    assert 'projected_secondary_only' not in r, "control must be the pre-fix block"
    assert res == 1, f"control expected residual 1, got {res}"
    assert r['sec_planned']['unchanged'] == 1, r['sec_planned']
    print(f"[PASS] control ({PRE_FIX_REV}): AP-0775 shape leaves child_identity_residual {res}")


def test_fixed_code_counts_ap0775_in_its_own_bucket():
    tasks = ap0775_shape()
    r = run_block(Path(REPO / 'sync_ap.py').read_text(encoding='utf-8'), tasks,
                  existing_delivery=existing_for_ap0775(), **MAPS)
    assert r['projected_secondary_only'] == 1, r['projected_secondary_only']
    assert residual(r, tasks) == 0, residual(r, tasks)
    # Nothing else moved: the leaf is still the one skipped_unchanged, and the
    # parent's Delivery header is still in the secondary overlay, not duplicated.
    assert r['skipped_unchanged'] == 1
    assert r['sec_planned'] == {'insert': 0, 'update': 0, 'unchanged': 1, 'clear_pending': 0, 'skip_pending': 0}
    print("[PASS] fixed: AP-0775 shape -> projected_secondary_only=1, residual 0, other buckets unchanged")


def test_secondary_only_row_is_counted_once_even_when_it_inserts():
    """A NEW director-led pure parent in a delivery-only family: the secondary
    insert is real work (split_projection) AND the row is counted once."""
    tasks = ap0775_shape()
    r = run_block(Path(REPO / 'sync_ap.py').read_text(encoding='utf-8'), tasks,
                  existing_delivery={k: v for k, v in existing_for_ap0775().items() if k[1] is False},
                  **MAPS)
    assert r['projected_secondary_only'] == 1
    assert r['sec_planned']['insert'] == 1, r['sec_planned']
    assert residual(r, tasks) == 0
    print("[PASS] fixed: secondary-only row that INSERTS is still counted exactly once")


def test_ordinary_shapes_do_not_touch_the_new_bucket():
    """Exit map: every other row shape stays in its own existing bucket."""
    tasks = [
        task('AP-0100-1', lead=PLL, status='In Progress', row_id=11),                 # insert (delivery)
        task('AP-0101-1', lead=TPM, status='In Progress', row_id=12),                 # insert (pc)
        task('AP-0102', lead=TPM, is_parent=True, is_child=False, row_id=13),         # pure parent, own module
        task('AP-0102-1', lead=TPM, row_id=14),
        task('AP-0200-1', lead='nobody@x.com', status='In Progress', row_id=15),      # out-of-scope active -> unmapped
        task('AP-0201-1', lead='nobody@x.com', status='Complete', row_id=16),         # out-of-scope inactive -> noop
        task('AP-0202-1', lead=DIRECTOR, status='Complete', row_id=17),               # director-only family, inactive -> noop
        task('AP-0203-1', lead=PLL, status='In Progress', row_id=18),                 # in-scope, blank text -> no_ap
    ]
    tasks[-1]['action_text'] = ''   # task() would default an empty title; blank it after
    r = run_block(Path(REPO / 'sync_ap.py').read_text(encoding='utf-8'), tasks, **MAPS)
    assert r['projected_secondary_only'] == 0
    assert residual(r, tasks) == 0, (residual(r, tasks), {n: r.get(n) for n in PRIMARY_NAMES})
    assert r['skipped_unmapped'] == 1 and r['skipped_inactive_noop'] == 2 and r['skipped_no_ap'] == 1
    print("[PASS] fixed: ordinary shapes unchanged, residual 0, new bucket stays 0")


def test_accounting_dict_carries_bucket_and_identity_sums_it():
    kw = {k: 0 for k in (
        'inserted_delivery', 'updated_delivery', 'inserted_pc', 'updated_pc',
        'skipped_unchanged', 'cleared_pending', 'cleared_pending_pc', 'closed_delivery',
        'closed_pc', 'skipped_pending', 'skipped_no_ap', 'skipped_ambiguous',
        'skipped_viewer', 'skipped_unmapped', 'skipped_inactive_noop',
        'skipped_inactive_wrongtable', 'failed_inserts_delivery', 'failed_inserts_pc',
        'parent_titles_total', 'titles_new_or_changed', 'titles_written',
        'date_baselines', 'date_events_detected', 'date_events_written',
        'date_events_skipped_inactive', 'mirror_candidates_total', 'mirror_upserted',
        'mirror_unchanged', 'mirror_echo_cleared', 'mirror_conflicts_ingested',
        'mirror_protected_pending', 'mirror_conflict_rows_logged', 'mirror_failed')}
    kw.update(split_projection={}, title_capture_failed=False, skipped_unchanged=2)
    acct = sync_ap.build_sync_accounting(child_tasks_total=3, projected_secondary_only=1, **kw)
    assert acct['child']['projected_secondary_only'] == 1
    assert acct['child']['child_identity_residual'] == 0, acct['child']
    acct0 = sync_ap.build_sync_accounting(child_tasks_total=3, projected_secondary_only=0, **kw)
    assert acct0['child']['child_identity_residual'] == 1
    print("[PASS] build_sync_accounting: bucket emitted and summed by the identity")


if __name__ == '__main__':
    test_control_prefix_code_leaves_residual_one()
    test_fixed_code_counts_ap0775_in_its_own_bucket()
    test_secondary_only_row_is_counted_once_even_when_it_inserts()
    test_ordinary_shapes_do_not_touch_the_new_bucket()
    test_accounting_dict_carries_bucket_and_identity_sums_it()
    print("ALL PASS")
