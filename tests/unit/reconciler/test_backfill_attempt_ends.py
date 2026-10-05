"""`scripts/backfill-attempt-ends.sh` closes the superseded attempts and nothing else (#630).

The one-off that closes the attempts the reconciler's fence and reclaim, and
the scheduler's failed dispatch, left open before they recorded the end
themselves. Run here as the REAL script, with a fake `curl` that IS Firestore's
REST API (the shapes `scripts/lib/common.sh` speaks: a paginated listing, a
document GET, a PATCH with an update mask and an `updateTime` precondition) and
a fake `gcloud` that hands out an access token, sharing one state file.

Pinned:

  B-1  The dry run, the default, counts -- superseded, current generation,
       task gone -- and writes nothing.
  B-2  --apply, after the project id is TYPED, closes exactly the superseded
       open attempts: completed_at from the superseding admission (or the
       lease's release, or the task's update), cause `superseded`, exit_code
       never written. The live attempt at the task's current generation, a
       finished task's unended attempt, and an attempt the worker ended are
       untouched.
  B-3  Idempotent: a second run finds nothing to close, asks nothing and
       writes nothing.
  B-4  The confirmation is required, typed, and SWARM_ASSUME_YES does not
       skip it.
  B-5  An attempt that changed between the listing and the write is refused
       by the precondition, not overwritten.

WHAT THIS CANNOT PROVE: that live Firestore answers in the shapes the fake
serves. They are the shapes `fs_list`, `fs_get` and `fs_patch` already use
against it (`scripts/offboard-tenant.sh`, `scripts/pool-limit.sh`).
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path("scripts") / "backfill-attempt-ends.sh"
PROJECT = "swarm-test-project"
DATABASE = "swarm"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="backfill-attempt-ends.sh needs bash and jq",
)

FAKE_CURL = r'''#!{python} -S
import json, os, sys
from urllib.parse import urlsplit, parse_qs

args = sys.argv[1:]
VALUED = {"-m", "--max-time", "-K", "--config", "-X", "--request", "-H", "--header",
          "--data-binary", "-d", "--data", "-o", "--output", "-w", "--write-out"}
opts, url, i = {}, None, 0
while i < len(args):
    a = args[i]
    if a in VALUED:
        opts[a] = args[i + 1]
        i += 2
        continue
    if a.startswith("http"):
        url = a
    i += 1
if opts.get("-K") == "-":
    sys.stdin.read()
method = opts.get("-X", "GET")
body = opts.get("--data-binary")

STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    state = json.load(fh)
docs = state["docs"]
base = "projects/%s/databases/%s/documents" % (os.environ["FAKE_PROJECT"], os.environ["FAKE_DATABASE"])
parts = urlsplit(url)
path = parts.path[len("/v1/"):]
query = parse_qs(parts.query)


def log(kind, **kw):
    with open(os.environ["FAKE_FS_LOG"], "a") as fh:
        fh.write(json.dumps(dict(kind=kind, **kw)) + "\n")


def reply(status, payload):
    text = json.dumps(payload)
    if "-o" in opts:
        with open(opts["-o"], "w") as fh:
            fh.write(text)
        if "http_code" in opts.get("-w", ""):
            sys.stdout.write(str(status))
    else:
        sys.stdout.write(text)
    sys.exit(0)


def save():
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def render(rel):
    d = docs[rel]
    return {"name": base + "/" + rel, "fields": d["fields"], "updateTime": d["updateTime"]}


if not path.startswith(base + "/"):
    reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "fake: " + path}})
rest = path[len(base) + 1:]
segs = rest.split("/")

if len(segs) == 1 and method == "GET":
    coll = segs[0]
    size = min(int(query.get("pageSize", ["300"])[0]), int(os.environ.get("FAKE_MAX_PAGE", "300")))
    start = int(query.get("pageToken", ["0"])[0])
    names = sorted(p for p in docs if p.startswith(coll + "/") and p.count("/") == 1)
    page = names[start:start + size]
    log("list", collection=coll, start=start, n=len(page))
    out = {"documents": [render(p) for p in page]}
    if start + size < len(names):
        out["nextPageToken"] = str(start + size)
    reply(200, out)

if len(segs) == 2 and method == "GET":
    log("get", doc=rest)
    if rest in docs:
        reply(200, render(rest))
    reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "no " + rest}})

if len(segs) == 2 and method == "PATCH":
    mask = query.get("updateMask.fieldPaths", [])
    precondition = query.get("currentDocument.updateTime", [None])[0]
    moved = os.environ.get("FAKE_MOVED", "")
    if rest == moved and rest in docs:
        # Its worker recorded its own end between the listing and this write.
        docs[rest]["fields"]["completed_at"] = {"timestampValue": "2026-10-05T09:00:00Z"}
        docs[rest]["fields"]["exit_code"] = {"integerValue": "0"}
        docs[rest]["updateTime"] = "2026-10-05T09:00:00.000001Z"
        save()
    if rest not in docs or (precondition and docs[rest]["updateTime"] != precondition):
        log("patch_refused", doc=rest)
        reply(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                              "message": "the document was modified"}})
    fields = json.loads(body)["fields"]
    assert set(fields) == set(mask), (fields, mask)
    docs[rest]["fields"].update(fields)
    docs[rest]["updateTime"] = docs[rest]["updateTime"][:-1] + "9Z"
    save()
    log("patch", doc=rest, mask=mask)
    reply(200, render(rest))

reply(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "fake: " + method + " " + rest}})
'''

FAKE_GCLOUD = r'''#!{python} -S
import sys
if sys.argv[1:3] == ["auth", "print-access-token"]:
    sys.stdout.write("fake-access\n")
    sys.exit(0)
sys.stderr.write("fake gcloud: unsupported " + " ".join(sys.argv[1:]) + "\n")
sys.exit(2)
'''


def _value(v: Any) -> dict[str, Any]:
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", v):
        return {"timestampValue": v}
    return {"stringValue": v}


def _doc(fields: dict[str, Any], updated: str = "2026-10-05T08:00:00.000001Z") -> dict[str, Any]:
    return {"fields": {k: _value(v) for k, v in fields.items()}, "updateTime": updated}


def _attempt(attempt_id: str, task_id: str, generation: int, created: str, **extra: Any):
    fields = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": "eng",
        "generation": generation, "lease_id": f"lease_{attempt_id[4:]}",
        "backend": "CLOUD_RUN_JOB", "created_at": created,
        "completed_at": None, "exit_code": None, "error": None,
    }
    fields.update(extra)
    return f"attempts/{attempt_id}", _doc(fields)


def _task(task_id: str, generation: int, state: str, updated: str):
    return f"tasks/{task_id}", _doc({
        "id": task_id, "tenant_id": "eng", "state": state,
        "current_generation": generation, "updated_at": updated,
    })


def world() -> dict[str, Any]:
    docs = dict([
        # task_a: fenced twice and re-admitted; its gen-3 attempt is live.
        _task("task_a", 3, "RUNNING", "2026-10-01T12:30:00Z"),
        _attempt("att_a1", "task_a", 1, "2026-10-01T10:00:00Z"),
        _attempt("att_a2", "task_a", 2, "2026-10-01T11:00:00Z"),
        _attempt("att_a3", "task_a", 3, "2026-10-01T12:00:00Z"),
        # task_b: reclaimed at gen 1, fenced to 2, never re-admitted; its gen-1
        # lease records the release.
        _task("task_b", 2, "FAILED", "2026-10-02T09:00:00Z"),
        _attempt("att_b1", "task_b", 1, "2026-10-02T08:00:00Z"),
        ("leases/lease_b1", _doc({"lease_id": "lease_b1", "task_id": "task_b",
                                  "released_at": "2026-10-02T08:20:00Z"})),
        # task_e: superseded, no later attempt, a lease with no release: the
        # task's update is the end; its attempt already carries an error.
        _task("task_e", 2, "READY", "2026-10-04T07:00:00Z"),
        _attempt("att_e1", "task_e", 1, "2026-10-04T06:00:00Z", error="worker: heartbeat lost"),
        # task_c: finished at its current generation without an attempt end:
        # lost_after_finish's, never this script's.
        _task("task_c", 1, "SUCCEEDED", "2026-10-03T10:00:00Z"),
        _attempt("att_c1", "task_c", 1, "2026-10-03T09:00:00Z"),
        # an attempt whose worker ended it, at an old generation.
        _attempt("att_a0", "task_a", 0, "2026-10-01T09:00:00Z",
                 completed_at="2026-10-01T09:30:00Z", exit_code=0),
        # an attempt whose task document is gone.
        _attempt("att_d1", "task_d", 1, "2026-10-01T08:00:00Z"),
    ])
    return {"docs": docs}


@dataclass
class Run:
    rc: int
    stderr: str
    state: dict[str, Any]
    log: list[dict[str, Any]]

    def fields(self, rel: str) -> dict[str, Any]:
        return self.state["docs"][rel]["fields"]

    def patches(self) -> list[str]:
        return [e["doc"] for e in self.log if e["kind"] == "patch"]


def _run(tmp_path: Path, state: dict, *args: str, typed: str | None = None,
         **env_extra: str) -> Run:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        path = bindir / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    fs_log = tmp_path / "fs.jsonl"
    home = tmp_path / "home"
    home.mkdir()

    env = {k: v for k, v in os.environ.items()
           if k not in ("SWARM_ASSUME_YES", "K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA",
                        "FIRESTORE_DATABASE", "ENVIRONMENT", "PROJECT_ID")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": PROJECT,
        "ENVIRONMENT": "dev",
        "FIRESTORE_DATABASE": DATABASE,
        "NO_COLOR": "1",
        "TMPDIR": str(tmp_path),
        "FAKE_STATE": str(state_file),
        "FAKE_PROJECT": PROJECT,
        "FAKE_DATABASE": DATABASE,
        "FAKE_FS_LOG": str(fs_log),
        # Two documents a page, so the listing has to follow its cursor.
        "FAKE_MAX_PAGE": "2",
        **env_extra,
    })
    cmd = ["bash", str(root / SCRIPT), *args]
    if typed is None:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=180, check=False)
        rc, stderr = proc.returncode, proc.stderr
    else:
        # A real terminal on stdin, so the answer is TYPED.
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.close(slave)
            os.write(master, (typed + "\n").encode())
            _, stderr = proc.communicate(timeout=180)
            rc = proc.returncode
        finally:
            os.close(master)
    log = [json.loads(x) for x in fs_log.read_text().splitlines() if x] if fs_log.exists() else []
    return Run(rc, stderr, json.loads(state_file.read_text()), log)


def _row(text: str, label: str) -> str:
    # The typed answer's newline is echoed to the terminal, not to stderr, so
    # the first row after the prompt shares its line.
    text = text.replace("to continue: ", "to continue:\n")
    m = re.search(r"^[ \t]+" + re.escape(label) + r"[ \t]+(\S+)[ \t]*$", text, re.M)
    assert m, f"no {label!r} row in:\n{text[-3000:]}"
    return m.group(1)


def _value_of(fields: dict[str, Any], name: str) -> Any:
    v = fields.get(name, {"nullValue": None})
    return next(iter(v.values()))


SUPERSEDED = ("attempts/att_a1", "attempts/att_a2", "attempts/att_b1", "attempts/att_e1")
UNTOUCHED = ("attempts/att_a3", "attempts/att_c1", "attempts/att_a0", "attempts/att_d1")


# --------------------------------------------------------------------------
# B-1
# --------------------------------------------------------------------------

def test_the_dry_run_counts_and_writes_nothing(tmp_path):
    before = world()

    run = _run(tmp_path, before)

    assert run.rc == 0, run.stderr
    assert run.state == before, "the dry run wrote"
    assert run.patches() == []
    assert _row(run.stderr, "attempts listed") == "8"
    assert _row(run.stderr, "open (no completed_at)") == "7"
    assert _row(run.stderr, "to close: superseded") == "4"
    assert _row(run.stderr, "end from next_attempt") == "2"
    assert _row(run.stderr, "end from lease_released") == "1"
    assert _row(run.stderr, "end from task_updated") == "1"
    assert _row(run.stderr, "left: at the current generation") == "2"
    assert _row(run.stderr, "left: task document gone") == "1"
    assert "DRY RUN" in run.stderr
    # The listing followed its cursor past the first page.
    assert len([e for e in run.log if e["kind"] == "list"]) == 4


def test_dry_run_said_out_loud_is_the_same_dry_run(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--dry-run")
    assert run.rc == 0, run.stderr
    assert run.state == before


# --------------------------------------------------------------------------
# B-2, B-3
# --------------------------------------------------------------------------

def test_apply_closes_exactly_the_superseded_attempts_and_is_idempotent(tmp_path):
    before = world()

    run = _run(tmp_path / "first", before, "--apply", typed=PROJECT)

    assert run.rc == 0, run.stderr
    assert sorted(run.patches()) == sorted(SUPERSEDED)
    ends = {
        "attempts/att_a1": "2026-10-01T11:00:00Z",   # att_a2's admission
        "attempts/att_a2": "2026-10-01T12:00:00Z",   # att_a3's admission
        "attempts/att_b1": "2026-10-02T08:20:00Z",   # lease_b1's release
        "attempts/att_e1": "2026-10-04T07:00:00Z",   # task_e's update
    }
    for rel, end in ends.items():
        fields = run.fields(rel)
        assert _value_of(fields, "completed_at") == end, rel
        assert _value_of(fields, "exit_code") is None, f"{rel}: an exit code was invented"
    assert _value_of(run.fields("attempts/att_a1"), "error").startswith("superseded: ")
    # An error the attempt already carried is kept.
    assert _value_of(run.fields("attempts/att_e1"), "error") == "worker: heartbeat lost"
    for rel in UNTOUCHED:
        assert run.state["docs"][rel] == before["docs"][rel], f"{rel} was written"
    assert _row(run.stderr, "closed") == "4"
    assert _row(run.stderr, "superseded and still open") == "0"

    again = _run(tmp_path / "second", run.state, "--apply")

    assert again.rc == 0, again.stderr
    assert again.patches() == [], "the second run wrote"
    assert again.state == run.state
    assert _row(again.stderr, "to close: superseded") == "0"
    assert "nothing to close" in again.stderr


# --------------------------------------------------------------------------
# B-4
# --------------------------------------------------------------------------

def test_a_wrong_answer_writes_nothing(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--apply", typed="yes")
    assert run.rc != 0
    assert run.patches() == []
    assert run.state == before


def test_assume_yes_does_not_skip_the_typed_confirmation(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--apply", SWARM_ASSUME_YES="1")
    assert run.rc != 0, "applied without a typed confirmation"
    assert "ignoring SWARM_ASSUME_YES" in run.stderr
    assert run.patches() == []
    assert run.state == before


def test_apply_and_dry_run_together_are_refused(tmp_path):
    run = _run(tmp_path, world(), "--apply", "--dry-run")
    assert run.rc != 0
    assert run.patches() == []


# --------------------------------------------------------------------------
# B-5
# --------------------------------------------------------------------------

def test_an_attempt_its_worker_ended_meanwhile_is_refused_not_overwritten(tmp_path):
    run = _run(tmp_path, world(), "--apply", typed=PROJECT, FAKE_MOVED="attempts/att_a1")

    assert run.rc == 0, run.stderr
    fields = run.fields("attempts/att_a1")
    assert _value_of(fields, "completed_at") == "2026-10-05T09:00:00Z"
    assert _value_of(fields, "exit_code") == "0"
    assert _value_of(fields, "error") is None
    assert _row(run.stderr, "refused (changed since read)") == "1"
    assert _row(run.stderr, "closed") == "3"
