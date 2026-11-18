"""The hourly doctor: `swarm doctor`'s checks, run by the `swarm-doctor` Cloud
Run job, posting what they find to swarm-api (plan §1.5 DAYONE-DOCTOR-1; D27).

    python -m swarm_install.scheduled_doctor

terraform/infra/doctor.tf runs this in the swarm-verify image, as the
`swarm-doctor` service account, every hour, with `PROJECT_ID`, `REGION`,
`ENVIRONMENT`, `API_URL` and `API_AUDIENCE` set. It:

1. reads the install state (`gs://swarm-tfstate-<P>/swarm-install/state.json`)
   for the outputs some probes need (the Desktop client id); an unreadable
   state leaves those probes `unknown`, never skipped;
2. runs every manifest probe (`swarm_install.doctor.run`), the same code
   `swarm doctor` runs on a laptop;
3. posts the checks and the findings to swarm-api, `PUT /v1/admin/setup/health`
   (swarm-api admits the doctor's verified email there and on the groups
   probe, and nowhere else), which Admin › Setup › Health draws.

**It invokes swarm-api and nothing else.** Every other call is a read of a
Google API with the doctor's read-only roles; the one write anywhere is the
health document, and swarm-api writes it. It exits 0 once the report is
posted, whatever the report says (the alert on the finding count is the
signal, and the next hour's run is the retry); 1 when it could not post.

The job's token comes from the metadata server; nothing is printed that could
hold it. Standard library and the manifest model only: the image carries no
Google client library.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import doctor
from . import manifest as manifest_model
from .exitcodes import MET, ExitCode

#: The route swarm-api stores the report on (apps/swarm-api/swarm_api/routes/setup.py).
HEALTH_ROUTE = "/v1/admin/setup/health"
GROUPS_PROBE_ROUTE = "/v1/admin/setup/health/groups-probe"
#: The only (method, path) pairs this job sends to swarm-api.
ROUTES = frozenset({("PUT", HEALTH_ROUTE), ("POST", GROUPS_PROBE_ROUTE)})

#: The install state's object, and the bucket it lives in (swarm_install.state).
STATE_OBJECT = "swarm-install/state.json"

REQUIRED_ENV = ("PROJECT_ID", "REGION", "ENVIRONMENT", "API_URL", "API_AUDIENCE")

#: `send(method, url, bearer, body) -> (status, payload)`.
Send = Callable[[str, str, str, Mapping[str, Any] | None], tuple[int, Mapping[str, Any]]]


def _send(method: str, url: str, bearer: str, body: Mapping[str, Any] | None) -> tuple[int, Mapping[str, Any]]:
    return doctor._call(method, url, bearer, body)


@dataclass
class SwarmApi:
    """swarm-api at its run.app URL (internal ingress, reached through the VPC),
    with a Google ID token for its own audience: the job's one invocation."""

    base_url: str
    audience: str
    id_token: Callable[[str], str] = doctor.identity_token
    send: Send = _send

    def _call(self, method: str, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if (method, path) not in ROUTES:
            raise ValueError(f"{method} {path}: the scheduled doctor calls only {sorted(p for _, p in ROUTES)}")
        status, payload = self.send(method, self.base_url.rstrip("/") + path, self.id_token(self.audience), body)
        if not 200 <= status < 300:
            raise RuntimeError(f"swarm-api answered {status} to {method} {path}")
        return payload

    def groups_probe(self) -> Sequence[Mapping[str, Any]]:
        answer = self._call("POST", GROUPS_PROBE_ROUTE, {})
        return list(answer.get("groups") or answer.get("results") or ())

    def put_health(self, report: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("PUT", HEALTH_ROUTE, report)


def read_outputs(gcp: doctor.Gcp, project: str) -> tuple[dict[str, Any], str]:
    """The install state's outputs, and why they could not be read (else "")."""
    bucket = f"swarm-tfstate-{project}"
    from urllib.parse import quote

    try:
        state = gcp.get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(STATE_OBJECT, safe='')}?alt=media",
            permission="storage.objects.get",
            resource=f"//storage.googleapis.com/projects/_/buckets/{bucket}/objects/{STATE_OBJECT}",
        )
    except Exception as exc:  # noqa: BLE001 - the probes that need an output say unknown
        return {}, f"{type(exc).__name__}: {exc}"
    outputs = state.get("outputs")
    return (dict(outputs) if isinstance(outputs, Mapping) else {}), ""


def report(findings: Sequence[doctor.Finding], at: datetime) -> dict[str, Any]:
    return {
        "run_at": at.isoformat(),
        "checks": [f.to_json() for f in findings],
        "findings": [f.to_json() for f in findings if f.status != MET],
    }


#: The `event` doctor.tf's `doctor_findings` log metric filters on.
FINDING_EVENT = "doctor_finding"


def finding_lines(findings: Sequence[doctor.Finding]) -> list[dict[str, Any]]:
    """One log line per finding that is not met. The probe's `detail` stays
    out: it quotes API answers, and the console already shows it."""
    return [
        {
            "event": FINDING_EVENT,
            "entry": f.id,
            "state": f.status,
            "fix": f.fix or (f"run `{f.repair}`" if f.repair else "see Admin › Setup › Health"),
            "step": f.step,
            "repair": f.repair,
            "cause": f.cause,
        }
        for f in findings
        if f.status != MET
    ]


def main(
    env: Mapping[str, str] | None = None,
    *,
    gcp: doctor.Gcp | None = None,
    api: SwarmApi | None = None,
    kube: Callable[[Mapping[str, Any]], doctor.Kube] | None = None,
    manifest: manifest_model.Manifest | None = None,
    principal: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not str(env.get(name, "")).strip()]
    if missing:
        print(f"swarm-doctor: {', '.join(missing)} not set; terraform/infra/doctor.tf sets them",
              file=sys.stderr)
        return ExitCode.USAGE
    if gcp is None:
        rest = doctor.RestGcp()
        gcp = rest
        kube = kube if kube is not None else doctor.kube_for(rest._bearer)
    if api is None:
        api = SwarmApi(base_url=env["API_URL"], audience=env["API_AUDIENCE"])
    if manifest is None:
        manifest = manifest_model.load()
    outputs, unreadable = read_outputs(gcp, env["PROJECT_ID"])
    if unreadable:
        print(f"swarm-doctor: the install state could not be read ({unreadable}); "
              "the probes that need it report unknown", file=sys.stderr)
    target = doctor.Target(
        project=env["PROJECT_ID"],
        environment=env["ENVIRONMENT"],
        region=env["REGION"],
        outputs=outputs,
        principal=doctor.principal() if principal is None else principal,
    )
    findings = doctor.run(manifest, target, doctor.Deps(gcp=gcp, api=api, kube=kube))
    body = report(findings, now())
    # Before the post: the alert fires on these even when swarm-api cannot be reached.
    for line in finding_lines(findings):
        print(json.dumps(line), flush=True)
    try:
        api.put_health(body)
    except Exception as exc:  # noqa: BLE001 - said, and the next hour retries
        print(f"swarm-doctor: the report could not be posted to swarm-api ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return ExitCode.FAIL
    print(json.dumps({"checks": len(body["checks"]), "findings": len(body["findings"]),
                      "exit_code": int(doctor.exit_code(findings))}))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
