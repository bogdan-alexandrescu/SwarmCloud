#!/usr/bin/env python3
"""From a tenant's worker pod, Firestore refuses another tenant's documents (ISO-ACCEPT).

docs/design/tenant-settings-integrity.md §6, lane ISO-ACCEPT; issue #1457.
Every tenant worker's account holds `swarmTenantWorkerFirestore` across the
whole `swarm` database (terraform/bootstrap/platform_roles.tf, `worker_firestore`),
so an agent in tenant A's pod can mint its worker's token from the metadata
server and read or write any document whose id it can name. Design B moves the
worker's state onto attempt-signed swarm-api routes, and ISO-IAM then removes
the role. This probe is what says the role is gone: from inside a tenant-A
worker, with that worker's own ambient credential, it asks Firestore for

  settings  an update of `workspace_settings/<B>` -- the document #1457's
            attack rewrites to switch B's judgment backend;
  tenant    a get of `tenants/<B>`;
  lease     a create under `leases/` -- a forged lease is over-admission.

and passes only when each is answered 403 PERMISSION_DENIED. It is RED until
ISO-IAM's switch is flipped; that is its job, so it is SKIPPED unless asked
for (below), and the acceptance workflow does not ask today.

WHERE IT RUNS. In a worker pod, as a task's own code: the acceptance group
submits a `generic` task in the smoke tenant (tenant A) whose `pytest` command
names this file (`input.paths`). That is why it lives under fixtures/:
scripts/acceptance/sandbox-sync.sh copies fixtures/, and only fixtures/, to
the sandbox repository acceptance tasks clone. The generic runner runs it with
the image's /opt/venv python and pytest, in an environment it builds (nothing
of the task's input reaches it), so it reads its identity from the metadata
server exactly as an agent would. Standard library only otherwise: it also
runs as a program, `python3 <this file>`, from any shell inside such a pod.

OPT-IN, either of:
  * `SWARM_ACCEPT_TENANT_ISOLATION=1` in the environment;
  * pytest STARTED IN THIS FILE'S OWN DIRECTORY with this file named on its
    command line: what the acceptance group's generic task does
    (`working_directory` this directory, `input.paths` this file). The
    generic runner builds its child's environment and passes nothing of the
    task's input, so the variable cannot reach it; the directory can.
    Naming the file alone is not enough: the repository's own checks name
    every changed test file from the repository root, and a pod running them
    would otherwise measure (and fail on) the live database. A pytest over a
    directory that holds it does not opt in either.
Run as a program it is always explicit.

IT WRITES NOTHING, WHATEVER IAM ANSWERS. Today the role allows both writes, so
each is built so Firestore refuses it AFTER IAM has said yes:
  * the update carries an update-time precondition no document can have
    (NEVER_UPDATED), and its mask names only PROBE_FIELD, never a setting;
  * the lease create is one atomic commit with a second write whose
    precondition cannot hold (a document that must exist and does not), so
    neither lands. The worker's role has no delete permission, which is why
    the create cannot be "create, then clean up".
IAM is evaluated before any precondition, so a 403 means IAM refused, and a
404 or 400 FAILED_PRECONDITION means IAM allowed it and the guard stopped it
(FAIL, nothing written). An answer of 200 means a guard did not hold: the
probe says what it wrote, and removes the probe field where the role lets it.

B DEFAULTS TO A TENANT THAT DOES NOT EXIST (`iso-accept-absent-<hex>`). The
role is granted on the database; no IAM condition can name a document
(terraform/modules/tenancy/main.tf), so IAM answers the same for every id, and
a real tenant's documents are not needed to measure it. A get of an absent
document that IAM allows is a 404, which fails here as it should. Set
SWARM_ACCEPT_ISO_OTHER_TENANT (or `--other-tenant`) to probe a real B; the
update is still precondition-guarded and the get reads no field (its mask
names PROBE_FIELD only).

ONLY the `swarm` Firestore database of the project the metadata server names.
No GKE cluster, VPC, bucket or service account is read or named, so nothing on
the shared project's deny-list (scripts/lib/common.sh) is reachable from here.

THE TOKEN never leaves this process: not printed, not in a result, not in an
argv. Results print the HTTP status and Firestore's status and message only.

    python3 test_cross_tenant_firestore.py [--other-tenant ID] [--results FILE]

writes one JSON line per check, in CHECKS order,
`{"check": key, "verdict": "PASS"|"FAIL", "reason": "..."}`, and exits 1
unless every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import pytest
except ImportError:  # run as a program in an image with no pytest
    pytest = None  # type: ignore[assignment]

#: The checks, in the order they are reported.
CHECKS = ("settings", "tenant", "lease")

OPT_IN_ENV = "SWARM_ACCEPT_TENANT_ISOLATION"
OTHER_TENANT_ENV = "SWARM_ACCEPT_ISO_OTHER_TENANT"

#: The platform's database (swarm_common.config, FIRESTORE_DATABASE's default).
#: Fixed, not configurable: this probe has no business with any other database.
DATABASE = "swarm"
#: `swarm_common.identity.worker_service_account_id`'s prefix. Restated, not
#: imported: the pod's /opt/venv has swarm_common, a bare python3 does not.
WORKER_ACCOUNT_PREFIX = "swarm-agent-worker-"
TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

METADATA = "http://metadata.google.internal/computeMetadata/v1"
FIRESTORE = "https://firestore.googleapis.com/v1"
TIMEOUT_SECONDS = 20

#: The only field any probe names. Not a setting anything reads.
PROBE_FIELD = "iso_accept_probe"
#: An update-time precondition no document can satisfy: Firestore stamps
#: update times from its own clock, never this one.
NEVER_UPDATED = "1970-01-01T00:00:00.000001Z"

#: ErrorInfo reasons that come with a 403 but are not IAM refusing the
#: worker's account: the API off, a perimeter, a token's scopes, billing.
#: Each is a different fact from "the role is gone", so none is a PASS.
NOT_IAM_REASONS = frozenset({
    "SERVICE_DISABLED",
    "API_DISABLED",
    "SECURITY_POLICY_VIOLATED",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    "BILLING_DISABLED",
    "CONSUMER_INVALID",
})


@dataclass(frozen=True)
class Verdict:
    check: str
    denied: bool
    reason: str

    def as_line(self) -> dict[str, str]:
        return {"check": self.check, "verdict": "PASS" if self.denied else "FAIL",
                "reason": self.reason}


@dataclass(frozen=True)
class Identity:
    project: str
    email: str
    tenant: str
    #: The worker's access token. Never printed; `__repr__` leaves it out.
    bearer: str

    def __repr__(self) -> str:
        return f"Identity(project={self.project!r}, email={self.email!r}, tenant={self.tenant!r})"


class NotMeasurable(RuntimeError):
    """The probe could not ask the question at all (no metadata server, not a worker)."""


# --------------------------------------------------------------------------
# Who this pod is
# --------------------------------------------------------------------------

def _metadata(path: str) -> str:
    request = urllib.request.Request(f"{METADATA}/{path}", headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise NotMeasurable(
            f"no metadata server answered {path} ({exc.__class__.__name__}): this probe "
            "measures a worker pod's ambient credential and must run inside one"
        ) from None


def tenant_of(email: str) -> str:
    """The tenant a worker account serves, or NotMeasurable for any other account."""
    local = email.split("@", 1)[0]
    if not local.startswith(WORKER_ACCOUNT_PREFIX):
        raise NotMeasurable(
            f"this pod runs as {email!r}, not a tenant worker account "
            f"({WORKER_ACCOUNT_PREFIX}<tenant>): what it is allowed says nothing "
            "about a tenant worker"
        )
    tenant = local[len(WORKER_ACCOUNT_PREFIX):]
    if not TENANT_ID.match(tenant):
        raise NotMeasurable(f"{email!r} does not name a tenant id")
    return tenant


def identity() -> Identity:
    email = _metadata("instance/service-accounts/default/email").strip()
    tenant = tenant_of(email)
    project = _metadata("project/project-id").strip()
    try:
        bearer = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    except (ValueError, KeyError, TypeError):
        raise NotMeasurable("the metadata server's token answer carried no access token") from None
    return Identity(project=project, email=email, tenant=tenant, bearer=bearer)


def other_tenant(own: str, chosen: str | None) -> str:
    """B: the caller's choice, else a tenant id that does not exist. Never A."""
    other = chosen or f"iso-accept-absent-{secrets.token_hex(6)}"
    if not TENANT_ID.match(other):
        raise NotMeasurable(f"{other!r} is not a tenant id")
    if other == own:
        raise NotMeasurable(f"the other tenant must not be this pod's own ({own!r})")
    return other


# --------------------------------------------------------------------------
# Asking Firestore, and reading its answer
# --------------------------------------------------------------------------

def _call(method: str, url: str, bearer: str, body: dict[str, Any] | None = None
          ) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {bearer}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": {"status": "UNREACHABLE", "message": exc.__class__.__name__}}


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _error(payload: Any) -> dict[str, Any]:
    """The google.rpc.Status a REST error carries; Firestore sometimes wraps it in a list."""
    if isinstance(payload, list) and payload:
        payload = payload[0]
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def classify(check: str, status: int, payload: Any) -> Verdict:
    """PASS only for IAM's refusal: 403 PERMISSION_DENIED with no non-IAM reason."""
    error = _error(payload)
    code = str(error.get("status") or "")
    message = " ".join(str(error.get("message") or "").split())[:200]
    reasons = {
        str(detail.get("reason"))
        for detail in error.get("details") or []
        if isinstance(detail, dict) and detail.get("reason")
    }
    said = f"HTTP {status} {code or '-'}: {message or 'no message'}"
    if status == 403 and code == "PERMISSION_DENIED":
        other = sorted(reasons & NOT_IAM_REASONS)
        if other:
            return Verdict(check, False,
                           f"refused, but not by IAM ({', '.join(other)}), so whether the "
                           f"worker's role is gone was not measured; {said}")
        return Verdict(check, True, f"denied: {said}")
    if status == 0:
        return Verdict(check, False, f"not measured: Firestore unreachable; {said}")
    if status == 401:
        return Verdict(check, False,
                       f"not measured: the worker's own token was not accepted; {said}")
    if 200 <= status < 300:
        return Verdict(check, False, f"ALLOWED AND ANSWERED: {said}")
    return Verdict(check, False,
                   f"allowed by IAM: Firestore went past the permission check to "
                   f"{code or status}, so the worker still holds the role; {said}")


@dataclass(frozen=True)
class Prober:
    who: Identity
    other: str

    @property
    def root(self) -> str:
        return f"projects/{self.who.project}/databases/{DATABASE}/documents"

    def _url(self, suffix: str, query: list[tuple[str, str]] | None = None) -> str:
        url = f"{FIRESTORE}/{self.root}{suffix}"
        return f"{url}?{urllib.parse.urlencode(query)}" if query else url

    def _marker(self) -> dict[str, Any]:
        return {PROBE_FIELD: {"stringValue": f"iso-accept from {self.who.tenant}"}}

    def settings(self) -> Verdict:
        path = f"/workspace_settings/{self.other}"
        query = [("updateMask.fieldPaths", PROBE_FIELD),
                 ("currentDocument.updateTime", NEVER_UPDATED)]
        status, payload = _call("PATCH", self._url(path, query), self.who.bearer,
                                {"fields": self._marker()})
        verdict = classify("settings", status, payload)
        if 200 <= status < 300:
            # The guard did not hold: take the probe field back out. The document
            # itself, if the write created it, stays: the role cannot delete.
            back, _ = _call("PATCH", self._url(path, [("updateMask.fieldPaths", PROBE_FIELD)]),
                            self.who.bearer, {"fields": {}})
            verdict = Verdict("settings", False,
                              f"{verdict.reason}; WROTE {PROBE_FIELD} on "
                              f"workspace_settings/{self.other} (removal answered HTTP {back}; "
                              "if the document did not exist before, delete it by hand)")
        return verdict

    def tenant(self) -> Verdict:
        # The mask names a field no tenant document has, so an allowed read
        # brings no tenant's data into this process.
        status, payload = _call("GET", self._url(f"/tenants/{self.other}",
                                                 [("mask.fieldPaths", PROBE_FIELD)]),
                                self.who.bearer)
        return classify("tenant", status, payload)

    def lease(self) -> Verdict:
        lease_id = f"iso-accept-{secrets.token_hex(8)}"
        create = f"{self.root}/leases/{lease_id}"
        guard = f"{self.root}/leases/{lease_id}-guard"
        body = {"writes": [
            {"update": {"name": create, "fields": self._marker()},
             "currentDocument": {"exists": False}},
            # Must exist, and was never created: the commit, both writes, fails.
            {"update": {"name": guard, "fields": self._marker()},
             "currentDocument": {"exists": True}},
        ]}
        status, payload = _call("POST", f"{FIRESTORE}/{self.root}:commit", self.who.bearer, body)
        verdict = classify("lease", status, payload)
        if 200 <= status < 300:
            verdict = Verdict("lease", False,
                              f"{verdict.reason}; WROTE leases/{lease_id} and "
                              f"leases/{lease_id}-guard, which the role cannot delete: "
                              "delete both by hand")
        return verdict

    def run(self, check: str) -> Verdict:
        return getattr(self, check)()


# --------------------------------------------------------------------------
# As a program
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--other-tenant", default=os.environ.get(OTHER_TENANT_ENV) or None)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args(argv)
    try:
        who = identity()
        prober = Prober(who=who, other=other_tenant(who.tenant, args.other_tenant))
        verdicts = [prober.run(check) for check in CHECKS]
    except NotMeasurable as exc:
        verdicts = [Verdict(check, False, f"not measured: {exc}") for check in CHECKS]
    lines = [json.dumps(v.as_line(), sort_keys=True) for v in verdicts]
    if args.results:
        args.results.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    for line in lines:
        print(line)
    return 0 if all(v.denied for v in verdicts) else 1


# --------------------------------------------------------------------------
# As a pytest module (the generic runner's `pytest`, in the pod)
# --------------------------------------------------------------------------

def opted_in(environ: Mapping[str, str], args: list[str], cwd: Path) -> bool:
    """The env switch, or pytest started in this file's directory naming this file."""
    if environ.get(OPT_IN_ENV) == "1":
        return True
    here = Path(__file__).resolve()
    if cwd.resolve() != here.parent:
        return False
    for arg in args:
        named = Path(arg.split("::", 1)[0])
        if (named if named.is_absolute() else cwd / named).resolve() == here:
            return True
    return False


if pytest is not None:

    @pytest.fixture(scope="module")
    def prober(request: Any) -> Prober:
        config = request.config
        if not opted_in(os.environ, list(config.args), Path(config.invocation_params.dir)):
            pytest.skip(
                f"tenant-isolation probe: set {OPT_IN_ENV}=1, or run pytest in this file's "
                "directory naming this file. It is red until ISO-IAM removes the worker's Firestore role "
                "(docs/design/tenant-settings-integrity.md §6)"
            )
        try:
            who = identity()
            return Prober(who=who, other=other_tenant(who.tenant, os.environ.get(OTHER_TENANT_ENV)))
        except NotMeasurable as exc:
            pytest.fail(f"not measured: {exc}")

    def test_update_of_another_tenants_workspace_settings_is_denied(prober: Prober) -> None:
        verdict = prober.run("settings")
        assert verdict.denied, f"workspace_settings/{prober.other}: {verdict.reason}"

    def test_get_of_another_tenants_tenant_document_is_denied(prober: Prober) -> None:
        verdict = prober.run("tenant")
        assert verdict.denied, f"tenants/{prober.other}: {verdict.reason}"

    def test_create_under_leases_is_denied(prober: Prober) -> None:
        verdict = prober.run("lease")
        assert verdict.denied, f"leases/: {verdict.reason}"


if __name__ == "__main__":
    sys.exit(main())
