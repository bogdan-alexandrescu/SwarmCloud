"""The production credential path: Secret Manager and the secret's IAM policy.

`SecretManagerCredentials.put_credential` is the function invariant 9 rests on,
and it was the one meaningful coverage hole in the three control-plane apps:
every other credential test wires `InMemoryCredentials`, which records a binding
in a dict and performs no IAM work at all. A wrong role string, a policy set on
the PROJECT rather than on the secret resource, or a replace where a merge was
needed -- any of which makes one tenant's key readable by another tenant's
workload, or locks a tenant out of its own -- passed the whole suite green.

The fake client below returns REAL `google.iam.v1.policy_pb2.Policy` messages
and raises REAL `google.api_core` exceptions, so what is under test is the same
call shape the library would receive.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.api_core import exceptions as gexc
from google.iam.v1 import policy_pb2

from swarm_common.models import Tenant

from swarm_api.credentials import ACCESSOR_ROLE, SecretManagerCredentials
from swarm_api.errors import UpstreamUnavailable, ValidationFailed

from .conftest import PROJECT

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

#: What terraform puts on a tenant secret: the worker may read it, and the
#: platform admins may add new versions (the key rotation path).
WORKER_SA = f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
SECRET_ADMIN = "group:swarm-admins@saga.xyz"


def tenant(service_account: str | None = WORKER_SA) -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=service_account,
        namespace="swarm-tenant-eng",
    )


class FakeSecretManager:
    """Enough of SecretManagerServiceClient to hold a policy and a version."""

    def __init__(self, *, existing: dict[str, policy_pb2.Policy] | None = None) -> None:
        self.policies: dict[str, policy_pb2.Policy] = dict(existing or {})
        self.created: list[dict] = []
        self.versions: list[dict] = []
        self.policy_writes: list[tuple[str, policy_pb2.Policy]] = []
        self.abort_next_set = 0

    # -- secrets ----------------------------------------------------------

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.policies:
            raise gexc.AlreadyExists(name)
        self.created.append(dict(request))
        self.policies[name] = policy_pb2.Policy(etag=b"v1")
        return object()

    def add_secret_version(self, request):
        self.versions.append(dict(request))
        return type("Version", (), {"name": f"{request['parent']}/versions/1"})()

    # -- iam --------------------------------------------------------------

    def get_iam_policy(self, request):
        policy = policy_pb2.Policy()
        policy.CopyFrom(self.policies.get(request["resource"], policy_pb2.Policy(etag=b"v1")))
        return policy

    def set_iam_policy(self, request):
        resource, policy = request["resource"], request["policy"]
        if self.abort_next_set > 0:
            self.abort_next_set -= 1
            # Somebody else wrote first; the etag we hold is stale.
            current = self.policies[resource]
            current.etag = bytes(current.etag) + b"!"
            raise gexc.Aborted("the policy etag is no longer current")
        stored = self.policies.get(resource, policy_pb2.Policy())
        if bytes(policy.etag) != bytes(stored.etag):
            raise gexc.Aborted("the policy etag is no longer current")
        written = policy_pb2.Policy()
        written.CopyFrom(policy)
        written.etag = bytes(stored.etag) + b"+"
        self.policies[resource] = written
        self.policy_writes.append((resource, written))
        return written


def terraform_managed_policy() -> policy_pb2.Policy:
    """What terraform's secret_manager module leaves on the secret."""
    policy = policy_pb2.Policy(etag=b"tf-1")
    accessor = policy.bindings.add()
    accessor.role = ACCESSOR_ROLE
    accessor.members.append(f"serviceAccount:{WORKER_SA}")
    adder = policy.bindings.add()
    adder.role = "roles/secretmanager.secretVersionAdder"
    adder.members.append(SECRET_ADMIN)
    return policy


def writer(client: FakeSecretManager) -> SecretManagerCredentials:
    return SecretManagerCredentials(PROJECT, client=client)


# -- the secret itself -----------------------------------------------------

def test_the_key_is_written_to_the_tenants_own_secret():
    client = FakeSecretManager()
    result = writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    assert result.secret_id == "swarm-tenant-eng-anthropic"
    assert client.created[0]["secret_id"] == "swarm-tenant-eng-anthropic"
    assert client.created[0]["secret"]["labels"]["tenant"] == "eng"
    assert result.created is True
    assert client.versions[0]["parent"].endswith("/secrets/swarm-tenant-eng-anthropic")
    assert client.versions[0]["payload"].data == b"sk-ant-" + b"a" * 20


def test_the_response_carries_no_key_material():
    client = FakeSecretManager()
    secret = "sk-ant-" + "z" * 24
    result = writer(client).put_credential(tenant(), "anthropic", secret)
    payload = result.to_api()
    assert secret not in repr(payload)
    assert "length" not in repr(payload)


def test_re_registering_a_key_adds_a_version_and_does_not_fail():
    client = FakeSecretManager()
    creds = writer(client)
    creds.put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)
    second = creds.put_credential(tenant(), "anthropic", "sk-ant-" + "b" * 20)

    assert second.created is False, "rotation is the normal path, not an error"
    assert len(client.versions) == 2


# -- the IAM policy: the part invariant 9 rests on -------------------------

def test_the_policy_is_set_on_the_secret_resource_not_the_project():
    """A project-level grant would make every tenant's key readable by every
    tenant's workload."""
    client = FakeSecretManager()
    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    resource, _ = client.policy_writes[0]
    assert resource == f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    assert resource != f"projects/{PROJECT}"


def test_exactly_one_role_is_granted_and_it_is_the_accessor_role():
    client = FakeSecretManager()
    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    _, policy = client.policy_writes[0]
    granted = {binding.role for binding in policy.bindings}
    assert granted == {"roles/secretmanager.secretAccessor"}
    assert ACCESSOR_ROLE == "roles/secretmanager.secretAccessor"


def test_the_accessor_is_the_tenants_own_service_account():
    client = FakeSecretManager()
    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    _, policy = client.policy_writes[0]
    members = list(policy.bindings[0].members)
    assert members == [f"serviceAccount:{WORKER_SA}"]
    assert "swarm-agent-worker-research" not in repr(members)


def test_the_terraform_managed_bindings_survive_a_tenants_key_registration():
    """This endpoint is reachable by any authenticated tenant member.

    Replacing the policy wiped terraform's accessor grant for the real worker
    identity and the `secretVersionAdder` grant that is the admin rotation path
    -- a tenant-triggered denial of service on the tenant's own key, and a silent
    loss of the rotation path.
    """
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    client = FakeSecretManager(existing={secret: terraform_managed_policy()})

    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    final = client.policies[secret]
    by_role = {binding.role: list(binding.members) for binding in final.bindings}
    assert by_role["roles/secretmanager.secretVersionAdder"] == [SECRET_ADMIN]
    assert f"serviceAccount:{WORKER_SA}" in by_role[ACCESSOR_ROLE]


def test_an_already_correct_policy_is_not_rewritten_at_all():
    """A tenant-reachable endpoint should not rewrite a credential store's IAM
    policy on every call. The grant is already there on every rotation."""
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    client = FakeSecretManager(existing={secret: terraform_managed_policy()})

    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    assert client.policy_writes == [], "nothing to change, so nothing was written"
    assert len(client.versions) == 1, "the key itself was still stored"


def test_the_write_carries_the_etag_it_read():
    """Without an etag a read-modify-write silently clobbers a concurrent one."""
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    policy = policy_pb2.Policy(etag=b"tf-7")
    client = FakeSecretManager(existing={secret: policy})

    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    _, written = client.policy_writes[0]
    assert bytes(written.etag).startswith(b"tf-7")


def test_a_concurrent_policy_change_is_re_read_and_retried():
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    client = FakeSecretManager(existing={secret: policy_pb2.Policy(etag=b"tf-1")})
    client.abort_next_set = 1

    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    assert len(client.policy_writes) == 1
    members = list(client.policies[secret].bindings[0].members)
    assert members == [f"serviceAccount:{WORKER_SA}"]


def test_a_policy_that_will_not_settle_is_an_error_not_a_silent_skip():
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    client = FakeSecretManager(existing={secret: policy_pb2.Policy(etag=b"tf-1")})
    client.abort_next_set = 5

    with pytest.raises(UpstreamUnavailable):
        writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)
    assert client.versions == [], "no key is stored if it could not be bound"


def test_a_conditional_binding_for_the_same_role_is_left_alone():
    """Adding a member to a conditional binding would grant nothing, and editing
    somebody's condition is not this endpoint's business."""
    secret = f"projects/{PROJECT}/secrets/swarm-tenant-eng-anthropic"
    policy = policy_pb2.Policy(etag=b"tf-1")
    conditional = policy.bindings.add()
    conditional.role = ACCESSOR_ROLE
    conditional.members.append("serviceAccount:break-glass@example.iam.gserviceaccount.com")
    conditional.condition.expression = 'request.time < timestamp("2026-01-01T00:00:00Z")'
    client = FakeSecretManager(existing={secret: policy})

    writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)

    final = client.policies[secret]
    unconditional = [
        b for b in final.bindings if b.role == ACCESSOR_ROLE and not b.condition.expression
    ]
    assert len(unconditional) == 1
    assert list(unconditional[0].members) == [f"serviceAccount:{WORKER_SA}"]
    still_conditional = [b for b in final.bindings if b.condition.expression]
    assert len(still_conditional) == 1
    assert "break-glass" in repr(list(still_conditional[0].members))


# -- refusals --------------------------------------------------------------

def test_a_tenant_with_no_service_account_cannot_have_a_key_bound():
    client = FakeSecretManager()
    with pytest.raises(UpstreamUnavailable):
        writer(client).put_credential(tenant(None), "anthropic", "sk-ant-" + "a" * 20)
    assert client.created == [] and client.versions == []


def test_a_failed_secret_creation_never_reaches_the_iam_or_version_calls():
    class Broken(FakeSecretManager):
        def create_secret(self, request):
            raise gexc.PermissionDenied("caller lacks secretmanager.secrets.create")

    client = Broken()
    with pytest.raises(UpstreamUnavailable):
        writer(client).put_credential(tenant(), "anthropic", "sk-ant-" + "a" * 20)
    assert client.policy_writes == [] and client.versions == []


@pytest.mark.parametrize("bad", ["short", "x" * 9000, "line\nbreak-in-a-key-value"])
def test_key_material_is_validated_before_anything_is_created(bad):
    client = FakeSecretManager()
    with pytest.raises(ValidationFailed):
        writer(client).put_credential(tenant(), "anthropic", bad)
    assert client.created == []
