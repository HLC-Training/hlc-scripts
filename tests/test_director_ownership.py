#!/usr/bin/env python3
"""
Test director-as-owner logic (Phase 2c, bug ca9beaeb) — supersedes Phase 2b.

Phase 2b's per-family fallback ("director owns only if the family has NO
executing owner elsewhere") was the wrong model and was structurally inert
in production (see decision doc 2026-09-11-owner-of-line-ownership.md).

The correct rule: every AP row's own lead owns that row, hierarchy-
independent. A director is a valid owner exactly like a tpm. resolve_owner()
now returns a third element, via_director, used ONLY to keep family
MEMBERSHIP (which families sync into ORiON at all) based on tpm/pll/admin
leads — a family led solely by a director (e.g. AP-0570/AP-0791) must not
newly enter ORiON just because directors became valid owners.
"""

import sys
import os
from pathlib import Path

os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test-key')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test-token')

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync_ap import resolve_owner


def test_director_only_lead_is_valid_owner():
    """Gate fixture: a row led by a director resolves to that director as owner."""
    email = "director@example.com"
    email_to_id = {}
    portal_email_to_id = {}
    viewer_emails = set()
    director_email_to_id = {"director@example.com": "dir-uuid-001"}

    destination, owner_id, via_director = resolve_owner(
        email, email_to_id, portal_email_to_id, viewer_emails,
        director_email_to_id=director_email_to_id)

    assert destination == 'pc', f"Expected 'pc' but got '{destination}'"
    assert owner_id == 'dir-uuid-001', f"Expected 'dir-uuid-001' but got '{owner_id}'"
    assert via_director is True, "Expected via_director=True for a director-sourced resolution"
    print("[PASS] Director-only lead resolves to that director as owner (destination='pc')")


def test_tpm_lead_unaffected_not_via_director():
    """TPM resolution is unchanged and via_director is False."""
    email_to_id = {}
    portal_email_to_id = {"tpm@example.com": "tpm-uuid-001"}
    viewer_emails = set()
    director_email_to_id = {"director@example.com": "dir-uuid-001"}

    destination, owner_id, via_director = resolve_owner(
        "tpm@example.com", email_to_id, portal_email_to_id, viewer_emails,
        director_email_to_id=director_email_to_id)

    assert destination == 'pc'
    assert owner_id == 'tpm-uuid-001'
    assert via_director is False, "TPM resolution must not be flagged via_director"
    print("[PASS] TPM lead resolves to pc with via_director=False (unchanged)")


def test_admin_director_dual_role_prefers_delivery():
    """Jim Rosen case: director in portal_users AND admin (non-viewer) in
    users. Delivery must win — must NOT become ambiguous or lose ownership."""
    email = "jim.rosen@example.com"
    email_to_id = {"jim.rosen@example.com": "jim-users-uuid"}  # admin, non-viewer
    portal_email_to_id = {}  # not tpm
    viewer_emails = set()
    director_email_to_id = {"jim.rosen@example.com": "jim-portal-uuid"}  # also director

    destination, owner_id, via_director = resolve_owner(
        email, email_to_id, portal_email_to_id, viewer_emails,
        director_email_to_id=director_email_to_id)

    assert destination == 'delivery', f"Expected 'delivery' (existing admin ownership preserved) but got '{destination}'"
    assert owner_id == 'jim-users-uuid'
    assert via_director is False
    print("[PASS] Dual-role (director + admin) resolves to delivery — no regression, not ambiguous")


def test_family_membership_excludes_director_only_leads():
    """AP-0570/AP-0791 case: a family led SOLELY by a director (no tpm/pll
    anywhere) must not count toward family membership — via_director=True
    signals the caller to exclude it from fam_modules."""
    email = "jennifer@example.com"
    email_to_id = {}
    portal_email_to_id = {}
    viewer_emails = set()
    director_email_to_id = {"jennifer@example.com": "jennifer-uuid"}

    destination, owner_id, via_director = resolve_owner(
        email, email_to_id, portal_email_to_id, viewer_emails,
        director_email_to_id=director_email_to_id)

    # Simulate the caller's membership-gating logic
    fam_modules_would_add = destination in ('delivery', 'pc') and not via_director
    assert destination == 'pc'
    assert via_director is True
    assert fam_modules_would_add is False, "A director-only lead must NOT establish family membership"
    print("[PASS] Director-only lead does not establish family membership (AP-0570/0791 stay out of scope)")


def test_pll_lead_unaffected():
    """PLL/admin lead resolution is completely unchanged."""
    email_to_id = {"pll@example.com": "pll-uuid-001"}
    portal_email_to_id = {}
    viewer_emails = set()
    director_email_to_id = {}

    destination, owner_id, via_director = resolve_owner(
        "pll@example.com", email_to_id, portal_email_to_id, viewer_emails,
        director_email_to_id=director_email_to_id)

    assert destination == 'delivery'
    assert owner_id == 'pll-uuid-001'
    assert via_director is False
    print("[PASS] PLL lead resolves to delivery, unaffected by director logic")


def test_no_director_map_backward_compatible():
    """director_email_to_id is optional (defaults to None) — callers that
    don't pass it get pre-2c behavior for non-director emails."""
    email_to_id = {}
    portal_email_to_id = {}
    viewer_emails = set()

    destination, owner_id, via_director = resolve_owner(
        "nobody@example.com", email_to_id, portal_email_to_id, viewer_emails)

    assert destination == 'none'
    assert owner_id is None
    assert via_director is False
    print("[PASS] Omitting director_email_to_id defaults safely to 'none' for unresolved emails")


if __name__ == '__main__':
    test_director_only_lead_is_valid_owner()
    test_tpm_lead_unaffected_not_via_director()
    test_admin_director_dual_role_prefers_delivery()
    test_family_membership_excludes_director_only_leads()
    test_pll_lead_unaffected()
    test_no_director_map_backward_compatible()
    print("\nAll tests passed!")
