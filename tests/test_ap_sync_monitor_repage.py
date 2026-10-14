#!/usr/bin/env python3
"""
AP sync monitor re-page rule (bug 6a9e3545, decision
2026-10-14-ap-sync-residual-and-repage.md).

The child identity check pushes on a STATUS transition or a CHANGE of the
residual value; while the same broken state persists it reminds at most once
per 7 days. (Before: an identical page every 12h — 58 in 34 days.) titles and
run_health keep the 12h re-nag; recovery behaviour is unchanged.

Every case drives the REAL monitor script through its own
--accounting-file / --state-file / --now / --last-run harness in a scratch
dir (so no ap_sync_monitor.log lands in the repo) and reads the push decision
from the script's "[TEST] would push" log lines. Cases a-e are also run
against the pre-fix script (git show e299023) as the control: the cases whose
answer changed show it, the unchanged one (e) shows it did not.

Run: python tests/test_ap_sync_monitor_repage.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
ENV = {**os.environ, 'PYTHONUTF8': '1'}   # the monitor's log uses the locale encoding otherwise
PRE_FIX_REV = 'e299023'
NOW = datetime(2026, 10, 14, 15, 0, 0, tzinfo=timezone.utc)
LAST_RUN = NOW - timedelta(minutes=10)


def accounting(residual: int) -> dict:
    return {
        'as_of': (NOW - timedelta(minutes=9, seconds=30)).isoformat(),
        'child': {'child_tasks_total': 915, 'failed_inserts_delivery': 0,
                  'failed_inserts_pc': 0, 'child_identity_residual': residual},
        'parent_titles': {'titles_new_or_changed': 0, 'titles_written': 0,
                          'title_capture_failed': False},
        'mirror': {'mirror_failed': 0},
    }


def child_state(status, last_push_ago, value):
    entry = {'status': status, 'last_push': (NOW - last_push_ago).isoformat()}
    if value is not None:
        entry['value'] = value
    return {'child': entry}


def run_cli(script_text: str, residual: int, state: dict) -> list:
    """Run a monitor script copy over a synthetic payload; return the
    list of pushed titles (child + others) it would have sent."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / 'ap_sync_monitor.py').write_text(script_text, encoding='utf-8')
        (d / 'acct.json').write_text(json.dumps(accounting(residual)))
        (d / 'state.json').write_text(json.dumps(state))
        r = subprocess.run(
            [sys.executable, 'ap_sync_monitor.py', '--accounting-file', 'acct.json',
             '--state-file', 'state.json', '--now', NOW.isoformat(),
             '--last-run', LAST_RUN.isoformat()],
            cwd=d, capture_output=True, text=True, encoding='utf-8', env=ENV)
        assert r.returncode == 0, r.stderr[-400:]
        log = (d / 'ap_sync_monitor.log').read_text(encoding='utf-8')
        return [ln.split('would push: ', 1)[1] for ln in log.splitlines() if '[TEST] would push' in ln]


def child_pushes(pushes):
    return [p for p in pushes if '(child)' in p or 'child broken' in p]


FIXED = (REPO / 'ap_sync_monitor.py').read_text(encoding='utf-8')


def pre_fix_source():
    r = subprocess.run(['git', 'show', f'{PRE_FIX_REV}:ap_sync_monitor.py'], cwd=REPO,
                       capture_output=True, text=True, encoding='utf-8')
    return r.stdout if r.returncode == 0 else None


CASES = [
    # id, description, residual, state, expected_fixed_push, expected_prefix_push
    ('a', 'same residual (1), last push 2 days ago', 1,
     child_state('residual_positive', timedelta(days=2), 1), False, True),
    ('b', 'same residual (1), last push 8 days ago', 1,
     child_state('residual_positive', timedelta(days=8), 1), True, True),
    ('c', 'residual changes 1 -> 2, last push 1 hour ago', 2,
     child_state('residual_positive', timedelta(hours=1), 1), True, False),
    ('d', 'healthy -> broken (residual 1), prior state healthy', 1,
     child_state('healthy', timedelta(days=30), 0), True, True),
    ('e', 'broken -> healthy (residual 0), prior state broken', 0,
     child_state('residual_positive', timedelta(hours=3), 1), True, True),
    ('f', 'legacy state entry with no stored value, same residual, 2 days', 1,
     child_state('residual_positive', timedelta(days=2), None), False, True),
]


def main():
    pre = pre_fix_source()
    failures = 0
    for cid, desc, residual, state, want_fixed, want_pre in CASES:
        fixed = child_pushes(run_cli(FIXED, residual, state))
        ok = bool(fixed) == want_fixed
        line = f"[{'PASS' if ok else 'FAIL'}] {cid}) {desc}: fixed -> {'PUSH' if fixed else 'no push'}"
        if fixed:
            line += f"  « {fixed[0]} »"
        print(line)
        failures += not ok
        if pre is not None:
            old = child_pushes(run_cli(pre, residual, state))
            ok_pre = bool(old) == want_pre
            print(f"        control ({PRE_FIX_REV}): {'PUSH' if old else 'no push'}"
                  + ("  « " + old[0] + " »" if old else "")
                  + ("" if ok_pre else "   <-- UNEXPECTED control result"))
            failures += not ok_pre

    # Other checks keep their 12h cadence and never learn a value: titles
    # mismatch still re-nags at 12h, and no 'value' is written for them.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / 'ap_sync_monitor.py').write_text(FIXED, encoding='utf-8')
        bad = accounting(0)
        bad['parent_titles'] = {'titles_new_or_changed': 3, 'titles_written': 0, 'title_capture_failed': False}
        (d / 'acct.json').write_text(json.dumps(bad))
        st = {'titles': {'status': 'mismatch', 'last_push': (NOW - timedelta(hours=13)).isoformat()}}
        (d / 'state.json').write_text(json.dumps(st))
        subprocess.run([sys.executable, 'ap_sync_monitor.py', '--accounting-file', 'acct.json',
                        '--state-file', 'state.json', '--now', NOW.isoformat(),
                        '--last-run', LAST_RUN.isoformat()],
                       cwd=d, capture_output=True, text=True, check=True, env=ENV)
        new_state = json.loads((d / 'state.json').read_text())
        log = (d / 'ap_sync_monitor.log').read_text(encoding='utf-8')
        titles_nag = 'titles mismatch' in log and '[TEST] would push' in log
        ok = titles_nag and 'value' not in new_state['titles']
        print(f"[{'PASS' if ok else 'FAIL'}] g) titles mismatch, 13h since last push: still re-nags at 12h, no value tracked")
        failures += not ok

    print("ALL PASS" if not failures else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
