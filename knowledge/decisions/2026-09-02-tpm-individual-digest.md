# Individual TPM weekly P&C digest, and the Vercel-cron → GitHub-Actions bridge

**Date:** 2026-09-02
**Repos:** `hlc-scripts` (digest + runner workflow), `orion-pll` (scheduler + bridge endpoint)
**Origin:** ORiON feedback `76944b43` (Jim, relaying Michele's ask)
**Action item:** SAM COS `8627d13f`
**Commits:** `hlc-scripts` 5fcd8a9 · `orion-pll` 031a883

Two things ship here. The digest is mostly proven reuse. The **bridge is the
first of its kind in this estate** and is the part worth reading twice.

---

## Part 1 — The digest

Michele asked for individual TPM digests: each TPM gets their own weekly email
scoped to their own P&C projects, Mondays. This is the **third** digest:

| script | cadence | recipient | scope |
|---|---|---|---|
| `send_pll_digest.py` | daily | each of 7 PLLs | their own Delivery `action_items` |
| `send_tpm_digest.py` | daily | **Michele only** | all TPMs' P&C, grouped by owner |
| `send_tpm_individual_digest.py` | **weekly (Mon)** | **each TPM** | **their own P&C projects** |

The new script is a near-clone of `send_tpm_digest.py`'s data layer, re-scoped
from "all TPMs, to Michele" to "one TPM's own projects, to that TPM". The other
two scripts were not touched.

### Four sections, uncapped, no dedup

`Newly assigned to you this week` (`created_at > watermark`), `Overdue`,
`Missing required information` (shared `incomplete_pc_rows` view),
`Approaching stale` (shared `pc_projects_staleness` view). Both views are
**read-only consumers** — the completeness and staleness predicates are defined
once, in the views, and are never reimplemented in Python.

Three deliberate departures from Michele's digest, each of which will look like
a bug to someone who reads only one of the two scripts:

- **"Newly assigned", not "new".** P&C projects arrive via `sync_ap.py` from
  Smartsheet, not by human authorship in ORiON. A row appearing in the window
  means it *landed in the TPM's bucket*, not that anyone wrote it. Michele reads
  her version as intake; a TPM reads theirs as an inbox. The heading, the row
  meta ("Assigned <date>", not "Logged") and the intro all say assignment.
- **No dedup, no precedence.** A project appears in *every* section it qualifies
  for. A newly-assigned project that is also missing required fields shows under
  both, because each section answers its own question ("what's new for me?" vs
  "what do I have to fix?"). Proven in the gate: the fixture's dual project
  renders exactly twice.
- **Uncapped.** Michele's digest caps Overdue/Approaching/Incomplete at 5 per TPM
  because she reads nine TPMs' worth in one email. Each TPM here reads only their
  own list, so every section renders in full with no "+N more".

  *Consequence worth knowing before go-live:* this is not hypothetical tidiness.
  On live data Kelly Kirby's first render was 47 newly assigned + 7 overdue + 89
  missing-info = **143 rows in one email**. That is the accepted trade for "your
  list is your list", but if Jim wants a cap after seeing the TEST bundle, Kelly's
  email is the one that will motivate it.

### Suppression

A TPM with nothing in **any** of the four sections gets no email. This is a
**standing weekly status** email, not change-only: a TPM with nothing newly
assigned but one overdue project still gets theirs. On the live TEST run, 8 of 9
TPMs received a render and Charles Wall (0 active projects) was correctly
suppressed.

### Recipients — and the boundary

`portal_users where role='tpm'`, exactly 9, and only those. Verified 2026-09-02:
active P&C projects are owned by tpm (261), **pll (4)**, **viewer (1)**, and
**12 rows with `owner_id IS NULL`**. The 4+1 non-TPM owners get no individual
email by decision — Michele's all-TPM digest covers them. The 12 null-owner rows
were not in the brief's picture and are flagged separately: they have no owner to
scope to, so they reach neither this digest nor a per-TPM group in Michele's.

### Watermark and the missed-Monday self-heal

Own table, `tpm_individual_digest_state` (RLS on, no policies, service-role only),
mirroring `tpm_digest_state`'s shape. It **never** shares a watermark with the
other two digests.

First run ever falls back to **7 days**, not the PLL digest's 72 hours — a weekly
job with a 3-day first window silently under-reports by four days.

A skipped or failed run does not advance the watermark, so the next successful
send covers everything since the last *successful* send, possibly 14+ days.
Newly-assigned items fold forward rather than being lost. **Because that window
is variable, the copy must never say "the last 7 days"** — `window_phrase()` is
the single place that wording lives and it always renders "since your last digest
(<date>)".

### Monday-only, holiday or not

`ge_holidays.py` is deliberately **not imported**. The daily digests gate on
weekday *and* holiday because skipping one send of five costs a day. A weekly job
that skipped a holiday Monday would drop an entire week. The gate is Monday-only.

This is not academic: **2026-09-07 is Labor Day and is the next Monday.** The
daily digests skip it; this one sends. That is the intended behavior and the
reason the shared gate was not reused.

### Live flag

`TPM_INDIVIDUAL_DIGEST_LIVE`, its own variable. The script reads exactly three
env vars (`grep -n os.environ` returns three hits) and only one is a live flag —
`PLL_DIGEST_LIVE` and `TPM_DIGEST_LIVE` appear nowhere in an `os.environ` call, so
flipping either can never turn this digest live. Ships **TEST-default**: every
render goes to Jim with a `[TEST — would go to <TPM>]` prefix. Unlike Michele's
digest (TEST indefinitely by Jim's call), **this one is meant to go live** once
Jim reviews the bundle.

---

## Part 2 — The bridge (new pattern)

**Vercel cron → authenticated endpoint → GitHub `repository_dispatch` → Actions → Python.**

A Vercel cron cannot run a Python script that lives in another repo; it can only
hit an HTTP endpoint. So responsibilities split three ways:

| role | where |
|---|---|
| **Scheduler** | `orion-pll/vercel.json` cron `9 11 * * 1` |
| **Bridge** | `orion-pll/app/api/cron/tpm-individual-digest/route.ts` |
| **Runner** | `hlc-scripts/.github/workflows/tpm-individual-digest.yml` |

`9 11 * * 1` = **Mondays 11:09 UTC = 06:09 America/Chicago during CDT, 05:09
during CST.** Minute 9 keeps it clear of the other cron slots in the estate
(PLL :07, TPM :21, AP sync :13/:43, xyleme :17/:47). The Python job re-checks
"is it Monday in Chicago" itself, so a delayed or duplicated firing stays safe.

### Why Vercel schedules and GitHub only runs

GitHub Actions cron is the unreliable half on this account. Observed on the PLL
digest, whose schedule is `7 7 * * 1-5`: actual `schedule`-event firings at
**12:04, 12:31, 15:01, 18:14 and 19:23**. Vercel (Pro) is minute-accurate.

The runner workflow therefore carries **no `schedule:` trigger** — only
`repository_dispatch` (type `tpm-individual-digest`) and `workflow_dispatch` for
manual runs. Exactly one scheduler exists. Adding a GitHub schedule back "for
reliability" would recreate the PLL digest's double-fire (bug `645438e0`, retired
in `4c0a9b2`). If Vercel is ever retired as the scheduler, the schedule goes back
in **the same commit** that removes the Vercel cron — never both at once.

### Endpoint auth — non-negotiable

`middleware.ts` exempts `/api/*` from session auth, so this path is publicly
reachable. An unauthenticated dispatch endpoint would let anyone who found the
URL fire TPM emails on demand.

- Vercel sends `Authorization: Bearer $CRON_SECRET` on cron invocations.
- The route compares it with `crypto.timingSafeEqual` (length-guarded) and answers
  **401** to anything else, including a request with no header at all.
- It **fails closed**: a missing `CRON_SECRET` is 401, never an open door. This is
  the detail to preserve in any future copy of this pattern — the obvious
  `if (secret && auth !== secret)` shape fails *open* when the env var is absent.
- `CRON_SECRET` and `GITHUB_DISPATCH_TOKEN` are Vercel env vars; neither value
  appears in code.

Proven live against production: bare request → 401, wrong bearer → 401, on both
`orion.ofstraining.com` and `orion-pll.vercel.app`. POST → 405 (only GET is
exported).

### What is NOT yet proven — the one open seam

The two halves were verified independently:

- **endpoint half** — 401 rejection, live, both hostnames.
- **runner half** — a real `repository_dispatch` fired Actions run
  [33645631864](https://github.com/HLC-Training/hlc-scripts/actions/runs/33645631864),
  which ran the script and correctly no-op'd on the Monday gate.

The **authenticated end-to-end round-trip is unproven**, because it needs two
Vercel env vars that only Jim can set (there is no Vercel CLI or token on this
machine, and the Vercel MCP exposes no env-var tooling):

1. `CRON_SECRET` — any strong random string. Vercel then sends it automatically
   on cron invocations.
2. `GITHUB_DISPATCH_TOKEN` — a GitHub PAT scoped to **`contents: write` on
   `HLC-Training/hlc-scripts` only**, which is the minimum `repository_dispatch`
   accepts. Creating it is Jim's action; it must never be pasted into code.

Until both are set, the Monday cron will reach the endpoint and get a 401 (if
`CRON_SECRET` is unset) or a 500 (if the token is unset) — it fails safe in both
cases, but **no digest will fire**. Confirm the first Monday.

---

## Verification summary

All against live data or live infrastructure unless noted.

| claim | evidence |
|---|---|
| 9 `role='tpm'` recipients; non-TPM owners excluded | SQL: tpm 261 / pll 4 / viewer 1 / null-owner 12 active projects |
| per-TPM scoping, 4 sections, no dedup, uncapped, suppression, label, window copy | 21/21 in-memory assertions on synthesized rows (pure `build_digest`) |
| both shared views actually queried, owner-scoped | `fetch_incomplete` → `incomplete_pc_rows`; `fetch_approaching` → `pc_projects_staleness` |
| 7-day first-run fallback | state row `sent_at - watermark_used` = **7 days 00:00:02** |
| env-var isolation | `grep -n os.environ` → 3 hits, one live flag |
| TEST send + exactly one state row | run 33645847441, 8 emails to Jim, 1 suppressed; `count(*)` = 1, `test_mode=true` |
| Monday-only, no holiday skip | `--check-date` 2026-09-07 (Labor Day Monday) → send; 2026-09-08 → no send |
| endpoint 401 unauthenticated | live curl, both hostnames |
| dispatch → Actions run | run 33645631864, `event: repository_dispatch` |
| **authenticated round-trip** | **NOT PROVEN — blocked on Jim setting two Vercel env vars** |

## Go-live

Code is done. `TPM_INDIVIDUAL_DIGEST_LIVE` was deliberately **not** flipped.
Jim's sequence: review the 8 TEST emails → set the two Vercel env vars → set
repo variable `TPM_INDIVIDUAL_DIGEST_LIVE=true` → confirm the first real Monday
send. Same rollout posture as the PLL digest.

## Addendum 2026-09-02 — the round-trip was tested on a Wednesday

Jim ran the full authenticated round-trip same-day: curl → 200 `{"ok":true}` →
Actions run [33651386674](https://github.com/HLC-Training/hlc-scripts/actions/runs/33651386674)
(`repository_dispatch`) completed GREEN in 15s having sent **zero** emails and
written **zero** state rows. That is **correct, not a break** — 2026-09-02 is
a Wednesday, and this is a Monday-only job; `repository_dispatch` carries no
inputs, so it can never bypass the gate the way a manual `workflow_dispatch
--force-date-gate` run can. Logged and closed as bug `af3d6fb1` (not a bug).

The send path itself was proven separately via the earlier forced
`workflow_dispatch` run (`33645847441`, 15:01 UTC): 8 Resend sends + state row
`2a53f6cf-...` matching exactly. Both trigger types run the identical Python
step; the only difference is whether `FORCE_DATE_GATE` can be set, which only
`workflow_dispatch` allows.

One real gap this exposed: the no-send path exited before writing any state,
so a correct self-suppression and a swallowed failure looked identical from
`tpm_individual_digest_state` alone. `record_date_gate_suppression()`
(hlc-scripts `b0fb22b`) now writes a marker row on that path, and
`fetch_state()` skips marker rows so the watermark can't be corrupted by a
Wednesday smoke test. See `tasks/lessons.md` 2026-09-02 for the full
write-up. **The first real Monday send (2026-09-07 — Labor Day, which this
job does not skip) is still the confirmation that actually matters.**
