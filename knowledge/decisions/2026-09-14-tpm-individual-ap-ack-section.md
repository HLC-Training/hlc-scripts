# Unacknowledged AP date changes — fifth section on the weekly individual TPM digest

**Date:** 2026-09-14
**Repo:** `hlc-scripts` only
**Script:** `send_tpm_individual_digest.py`
**Origin:** reach-extender flagged at bug `ca9beaeb` Phase 3 close (SAM COS `5bd11694`)
**Reuses:** `ap_date_acks.py` (shared data layer, unchanged) and the rendering
pattern from `send_tpm_digest.py`'s own Phase 3 section (commit `0a5f948`)

---

## What shipped

A fifth section, **"Unacknowledged AP date changes"**, on the weekly per-TPM
P&C digest — the TPM's own unacked parent-AP end-date changes, one line per
AP (latest old → new date, plus "+N earlier" when more than one move is
outstanding on the same AP). It closes the last gap from Phase 3: the PLL
daily digest and Michele's daily TPM roll-up both got an ack section on
2026-09-14, but the weekly individual TPM mail — the only channel that
reaches TPMs directly — did not.

No new query shape was invented. `fetch_unacked_ap_date_changes(db,
owner_ids, 'pc_projects', sorted(DONE_STATUSES))` is the exact call
`send_tpm_digest.py` already makes; this script just makes it too, keyed by
its own `owner_ids`. The row renderer (`ap_change_row_html`) and the
point-back link constant (`AP_ACK_URL`) are the same shapes as the daily
digest's, trimmed of the per-TPM `tpm_group_html` wrapper that file needs
(this digest is already one-TPM-per-email, so there is nothing to group).

## Point-back, not email-ack

`AP_ACK_URL = f"{ORION_URL}/pc/projects"` — a **plain, un-tokenized** URL to
the P&C module whose banner carries the Acknowledge button. There is no
email-side ack and no per-recipient token anywhere in this change. That is
deliberate, not an oversight: it is the same GE-mail-consumes-single-use-
tokens failure mode as the August TPM-onboarding incident (bugs `cab4810f` /
`065c166e`), where a mail client's link-prefetch or scanning silently
consumed single-use tokens before the human ever clicked. Acknowledging an
AP date change stays a real, authenticated action in ORiON — nothing this
digest can shortcut.

## Conventions preserved, not reinvented

This digest's four existing sections have their own settled rules
(2026-09-02 grill), and the new section follows every one of them rather
than introducing a sixth pattern:

- **No dedup.** A project can appear under "Newly assigned to you this
  week" *and* have its AP show up under the new section — nothing
  suppresses either. This falls out structurally: the ack section is keyed
  by AP number from a wholly separate fetch (`ap_end_date_changes` /
  `ap_end_date_acks`), not by project row, so there was no shared key to
  accidentally dedup against. Proven with a fixture project that is both
  newly-assigned and has an unacked change on its AP — it renders in both
  sections.
- **Window copy.** The section is **not time-windowed** — "unacknowledged"
  is a live state, not a watermark comparison — so it never touches
  `window_phrase()` and never says "the last 7 days" or any other span.
  This is a deliberate absence, not an oversight: there was no timing claim
  to phrase correctly in the first place.
- **Suppression.** The existing rule ("no email if all sections empty")
  now correctly flips for a TPM whose *only* content is one unacked AP date
  change — previously that TPM would have been suppressed entirely; now
  they get an email for the ack section alone. That is the intended effect
  of adding an actionable section, not a suppression-logic bug. Proven with
  a fixture TPM carrying zero rows in the other four sections and one
  outstanding AP change.
- **Uncapped.** The section carries no cap and no "+N more" line, matching
  the other four (2026-09-02 grill 6b: "your list is your list"). It was
  not made an exception, but it is also not the section that would ever
  drive a cap decision — it is per-owner *unacknowledged events*, which is
  inherently low-volume compared to a TPM's full project list (Kelly
  Kirby's 143-row TEST render is driven by Overdue/Incomplete, not by AP
  acks). **Coupling to record:** if a future cap is ever added to this
  digest for the other sections, the AP-ack section caps with it rather
  than being left as the one uncapped list — noted here so the decision is
  on record; no cap was built in this change.

## What did not change

- `send_tpm_digest.py` (Michele's daily) — untouched; it already got its
  Phase 3 section on 2026-09-14 in `0a5f948`.
- `send_pll_digest.py` — untouched.
- The Vercel-cron → GitHub-Actions bridge, the endpoint auth, the
  `tpm_individual_digest_state` watermark table, the Monday-only gate, or
  any env var. This is a content-only change to one script's rendering and
  one additional read-only fetch call in `main()`.
- `TPM_INDIVIDUAL_DIGEST_LIVE` — stays as-is (live, per Jim's 2026-09-14
  confirmation of Monday sends). Not touched by this change.

This does not reopen `ca9beaeb` (already resolved) — it is referenced here
as origin only.

## Verification

`ap_date_acks.py`'s query shape was not touched, so no live-data risk there.
What needed proving was specific to the new call site:

| claim | evidence |
|---|---|
| section renders correctly for a real owner, on live data | local `--dry-run` (Smart App Control disabled on HERMES 2026-09-14 — `import supabase` → `2.31.0`, see `tasks/lessons.md`); e.g. Tamara Biediger's render shows `AP-0379 — Develop GT TM I-IV Competencies`, `Nov 30 → Dec 30`, `+1 earlier change`, and the plain `https://orion.ofstraining.com/pc/projects` link |
| no token anywhere in the rendered link | regex-checked the HTML and text bodies for `/pc/projects?...token`; none found in either live or fixture renders |
| no-dedup: dual project (newly-assigned + unacked AP change) renders in both sections | fixture `build_digest` call — both `newly_assigned` and `ap_changes` non-empty for the same TPM; both headings present in the rendered HTML |
| suppression flips for an ack-only TPM | fixture TPM with empty `newly_assigned`/`overdue`/`incomplete`/`approaching` and one `ap_changes` entry — `build_digest` returns non-`None`; previously this TPM shape returned `None` |
| section fully absent (not empty-with-header) for a TPM with zero unacked changes | fixture TPM with other content but `ap_changes == []` — rendered HTML has no "Unacknowledged AP date changes" heading |
| live data: 7 of 9 TPMs currently carry outstanding AP acks, none live-suppression-boundary (all also have other content) | full live `--dry-run`; only the fixture cases above prove the suppression-flip scenario directly, since no live TPM is currently ack-only |

No live send took place — verification was `--dry-run` only, per the build
brief's instruction not to send live as part of verification.
