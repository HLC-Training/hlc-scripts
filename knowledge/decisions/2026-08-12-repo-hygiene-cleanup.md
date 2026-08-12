# Repo hygiene cleanup: hlc-scripts + orion

**Decided:** 2026-08-12
**Repos:** hlc-scripts, orion
**Action item:** 5fc16de8-312e-4dd3-9ba5-06d45d81598a

Desk-wrap verification surfaced dirty/untracked state and stray branches in
both repos, none of it from that night's shipped work. Investigated each
item before touching it, per the brief's rule: a branch only gets deleted
once its content is confirmed merged or superseded, never on the assumption
that an old-looking branch is safe.

## hlc-scripts

**`.claude/` dirty state.** Nothing under it was ever tracked — just Claude
Code's own local artifacts (`launch.json`, `scheduled_tasks.lock`,
`settings.local.json`). Added `.claude/` to `.gitignore`
([.gitignore](../../.gitignore)).

While investigating, found the untracked `.claude/` also contained a broken
worktree checkout at `.claude/worktrees/fervent-villani-328800/` — a full
Next.js app checkout whose `.git` pointed at
`C:/Users/rosen/Documents/orion-portal/.git/worktrees/fervent-villani-328800`,
a path that no longer exists on this machine. Not part of hlc-scripts's own
history; almost certainly a stray isolated-worktree artifact from a past
`orion` session. It disappeared on its own partway through this session
(most likely automatic stale-worktree cleanup) before any manual removal was
needed. The matching local branch `claude/fervent-villani-328800` still
existed in `orion` — see below.

**Stray branch `claude/pll-digest-cron-shift-i1204x`.** One commit
(`6f0fe01`, "Shift PLL digest cron from 11:07 UTC to 07:07 UTC"),
content-identical (same file diffs, same Claude session ID) to main's
`4a4b185`, the squash-merged version via PR #1. Fully superseded — deleted
from origin.

## orion

**Dirty `orion/` path.** Not a nested clone or build artifact — a single
misplaced file, `orion/knowledge/decisions/2026-08-05-harness-random-executive-uuid.md`.
The shared decisions table's `doc_path` convention prefixes entries with the
repo name (`orion/knowledge/decisions/...`), but that prefix isn't part of
the in-repo path (files live at `knowledge/decisions/...` relative to repo
root — confirmed against hlc-scripts's own decision docs, which follow the
same convention). Someone wrote the file using the doc_path literally as a
filesystem path from inside the orion repo, creating a redundant `orion/`
subdirectory, and the doc was never actually committed at its real path.
Moved to `knowledge/decisions/2026-08-05-harness-random-executive-uuid.md`
and committed; the now-empty `orion/` directory removed.

**Untracked `scripts/rls-harness/verify-uuid-randomization.mjs`.** Read in
full: a genuine, reusable verification script for decision `7bf68884`
(random executive UUID per RLS harness run) — calls `resolvePersonas()`
twice and asserts the two UUIDs differ, no hardcoded fixture values. Its
`./personas.js` import resolves against `personas.ts` the same way the
harness's own `tsx`-run scripts do (`package.json`'s `rls:check` runs
`index.ts` the same way), consistent with the decision doc's claim that it
was run live. Committed alongside the relocated decision doc, one commit
covering both since they're artifacts of the same session gap.

**Four stray remote branches.**

- `authority-model-split` — 0 commits ahead of main. Fully merged. Deleted
  (remote + local).
- `claude/great-clarke-85a775` — 0 commits ahead of main. Fully merged.
  Deleted (remote + local).
- `claude/orion-viewers-all-modules-readonly-a70682` — 1 commit (`d94db81`),
  content-identical to main's `34b2cf6` ("rls: viewers are all-modules
  read-only (7f0a19e9)", squash-merged via PR #1). Deleted (remote only; no
  local copy existed).
- `feat/resources-needed` — **kept, not deleted.** One commit (`8bdf130`),
  commit message explicit: "DO NOT MERGE until Michele confirms the option
  list - the branch exists so the merge is one click when she does." Adds a
  Resources Needed dropdown across the P&C create/edit/review flow with a
  placeholder option list gated on Michele's sign-off; the `resources_needed`
  column is already live and idle in the DB. This is real, deliberately-held
  work, not abandoned. Left as-is.

**Bonus: local branch `claude/fervent-villani-328800`.** Surfaced while
tracing the stray hlc-scripts worktree above. 0 commits ahead of main.
Deleted locally (no remote copy existed).

## Result

Both repos' `git status --short` clean. `feat/resources-needed` is the one
deliberate exception — real gated work, not touched.
