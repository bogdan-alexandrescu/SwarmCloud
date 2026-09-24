"""`scripts/smoke-test.sh`, driven end to end, as it runs inside the VPC.

WHY THIS FILE EXISTS
--------------------
Release 36038727721 ran the smoke suite inside the VPC for the first time
(`swarm-verify`, execution swarm-verify-m9prt). Nine of eleven cases passed: a
mock task was submitted, dispatched, SUCCEEDED, released its lease and left its
artifacts under the tenant's prefix. The other two failed with one cause:

    FAIL could not read the runner-profile catalogue; this suite cannot know
         which backends exist
    FAIL the backend matrix visited 0 backends; nothing below proves any
         dispatch path works

The backend matrix imported `apps/common/swarm_common` with python3, and the
image the suite runs in -- images/swarm-verify, alpine with bash, curl and jq --
has no python on purpose and carries no apps/ directory. It now reads the
deployed API's catalogue, GET /v1/runtimes, with jq.

The same run exposed the next problem before it could bite. The owner has
PAUSED browser admission (pools/resource:browser at hard_limit 0) until the GKE
fixes are proven, so the moment the matrix worked it would have submitted a
browser task into a closed pool and waited out its whole --timeout, then failed
the platform for obeying its operator. A paused row is now SKIPPED: printed,
counted as a skip and not a pass, and leaving the exit status alone -- unless
EVERY backend was skipped, which proves nothing and fails.

HOW
---
Exactly the environment of the in-VPC gate: CLOUD_RUN_JOB is set, so every
credential comes from the metadata server, and the metadata server, the
Firestore REST API, the Cloud Run Admin API, GCS and swarm-api are all one fake
`curl` on PATH. `python3` and `python` on PATH are TRIPWIRES that record any
call and fail, because the image has neither -- a suite that reaches for one
here is a suite that cannot run where it is deployed. `gcloud` is a tripwire
too: the image has no Cloud SDK.

The catalogue the fake serves is built by the production route itself
(`swarm_api.routes.platform.runtimes`), so it cannot drift from what the
deployed API answers.

WHAT IT CANNOT PROVE: that the real Firestore and the real API serve the shapes
below. The catalogue is the route's own output; the pool, task, event and lease
documents are hand-built in the Firestore REST encoding that common.sh's FS_JQ
decodes, the same way tests/integration/fake_platform.py builds them.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "smoke-test.sh"

PROJECT = "swarm-test-project"
REGION = "us-central1"
DATABASE = "swarm"
API_URL = "https://swarm-api.fake.invalid"
IDENTITY = f"swarm-verify@{PROJECT}.iam.gserviceaccount.com"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="smoke-test.sh needs bash and jq",
)


#: The platform, in bash and jq -- the only tools the verify image has, which
#: is also why the tripwires below can shadow python without breaking the fake.
#:
#: It answers the invocation shapes common.sh actually uses:
#:
#:   fs_request        -K - -X M [--data-binary B] -o FILE -w '%{http_code}'
#:   api_request       -K - -X M [--data-binary B]         -w $'\n%{http_code}'
#:   readyz probe      -K - -o /dev/null -w '%{http_code}'
#:   metadata, Cloud Run Admin, GCS, fs_database_exists     (body on stdout)
FAKE_CURL = r"""#!/usr/bin/env bash
set -uo pipefail

out=""; wfmt=""; method=""; url=""; data=""; from_stdin=0; prev=""
for arg in "$@"; do
  case "${prev}" in
    -o)            out="${arg}" ;;
    -w)            wfmt="${arg}" ;;
    -X)            method="${arg}" ;;
    --data-binary) data="${arg}" ;;
    -K)            from_stdin=1 ;;
  esac
  case "${arg}" in http://*|https://*) url="${arg}" ;; esac
  prev="${arg}"
done
# The Authorization header arrives on stdin as a curl config; drain it so the
# writing side of the pipe never blocks.
[[ "${from_stdin}" -eq 1 ]] && cat >/dev/null 2>&1
[[ -n "${method}" ]] || { if [[ -n "${data}" ]]; then method=POST; else method=GET; fi; }

printf '%s %s\n' "${method}" "${url}" >>"${FAKE_LOG}"

FS="projects/${FAKE_PROJECT}/databases/${FAKE_DATABASE}/documents"
status=200
body='{}'

doc() {  # doc PATH FIELDS_JSON
  jq -nc --arg n "${FS}/$1" --argjson f "$2" '{name: $n, fields: $f}'
}

case "${url}" in
  http://metadata.google.internal/*/token*)
    body='{"access_token":"fake-access-token","expires_in":3600}' ;;
  http://metadata.google.internal/*/identity*)
    body='fake-id-token' ;;
  http://metadata.google.internal/*/email*)
    body="${FAKE_IDENTITY}" ;;

  "https://firestore.googleapis.com/v1/projects/${FAKE_PROJECT}/databases/${FAKE_DATABASE}")
    body="$(jq -nc --arg n "projects/${FAKE_PROJECT}/databases/${FAKE_DATABASE}" '{name: $n}')" ;;
  *:runAggregationQuery)
    body='[{"result":{"aggregateFields":{"n":{"integerValue":"0"}}},"readTime":"2026-09-24T00:00:00Z"}]' ;;
  *:runQuery)
    body='[{"readTime":"2026-09-24T00:00:00Z"}]' ;;

  */documents/pools/*)
    pool="${url##*/documents/pools/}"; pool="${pool%%\?*}"
    if [[ -n "${FAKE_POOL_READ_STATUS:-}" ]]; then
      status="${FAKE_POOL_READ_STATUS}"
      body='{"error":{"code":500,"message":"backend unavailable","status":"UNAVAILABLE"}}'
    elif [[ -f "${FAKE_POOLS}/${pool}.json" ]]; then
      body="$(doc "pools/${pool}" "$(cat "${FAKE_POOLS}/${pool}.json")")"
    else
      status=404
      body='{"error":{"code":404,"message":"no such document","status":"NOT_FOUND"}}'
    fi
    ;;
  */documents/tasks/*/events*)
    rest="${url##*/documents/tasks/}"; id="${rest%%/*}"
    body="$(jq -nc --arg fs "${FS}" --arg id "${id}" '
      {documents: ([ ["submitted", null], ["lease_acquired", "lease-\($id)"],
                     ["dispatched", "lease-\($id)"], ["succeeded", null] ]
                   | to_entries | map(
         {name: "\($fs)/tasks/\($id)/events/e\(.key)",
          fields: ({type: {stringValue: .value[0]}}
                   + (if .value[1] then {lease_id: {stringValue: .value[1]}} else {} end))}))}')" ;;
  */documents/tasks/*)
    id="${url##*/documents/tasks/}"; id="${id%%\?*}"
    body="$(doc "tasks/${id}" "$(jq -nc '{state: {stringValue: "SUCCEEDED"},
      tenant_id: {stringValue: "eng"}, resource_class: {stringValue: "standard"},
      attempt_count: {integerValue: "1"}, last_error: {nullValue: null}}')")" ;;
  */documents/leases/*)
    id="${url##*/documents/leases/}"; id="${id%%\?*}"
    body="$(doc "leases/${id}" "$(jq -nc '{released_at: {timestampValue: "2026-09-24T00:00:10Z"}}')")" ;;

  https://run.googleapis.com/v2/*/services/*)
    svc="${url##*/services/}"
    body="$(jq -nc --arg u "https://${svc}.fake.run.app" \
      '{uri: $u, terminalCondition: {state: "CONDITION_SUCCEEDED"}}')" ;;
  https://storage.googleapis.com/*)
    body='{"items":[{"name":"stdout.log"},{"name":"stderr.log"}]}' ;;

  "${FAKE_API}/readyz")
    body='' ;;
  "${FAKE_API}/v1/tenants/me")
    body="$(jq -nc --arg e "${FAKE_IDENTITY}" '{tenant: {tenant_id: "eng"}, principal: {email: $e}}')" ;;
  "${FAKE_API}/v1/runtimes")
    status="${FAKE_RUNTIMES_STATUS:-200}"
    if [[ "${status}" == 2* ]]; then body="$(cat "${FAKE_RUNTIMES}")"
    else body='{"code":"unavailable","message":"the fake refused the catalogue"}'; fi ;;
  "${FAKE_API}/v1/tasks")
    profile="$(jq -r '.runner_profile // ""' <<<"${data}")"
    if jq -e 'has("image") or has("command")' >/dev/null <<<"${data}"; then
      status=422; body='{"code":"validation_failed","message":"forbidden field"}'
    elif ! jq -e --arg p "${profile}" '.runtimes | has($p)' >/dev/null <"${FAKE_RUNTIMES}"; then
      status=422; body='{"code":"validation_failed","message":"unknown runner_profile"}'
    else
      n=$(( $(wc -l <"${FAKE_SUBMITTED}") + 1 ))
      printf '%s\n' "${profile}" >>"${FAKE_SUBMITTED}"
      status=201; body="$(jq -nc --arg id "task-${profile}-${n}" '{id: $id}')"
    fi ;;
  "${FAKE_API}"/v1/tasks/*/cancel)
    body='{"cancel_requested": true}' ;;
  "${FAKE_API}"/v1/tasks/*)
    id="${url##*/v1/tasks/}"
    body="$(jq -nc --arg id "${id}" '{id: $id, state: "SUCCEEDED", tenant_id: "eng"}')" ;;
  *)
    printf 'UNHANDLED %s %s\n' "${method}" "${url}" >>"${FAKE_LOG}"
    status=500; body='{"error":{"code":500,"message":"the fake platform has no route for this"}}' ;;
esac

if [[ -n "${out}" ]]; then printf '%s' "${body}" >"${out}"; else printf '%s' "${body}"; fi
[[ -n "${wfmt}" ]] && printf '%s' "${wfmt//%\{http_code\}/${status}}"
exit 0
"""

#: Anything the verify image does not have. A call is recorded, then refused.
TRIPWIRE = r"""#!/usr/bin/env bash
printf '%s %s\n' "$(basename "$0")" "$*" >>"${FAKE_TRIPWIRE}"
echo "$(basename "$0"): not available in images/swarm-verify" >&2
exit 127
"""


def _catalogue() -> dict:
    """GET /v1/runtimes, as the production route builds it."""
    from swarm_api.routes.platform import runtimes

    return runtimes(auth=None)


def _run(tmp: Path, *, pools: dict | None = None, catalogue: dict | None = None,
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_CURL)
    for name in ("python3", "python", "gcloud"):
        (bin_dir / name).write_text(TRIPWIRE)
    for path in bin_dir.iterdir():
        path.chmod(0o755)

    pools_dir = tmp / "pools"
    pools_dir.mkdir(exist_ok=True)
    for name, fields in (pools or {}).items():
        (pools_dir / f"{name}.json").write_text(json.dumps(fields))

    runtimes = tmp / "runtimes.json"
    runtimes.write_text(json.dumps(catalogue if catalogue is not None else _catalogue()))

    (tmp / "tmp").mkdir(exist_ok=True)
    for name in ("requests.log", "submitted.log", "tripwire.log"):
        (tmp / name).write_text("")

    # common.sh refuses to source a group- or world-writable env file.
    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        f"REGION={REGION}\n"
        "ENVIRONMENT=dev\n"
        f"FIRESTORE_DATABASE={DATABASE}\n"
        f"ARTIFACT_BUCKET=swarm-artifacts-{PROJECT}\n"
        f"API_URL={API_URL}\n"
    )
    env_file.chmod(0o600)

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "SWARM_ENV_FILE": str(env_file),
        "NO_COLOR": "1",
        # The in-VPC branch: credentials from the metadata server, no gcloud.
        "CLOUD_RUN_JOB": "swarm-verify",
        # A front door that is not API_URL, so api_credential sends the ID
        # token and nothing consults terraform or the tfvars for a hostname.
        "API_HOST": "front-door.fake.invalid",
        "TMPDIR": str(tmp / "tmp"),
        "SWARM_POLL_INTERVAL_SECONDS": "0",
        "FAKE_PROJECT": PROJECT,
        "FAKE_DATABASE": DATABASE,
        "FAKE_API": API_URL,
        "FAKE_IDENTITY": IDENTITY,
        "FAKE_POOLS": str(pools_dir),
        "FAKE_RUNTIMES": str(runtimes),
        "FAKE_LOG": str(tmp / "requests.log"),
        "FAKE_SUBMITTED": str(tmp / "submitted.log"),
        "FAKE_TRIPWIRE": str(tmp / "tripwire.log"),
    })
    for key in ("SWARM_ID_TOKEN", "SWARM_IMPERSONATE_SA", "API_AUDIENCE",
                "SUITE_SKIPS_ARE_FAILURES", "K_SERVICE"):
        env.pop(key, None)
    env.update(extra_env or {})

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--timeout", "20"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )
    proc.transcript = proc.stdout + proc.stderr  # type: ignore[attr-defined]
    proc.requests = (tmp / "requests.log").read_text()  # type: ignore[attr-defined]
    proc.submitted = (tmp / "submitted.log").read_text().split()  # type: ignore[attr-defined]
    proc.tripwire = (tmp / "tripwire.log").read_text()  # type: ignore[attr-defined]
    return proc


def _explain(proc) -> str:
    return (
        f"exit {proc.returncode}\n--- transcript ---\n{proc.transcript[-6000:]}"
        f"\n--- requests ---\n{proc.requests[-3000:]}\n--- tripwire ---\n{proc.tripwire}"
    )


def _lines(proc, marker: str) -> list[str]:
    return [line.strip() for line in proc.transcript.splitlines() if line.strip().startswith(marker)]


CLOSED = {"hard_limit": {"integerValue": "0"}, "active": {"integerValue": "0"},
          "enabled": {"booleanValue": True}}
DISABLED = {"hard_limit": {"integerValue": "10"}, "active": {"integerValue": "0"},
            "enabled": {"booleanValue": False}}
OPEN = {"hard_limit": {"integerValue": "10"}, "active": {"integerValue": "0"},
        "enabled": {"booleanValue": True}}


def test_both_backends_are_exercised_from_the_served_catalogue(tmp_path):
    """The m9prt run, with the matrix working: no python, both backends, exit 0."""
    proc = _run(tmp_path, pools={"resource:standard": OPEN, "resource:browser": OPEN})
    assert proc.tripwire == "", (
        "the suite called a tool the verify image does not have:\n" + _explain(proc)
    )
    assert f"GET {API_URL}/v1/runtimes" in proc.requests, _explain(proc)
    assert "browser" in proc.submitted, (
        "no browser task was submitted: GKE_AUTOPILOT went unexercised\n" + _explain(proc)
    )
    # Twice: the matrix row, and the deep lifecycle case that follows it.
    assert proc.submitted.count("mock") == 2, _explain(proc)
    assert "claude-code" not in proc.submitted, (
        "CLOUD_RUN_JOB was covered by a profile the verify tenant holds no credential for\n"
        + _explain(proc)
    )
    assert any("2 of 2 backend(s) exercised" in line for line in _lines(proc, "PASS")), _explain(proc)
    assert _lines(proc, "FAIL") == [], _explain(proc)
    assert proc.returncode == 0, _explain(proc)


@pytest.mark.parametrize("closed", [CLOSED, DISABLED], ids=["hard_limit-0", "enabled-false"])
def test_a_paused_resource_pool_row_is_skipped_not_failed(tmp_path, closed):
    """pools/resource:browser closed by the owner: SKIP, not submitted, exit 0."""
    proc = _run(tmp_path, pools={"resource:standard": OPEN, "resource:browser": closed})
    assert proc.tripwire == "", _explain(proc)
    assert "browser" not in proc.submitted, (
        "a browser task was submitted into a pool an operator closed; it would sit "
        "QUEUED for the whole --timeout\n" + _explain(proc)
    )
    skips = [line for line in _lines(proc, "SKIP") if "pools/resource:browser" in line]
    assert skips and all("paused by operator" in line for line in skips), _explain(proc)
    # Counted as a skip in the summary, and never as a pass.
    assert "NOT MEASURED" in proc.transcript, _explain(proc)
    assert not any("GKE_AUTOPILOT" in line for line in _lines(proc, "PASS")), _explain(proc)
    assert any("1 of 2 backend(s) exercised" in line for line in _lines(proc, "PASS")), _explain(proc)
    # The other backend still ran, all the way through.
    assert proc.submitted.count("mock") == 2, _explain(proc)
    assert _lines(proc, "FAIL") == [], _explain(proc)
    assert proc.returncode == 0, _explain(proc)


def test_an_absent_pool_is_open_not_paused(tmp_path):
    """No document is unlimited by construction; it must not read as a pause."""
    proc = _run(tmp_path, pools={})
    assert "browser" in proc.submitted, _explain(proc)
    assert not any("paused" in line for line in _lines(proc, "SKIP")), _explain(proc)
    assert proc.returncode == 0, _explain(proc)


def test_every_backend_paused_fails_the_suite(tmp_path):
    """A run made only of skips proves no dispatch path and must not be green."""
    proc = _run(tmp_path, pools={"resource:standard": CLOSED, "resource:browser": CLOSED})
    assert proc.submitted == [], (
        "a task was submitted into a closed pool\n" + _explain(proc)
    )
    assert any("all 2 backend(s) are paused" in line for line in _lines(proc, "FAIL")), _explain(proc)
    # The deep lifecycle case is about a mock task, whose pool is closed too:
    # it ends the suite with a summary instead of waiting out --timeout.
    assert "none of them was measured" in proc.transcript, _explain(proc)
    assert proc.returncode != 0, _explain(proc)


def test_a_pause_is_not_turned_into_a_failure_by_the_strict_switch(tmp_path):
    """SUITE_SKIPS_ARE_FAILURES means "the evidence must exist"; a pause says it will not."""
    proc = _run(
        tmp_path,
        pools={"resource:standard": OPEN, "resource:browser": CLOSED},
        extra_env={"SUITE_SKIPS_ARE_FAILURES": "1"},
    )
    assert "browser" not in proc.submitted, _explain(proc)
    assert _lines(proc, "FAIL") == [], _explain(proc)
    assert proc.returncode == 0, _explain(proc)


def test_an_unreadable_pool_is_a_failure_not_a_skip(tmp_path):
    """A failed read is not evidence of a pause, and must never become a skip."""
    proc = _run(tmp_path, pools={"resource:browser": CLOSED},
                extra_env={"FAKE_POOL_READ_STATUS": "500"})
    fails = _lines(proc, "FAIL")
    assert any("could not read pools/resource:browser" in line for line in fails), _explain(proc)
    assert not any("paused by operator" in line for line in _lines(proc, "SKIP")), _explain(proc)
    assert proc.returncode != 0, _explain(proc)


def test_a_backend_with_no_available_profile_fails(tmp_path):
    """Disable browser in the SERVED catalogue: GKE_AUTOPILOT must fail, not vanish."""
    served = copy.deepcopy(_catalogue())
    served["runtimes"]["browser"]["available"] = False
    proc = _run(tmp_path, catalogue=served)
    assert any("GKE_AUTOPILOT: no AVAILABLE profile" in line for line in _lines(proc, "FAIL")), (
        _explain(proc)
    )
    assert "browser" not in proc.submitted, _explain(proc)
    assert proc.returncode != 0, _explain(proc)


def test_an_unreadable_catalogue_fails_and_says_why(tmp_path):
    """The status the API answered is the diagnosis; it must reach the transcript."""
    proc = _run(tmp_path, extra_env={"FAKE_RUNTIMES_STATUS": "500"})
    fails = _lines(proc, "FAIL")
    assert any("could not read the runner-profile catalogue" in line and "HTTP 500" in line
               for line in fails), _explain(proc)
    assert any("visited 0 backends" in line for line in fails), _explain(proc)
    assert proc.tripwire == "", _explain(proc)
    assert proc.returncode != 0, _explain(proc)
