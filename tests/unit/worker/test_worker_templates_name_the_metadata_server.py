"""Both copies of the GKE worker Job name the metadata server by address.

`GkeJobDispatcher` sets GCE_METADATA_HOST and GCE_METADATA_IP on the GKE worker
container so google-auth never resolves `metadata.google.internal` (the reason,
and what it does NOT fix, is next to the value in
apps/scheduler/scheduler/dispatch.py; the dispatcher side is proven in
tests/unit/control_plane/test_gke_worker_metadata_env.py).

The YAML under kubernetes/worker-templates/ is the other copy of that Job: what
`render.py job` writes, what `make lint` validates, and what an operator applies
by hand during an incident. `_behaviour` in test_kubernetes_manifests.py leaves
the container environment out of its comparison on purpose, and
`_TEMPLATE_RELEVANT` there only covers names in the shared `worker_env` -- so
without this file a template could drop the override and every existing test
would stay green, and a pod started from it would take the DNS path the
dispatcher's pod no longer takes. That is the mirrored-copy shape
docs/mirrored-values.md records, so the copies are compared rather than trusted.

And the address is only useful if the tenant egress policy lets the pod reach
it on the port google-auth uses. That is asserted here too, against the rendered
policy, so a rule change that closed it would fail next to the variables that
depend on it.
"""

from __future__ import annotations

from typing import Any

from swarm_common.profiles import RUNNER_PROFILES

#: The two names google-auth reads for the metadata server's host.
METADATA_ENV = ("GCE_METADATA_HOST", "GCE_METADATA_IP")


def _worker_env(job: dict[str, Any]) -> dict[str, str]:
    """The `worker` container's env. An init container's is not the agent's."""
    containers = job["spec"]["template"]["spec"]["containers"]
    worker = next(c for c in containers if c["name"] == "worker")
    return {e["name"]: e.get("value") for e in worker.get("env") or []}


def _dispatched_values() -> dict[str, str]:
    from test_kubernetes_manifests import _dispatcher_job  # noqa: PLC0415

    env = _worker_env(_dispatcher_job("browser"))
    values = {name: env.get(name) for name in METADATA_ENV}
    # The anchor. If the dispatcher set neither, "the templates agree with it"
    # would pass on two copies that both omit the override.
    assert values == {name: "169.254.169.254" for name in METADATA_ENV}, (
        f"the GKE dispatcher sends {values}; see "
        f"tests/unit/control_plane/test_gke_worker_metadata_env.py"
    )
    return values


def test_every_worker_job_carries_the_dispatchers_metadata_address():
    """Rendered and dispatched, every profile, plus the gvisor shape.

    MUTATION: delete the two entries from any one of the three templates'
    `worker` container, or move them into v2's `install-credential` init
    container. This fails naming the Job.
    """
    from test_kubernetes_manifests import _every_worker_job  # noqa: PLC0415

    expected = _dispatched_values()
    jobs = _every_worker_job()
    disagree: dict[str, dict[str, Any]] = {}
    for where, job in jobs:
        env = _worker_env(job)
        got = {name: env.get(name) for name in METADATA_ENV}
        if got != expected:
            disagree[where] = got
    # Two producers for every profile, plus the gvisor template: all three YAML
    # files and the dispatcher were visited.
    assert len(jobs) == 2 * len(RUNNER_PROFILES) + 1, [where for where, _ in jobs]
    assert not disagree, (
        f"these worker Jobs do not name the metadata server the way the GKE "
        f"dispatcher does ({expected}): {disagree}. A pod started from one of them "
        f"resolves metadata.google.internal for its project id and every token, "
        f"which is the lookup the tenant egress policy dropped on swarm-autopilot "
        f"(task_1f699ef4cdbb4cc79c16)."
    )


def test_the_tenant_egress_policy_opens_that_address_on_port_80():
    """google-auth's metadata requests are plain HTTP to the host, so port 80.

    Deliberately narrow: this does not assert which OTHER ports the rule opens
    (988, and possibly 8080 per GKE's Dataplane V2 guidance), only that the one
    the override depends on is there.
    """
    from test_kubernetes_manifests import (  # noqa: PLC0415
        by_kind,
        documents,
        render,
        tenant_values,
    )

    address = _dispatched_values()["GCE_METADATA_HOST"]
    docs = documents(render.render_files(render.TENANT_FILES, tenant_values()))
    policies = [
        p
        for p in by_kind(docs, "NetworkPolicy")
        if p["metadata"]["name"] == "swarm-allow-worker-egress"
    ]
    assert len(policies) == 1, "expected exactly one swarm-allow-worker-egress policy"

    reaching = [
        rule
        for rule in policies[0]["spec"].get("egress") or []
        if any(
            peer.get("ipBlock", {}).get("cidr") == f"{address}/32"
            for peer in rule.get("to") or []
        )
    ]
    assert reaching, f"no egress rule names {address}/32"
    ports = {
        (p.get("protocol", "TCP"), p.get("port"))
        for rule in reaching
        for p in rule.get("ports") or []
    }
    assert ("TCP", 80) in ports, (
        f"the tenant egress policy reaches {address} only on {sorted(ports)}. The "
        f"GKE worker sends google-auth's project-id and token requests there on "
        f"TCP 80, so without it the override just moves the hang from DNS to the "
        f"metadata server itself."
    )
