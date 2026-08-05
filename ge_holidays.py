"""
ge_holidays.py
─────────────────────────────────────────────────────────────────
Shared 2026 GE Vernova holiday calendar (FieldCore US Staff/RETX,
sam-drop) for weekday+holiday send gates. Factored out of
send_pll_digest.py 2026-08-05 when send_tpm_digest.py needed the same
list — do not fork a second copy; both digest jobs import HOLIDAYS from
here.

Refresh for 2027 before the year turns.
─────────────────────────────────────────────────────────────────
"""

from datetime import date

HOLIDAYS = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 11, 27),  # Day After Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2026, 12, 25),  # Christmas Day
}


def send_decision(d: date) -> tuple[bool, str]:
    """Weekday + holiday gate on a Chicago-local date. Shared by every
    scheduled digest job so the calendar can't drift between them."""
    if d.weekday() >= 5:
        return False, f"{d} is a {d.strftime('%A')} — weekend, no send."
    if d in HOLIDAYS:
        return False, f"{d} is a GE Vernova 2026 holiday — no send."
    return True, f"{d} is an ordinary weekday — send."
