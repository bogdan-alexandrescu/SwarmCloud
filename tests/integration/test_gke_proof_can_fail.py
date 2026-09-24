"""Does `scripts/prove-gke-dispatch.sh` fail on each way GKE dispatch has failed?

WHY THE SCRIPT EXISTS. docs/incidents/2026-09-24-gke-dispatch.md section 5:
every `browser` task this platform ever accepted failed, seven causes stacked,
and "the cheapest proof is one task". The release now runs that one task after
every deploy. A proof step that cannot fail is worse than none -- it is the
green that let GKE stay dead for two days while `make smoke` passed -- so each
check in it is exercised here against a platform where the fact it covers is
FALSE, with everything else healthy:

  * the task went to a backend other than GKE_AUTOPILOT;
  * it was never dispatched at all (the 403-that-means-404 of causes 1, 3, 6);
  * it PARKED on CREDENTIAL_MISSING -- which proves nothing about dispatch, and
    must say so rather than wait out its timeout;
  * it started and died (cause 7, the read-only root filesystem);
  * it succeeded and left no artifact under the tenant's own prefix;
  * it never reached a terminal state;
  * it finished and its lease still holds capacity.

AND ONE PROPERTY OF WHAT IT SUBMITS. The browser runner refuses an input with
neither `url` nor `actions` ("browser runner needs input.url or at least one
action"). The smoke suite's backend matrix submits `{message, run_id}` to every
profile, so its GKE row fails at the runner even when dispatch is perfect. The
fake below simulates that refusal, so a proof that submits the wrong shape
fails here instead of on the first real release.

The platform is faked at `curl`, the seam every script here goes through; see
tests/integration/fake_platform.py for why that seam and what it cannot prove.
Unlike e2e-test's cases these are NOT skipped when the script is absent: a
proof step that has not been written must read as red, not as skipped.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fake_platform import install

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "scripts" / "prove-gke-dispatch.sh"

TASK_ID = "task_gkeproof"

HEALTHY = {
    # "auto" derives the backend from the profile actually submitted, which is
    # what the real router does; anything else is a deviation.
    "dispatched_backend": "auto",
    "final_state": "SUCCEEDED",
    "park_reason": None,
    "last_error": None,
    "artifacts": 2,
    "lease_released": True,
}

CURL_SHIM = r'''#!/usr/bin/env python3
"""A curl for prove-gke-dispatch.sh. See tests/integration/test_gke_proof_can_fail.py."""
import json, os, re, sys

sys.path.insert(0, os.environ["FAKE_SWARM_HELPERS"])
from fake_platform import firestore_document  # noqa: E402

SCENARIO = json.loads(open(os.environ["FAKE_SWARM_SCENARIO"]).read())
STATE_PATH = os.environ["FAKE_SWARM_STATE"]
TASK_ID = "task_gkeproof"
LEASE_ID = "lease_gkeproof"
ATTEMPT_ID = "att_gkeproof"
TENANT = "eng"
API = "https://swarm-api.fake.run.app"


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.loads(fh.read() or "{}")
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as fh:
        fh.write(json.dumps(state))


args = sys.argv[1:]
out_path = None
write_format = ""
method = None
body = None
url = None
params = {}
config_stdin = False
i = 0
while i < len(args):
    a = args[i]
    if a == "-o":
        out_path = args[i + 1]; i += 2; continue
    if a == "-w":
        write_format = args[i + 1]; i += 2; continue
    if a == "-X":
        method = args[i + 1]; i += 2; continue
    if a == "--data-urlencode":
        key, _, value = args[i + 1].partition("=")
        params[key] = value
        i += 2; continue
    if a in ("--data-binary", "-d", "--data", "-H", "--header", "-m", "--max-time", "-K"):
        if a in ("--data-binary", "-d", "--data"):
            body = args[i + 1]
        if a == "-K":
            config_stdin = True
        i += 2; continue
    if a.startswith("-") and a != "-":
        i += 1; continue
    url = a
    i += 1

if config_stdin:
    try:
        sys.stdin.read()
    except Exception:
        pass
if method is None:
    method = "POST" if body is not None else "GET"


def respond(status, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    if out_path:
        with open(out_path, "w") as fh:
            fh.write(text)
        if "%{http_code}" in write_format:
            sys.stdout.write(str(status))
    elif "%{http_code}" in write_format:
        prefix = write_format.split("%{http_code}")[0]
        sys.stdout.write(text + prefix + str(status))
    else:
        sys.stdout.write(text)
    sys.exit(0)


def submitted():
    return load_state().get("submitted") or {}


def backend():
    chosen = SCENARIO["dispatched_backend"]
    if chosen != "auto":
        return chosen
    return "GKE_AUTOPILOT" if submitted().get("runner_profile") == "browser" else "CLOUD_RUN_JOB"


def verdict():
    """What the task ends as. The browser runner's input check is simulated."""
    sent = submitted()
    payload = sent.get("input") or {}
    if sent.get("runner_profile") == "browser" and not (payload.get("url") or payload.get("actions")):
        return "FAILED", "RunnerFailure: browser runner needs input.url or at least one action"
    return SCENARIO["final_state"], SCENARIO["last_error"]


def task_document():
    state, error = verdict()
    return {
        "id": TASK_ID, "tenant_id": TENANT, "state": state,
        "runner_profile": submitted().get("runner_profile"),
        "current_lease_id": LEASE_ID, "attempt_count": 1,
        "last_error": error,
        "park_reason": SCENARIO["park_reason"] if state == "PARKED" else None,
    }


def event_documents():
    state, error = verdict()
    rows = [("ev_1", "submitted", {}), ("ev_2", "lease_acquired", {})]
    chosen = backend()
    if chosen and state != "PARKED":
        rows.append(("ev_3", "dispatched", {"execution_name": "swarm-gkeproof-1", "backend": chosen}))
    if state == "PARKED":
        rows.append(("ev_4", "parked", {"reason": SCENARIO["park_reason"]}))
    elif state == "SUCCEEDED":
        rows.append(("ev_4", "succeeded", {}))
    elif state in ("FAILED", "DEAD_LETTERED"):
        rows.append(("ev_4", "failed", {"error": error}))
    return [
        firestore_document("tasks/%s/events" % TASK_ID, ev_id,
                           {"type": kind, "task_id": TASK_ID, "detail": detail})
        for ev_id, kind, detail in rows
    ]


def attempt_documents():
    state, error = verdict()
    if state == "PARKED":
        return []
    return [{"document": firestore_document("attempts", ATTEMPT_ID, {
        "attempt_id": ATTEMPT_ID, "task_id": TASK_ID, "tenant_id": TENANT,
        "generation": 1, "lease_id": LEASE_ID, "backend": backend(),
        "execution_name": "swarm-gkeproof-1", "error": error,
        "exit_code": 0 if state == "SUCCEEDED" else 1,
    })}]


if url.startswith("http://metadata.google.internal/"):
    if "/identity" in url:
        respond(200, "fake-id-token")
    if "/email" in url:
        respond(200, "swarm-verify@swarm-test-project.iam.gserviceaccount.com")
    respond(200, {"access_token": "fake-access-token", "expires_in": 3600})

if re.match(r"^https://firestore\.googleapis\.com/v1/projects/[^/]+/databases/[^/:]+$", url):
    respond(200, {"name": "projects/x/databases/swarm", "type": "FIRESTORE_NATIVE"})

if "firestore.googleapis.com" in url and ":runQuery" in url:
    collection = json.loads(body or "{}")["structuredQuery"]["from"][0]["collectionId"]
    rows = attempt_documents() if collection == "attempts" else []
    respond(200, rows or [{"readTime": "2026-09-24T00:00:00Z"}])

if "firestore.googleapis.com" in url and "/documents/" in url:
    path = url.split("/documents/", 1)[1].split("?", 1)[0]
    if path == "tasks/%s/events" % TASK_ID:
        respond(200, {"documents": event_documents()})
    if path == "tasks/%s" % TASK_ID and submitted():
        respond(200, firestore_document("tasks", TASK_ID, task_document()))
    if path == "leases/%s" % LEASE_ID:
        released = "2026-09-24T00:05:00Z" if SCENARIO["lease_released"] else None
        respond(200, firestore_document("leases", LEASE_ID, {
            "lease_id": LEASE_ID, "task_id": TASK_ID, "released_at": released,
        }))
    respond(404, {"error": {"code": 404, "message": "not found: " + path}})

if url.startswith("https://storage.googleapis.com/storage/v1/b/"):
    prefix = params.get("prefix", "")
    state, _ = verdict()
    own = "tenants/%s/tasks/%s/" % (TENANT, TASK_ID)
    if state != "SUCCEEDED" or not prefix.startswith(own):
        respond(200, {"kind": "storage#objects"})
    items = [{"name": own + "attempts/%s/artifacts/file-%d.png" % (ATTEMPT_ID, n)}
             for n in range(SCENARIO["artifacts"])]
    respond(200, {"kind": "storage#objects", **({"items": items} if items else {})})

if url.startswith(API):
    path = url[len(API):]
    if path == "/readyz":
        respond(200, {"status": "ready"})
    if path == "/v1/tasks" and method == "POST":
        state = load_state()
        state["submitted"] = json.loads(body or "{}")
        save_state(state)
        respond(201, {"id": TASK_ID, "state": "QUEUED"})
    m = re.match(r"^/v1/tasks/([^/]+)/cancel$", path)
    if m:
        state = load_state()
        state["cancelled"] = True
        save_state(state)
        respond(200, {"task_id": m.group(1), "cancel_requested": True})
    respond(404, {"detail": "the fake has no route for " + path})

respond(500, {"error": {"message": "the fake was asked for " + str(url)}})
'''


def run_proof(tmp_path: Path, *extra: str, **deviations) -> tuple[int, str, dict]:
    env = install(tmp_path)
    shim = tmp_path / "bin" / "curl"
    shim.write_text(CURL_SHIM)
    shim.chmod(0o755)

    unknown = set(deviations) - set(HEALTHY)
    assert not unknown, f"unknown scenario keys {sorted(unknown)}"
    scenario = dict(HEALTHY, **deviations)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    env["FAKE_SWARM_SCENARIO"] = str(scenario_path)
    env["SWARM_POLL_INTERVAL_SECONDS"] = "1"

    proc = subprocess.run(
        ["bash", str(PROOF), *(extra or ("--timeout", "60"))],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    state_path = Path(env["FAKE_SWARM_STATE"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    return proc.returncode, proc.stdout + proc.stderr, state


def verdicts(out: str, kind: str) -> list[str]:
    """The PASS or FAIL lines of a run -- what the proof is judged on.

    Asserting on these, not on "exit non-zero and the word appears somewhere",
    is what makes each case below about ONE check. The first mutation round
    showed why: with two checks broken at once, the backend case still went
    red for an unrelated failure while mentioning the right backend in a
    diagnostic line, and so proved nothing about the backend check itself.
    """
    return [line.strip()[len(kind):].strip() for line in out.splitlines()
            if line.strip().startswith(kind + " ")]


def failed_on(out: str, *needles: str) -> bool:
    return any(all(n in line for n in needles) for line in verdicts(out, "FAIL"))


def passed_on(out: str, *needles: str) -> bool:
    return any(all(n in line for n in needles) for line in verdicts(out, "PASS"))


# ---------------------------------------------------------------------------
# the control
# ---------------------------------------------------------------------------


def test_a_browser_task_that_runs_on_gke_is_proven(tmp_path):
    code, out, state = run_proof(tmp_path)
    assert code == 0, out
    assert not verdicts(out, "FAIL"), out
    assert passed_on(out, "dispatched to GKE_AUTOPILOT"), out
    assert passed_on(out, "SUCCEEDED"), out
    assert passed_on(out, "object(s) under"), out
    assert passed_on(out, "released"), out

    sent = state.get("submitted") or {}
    assert sent.get("runner_profile") == "browser", sent
    payload = sent.get("input") or {}
    assert payload.get("actions") or payload.get("url"), (
        f"the proof submitted {payload!r}; the browser runner refuses an input "
        "with neither url nor actions, so the task would fail with dispatch working"
    )
    assert not state.get("cancelled"), "a task that finished must not be cancelled"


# ---------------------------------------------------------------------------
# one fact false at a time
# ---------------------------------------------------------------------------


def test_a_task_dispatched_to_another_backend_is_not_a_gke_proof(tmp_path):
    # Everything else about this run is healthy -- it SUCCEEDED, left
    # artifacts and released its lease -- so the backend check is the only
    # thing standing between it and a green proof of a path it never took.
    code, out, _ = run_proof(tmp_path, dispatched_backend="CLOUD_RUN_JOB")
    assert code != 0, out
    assert failed_on(out, "CLOUD_RUN_JOB", "GKE_AUTOPILOT"), out
    assert passed_on(out, "SUCCEEDED"), out
    assert len(verdicts(out, "FAIL")) == 1, out


def test_a_task_that_was_never_dispatched_fails_and_says_why(tmp_path):
    code, out, _ = run_proof(
        tmp_path,
        dispatched_backend=None,
        final_state="FAILED",
        last_error="gke_create_job_forbidden",
    )
    assert code != 0, out
    assert failed_on(out, "never dispatched", "gke_create_job_forbidden"), out


def test_a_parked_task_proves_nothing_and_fails_fast(tmp_path):
    started = time.monotonic()
    code, out, state = run_proof(
        tmp_path,
        "--timeout", "180",
        dispatched_backend=None,
        final_state="PARKED",
        park_reason="CREDENTIAL_MISSING",
    )
    elapsed = time.monotonic() - started
    assert code != 0, out
    assert failed_on(out, "PARKED on CREDENTIAL_MISSING"), out
    assert elapsed < 120, (
        f"took {elapsed:.0f}s: a CREDENTIAL_MISSING park does not resolve itself, "
        "so waiting out the timeout only delays the same answer"
    )
    assert state.get("cancelled"), "a proof task left PARKED must be cancelled, not abandoned"


def test_a_worker_that_dies_on_start_fails_with_its_error(tmp_path):
    code, out, _ = run_proof(
        tmp_path,
        final_state="FAILED",
        last_error="OSError: [Errno 30] Read-only file system: '/artifacts'",
    )
    assert code != 0, out
    # Dispatched fine; it is the RUN that failed, and the worker's own words
    # must reach the reader.
    assert passed_on(out, "dispatched to GKE_AUTOPILOT"), out
    assert failed_on(out, "ended FAILED"), out
    assert "Read-only file system" in out, out


def test_a_success_that_left_no_artifact_is_not_a_proof(tmp_path):
    code, out, _ = run_proof(tmp_path, artifacts=0)
    assert code != 0, out
    assert failed_on(out, "no artifacts under"), out
    assert len(verdicts(out, "FAIL")) == 1, out


def test_a_task_that_never_finishes_fails_at_the_timeout(tmp_path):
    code, out, state = run_proof(tmp_path, "--timeout", "3", final_state="RUNNING")
    assert code != 0, out
    assert failed_on(out, "no terminal state", "RUNNING"), out
    assert state.get("cancelled"), "a proof task still running at the timeout must be cancelled"


def test_a_lease_that_is_never_released_fails(tmp_path):
    code, out, _ = run_proof(tmp_path, lease_released=False)
    assert code != 0, out
    assert failed_on(out, "still holding capacity"), out
    assert len(verdicts(out, "FAIL")) == 1, out
