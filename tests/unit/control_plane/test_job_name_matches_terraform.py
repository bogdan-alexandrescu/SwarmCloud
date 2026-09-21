"""The dispatcher's Cloud Run Job id must match the name terraform creates.

terraform/infra/locals.tf names each job
`${name_prefix}-job-${tenant_id}-${profile_name}`. The dispatcher built
`swarm-<tenant>-<profile>`, omitting the "job" segment, so every dispatch
returned 404 and the dispatcher then created its OWN job under the wrong name.

That second failure is the dangerous one: a dispatcher-created job carries no
`managed-by=swarm-terraform` label, and scripts/destroy.sh ABORTS rather than
delete an unlabelled resource -- so teardown would have refused to run.
"""

from __future__ import annotations

import pytest

from scheduler.dispatch import job_id_for

NAME_PREFIX = "swarm"


def terraform_job_name(tenant_id: str, profile_name: str) -> str:
    """Mirror of terraform/infra/locals.tf `job_matrix.key`."""
    return f"{NAME_PREFIX}-job-{tenant_id}-{profile_name}"


@pytest.mark.parametrize(
    "tenant,profile",
    [("eng", "mock"), ("eng", "claude-code"), ("smoke", "generic"), ("u-bogdan", "mock")],
)
def test_dispatcher_job_id_equals_the_terraform_name(tenant, profile):
    assert job_id_for(tenant, profile) == terraform_job_name(tenant, profile)


def test_a_smaller_resource_class_gets_its_own_job_but_keeps_the_prefix():
    """A workflow step naming a smaller class needs a distinct Job, because
    Cloud Run pins CPU/memory/disk on the Job and cannot override per execution.
    It must still sit in the same managed namespace."""
    name = job_id_for("eng", "claude-code", "browser")
    assert name.startswith(f"{NAME_PREFIX}-job-eng-claude-code")
    assert name != terraform_job_name("eng", "claude-code")


# -- one escaping rule, not four -------------------------------------------

def test_every_name_sanitiser_shares_one_character_class() -> None:
    """`[^a-z0-9-]+` is declared once and imported, never re-typed.

    Four modules need it: the frozen `identity._TENANT_SAFE` that defines it,
    the dispatcher that builds a Cloud Run / Kubernetes name from a Firestore
    id, the reconciler that reverse-maps a label back to that id, and
    `kubernetes/render.py`. The reconciler's `sanitised()` only round-trips
    while its rule and the dispatcher's are the same rule -- diverge them and
    the reconciler either terminates a live execution it read as orphaned or
    never notices a genuinely orphaned one. Identity (`is`) rather than pattern
    equality, because two equal-but-separate objects are exactly the state this
    asserts against. docs/audits/2026-09-18/08-frozen-contract-restatements.md,
    finding 1.
    """
    from reconciler import detect
    from scheduler import dispatch
    from swarm_common import identity

    assert dispatch._NAME_SAFE is identity._TENANT_SAFE
    assert detect._NAME_SAFE is identity._TENANT_SAFE


def test_the_dispatcher_and_the_reconciler_round_trip_awkward_ids() -> None:
    """The property the shared rule exists for, exercised rather than assumed."""
    from reconciler.detect import sanitised
    from scheduler.dispatch import sanitize_name

    for value in ("task_9f3a", "att_00FF", "u-bogdan", "Tenant_With.Dots", "a__b"):
        assert sanitised(value) == sanitize_name(value), value
