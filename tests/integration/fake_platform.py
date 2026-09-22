"""A deployed swarm, faked at the one seam every suite goes through: `curl`.

`scripts/e2e-test.sh` reaches the platform exclusively through `curl` -- the
control-plane API, the Firestore REST API, the Cloud Run Admin API and the
metadata server, all of them, because `scripts/lib/common.sh` has no other
transport. Putting a `curl` on PATH that answers from a scenario file therefore
drives the REAL script, with its real jq, its real argument handling and its
real assertions, against a platform whose every fact is chosen by the test.

WHY THIS IS THE RIGHT SEAM AND NOT A CHEAT. What is under test is the suite's
JUDGEMENT: does a check notice when the fact it covers is false? That can only
be answered by making the fact false, and making it false on a live deployment
means breaking a running platform. The next seam down is the wire, which is
here. Everything above it -- every assertion, every jq expression, every
comparison -- is the shipped script.

IT IS A SIMULATOR, NOT A TABLE OF CANNED REPLIES. The suite mints a fresh nonce
on every run and names its artifact after it, so a fixture of fixed responses
could not answer it. The shim reads the workflow the suite actually POSTs,
derives the facts a healthy platform would then hold -- this artifact, of these
bytes, under this attempt's prefix -- and serves those back consistently
afterwards. A scenario says only how to DEVIATE from that, which is what makes
each mutation one flag rather than a rewritten fixture.

WHAT IT CANNOT PROVE, said plainly: that the real API and the real Firestore
return the shapes below. A scenario is only as honest as the shapes it serves,
so `test_e2e_suite_can_fail.py` checks the load-bearing ones against the
production serialisers rather than trusting this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCENARIO_ENV = "FAKE_SWARM_SCENARIO"

API_URL = "https://swarm-api.fake.run.app"
PROJECT = "swarm-test-project"
REGION = "us-central1"
DATABASE = "swarm"
BUCKET = f"swarm-artifacts-{PROJECT}"
TENANT = "eng"

#: Written by the runner for every attempt, whatever the workload did. The
#: reason the artifact outage was invisible, so the fake reproduces it.
RUNNER_LOGS = ("stdout", "stderr")


def firestore_value(value: Any) -> dict[str, Any]:
    """Encode one Python value the way the Firestore REST API returns it.

    `common.sh`'s FS_JQ decodes exactly these spellings, so a document built
    here is decoded by the production jq rather than by anything in this file.
    `bool` is tested before `int` because `True` is an `int` in Python and a
    `booleanValue` is not an `integerValue` -- the same conflation the worker's
    `record_spend` guards against on the way in.
    """
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: firestore_value(v) for k, v in value.items()}}}
    raise TypeError(f"cannot encode {value!r} as a Firestore value")


def firestore_document(collection: str, doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"projects/{PROJECT}/databases/{DATABASE}/documents/{collection}/{doc_id}",
        "fields": {k: firestore_value(v) for k, v in data.items()},
        "createTime": "2026-09-22T00:00:00Z",
        "updateTime": "2026-09-22T00:00:00Z",
    }


def artifact_uri(task_id: str, attempt_id: str, name: str) -> str:
    return (
        f"gs://{BUCKET}/tenants/{TENANT}/tasks/{task_id}/attempts/{attempt_id}"
        f"/artifacts/{name}"
    )


# ---------------------------------------------------------------------------
# the scenario
# ---------------------------------------------------------------------------

#: A healthy platform. Every key is a deviation a test can set; the default of
#: each is what a working deployment does.
HEALTHY: dict[str, Any] = {
    "api_url": API_URL,
    # "ok" | "runner_only"  -- runner_only is the SWARM_ARTIFACTS_DIR outage:
    # a manifest holding the runner's own files and nothing the workload made.
    "produce_manifest": "ok",
    # "ok" | "missing" | "wrong_bytes" | "foreign_source"
    "staged": "ok",
    # What the negative control's consumer ends up in. A healthy platform fails
    # it; "SUCCEEDED" is the platform that stages nothing and says nothing.
    "negative_consume_state": "FAILED",
    "negative_error_names_file": True,
    # "ok" | "null" | "omit_keys"  -- "null" is attempt_from_dict dropping the
    # five spend fields while Firestore holds them.
    "api_spend": "ok",
    # Whether any attempt in the deployment has recorded usage at all.
    "spend_recorded": True,
    # "ok" | "leak_verifier" | "http_url" | "short_state" | "no_challenge" | "error"
    "authorize": "ok",
    # Task ids whose API state should disagree with Firestore, mapped to what
    # the API should claim instead.
    "api_state_override": {},
    # "idle" | "live" | "dead" -- dead is a task holding capacity against an
    # execution that has completed.
    "executions": "idle",
    # What GET /v1/workflows/<id> reports once every step has succeeded.
    "workflow_state": "SUCCEEDED",
}


def scenario(**deviations: Any) -> dict[str, Any]:
    base = dict(HEALTHY)
    unknown = set(deviations) - set(base)
    if unknown:
        raise KeyError(f"unknown scenario key(s): {sorted(unknown)}")
    base.update(deviations)
    return base


# ---------------------------------------------------------------------------
# the shim
# ---------------------------------------------------------------------------

#: A `curl` that answers from the scenario instead of the network.
#:
#: It honours the three invocation shapes `common.sh` actually uses, because the
#: difference between them has already broken a fixture in this repository once
#: (the fake curl in test_register_tenant_grants.py ignored -o and -w, so every
#: status read as the literal body and three files errored in their fixture for
#: 95 commits):
#:
#:   -o FILE -w '%{http_code}'      body to FILE, status to stdout   (fs_request)
#:   -w $'\n%{http_code}'           body, newline, status on stdout  (api_request)
#:   plain                          body on stdout        (metadata, Cloud Run)
#:
#: `-K -` puts the Authorization header on curl's stdin, so stdin is always
#: drained: the caller writes into a pipe and would otherwise block.
CURL_SHIM = r'''#!/usr/bin/env python3
"""A curl that answers from FAKE_SWARM_SCENARIO. See tests/integration/fake_platform.py."""
import json, os, re, sys

sys.path.insert(0, os.environ["FAKE_SWARM_HELPERS"])
from fake_platform import (  # noqa: E402
    artifact_uri, firestore_document, RUNNER_LOGS, TENANT,
)

SCENARIO = json.loads(open(os.environ["FAKE_SWARM_SCENARIO"]).read())
STATE_PATH = os.environ["FAKE_SWARM_STATE"]
LOG = os.environ.get("FAKE_SWARM_LOG")


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
fail_silently = False
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
    if a in ("--data-binary", "-d", "--data", "--data-urlencode", "-H", "--header",
             "-m", "--max-time", "-K"):
        if a in ("--data-binary", "-d", "--data"):
            body = args[i + 1]
        if a == "-K":
            config_stdin = True
        i += 2; continue
    if a.startswith("-") and a != "-":
        if "f" in a.lstrip("-"):
            fail_silently = True
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
    if LOG:
        with open(LOG, "a") as fh:
            fh.write("%s %s -> %d\n" % (method, url, status))
    if fail_silently and not (200 <= status < 300):
        sys.exit(22)
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


# ---------------------------------------------------------------------------
# deriving the facts a healthy platform would hold, from what was submitted
# ---------------------------------------------------------------------------

SPEND = {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_read_input_tokens": 89,
    "cache_creation_input_tokens": 10,
    "cost_usd": 0.4213,
}


def register_workflow(sent):
    """Turn a submitted workflow into the documents it would produce."""
    state = load_state()
    negative = sent.get("on_step_failure") == "continue"
    key = "negative" if negative else "main"
    steps = sent["steps"]
    produce_in = steps[0]["input"]
    name = produce_in["artifact_name"]
    text = produce_in["artifact_text"]
    declared = steps[1]["input_from"]["produce"]
    record = {
        "workflow_id": "wf_" + key,
        "produce": "task_produce_" + key,
        "consume": "task_consume_" + key,
        "produce_attempt": "att_produce_" + key,
        "consume_attempt": "att_consume_" + key,
        "artifact_name": name,
        "artifact_bytes": len(text.encode("utf-8")),
        "declared": declared,
        "negative": negative,
    }
    state[key] = record
    save_state(state)
    return record


def records():
    return load_state()


def find_task(task_id):
    for key, rec in records().items():
        if rec["produce"] == task_id:
            return rec, "produce"
        if rec["consume"] == task_id:
            return rec, "consume"
    return None, None


def produce_summary(rec):
    entries = [
        {"name": "%s.%s.log" % ("mock", stream), "bytes": 40,
         "uri": artifact_uri(rec["produce"], rec["produce_attempt"],
                             "%s.%s.log" % ("mock", stream))}
        for stream in RUNNER_LOGS
    ]
    if SCENARIO["produce_manifest"] == "ok":
        entries.append({
            "name": rec["artifact_name"],
            "bytes": rec["artifact_bytes"],
            "uri": artifact_uri(rec["produce"], rec["produce_attempt"],
                                rec["artifact_name"]),
        })
    return {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {
            "stdout": "gs://b/logs/stdout.log",
            "stderr": "gs://b/logs/stderr.log",
        },
    }


def consume_summary(rec):
    if rec["negative"]:
        return {"artifacts": [], "artifact_bytes": 0, "logs": {}}
    mode = SCENARIO["staged"]
    if mode == "missing":
        return {"artifacts": [], "artifact_bytes": 0, "logs": {}}
    source = rec["produce"]
    size = rec["artifact_bytes"]
    if mode == "wrong_bytes":
        size = size + 7
    if mode == "foreign_source":
        source = "task_somebody_elses"
    return {
        "artifacts": [],
        "artifact_bytes": 0,
        "logs": {},
        "staged_inputs": [{
            "task_id": source,
            "filename": rec["artifact_name"],
            "path": rec["artifact_name"],
            "bytes": size,
            "uri": artifact_uri(source, rec["produce_attempt"], rec["artifact_name"]),
        }],
    }


def task_document(task_id):
    rec, role = find_task(task_id)
    if rec is None:
        return None
    if role == "produce":
        return {
            "id": task_id, "tenant_id": TENANT, "state": "SUCCEEDED",
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": 1, "attempt_count": 1,
            "result_summary": produce_summary(rec), "last_error": None,
            "blocked_by": [], "workflow_id": rec["workflow_id"],
        }
    if rec["negative"]:
        state = SCENARIO["negative_consume_state"]
        error = None
        if state != "SUCCEEDED":
            error = (
                "declared input %r is not in the manifest of upstream task %s"
                % (rec["declared"], rec["produce"])
                if SCENARIO["negative_error_names_file"]
                else "the attempt could not start"
            )
        return {
            "id": task_id, "tenant_id": TENANT, "state": state,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": 1, "attempt_count": 3,
            "result_summary": {"artifacts": [], "logs": {}},
            "last_error": error, "blocked_by": [],
            "workflow_id": rec["workflow_id"],
        }
    return {
        "id": task_id, "tenant_id": TENANT, "state": "SUCCEEDED",
        "runner_profile": "mock", "resource_class": "standard",
        "current_generation": 1, "attempt_count": 1,
        "result_summary": consume_summary(rec), "last_error": None,
        "blocked_by": [], "workflow_id": rec["workflow_id"],
    }


def attempt_documents():
    """Every attempt document, keyed by id. Spend lands on the producer's."""
    out = {}
    for rec in records().values():
        doc = {
            "attempt_id": rec["produce_attempt"], "task_id": rec["produce"],
            "tenant_id": TENANT, "generation": 1, "lease_id": "lease_1",
            "backend": "CLOUD_RUN_JOB", "exit_code": 0,
            "peak_rss_bytes": 123456,
        }
        if SCENARIO["spend_recorded"] and not rec["negative"]:
            doc.update(SPEND)
        out[rec["produce_attempt"]] = doc
        out[rec["consume_attempt"]] = {
            "attempt_id": rec["consume_attempt"], "task_id": rec["consume"],
            "tenant_id": TENANT, "generation": 1, "lease_id": "lease_2",
            "backend": "CLOUD_RUN_JOB", "exit_code": 0,
        }
    if SCENARIO["executions"] in ("live", "dead"):
        out["att_inflight"] = {
            "attempt_id": "att_inflight", "task_id": "task_inflight",
            "tenant_id": TENANT, "generation": 2, "lease_id": "lease_z",
            "backend": "CLOUD_RUN_JOB",
            "execution_name": "projects/p/locations/r/jobs/j/executions/" + EXECUTION,
        }
    return out


#: The one execution the in-flight task points at. `live` leaves it running and
#: `dead` gives it a completionTime -- the same name either way, so the check
#: under test turns on the execution's STATE and not on a name that stopped
#: matching.
EXECUTION = "exec-inflight"


def tasks_by_state(state):
    if SCENARIO["executions"] in ("live", "dead") and state == "RUNNING":
        return {"task_inflight": {
            "id": "task_inflight", "tenant_id": TENANT, "state": "RUNNING",
            "runner_profile": "mock", "current_generation": 2,
        }}
    return {}


def executions():
    if SCENARIO["executions"] == "live":
        return [{"name": "projects/p/locations/r/jobs/j/executions/" + EXECUTION}]
    if SCENARIO["executions"] == "dead":
        return [{"name": "projects/p/locations/r/jobs/j/executions/" + EXECUTION,
                 "completionTime": "2026-09-22T01:00:00Z"}]
    return []


def api_task(task_id):
    doc = task_document(task_id)
    if doc is None:
        return None
    served = dict(doc)
    override = SCENARIO["api_state_override"].get(task_id)
    if override:
        served["state"] = override
    return served


def api_attempts(task_id):
    rows = [d for d in attempt_documents().values() if d["task_id"] == task_id]
    if not rows:
        return None
    served = []
    for row in rows:
        entry = {
            "attempt_id": row["attempt_id"], "task_id": row["task_id"],
            "tenant_id": row["tenant_id"], "generation": row["generation"],
            "exit_code": row.get("exit_code"),
            "peak_rss_bytes": row.get("peak_rss_bytes"),
        }
        if SCENARIO["api_spend"] == "omit_keys":
            pass
        elif SCENARIO["api_spend"] == "null":
            entry.update({k: None for k in SPEND})
        else:
            entry.update({k: row.get(k) for k in SPEND})
        served.append(entry)
    return {"task_id": task_id, "attempts": served}


def api_artifacts(task_id):
    doc = task_document(task_id)
    if doc is None:
        return None
    summary = doc.get("result_summary") or {}
    return {
        "task_id": task_id,
        "artifacts": summary.get("artifacts", []),
        "artifacts_skipped": [],
        "artifact_bytes": summary.get("artifact_bytes"),
        "complete": bool(summary),
    }


def authorize_response():
    mode = SCENARIO["authorize"]
    if mode == "error":
        return 500, {"detail": "BrokerClient._call() got an unexpected keyword argument"}
    state = "s" * 43
    url = ("https://claude.ai/oauth/authorize?client_id=x&state=%s"
           "&code_challenge=abc&code_challenge_method=S256" % state)
    body = {"authorize_url": url, "state": state, "expires_in_seconds": 600}
    if mode == "leak_verifier":
        body["code_verifier"] = "v" * 43
    if mode == "http_url":
        body["authorize_url"] = url.replace("https://", "http://")
    if mode == "short_state":
        body["state"] = "abc"
        body["authorize_url"] = url.replace(state, "abc")
    if mode == "no_challenge":
        body["authorize_url"] = ("https://claude.ai/oauth/authorize?client_id=x&state=%s"
                                 % state)
    return 200, body


def workflow_view(workflow_id):
    for rec in records().values():
        if rec["workflow_id"] != workflow_id:
            continue
        tasks = [task_document(rec["produce"]), task_document(rec["consume"])]
        return {
            "workflow": {"workflow_id": workflow_id, "tenant_id": TENANT,
                         "state": SCENARIO["workflow_state"], "steps": []},
            "dispatch": {"strategy": "collect", "carrier": "checkpoints"},
            "tasks": [{"id": t["id"], "state": t["state"]} for t in tasks],
        }
    return None


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

if url.startswith("http://metadata.google.internal/"):
    if "/identity" in url:
        respond(200, "fake-id-token")
    respond(200, {"access_token": "fake-access-token", "expires_in": 3600})

if re.match(r"^https://firestore\.googleapis\.com/v1/projects/[^/]+/databases/[^/:]+$", url):
    respond(200, {"name": "projects/x/databases/swarm", "type": "FIRESTORE_NATIVE"})

if "firestore.googleapis.com" in url and ":runQuery" in url:
    query = json.loads(body or "{}")["structuredQuery"]
    collection = query["from"][0]["collectionId"]
    where = query.get("where") or {}
    if collection == "attempts":
        source = attempt_documents()
    elif collection == "tasks":
        wanted_state = ""
        if "fieldFilter" in where:
            wanted_state = where["fieldFilter"]["value"].get("stringValue", "")
        source = tasks_by_state(wanted_state)
    else:
        source = {}
    rows = []
    for doc_id, data in source.items():
        if "fieldFilter" in where:
            f = where["fieldFilter"]
            if data.get(f["field"]["fieldPath"]) != f["value"].get("stringValue"):
                continue
        if "unaryFilter" in where:
            f = where["unaryFilter"]
            present = data.get(f["field"]["fieldPath"]) is not None
            if f["op"] == "IS_NOT_NULL" and not present:
                continue
            if f["op"] == "IS_NULL" and present:
                continue
        rows.append({"document": firestore_document(collection, doc_id, data)})
    respond(200, rows or [{"readTime": "2026-09-22T00:00:00Z"}])

if "firestore.googleapis.com" in url and "/documents/" in url:
    path = url.split("/documents/", 1)[1].split("?", 1)[0]
    collection, _, doc_id = path.partition("/")
    if collection == "tasks":
        data = task_document(doc_id)
    elif collection == "attempts":
        data = attempt_documents().get(doc_id)
    else:
        data = None
    if data is None:
        respond(404, {"error": {"code": 404, "message": "not found"}})
    respond(200, firestore_document(collection, doc_id, data))

if "run.googleapis.com" in url and "/executions" in url:
    respond(200, {"executions": executions()})

API = SCENARIO["api_url"]
if url.startswith(API):
    path = url[len(API):]
    if path == "/readyz":
        respond(200, {"status": "ready"})
    if path == "/v1/workflows" and method == "POST":
        rec = register_workflow(json.loads(body or "{}"))
        respond(201, {
            "workflow": {
                "workflow_id": rec["workflow_id"], "tenant_id": TENANT,
                "state": "QUEUED",
                "steps": [
                    {"step_id": "produce", "task_id": rec["produce"]},
                    {"step_id": "consume", "task_id": rec["consume"]},
                ],
            },
            "dispatch": {"strategy": "collect", "carrier": "checkpoints",
                         "integrator_step_id": None},
        })
    m = re.match(r"^/v1/workflows/([^/]+)$", path)
    if m and method == "GET":
        view = workflow_view(m.group(1))
        respond(200, view) if view else respond(404, {"detail": "not found"})
    m = re.match(r"^/v1/tasks/([^/]+)/cancel$", path)
    if m:
        respond(200, {"task_id": m.group(1), "cancel_requested": True})
    m = re.match(r"^/v1/tasks/([^/]+)/artifacts$", path)
    if m:
        served = api_artifacts(m.group(1))
        respond(200, served) if served else respond(404, {"detail": "not found"})
    m = re.match(r"^/v1/tasks/([^/]+)/attempts$", path)
    if m:
        served = api_attempts(m.group(1))
        respond(200, served) if served else respond(404, {"detail": "not found"})
    m = re.match(r"^/v1/tasks/([^/]+)$", path)
    if m:
        served = api_task(m.group(1))
        respond(200, served) if served else respond(404, {"detail": "not found"})
    if path == "/v1/accounts/authorize" and method == "POST":
        status, payload = authorize_response()
        respond(status, payload)
    respond(404, {"detail": "the fake platform has no route for " + path})

respond(500, {"error": {"message": "the fake platform was asked for " + url}})
'''


def install(tmp: Path) -> dict[str, str]:
    """Put the shims on PATH and return the environment a faked run needs.

    The `gcloud` stub is not decoration. `common.sh` falls back to
    `gcloud auth print-access-token` whenever it does not believe it is inside
    Cloud Run, and a run that silently reached a real gcloud would be a test
    authenticating against a real project. Failing loudly is the only safe
    answer; `CLOUD_RUN_JOB` is set below so the metadata branch is the one that
    runs -- which is also the branch the in-VPC gate uses, and the one whose
    `service_accounts` spelling kept it from ever authenticating.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp / "tmp").mkdir(parents=True, exist_ok=True)

    curl = bin_dir / "curl"
    curl.write_text(CURL_SHIM)
    curl.chmod(0o755)

    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'the fake platform refuses to run gcloud: a test must not reach "
        "a real project' >&2\n"
        "exit 1\n"
    )
    gcloud.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        f"REGION={REGION}\n"
        "ENVIRONMENT=dev\n"
        f"FIRESTORE_DATABASE={DATABASE}\n"
        f"ARTIFACT_BUCKET={BUCKET}\n"
        f"API_URL={API_URL}\n"
    )
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_SWARM_HELPERS"] = str(Path(__file__).resolve().parent)
    env["FAKE_SWARM_STATE"] = str(tmp / "state.json")
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["CLOUD_RUN_JOB"] = "swarm-verify"
    env["TMPDIR"] = str(tmp / "tmp")
    return env
