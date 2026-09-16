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
