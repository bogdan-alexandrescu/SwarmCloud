"""`GET /v1/runtimes` -- what a `runner_profile` NAME actually means.

Invariant 10 is that a caller picks a runner profile by name and supplies
nothing else. The invariant holds; what was missing is the other half of it.
A name is the caller's entire vocabulary, and `claude-code` on its own does not
say that it runs on Cloud Run Jobs, sizes to `standard` (4 vCPU / 8 GiB, with
the workspace carved out of that memory), times out at two hours, or takes a
subscription token INSTEAD of an API key rather than as well as one. Every one
of those facts existed only in a Python file in this repository, which the
person calling the API over HTTP does not have.

These tests pin the properties that make the route worth having:

  * it is a READ of the frozen catalogue and holds no copy of it -- proved by
    patching the frozen structures and watching the response move, which a
    hand-written table in the route could not do. `check-contract-parity.sh`
    catches that drift in shell and jq and does not cover this file, so the
    guarantee has to come from there being no literal here to drift;
  * it publishes secret NAMES and never a secret VALUE;
  * it distinguishes "any one of these credentials" from "all of these", which
    is the difference between a correct refusal and telling a tenant to buy
    metered API access they already have a subscription for;
  * it is authenticated like every other /v1 route, and is not admin-only --
    the person who needs to know what a runtime is, is the person submitting
    to it.
"""

from __future__ import annotations

from swarm_common.profiles import (
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    ResourceClass,
    RunnerProfile,
    resolve_backend,
)

from swarm_api.validation import known_providers

from .conftest import auth_header


def _runtimes(client, user: str = "alice") -> dict:
    response = client.get("/v1/runtimes", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["runtimes"]


def test_every_frozen_profile_is_published(client):
    # A profile the catalogue accepts and this route omits is a name a caller
    # can submit and cannot look up -- the exact gap the route exists to close.
    assert set(_runtimes(client)) == set(RUNNER_PROFILES)


def test_each_runtime_matches_the_frozen_catalogue_field_for_field(client):
    served = _runtimes(client)
    for name, profile in RUNNER_PROFILES.items():
        rc = RESOURCE_CLASSES[profile.resource_class]
        assert served[name] == {
            "name": profile.name,
            "image": profile.image,
            "backend": profile.backend.value,
            "resolved_backend": resolve_backend(profile).value,
            "provider": profile.provider,
            "secrets": list(profile.secrets),
            "secrets_any_of": profile.secrets_any_of,
            "timeout_seconds": profile.timeout_seconds,
            "resource_class": profile.resource_class,
            "resources": {
                "name": rc.name,
                "cpu": rc.cpu,
                "memory_gib": rc.memory_gib,
                "disk_gib": rc.disk_gib,
                "units": rc.units,
            },
            # Served for EVERY profile, including disabled ones. A caller
            # reading a task that names a disabled profile still needs its
            # shape to render that task.
            "available": profile.available,
            "disabled_reason": profile.disabled_reason,
        }, f"{name} drifted from the frozen catalogue"


def test_sizing_is_read_from_the_catalogue_not_copied_into_the_route(client, monkeypatch):
    """Resize a class in the frozen catalogue; the route must move with it.

    This is the test that a hand-written table of cpu/memory/disk in the route
    would fail. It is the only way to assert "no copy" from the outside, and
    `check-contract-parity.sh` does not cover Python route modules.
    """
    shrunk = ResourceClass("standard", cpu=1, memory_gib=2, disk_gib=1, units=7)
    monkeypatch.setitem(RESOURCE_CLASSES, "standard", shrunk)

    served = _runtimes(client)
    on_standard = [
        name
        for name, profile in RUNNER_PROFILES.items()
        if profile.resource_class == "standard"
    ]
    assert on_standard, "the catalogue no longer has a profile on `standard`; retarget this test"
    for name in on_standard:
        assert served[name]["resources"] == {
            "name": "standard",
            "cpu": 1,
            "memory_gib": 2,
            "disk_gib": 1,
            "units": 7,
        }, f"{name} served sizing that did not come from RESOURCE_CLASSES"


def test_a_profile_added_to_the_catalogue_appears_without_touching_the_route(client, monkeypatch):
    added = RunnerProfile(
        name="probe",
        image="agent-runtime-probe",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
        provider=None,
        timeout_seconds=111,
    )
    monkeypatch.setitem(RUNNER_PROFILES, "probe", added)

    served = _runtimes(client)
    assert served["probe"]["image"] == "agent-runtime-probe"
    assert served["probe"]["timeout_seconds"] == 111


def test_auto_backend_is_published_as_both_declared_and_resolved(client, monkeypatch):
    """The declared/resolved split answers two different questions.

    No profile in the catalogue is AUTO today, so nothing above exercises this.
    `Backend.AUTO` is a value the frozen enum permits, and a caller shown only
    "AUTO" learns nothing about where the task runs, while a caller shown only
    the resolved backend cannot tell that the platform -- not the profile --
    chose it.
    """
    auto = RunnerProfile(
        name="probe-auto",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.AUTO,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
    )
    monkeypatch.setitem(RUNNER_PROFILES, "probe-auto", auto)

    entry = _runtimes(client)["probe-auto"]
    assert entry["backend"] == Backend.AUTO.value
    assert entry["resolved_backend"] == resolve_backend(auto).value
    assert entry["resolved_backend"] != Backend.AUTO.value


def test_resolved_backend_is_never_auto_for_the_shipped_catalogue(client):
    served = _runtimes(client)
    for name in RUNNER_PROFILES:
        assert served[name]["resolved_backend"] in (
            Backend.CLOUD_RUN_JOB.value,
            Backend.GKE_AUTOPILOT.value,
        ), f"{name} resolved to something no dispatcher can route"


def test_interchangeable_credentials_are_labelled_as_interchangeable(client):
    """`secrets` without `secrets_any_of` is worse than no list at all.

    claude-code runs on EITHER metered API access or a Claude subscription
    token. A caller reading two names and assuming both are required would be
    told to buy access they already pay for -- the refusal the flag exists in
    the frozen catalogue to prevent.
    """
    served = _runtimes(client)
    for name, profile in RUNNER_PROFILES.items():
        assert served[name]["secrets_any_of"] is profile.secrets_any_of
        if served[name]["secrets_any_of"]:
            assert len(served[name]["secrets"]) > 1, (
                f"{name} says 'any one of these' about a single credential, "
                "which reads as optional and is not"
            )


def test_secret_names_are_published_and_secret_values_are_not(client, monkeypatch):
    """The route must read no environment, only the catalogue's NAMES.

    A runtime view is the natural place for someone to reach for the value
    next. Planting a sentinel in every variable the catalogue names proves the
    response carries the name and nothing behind it.
    """
    sentinels = []
    for profile in RUNNER_PROFILES.values():
        for secret_name in profile.secrets:
            sentinel = f"sentinel-{secret_name}-must-not-be-served"
            monkeypatch.setenv(secret_name, sentinel)
            sentinels.append(sentinel)
    assert sentinels, "the catalogue names no secrets; this test no longer proves anything"

    body = client.get("/v1/runtimes", headers=auth_header("alice")).text
    for sentinel in sentinels:
        assert sentinel not in body


def test_every_named_provider_is_one_a_tenant_can_register(client):
    # Otherwise the view says "this runtime needs `foo`" and no credential
    # route accepts `foo`, which is a dead end wearing an instruction.
    registrable = set(known_providers())
    for name, entry in _runtimes(client).items():
        if entry["provider"] is not None:
            assert entry["provider"] in registrable, f"{name} needs an unregistrable provider"


def test_a_runtime_that_needs_no_provider_says_so_with_null(client):
    served = _runtimes(client)
    without = [name for name, p in RUNNER_PROFILES.items() if p.provider is None]
    assert without, "the catalogue no longer has a provider-free runtime; retarget this test"
    for name in without:
        # Explicitly null, not omitted and not "": a missing key reads as
        # "unknown", and the fact here is "needs nothing", which is what makes
        # the runtime usable on a tenant that has registered no credential.
        assert served[name]["provider"] is None


def test_sizing_agrees_with_the_resource_classes_route(client):
    """The two published views of the same numbers must not disagree.

    /v1/resource-classes lists the classes; this lists which class each runtime
    resolves to and how big that is. A screen reading one and a screen reading
    the other would otherwise be able to show different ceilings for the same
    attempt.
    """
    classes = client.get("/v1/resource-classes", headers=auth_header("alice")).json()[
        "resource_classes"
    ]
    for name, entry in _runtimes(client).items():
        published = classes[entry["resource_class"]]
        assert entry["resources"] == published, f"{name} disagrees with /v1/resource-classes"


def test_requires_authentication_like_every_other_v1_route(client):
    assert client.get("/v1/runtimes").status_code == 401


def test_available_to_a_non_admin(client):
    # carol is in no registered group and gets a personal tenant. She submits
    # tasks by runner_profile name like anyone else, so she is exactly the
    # caller who needs to know what the names mean.
    assert client.get("/v1/runtimes", headers=auth_header("carol")).status_code == 200


def test_reads_nothing_from_firestore(client, db):
    # A catalogue route that touched the store would need a seeded tenant to
    # answer, and would fail for a caller whose first request is this one.
    db.docs.clear()
    assert client.get("/v1/runtimes", headers=auth_header("alice")).status_code == 200
