#!/usr/bin/env python3
"""
sync_repo_docs.py
─────────────────────────────────────────────────────────────────
Full reconcile of one repo's chat-readable reasoning docs into SAM COS
Supabase `repo_docs`. The same script runs identically in samcos, orion,
orion-pll, and hlc-scripts — each repo's GitHub Actions workflow invokes it
against its own checkout. Every run is a complete reconcile (upsert every
matched file's current content, delete rows for anything no longer
matched), so a backfill run and an incremental run are the same operation —
there is no separate backfill code path to drift from this one.

Scope: reasoning (decisions, lessons, conventions, gotchas) — the things a
Claude Code session can't rediscover by reading the file, because the file
looks fine. Deliberately excludes schema (query information_schema
directly) and source code (Claude Code reads it natively on checkout).
See knowledge/decisions/2026-07-31-repo-docs-sync.md.

Schedule: GitHub Actions, on push to main + workflow_dispatch (backfill).
─────────────────────────────────────────────────────────────────
"""

import argparse
import glob
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SYNC_PATHS = [
    "CLAUDE.md",
    "tasks/lessons.md",
    "knowledge/**/*.md",
    "docs/architecture*.md",
]

MAX_BYTES = 500_000


def find_docs(root: Path) -> dict:
    """Return {repo-relative doc_name: absolute Path} for every matched file."""
    matched = {}
    for pattern in SYNC_PATHS:
        for hit in glob.glob(str(root / pattern), recursive=True):
            path = Path(hit)
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            matched[rel] = path
    return matched


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_sync_meta(client, repo: str, root: Path) -> None:
    """Hash this script and its workflow as they exist on disk in this checkout,
    and upsert one row into repo_sync_meta. Reports what is actually running,
    same principle as commit_sha — never fails the doc sync it rides along with.
    """
    try:
        script_sha = sha256_file(Path(__file__).resolve())
        workflow_sha = sha256_file(root / ".github" / "workflows" / "sync-repo-docs.yml")
        client.table("repo_sync_meta").upsert({
            "repo": repo,
            "script_sha": script_sha,
            "workflow_sha": workflow_sha,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="repo").execute()
    except Exception as exc:
        print(f"::warning::Failed to record repo_sync_meta for {repo}: {exc}")


def build_rows(matched: dict, repo: str, commit_sha: str):
    rows = []
    skipped = []
    # updated_at is set explicitly on every row (not left to the column's
    # DEFAULT now()) — bug d2215a97: an upsert's ON CONFLICT DO UPDATE does
    # not re-invoke a column default unless the column is named in the
    # payload, so content/commit_sha were changing on update while
    # updated_at stayed frozen at the original insert time.
    synced_at = datetime.now(timezone.utc).isoformat()
    for doc_name, path in sorted(matched.items()):
        size = path.stat().st_size
        if size > MAX_BYTES:
            skipped.append((doc_name, size))
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        rows.append({
            "repo": repo,
            "doc_name": doc_name,
            "content": content,
            "commit_sha": commit_sha,
            "updated_at": synced_at,
        })
    return rows, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Print matched/skipped files only; no Supabase calls.")
    args = parser.parse_args()

    root = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    github_sha = os.environ.get("GITHUB_SHA", "")

    matched = find_docs(root)

    if args.dry_run:
        repo = github_repository.split("/", 1)[-1] or "(unset)"
        rows, skipped = build_rows(matched, repo, github_sha or "(unset)")
        print(f"[dry-run] root={root}")
        print(f"[dry-run] would sync {len(rows)} file(s):")
        for row in rows:
            print(f"  {row['doc_name']} ({len(row['content'])} chars)")
        if skipped:
            print(f"[dry-run] would skip {len(skipped)} file(s) over {MAX_BYTES} bytes:")
            for name, size in skipped:
                print(f"  {name} ({size} bytes)")
        return

    supabase_url = os.environ.get("SAMCOS_SUPABASE_URL", "")
    supabase_key = os.environ.get("SAMCOS_SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not supabase_key:
        raise SystemExit("ERROR: SAMCOS_SUPABASE_URL / SAMCOS_SUPABASE_SERVICE_ROLE_KEY not set.")
    if not github_sha:
        raise SystemExit("ERROR: GITHUB_SHA not set — commit_sha is mandatory on every row.")
    if "/" not in github_repository:
        raise SystemExit("ERROR: GITHUB_REPOSITORY not set (expected 'owner/repo').")

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)

    repo = github_repository.split("/", 1)[1]
    rows, skipped = build_rows(matched, repo, github_sha)

    if skipped:
        print(f"::warning::Skipped {len(skipped)} file(s) over {MAX_BYTES} bytes:")
        for name, size in skipped:
            print(f"::warning::  {name} ({size} bytes)")

    failed = False

    if rows:
        try:
            client.table("repo_docs").upsert(rows, on_conflict="repo,doc_name").execute()
        except Exception as exc:
            print(f"::error::Upsert failed for {repo}: {exc}")
            failed = True

    # Reconcile deletions: any row already in repo_docs for this repo whose
    # doc_name wasn't matched this run has been renamed or deleted upstream.
    try:
        existing = client.table("repo_docs").select("doc_name").eq("repo", repo).execute()
        existing_names = {r["doc_name"] for r in existing.data}
    except Exception as exc:
        print(f"::error::Failed to read existing rows for {repo}: {exc}")
        sys.exit(1)

    stale = existing_names - set(matched.keys())
    if stale:
        try:
            client.table("repo_docs").delete().eq("repo", repo).in_("doc_name", sorted(stale)).execute()
        except Exception as exc:
            print(f"::error::Delete failed for {repo}: {exc}")
            failed = True

    print(f"{repo}: {len(rows)} synced, {len(stale)} deleted, {len(skipped)} skipped, commit {github_sha[:12]}")

    record_sync_meta(client, repo, root)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
