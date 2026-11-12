"""
Automated History Purge for Trendshift Pipeline
Maintains a bounded git history for automated sync commits so that the
git packfile does not bloat indefinitely over time.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from typing import Dict, Any, List, Optional


SYNC_DAILY_PATTERN = re.compile(r"^sync:\s*\d{4}-\d{2}-\d{2}", re.IGNORECASE)
ARCHIVE_PATTERN = re.compile(r"^sync:\s*historical archive", re.IGNORECASE)


def run_git(args: List[str], cwd: str | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def get_commit_list(cwd: str | None = None) -> List[tuple[str, str]]:
    """Returns list of (sha, subject) from newest to oldest."""
    return [(sha, subj) for sha, subj, _ in get_commit_list_with_dates(cwd=cwd)]


def get_commit_list_with_dates(cwd: str | None = None) -> List[tuple[str, str, int]]:
    """Returns list of (sha, subject, author_epoch) from newest to oldest.

    Author date is used (not committer date) because `git rebase` rewrites the
    committer date of every replayed commit, which would make all commits look
    brand new after each purge.
    """
    out = run_git(["log", "--format=%H %at %s"], cwd=cwd)
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 2)
        sha = parts[0]
        ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        subj = parts[2] if len(parts) > 2 else ""
        commits.append((sha, subj, ts))
    return commits


def purge_history(
    cwd: str | None = None,
    max_sync_commits: Optional[int] = None,
    dry_run: bool = False,
    target_branch: str = "main",
    max_age_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Purges old sync commits. A sync commit is kept only while it is newer than
    max_age_days (if given) AND among the most recent max_sync_commits (if
    given). Consolidates purged sync commits and previous archive commits into
    a single historical archive commit so git history does not grow forever.
    """
    if max_sync_commits is None and max_age_days is None:
        raise ValueError("Specify max_sync_commits and/or max_age_days.")

    dated = get_commit_list_with_dates(cwd=cwd)
    commits = [(sha, subj) for sha, subj, _ in dated]
    total_before = len(commits)
    ts_by_sha = {sha: ts for sha, _, ts in dated}
    cutoff = None
    if max_age_days is not None:
        cutoff = (now if now is not None else time.time()) - max_age_days * 86400

    # Commits ordered oldest to newest
    chronological = list(reversed(commits))

    sync_commits = [
        (sha, subj)
        for sha, subj in chronological
        if SYNC_DAILY_PATTERN.match(subj)
    ]

    sync_count = len(sync_commits)

    # Index (chronological) of the newest sync commit that must be purged.
    last_purge_idx = -1
    if max_sync_commits is not None and sync_count > max_sync_commits:
        last_purge_idx = sync_count - max_sync_commits - 1
    if cutoff is not None:
        for idx, (sha, _) in enumerate(sync_commits):
            if ts_by_sha[sha] < cutoff:
                last_purge_idx = max(last_purge_idx, idx)

    if last_purge_idx < 0:
        print(f"[OK] {sync_count} sync commits found, none beyond retention. No purge needed.")
        return {
            "total_before": total_before,
            "sync_before": sync_count,
            "purged": 0,
            "total_after": total_before,
        }

    boundary_sha = sync_commits[last_purge_idx][0]
    sync_and_archive = [
        (sha, subj)
        for sha, subj in chronological
        if SYNC_DAILY_PATTERN.match(subj) or ARCHIVE_PATTERN.match(subj)
    ]
    boundary_pos = next(i for i, (sha, _) in enumerate(sync_and_archive) if sha == boundary_sha)
    to_purge = sync_and_archive[: boundary_pos + 1]
    purged_count = len(to_purge)

    print(f"Purging {purged_count} old sync/archive commits (kept {sync_count - last_purge_idx - 1} recent syncs)...")

    if dry_run:
        print(f"[DRY-RUN] Would squash {purged_count} commits up to {to_purge[-1][0][:7]}.")
        return {
            "total_before": total_before,
            "sync_before": sync_count,
            "purged": purged_count,
            "total_after": total_before - purged_count + 1,
        }

    first_purged_sha = to_purge[0][0]
    last_purged_sha = to_purge[-1][0]

    # Find parent of the first purged commit
    try:
        parent_sha = run_git(["rev-parse", f"{first_purged_sha}^"], cwd=cwd)
        parent_args = ["-p", parent_sha]
    except subprocess.CalledProcessError:
        parent_args = []

    # Tree at the last purged commit
    tree_sha = run_git(["rev-parse", f"{last_purged_sha}^{{tree}}"], cwd=cwd)

    # Create squashed archive commit
    commit_msg = f"sync: historical archive (squashed up to {to_purge[-1][1]})"
    commit_tree_cmd = ["commit-tree", tree_sha, *parent_args, "-m", commit_msg]
    squash_sha = run_git(commit_tree_cmd, cwd=cwd)

    # Save current branch name if attached
    try:
        branch_name = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    except Exception:
        branch_name = None

    # Rebase remaining commits on top of squash commit
    head_sha = commits[0][0]
    try:
        run_git(["rebase", "--onto", squash_sha, last_purged_sha, head_sha], cwd=cwd)
    except Exception as e:
        try:
            run_git(["rebase", "--abort"], cwd=cwd)
        except Exception:
            pass
        raise RuntimeError(f"Git rebase failed during history purge: {e}") from e

    # Update target branch reference to ensure local branch points to rebased HEAD even if detached
    new_head = run_git(["rev-parse", "HEAD"], cwd=cwd)
    branch_to_update = branch_name if (branch_name and branch_name != "HEAD") else target_branch
    try:
        run_git(["update-ref", f"refs/heads/{branch_to_update}", new_head], cwd=cwd)
        run_git(["checkout", branch_to_update], cwd=cwd)
    except Exception as e:
        print(f"[WARN] Failed to switch/update branch {branch_to_update}: {e}", file=sys.stderr)

    # Expire reflog and prune packfile bloat
    run_git(["reflog", "expire", "--expire=now", "--all"], cwd=cwd)
    run_git(["gc", "--prune=now"], cwd=cwd)

    total_after = len(get_commit_list(cwd=cwd))
    print(f"[OK] History purged successfully. Commits: {total_before} -> {total_after}.")

    return {
        "total_before": total_before,
        "sync_before": sync_count,
        "purged": purged_count,
        "total_after": total_after,
    }


def main():
    parser = argparse.ArgumentParser(description="Purge old sync commits to prevent git packfile bloat.")
    parser.add_argument(
        "--max-sync-commits",
        type=int,
        default=None,
        help="Optional cap on the number of recent daily sync commits to retain.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Squash sync commits older than this many days (default: 30 when no limit is given).",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default="main",
        help="Branch ref to update (default: main).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate purge without rewriting git history.",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=".",
        help="Path to repository root (default: current directory).",
    )

    args = parser.parse_args()
    max_age_days = args.max_age_days
    if max_age_days is None and args.max_sync_commits is None:
        max_age_days = 30
    purge_history(
        max_age_days=max_age_days,
        cwd=args.repo_dir,
        max_sync_commits=args.max_sync_commits,
        dry_run=args.dry_run,
        target_branch=args.branch,
    )



if __name__ == "__main__":
    main()
