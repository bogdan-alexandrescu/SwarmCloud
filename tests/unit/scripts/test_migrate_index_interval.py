"""`scripts/migrate-index-interval.sh` moves the stored old default, and only it.

OWNER DECISION, BGD 2026-10-28 (#2524): "Index interval: a ONE-TIME
MIGRATION, not a read-time rule. Reads of min_change_interval_minutes stay
literal." Every registration created before #2524 stored
`index.min_change_interval_minutes = 30` (`repositories._index_defaults`);
this script lists those and, only with --apply and a typed confirmation that
SWARM_ASSUME_YES does not answer, rewrites them to 120.

Asserted against the REAL script with a fake `curl` (which IS Firestore here)
and a fake `gcloud` (the access token) on PATH, sharing one state file:

  * the dry run, the default, lists every registration storing exactly the
    integer 30, prints the count, and writes nothing;
  * --apply with SWARM_ASSUME_YES and no terminal writes nothing, and neither
    does --apply answered wrongly at a terminal;
  * --apply answered `migrate-<count>` patches only the 30s, each with an
    updateMask naming only `index.min_change_interval_minutes` and an
    updateTime precondition, and keeps every other index setting;
  * a registration edited between the listing and the write is refused by the
    precondition, named, and the run exits non-zero; a re-run finds it.

The SWARM_ASSUME_YES guard is also MUTATED: with the `unset` removed, the same
run must patch, so the refusal test is shown to be able to see a write.

WHAT THIS CANNOT PROVE: that the real Firestore REST API answers in the shapes
the fake serves. They are the shapes `scripts/lib/common.sh` (`fs_list`,
`fs_patch`) already speaks and `tests/unit/scripts/test_offboard_tenant.py`
fakes the same way.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT_REL = Path("scripts") / "migrate-index-interval.sh"
PROJECT = "swarm-test-project"
DATABASE = "swarm"
DOCS = f"projects/{PROJECT}/databases/{DATABASE}/documents"
FIELD = "index.min_change_interval_minutes"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="migrate-index-interval.sh needs bash and jq",
)

#: Firestore over the curl shape `fs_request` uses:
#:   -sS -m T -K - -X M [-H ...] [--data-binary B] -o FILE -w '%{http_code}' URL
#: FAKE_EDIT_AFTER_LIST=<repo_id>: that document's updateTime moves right after
#: it is listed -- an edit landing between the listing and the write.
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
STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    state = json.load(fh)
base = "projects/%s/databases/%s/documents" % (os.environ["FAKE_PROJECT"], os.environ["FAKE_DATABASE"])
parts = urlsplit(url)
path = parts.path[len("/v1/"):]
query = parse_qs(parts.query)


def log(**kw):
    with open(os.environ["FAKE_FS_LOG"], "a") as fh:
        fh.write(json.dumps(kw) + "\n")


def answer(code, body):
    with open(opts["-o"], "w") as fh:
        json.dump(body, fh)
    sys.stdout.write(str(code))
    sys.exit(0)


log(method=method, path=path, query=query)
if method == "GET" and path == base + "/repositories":
    docs = [dict(doc, name=base + "/repositories/" + rid) for rid, doc in sorted(state["docs"].items())]
    edited = os.environ.get("FAKE_EDIT_AFTER_LIST")
    if edited in state["docs"]:
        state["docs"][edited]["updateTime"] = "2026-10-28T12:00:00.000001Z"
        with open(STATE, "w") as fh:
            json.dump(state, fh)
    answer(200, {"documents": docs})
if method == "PATCH" and path.startswith(base + "/repositories/"):
    rid = path.rsplit("/", 1)[1]
    doc = state["docs"].get(rid)
    want = query.get("currentDocument.updateTime", [None])[0]
    if doc is None or (want is not None and want != doc["updateTime"]):
        answer(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                               "message": "the stored version does not match"}})
    body = json.loads(opts["--data-binary"])["fields"]
    for field_path in query.get("updateMask.fieldPaths", []):
        src, dst = body, doc["fields"]
        keys = field_path.split(".")
        for key in keys[:-1]:
            src = src[key]["mapValue"]["fields"]
            dst = dst.setdefault(key, {"mapValue": {"fields": {}}})["mapValue"]["fields"]
        dst[keys[-1]] = src[keys[-1]]
    doc["updateTime"] = "2026-10-28T13:00:00.000001Z"
    with open(STATE, "w") as fh:
        json.dump(state, fh)
    answer(200, dict(doc, name=path))
answer(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": path}})
'''

FAKE_GCLOUD = r'''#!/bin/sh
case "$*" in
  *print-access-token*) printf '%s\n' "$FAKE_ACCESS" ;;
  *) exit 1 ;;
esac
'''


def _int(n: int) -> dict:
    return {"integerValue": str(n)}


def _registration(tenant: str, repo: str, minutes: dict | None) -> dict:
    index = {"interval_hours": _int(24), "on_change": {"stringValue": "poll"},
             "full_every_days": _int(7)}
    if minutes is not None:
        index["min_change_interval_minutes"] = minutes
    return {
        "updateTime": "2026-10-01T09:00:00.000001Z",
        "fields": {
            "tenant_id": {"stringValue": tenant},
            "owner": {"stringValue": "example-org"},
            "repo": {"stringValue": repo},
            "index": {"mapValue": {"fields": index}},
        },
    }


def _world() -> dict:
    return {"docs": {
        "gh_old_a": _registration("eng", "alpha", _int(30)),
        "gh_old_b": _registration("ops", "bravo", _int(30)),
        "gh_new": _registration("eng", "charlie", _int(120)),
        "gh_chosen": _registration("eng", "delta", _int(60)),
        "gh_none": _registration("eng", "echo", None),
        # Not the integer `_index_defaults` wrote: left alone.
        "gh_double": _registration("eng", "foxtrot", {"doubleValue": 30.0}),
        "gh_string": _registration("eng", "golf", {"stringValue": "30"}),
    }}


THIRTIES = ("gh_old_a", "gh_old_b")


class Run:
    def __init__(self, rc: int, stdout: str, stderr: str, state: dict, log: list[dict]):
        self.rc, self.stdout, self.stderr, self.state, self.log = rc, stdout, stderr, state, log

    @property
    def patches(self) -> list[dict]:
        return [e for e in self.log if e["method"] == "PATCH"]

    @property
    def lists(self) -> list[dict]:
        return [e for e in self.log if e["method"] == "GET"]

    def minutes(self, rid: str):
        return self.state["docs"][rid]["fields"]["index"]["mapValue"]["fields"].get(
            "min_change_interval_minutes")


def _run(tmp_path: Path, state: dict, *args: str, typed: str | None = None,
         mutate: tuple[str, str] | None = None, **env_extra: str) -> Run:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    script = root / SCRIPT_REL
    if mutate is not None:
        old, new = mutate
        text = script.read_text()
        assert text.count(old) == 1, f"the mutation target {old!r} has moved"
        script.write_text(text.replace(old, new))
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
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "FIRESTORE_DATABASE": DATABASE,
        "NO_COLOR": "1",
        "TMPDIR": str(tmp_path),
        "FAKE_STATE": str(state_file),
        "FAKE_PROJECT": PROJECT,
        "FAKE_DATABASE": DATABASE,
        "FAKE_FS_LOG": str(fs_log),
        # Built at runtime: nothing here may look like a credential.
        "FAKE_ACCESS": "fake-" + "x" * 12,
        **env_extra,
    })
    cmd = ["bash", str(script), *args]
    if typed is None:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120, check=False)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    else:
        # A real terminal on stdin, so `[[ -t 0 ]]` is true and the answer is TYPED.
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.close(slave)
            os.write(master, (typed + "\n").encode())
            stdout, stderr = proc.communicate(timeout=120)
            rc = proc.returncode
        finally:
            os.close(master)
    log = [json.loads(x) for x in fs_log.read_text().splitlines() if x] if fs_log.exists() else []
    return Run(rc, stdout, stderr, json.loads(state_file.read_text()), log)


# ---------------------------------------------------------------------------
# the house rules
# ---------------------------------------------------------------------------

def test_the_script_keeps_the_house_rules():
    script = REPO / SCRIPT_REL
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert os.access(script, os.X_OK), "the script is not executable"
    effective = [line for line in text.splitlines()[1:]
                 if line.strip() and not line.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"
    assert 'lib/common.sh"' in effective[1]
    # bash 3.2: no associative arrays, no mapfile/readarray, no ${var,,}.
    for banned in (r"declare\s+-A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+,,\}"):
        assert not re.search(banned, text), banned
    assert "unset SWARM_ASSUME_YES" in text


# ---------------------------------------------------------------------------
# the dry run
# ---------------------------------------------------------------------------

def test_the_dry_run_lists_the_30s_prints_the_count_and_writes_nothing(tmp_path):
    world = _world()
    run = _run(tmp_path, world)
    assert run.rc == 0, run.stderr
    listed = [line.split("\t")[0] for line in run.stdout.splitlines() if "\t" in line]
    assert sorted(listed) == sorted(THIRTIES)
    assert "gh_old_b\tops\texample-org/bravo" in run.stdout
    assert f"2 registration(s) store {FIELD} = 30" in run.stdout
    assert run.lists, "the dry run never read the collection; its count proves nothing"
    assert run.patches == []
    assert run.state == world


def test_a_world_with_no_30s_counts_zero(tmp_path):
    world = {"docs": {"gh_new": _registration("eng", "charlie", _int(120))}}
    run = _run(tmp_path, world, "--apply")
    assert run.rc == 0, run.stderr
    assert f"0 registration(s) store {FIELD} = 30" in run.stdout
    assert run.patches == []


# ---------------------------------------------------------------------------
# --apply without the typed confirmation
# ---------------------------------------------------------------------------

def test_apply_with_assume_yes_and_no_terminal_writes_nothing(tmp_path):
    world = _world()
    run = _run(tmp_path, world, "--apply", SWARM_ASSUME_YES="1")
    assert run.rc != 0
    assert "ignoring SWARM_ASSUME_YES" in run.stderr
    assert run.patches == []
    assert run.state == world


def test_apply_answered_wrongly_writes_nothing(tmp_path):
    world = _world()
    run = _run(tmp_path, world, "--apply", typed="yes", SWARM_ASSUME_YES="1")
    assert run.rc != 0
    assert "confirmation did not match" in run.stderr
    assert run.patches == []
    assert run.state == world


def test_the_assume_yes_guard_is_what_refuses(tmp_path):
    """MUTATION: without the `unset`, SWARM_ASSUME_YES answers the confirmation
    and the run writes -- so the refusal above is the guard's doing."""
    run = _run(tmp_path, _world(), "--apply", SWARM_ASSUME_YES="1",
               mutate=("  unset SWARM_ASSUME_YES\n", "  :\n"))
    assert run.rc == 0, run.stderr
    assert len(run.patches) == len(THIRTIES)


# ---------------------------------------------------------------------------
# --apply with it
# ---------------------------------------------------------------------------

def test_apply_with_the_confirmation_patches_only_the_30s(tmp_path):
    world = _world()
    run = _run(tmp_path, world, "--apply", typed="migrate-2")
    assert run.rc == 0, run.stderr
    assert sorted(p["path"].rsplit("/", 1)[1] for p in run.patches) == sorted(THIRTIES)
    for patch in run.patches:
        assert patch["query"]["updateMask.fieldPaths"] == [FIELD]
        assert patch["query"]["currentDocument.updateTime"] == ["2026-10-01T09:00:00.000001Z"]
    for rid in THIRTIES:
        assert run.minutes(rid) == _int(120)
        # Every other index setting is kept.
        kept = run.state["docs"][rid]["fields"]["index"]["mapValue"]["fields"]
        assert kept["interval_hours"] == _int(24) and kept["full_every_days"] == _int(7)
    for rid, doc in world["docs"].items():
        if rid not in THIRTIES:
            assert run.state["docs"][rid] == doc, rid
    assert "2 of 2 registration(s) moved to 120" in run.stdout

    again = _run(tmp_path / "again", run.state, "--apply")
    assert again.rc == 0, again.stderr
    assert f"0 registration(s) store {FIELD} = 30" in again.stdout
    assert again.patches == []


def test_a_registration_edited_after_the_listing_is_not_overwritten(tmp_path):
    run = _run(tmp_path, _world(), "--apply", typed="migrate-2",
               FAKE_EDIT_AFTER_LIST="gh_old_b")
    assert run.rc != 0
    assert "gh_old_b (ops) not moved" in run.stderr
    assert run.minutes("gh_old_a") == _int(120)
    assert run.minutes("gh_old_b") == _int(30)
    assert "1 of 2 registration(s) moved to 120" in run.stdout
