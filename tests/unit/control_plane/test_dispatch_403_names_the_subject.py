"""The 403 that means 404 must say WHERE it tried and WHO it was.

One error message produced four different root causes during the 2026-09-24 GKE
dispatch outage (docs/incidents/2026-09-24-gke-dispatch.md):

    jobs.batch is forbidden: User "117405034245659033603" cannot create
    resource "jobs" in API group "batch" in the namespace "swarm-tenant-eng"

* the namespace was spelled `swarm-eng` by the provisioner and
  `swarm-tenant-eng` by the dispatcher, so it did not exist;
* no dispatcher Role existed in it;
* no RoleBinding existed in it;
* and finally the RoleBinding existed and named the scheduler by its EMAIL,
  while GKE names a service account that authenticated with an OAuth access
  token by its numeric `uniqueId` -- so it applied cleanly and authorised nobody.

Kubernetes authorises before it resolves, so all four return this identical 403
and never a 404. Two facts separate them, and until 2026-09-24 the message
carried only the first: the NAMESPACE it tried and the IDENTITY it presented.
The upstream text supplies the numeric uniqueId; only this process knows which
email that is, and the distance between those two spellings is what hid the real
cause for two days.

These assertions are about the dispatch-failure MESSAGE, which is what reaches
`loop.py`'s `dispatch failed` log line -- the tenant gets `exc.code` only, so
nothing asserted here is served to a caller. The companion cases in
test_dispatch_manifests.py cover the 403/404 ambiguity wording itself; this file
covers the two facts and the identity resolution behind them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import google.auth._helpers as google_auth_helpers
import google.auth.credentials
import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from scheduler.dispatch import (
    UNNAMED_IDENTITY,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    authenticated_identity,
)

from .conftest import PROJECT, scheduler_settings

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

NAMESPACE = "swarm-tenant-eng"
SCHEDULER_SA = f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com"

#: The scheduler service account's real uniqueId, as GKE reported it on
#: 2026-09-24. Used verbatim so the fake 403 is the message that was measured
#: rather than one shaped to fit the assertions.
SCHEDULER_UID = "117405034245659033603"

#: Enough of a PEM to be written to disk. Nothing parses it and no client ever
#: opens a connection.
CA_PEM = b"-----BEGIN CERTIFICATE-----\nnot-a-real-ca\n-----END CERTIFICATE-----\n"


def make_tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace=NAMESPACE,
    )


def make_task() -> Task:
    profile = RUNNER_PROFILES["browser"]
    return Task(
        id="task_abc123",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile="browser",
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        timeout_seconds=1800,
    )


def make_lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id=task.tenant_id,
        generation=3,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )


class ForbiddenBatchApi:
    """The 403 exactly as it was measured, uniqueId and all.

    `.status` is what `kubernetes.client.ApiException` carries and the text is
    what the API server sent; both are supplied because the classifier reads the
    status first and falls back to the text.
    """

    def create_namespaced_job(self, namespace, body):
        error = Exception(
            "(403)\nReason: Forbidden\njobs.batch is forbidden: User "
            f'"{SCHEDULER_UID}" cannot create resource "jobs" in API group '
            f'"batch" in the namespace "{namespace}": requires one of '
            '["container.jobs.create"] permission(s) in Cloud IAM or a '
            'Kubernetes RBAC role with verb "create" for resource "jobs".'
        )
        error.status = 403
        raise error


class UnreachableBatchApi:
    def create_namespaced_job(self, namespace, body):
        raise Exception("HTTPSConnectionPool(host=...): Max retries exceeded")


def dispatch_failure(dispatcher: GkeJobDispatcher) -> DispatchError:
    task = make_task()
    with pytest.raises(DispatchError) as raised:
        dispatcher.dispatch(
            task=task,
            lease=make_lease(task),
            profile=RUNNER_PROFILES["browser"],
            tenant=make_tenant(),
        )
    return raised.value


# -- the message ------------------------------------------------------------

def test_a_403_names_the_namespace_it_tried_and_the_identity_it_presented():
    """Both facts, in the one line an operator actually reads.

    THE MUTATION THIS CATCHES: drop `as {identity}` from the 403 branch of
    `GkeJobDispatcher.dispatch`. The message keeps the namespace and the 403/404
    explanation -- everything test_dispatch_manifests.py asserts -- and still
    leaves the reader unable to tell a missing namespace from a RoleBinding that
    names the right account by the wrong one of its two names, because it never
    says which account was presented.
    """
    dispatcher = GkeJobDispatcher(
        scheduler_settings(),
        target=GkeTarget("https://k8s", "/ca.pem"),
        batch_api=ForbiddenBatchApi(),
        identity=SCHEDULER_SA,
    )

    failure = dispatch_failure(dispatcher)
    message = str(failure)

    # WHERE. Without it the reader cannot even go and look for the namespace.
    assert NAMESPACE in message
    # WHO. Without it the numeric subject in the upstream text below is a
    # 21-digit number with nothing to compare it to.
    assert SCHEDULER_SA in message
    # The upstream text is carried through, so the digits GKE resolved us to sit
    # in the same line as the email they belong to -- which is the comparison
    # nobody made for two days.
    assert SCHEDULER_UID in message
    # And the rule that connects them, stated: an email-only subject is a
    # RoleBinding that applies and authorises nobody.
    assert "BOTH by email and by numeric uniqueId" in message
    assert failure.code == "gke_create_job_forbidden"


def test_a_dispatch_failure_that_is_not_a_403_still_says_who_it_was():
    """A connection failure is not an authorisation failure, but WHO still helps.

    The 403/404 advice stays off this path -- attaching it to every failure would
    train people to ignore it -- while the identity stays on, because a
    connection refused as the wrong service account is a real and different
    problem from one refused as the right one.
    """
    dispatcher = GkeJobDispatcher(
        scheduler_settings(),
        target=GkeTarget("https://k8s", "/ca.pem"),
        batch_api=UnreachableBatchApi(),
        identity=SCHEDULER_SA,
    )

    failure = dispatch_failure(dispatcher)

    assert failure.code == "gke_create_job_failed"
    assert NAMESPACE in str(failure)
    assert SCHEDULER_SA in str(failure)
    assert "DOES NOT EXIST" not in str(failure)


def test_an_identity_that_could_not_be_resolved_is_said_so_and_never_invented():
    """The one thing worse than no identity is a plausible wrong one.

    A caller that supplies its own kubernetes client has taken over
    authentication, so this dispatcher cannot know who it authenticates as. The
    message says that rather than printing an empty string (which reads as a
    truncated message) or a derived `swarm-scheduler@<project>` address (which
    would send the reader to check a RoleBinding against an account that was
    never presented -- the same fabricated-default mistake `render.py` refuses
    for the numeric subject).

    THE MUTATION THIS CATCHES: make `presented_identity` return `self._identity`
    unchanged. The message then reads "... in swarm-tenant-eng as : (403) ..."
    and nothing says the identity is unknown rather than missing.
    """
    dispatcher = GkeJobDispatcher(
        scheduler_settings(),
        target=GkeTarget("https://k8s", "/ca.pem"),
        batch_api=ForbiddenBatchApi(),
    )

    message = str(dispatch_failure(dispatcher))

    assert UNNAMED_IDENTITY in message
    assert "gserviceaccount.com" not in message, (
        "no identity was resolved, so no address may appear in the message"
    )


# -- where the identity comes from -----------------------------------------

def test_authenticated_identity_refuses_the_unrefreshed_default():
    """`service_account_email` is the literal "default" until a refresh.

    Compute and Cloud Run credentials report it that way, which is why
    `_api()` reads it only after `install_google_bearer_token` has minted a
    token. Printing "default" as the subject to check a RoleBinding against
    would be worse than saying nothing.

    THE MUTATION THIS CATCHES: drop the `!= "default"` test (or the `"@"` test)
    from `authenticated_identity`.
    """

    class Credential:
        def __init__(self, email):
            self.service_account_email = email

    assert authenticated_identity(Credential(SCHEDULER_SA)) == SCHEDULER_SA
    assert authenticated_identity(Credential("default")) == ""
    assert authenticated_identity(Credential("")) == ""
    assert authenticated_identity(Credential(None)) == ""
    # A user credential from `gcloud auth application-default login` has no such
    # attribute at all, and must not raise out of a failure path.
    assert authenticated_identity(object()) == ""


def test_the_identity_in_the_message_is_the_one_the_client_authenticated_with(
    monkeypatch, tmp_path
):
    """The production path, offline: no injected identity anywhere.

    `GkeJobDispatcher` really calls `google.auth.default()`, really builds the
    kubernetes `Configuration`, really installs the per-request token hook and
    really reads the identity off the credentials that minted the token. Only the
    credential SOURCE is replaced, exactly as
    tests/unit/control_plane/test_gke_client_auth.py does it, so this runs with
    no cloud credentials and no metadata server.

    The Kubernetes client built that way cannot be made to return a 403 without
    a network, so the cached client is replaced afterwards with the recorded
    403 -- the same attribute the `batch_api` constructor argument sets. What
    this pins is the part a unit test can reach and the part that was missing:
    that the email in the message comes from the credential that signed the
    request, not from a constructor argument a test supplied.

    THE MUTATION THIS CATCHES: delete the
    `self._identity = authenticated_identity(credentials)` line in `_api()`.
    Every assertion in this file's other cases still passes, because they all
    inject the identity; this one fails, naming the address that went missing.
    """
    import google.auth
    import google.auth.transport.requests

    class MetadataCredentials(google.auth.credentials.Credentials):
        """What Cloud Run hands `google.auth.default()`, minus the network."""

        def __init__(self) -> None:
            super().__init__()
            # "default" until refreshed, which is the whole reason the identity
            # is read after the token is minted and not before.
            self.service_account_email = "default"

        def refresh(self, request: object) -> None:
            self.token = "access-token-1"
            # Relative to google.auth's own clock, not to a literal date: an
            # expiry pinned to a calendar day makes the test's outcome depend on
            # when it is run, which this repository has already been bitten by.
            self.expiry = google_auth_helpers.utcnow() + timedelta(minutes=35)
            self.service_account_email = SCHEDULER_SA

    credentials = MetadataCredentials()
    monkeypatch.setattr(
        google.auth, "default", lambda **kwargs: (credentials, PROJECT)
    )
    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )

    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(CA_PEM)
    dispatcher = GkeJobDispatcher(
        scheduler_settings(), target=GkeTarget("10.0.0.1", str(ca_file))
    )

    assert dispatcher.presented_identity() == UNNAMED_IDENTITY, (
        "nothing has authenticated yet, so there is no identity to claim"
    )

    dispatcher._api()

    assert dispatcher.presented_identity() == SCHEDULER_SA
    dispatcher._batch_api = ForbiddenBatchApi()
    assert SCHEDULER_SA in str(dispatch_failure(dispatcher))
