#!/usr/bin/env python3
"""
Test director-fallback logic (Phase 2b, bug ca9beaeb).
Verify that directors are assigned as owners when they are the only lead.
"""

import sys
import os
from pathlib import Path

# Set dummy env vars to allow module import
os.environ.setdefault('ORION_SUPABASE_SERVICE_KEY', 'test-key')
os.environ.setdefault('SMARTSHEET_API_TOKEN', 'test-token')

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sync_ap import resolve_owner


def test_director_only_lead():
    """Test (a): director is the only lead -> director assigned as owner."""
    # Setup: director email in portal_users but not in portal_email_to_id (tpm-filtered)
    email = "director@example.com"
    email_to_id = {}
    portal_email_to_id = {}  # Empty - no tpm routed
    viewer_emails = set()
    all_portal_users = [
        {
            'id': 'dir-uuid-001',
            'email': 'director@example.com',
            'role': 'director',
            'name': 'Michele Alcantar'
        }
    ]

    destination, owner_id = resolve_owner(email, email_to_id, portal_email_to_id, viewer_emails,
                                          all_portal_users=all_portal_users)

    assert destination == 'director', f"Expected 'director' but got '{destination}'"
    assert owner_id == 'dir-uuid-001', f"Expected director id 'dir-uuid-001' but got '{owner_id}'"
    print("[PASS] Test (a): director is only lead -> director detected")


def test_director_with_tpm_lead():
    """Test (b): director lead AND TPM lead exist -> TPM owns, not director."""
    # Setup: director email in portal_users, TPM email in portal_email_to_id
    tpm_email = "tpm@example.com"
    email_to_id = {}
    portal_email_to_id = {
        'tpm@example.com': 'tpm-uuid-001'
    }
    viewer_emails = set()
    all_portal_users = [
        {
            'id': 'tpm-uuid-001',
            'email': 'tpm@example.com',
            'role': 'tpm',
            'name': 'Bob Smith'
        },
        {
            'id': 'dir-uuid-001',
            'email': 'director@example.com',
            'role': 'director',
            'name': 'Michele Alcantar'
        }
    ]

    # Resolve TPM (should return pc)
    destination, owner_id = resolve_owner(tpm_email, email_to_id, portal_email_to_id, viewer_emails,
                                          all_portal_users=all_portal_users)

    assert destination == 'pc', f"Expected 'pc' for TPM but got '{destination}'"
    assert owner_id == 'tpm-uuid-001', f"Expected TPM id 'tpm-uuid-001' but got '{owner_id}'"
    print("[PASS] Test (b) part 1: TPM lead resolves to pc (not director)")

    # Verify director still resolves to 'director' when called directly
    director_email = "director@example.com"
    destination, owner_id = resolve_owner(director_email, email_to_id, portal_email_to_id, viewer_emails,
                                          all_portal_users=all_portal_users)

    assert destination == 'director', f"Expected 'director' but got '{destination}'"
    assert owner_id == 'dir-uuid-001', f"Expected director id 'dir-uuid-001' but got '{owner_id}'"
    print("[PASS] Test (b) part 2: director still resolves to director when called directly")


def test_director_with_pll_lead():
    """Test: director lead AND PLL lead exist -> PLL owns, not director."""
    pll_email = "pll@example.com"
    email_to_id = {
        'pll@example.com': 'pll-uuid-001'
    }
    portal_email_to_id = {}
    viewer_emails = set()
    all_portal_users = [
        {
            'id': 'dir-uuid-001',
            'email': 'director@example.com',
            'role': 'director',
            'name': 'Michele Alcantar'
        }
    ]

    # Resolve PLL (should return delivery)
    destination, owner_id = resolve_owner(pll_email, email_to_id, portal_email_to_id, viewer_emails,
                                          all_portal_users=all_portal_users)

    assert destination == 'delivery', f"Expected 'delivery' for PLL but got '{destination}'"
    assert owner_id == 'pll-uuid-001', f"Expected PLL id 'pll-uuid-001' but got '{owner_id}'"
    print("[PASS] Test: PLL lead resolves to delivery (not director)")


def test_non_director_role_not_returned_as_director():
    """Test: non-director roles (tpm, ops, etc.) don't return 'director' even if not in portal_email_to_id."""
    email = "cpm@example.com"
    email_to_id = {}
    portal_email_to_id = {}  # CPM not in tpm-filtered map
    viewer_emails = set()
    all_portal_users = [
        {
            'id': 'cpm-uuid-001',
            'email': 'cpm@example.com',
            'role': 'cpm',  # Not 'director'
            'name': 'Charlie Brown'
        }
    ]

    destination, owner_id = resolve_owner(email, email_to_id, portal_email_to_id, viewer_emails,
                                          all_portal_users=all_portal_users)

    # CPM is not in portal_email_to_id (only tpm), so should return 'none'
    assert destination == 'none', f"Expected 'none' for CPM not in tpm-map, got '{destination}'"
    assert owner_id is None, f"Expected no owner_id, got '{owner_id}'"
    print("[PASS] Test: non-director roles outside tpm-map return 'none' not 'director'")


if __name__ == '__main__':
    test_director_only_lead()
    test_director_with_tpm_lead()
    test_director_with_pll_lead()
    test_non_director_role_not_returned_as_director()
    print("\nAll tests passed!")
