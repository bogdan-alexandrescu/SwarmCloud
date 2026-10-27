"""An expired pool token mid-task reloads and resumes instead of failing (#1855).

Task task_109f1389d53342a1a2f0 ran 6,160 s and then failed on "Failed to
authenticate. API Error: 401 OAuth access token has expired." The broker
refreshes a held account before its token expires (#626) and the worker has a
reload-and-resume path for a refused credential -- but the runner never
recognised an EXPIRED token as one, so the attempt failed as an ordinary
error. These run the real worker and the real claude-code runner against a
fake CLI that prints the observed text and exits non-zero, then finishes once
resumed.

MUTATION: drop the two expiry markers from `cliagent._CREDENTIAL_MARKERS`:
the first run's failure is an ordinary runner failure, no
`credential_reloaded` event is written, there is no second start, and the
task does not reach SUCCEEDED.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_worker.errors import ExitCode
from agent_worker.runners.cliagent import RELOAD_RESUME_PROMPT
from swarm_common.states import TaskState

from worker_seeds import seed_attempt, seed_tenant

#: Records each start in PLAN_DIR/runs.jsonl. Start i behaves per
#: `plan.json`'s `modes[i]`: "expired" prints the observed text (as the
#: stream's result event, or on stderr) and exits 1; "finish" succeeds.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, pathlib, sys
argv = sys.argv[1:]
plan_dir = pathlib.Path(PLAN_DIR)
plan = json.loads((plan_dir / "plan.json").read_text())
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
with (plan_dir / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "stdin": sys.stdin.read()}) + "\n")
runs = sum(1 for _ in (plan_dir / "runs.jsonl").open())
mode = plan["modes"][min(runs - 1, len(plan["modes"]) - 1)]
session = resume or plan["session"]
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
observed = ("Failed to authenticate. API Error: 401 OAuth access token has expired. "
            "Re-authenticate to continue.")
if mode == "expired":
    print(json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "result": observed, "session_id": session}))
    sys.exit(1)
if mode == "expired-stderr":
    print(observed, file=sys.stderr)
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "session_id": session}))
'''


@pytest.fixture
def cli(tmp_path, monkeypatch) -> Path:
    binary = tmp_path / "fake-claude"
    plan = tmp_path / "plan"
    plan.mkdir()
    binary.write_text(FAKE_CLI.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def _run(db, worker_factory, tmp_path, *, modes, session):
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the long work"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _config, _ = worker_factory(runner_profile="claude-code")
    plan = tmp_path / "plan"
    (plan / "plan.json").write_text(json.dumps({"modes": modes, "session": session}))
    code = worker.run()
    runs_file = plan / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if (
        runs_file.exists()) else []
    return code, runs


def _reloads(db) -> list[dict]:
    return [e["detail"] for e in db.events("task_1")
            if (e.get("detail") or {}).get("cause") == "credential_reloaded"]


@pytest.mark.parametrize("mode", ["expired", "expired-stderr"])
def test_an_expired_token_reloads_the_credential_and_resumes_the_session(
    db, worker_factory, tmp_path, cli, log_stream, mode
):
    session = str(uuid.uuid4())
    code, runs = _run(db, worker_factory, tmp_path, modes=[mode, "finish"], session=session)

    reloads = _reloads(db)
    assert len(reloads) == 1, (
        "an expired OAuth token did not reach the credential reload path: "
        + log_stream.getvalue()[-3000:]
    )
    assert reloads[0]["resumed"] is True
    assert [r["resume"] for r in runs] == [None, session]
    assert runs[1]["stdin"] == RELOAD_RESUME_PROMPT
    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["state"] != TaskState.FAILED.value
    # A reload is not the task's fault: it spends none of its attempts.
    assert int(task.get("attempt_count", 1)) == 1
