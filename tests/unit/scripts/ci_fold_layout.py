"""The one note that CI-FOLD (archived issue 1799) shipped its workflow change
by hand (part of issue 71).

Only `PATCH_NOTE` is kept: test_fork_prs_off_self_hosted.py and
test_accept_tenant_isolation_job.py, two of the CI security guards the owner
kept on 2026-11-25 (scripts/check_test_reads.py, SECURITY_GUARDS), append it to
their messages. The folded-job layout this module held pinned CI YAML and was
deleted with the other CI pins; this module reads nothing.
"""

from __future__ import annotations

PATCH_NOTE = ("-- the workflow change of CI-FOLD (#1799) is not applied: it ships as "
              "workflow-lines.patch, which the operator pushes by hand")
