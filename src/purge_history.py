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
from typing import Dict, Any, List


SYNC_PATTERN = re.compile(r"^sync:\s*\d{4}-\d{2}-\d{2}", re.IGNORECASE)


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
    out = run_git(["log", "--format=%H %s"], cwd=cwd)
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 1)
        sha = parts[0]
        subj = parts[1] if len(parts) > 1 else ""
        commits.append((sha, subj))
    return commits


def purge_history(
    cwd: str | None = None,
    max_sync_commits: int = 14,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Purges older sync commits, keeping only the most recent max_sync_commits.
    Consolidates older sync commits into a single historical archive commit.
    """
    commits = get_commit_list(cwd=cwd)
    total_before = len(commits)

    # Commits ordered oldest to newest
    chronological = list(reversed(commits))

    sync_commits = [
        (sha, subj)
        for sha, subj in chronological
        if SYNC_PATTERN.match(subj)
    ]

    sync_count = len(sync_commits)
    if sync_count <= max_sync_commits:
        print(f"[OK] {sync_count} sync commits found (<= threshold {max_sync_commits}). No purge needed.")
        return {
            "total_before": total_before,
            "sync_before": sync_count,
            "purged": 0,
            "total_after": total_before,
        }

    to_purge = sync_commits[:-max_sync_commits]
    to_keep = sync_commits[-max_sync_commits:]
    purged_count = len(to_purge)

    print(f"Purging {purged_count} old sync commits (keeping latest {max_sync_commits})...")

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
    run_git(["rebase", "--onto", squash_sha, last_purged_sha, head_sha], cwd=cwd)

    if branch_name and branch_name != "HEAD":
        run_git(["checkout", "-B", branch_name], cwd=cwd)

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
        default=14,
        help="Maximum number of recent daily sync commits to retain (default: 14).",
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
    purge_history(
        cwd=args.repo_dir,
        max_sync_commits=args.max_sync_commits,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
