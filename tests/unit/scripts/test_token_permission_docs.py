"""forgeperms' required GitHub permissions include Actions: Read and write
(the owner, 2026-11-05: a workspace's GitHub token needs Actions: Read and
write).

The merge step re-runs the failed jobs of a run whose failure was a flake
(`GitHubWriter.rerun_failed_jobs`, `POST actions/runs/<run>/rerun-failed-jobs`),
and GitHub refuses that with 403 on a token holding Actions: Read only, so the
merge fails with `checks_failed`.
"""

from __future__ import annotations


def test_actions_requirement_is_read_and_write():
    # Imported here, not at module top: swarm_api pulls in the service stack,
    # and every collection of tests/unit/scripts would pay for it
    # (test_collection_stays_cheap).
    from swarm_api import forgeperms

    assert ("actions", "write") in {(r.permission, r.access) for r in forgeperms.REQUIRED}
