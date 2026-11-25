"""The one note that CI-FOLD (archived issue 1799) shipped its workflow change
by hand (part of issue 71).

Only `PATCH_NOTE` is kept: test_fork_prs_off_self_hosted.py and
test_accept_tenant_isolation_job.py, two of the CI security guards the owner
kept on 2026-11-25 (scripts/check_test_reads.py, SECURITY_GUARDS), append it to
their messages. `FOLDED`, the folded-job layout as data, is kept for
test_ci_reuse.py's restored gate test (the name-keepers that must always run).
The helpers that read the workflows were deleted with the other CI pins; this
module reads nothing.
"""

from __future__ import annotations

PATCH_NOTE = ("-- the workflow change of CI-FOLD (#1799) is not applied: it ships as "
              "workflow-lines.patch, which the operator pushes by hand")


#: Per workflow: the job the sub-minute checks are folded into, and each old
#: job id kept as a name-keeper with the check name it reports.
FOLDED: dict[str, tuple[str, dict[str, str]]] = {
    "application.yml": ("quick-checks", {
        "workflows": "release workflow wiring (actionlint)",
        "ruff": "ruff check",
        "manifests": "kubernetes manifests",
        "python-checks": "lock / contract / format / bridge install",
    }),
    # `trivy (repo)` (`filesystem`) is not folded: it uploads SARIF, so it is
    # the one job that holds security-events: write, and the folded job runs
    # repository scripts (owner decision 2026-10-21 on PR 1813).
    "security.yml": ("checks", {
        "secrets": "secret scan",
        "policy": "platform policy assertions",
        # `deployment-values` is its own job, never folded: it holds the
        # pattern list, and the folded job runs the change's own scripts.
    }),
    "terraform.yml": ("offline", {
        "validate": "fmt / validate / tflint",
        "test": "terraform test",
    }),
}
