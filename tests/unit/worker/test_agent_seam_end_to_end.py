"""The seam between a coding-agent CHILD PROCESS and the rest of the platform.

Everything here runs the production `Worker` on the `claude-code` profile with a
real agent child on the other end of `run_child` -- a small executable standing
in for the `claude` binary, started through `CLAUDE_CODE_BIN` exactly as the
deployed Job starts the real one. Nothing is mocked at the seam, because the
seam is what has been breaking.

WHY THIS FILE EXISTS ALONGSIDE THE ONES THAT LOOK LIKE IT.

`test_input_from.py` already proves that a declared artifact reaches a
downstream agent's working directory -- against the MOCK runner, where the
runner and the agent are the same process and the artifact is written by
`ctx.write_artifact` in-process. `test_runner_env_contract.py` already proves
that `SWARM_ARTIFACTS_DIR` appears in `cliagent.py` -- by reading the source
text. Between those two there was no test that RAN a separate agent process and
watched what it wrote travel, and that is exactly the gap the platform's
headline defect lived in:

    runners/cliagent.py never put SWARM_ARTIFACTS_DIR in the agent child
    environment, so no claude-code agent could write an artifact, so
    input_from could never stage anything, so work could not pass between
    agents. The attempt still SUCCEEDED, because writing an artifact is not a
    success condition, and result_summary.artifacts was never empty because the
    RUNNER writes its own stdout/stderr/transcript there.

So these tests are written to fail on that shape specifically:

  * the agent's artifact is asserted APART FROM the runner's own files, by
    name, rather than by `artifacts != []` (which the bug satisfied);
  * the downstream assertion is on the BYTES that arrived in the next step's
    workspace, echoed back out of the agent that read them, rather than on a
    SUCCEEDED state (which the bug also satisfied);
  * `test_an_agent_that_cannot_find_the_directory_...` reproduces the failure
    end to end and pins what it looked like: green upstream, empty handoff.

THE PROMPT IS THE INSTRUCTION CHANNEL, and deliberately so. `input.prompt` is
the only caller-controlled value that reaches the agent's argv (invariant 10),
and the child's environment is built by the platform, not by the test. Handing
the stand-in agent a JSON prompt is therefore the one way to vary its behaviour
that a real caller also has -- so nothing here reaches around the boundary the
tests are meant to be checking.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_api.codec import attempt_from_dict, attempt_to_api
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

PROFILE = "claude-code"

#: What the upstream agent writes and the downstream agent reads back. Chosen to
#: contain no token the redaction pass or the rate-limit detector reacts to: a
#: body containing "rate limit" would be parked rather than completed, and a
#: body containing the resolved credential would come back "***REDACTED***" and
#: the byte comparison below would be asserting the wrong thing.
FINDING_NAME = "finding.md"
FINDING_TEXT = "the upstream agent's finding: marker-7f3a91c4\nsecond line\n"
FINDING_BYTES = len(FINDING_TEXT.encode("utf-8"))

#: The runner writes these into `artifacts/` itself, for every claude-code
#: attempt, whether or not the agent produced anything. They are the reason the
#: original defect was invisible, so every assertion below names them.
RUNNER_OWN_ARTIFACTS = {
    "claude-code.stdout.log",
    "claude-code.stderr.log",
    "claude-transcript.json",
}


# ---------------------------------------------------------------------------
# the stand-in agent
# ---------------------------------------------------------------------------

#: A real executable that behaves like `claude --print --output-format json`:
#: it reads its instructions from the last argv element (the prompt), does what
#: they say, and prints one JSON object on stdout.
#:
#: `artifacts_env` is the variable it looks its output directory up in. The
#: default is the contract's own name; a test that sets it to something else is
#: simulating an agent that was never told where to write -- which is what the
#: real Claude Code agent did, verbatim, on wf_bcdc9180e4fb4a209f31.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

# The plan is the JSON value the prompt STARTS with. Every CLI prompt now ends
# with the platform's line naming $SWARM_ARTIFACTS_DIR (#184), so only the
# leading value is decoded; `json.loads` of the whole prompt would fail, and
# the empty plan it fell back to would make every assertion below vacuous.
try:
    plan, _end = json.JSONDecoder().raw_decode(sys.argv[-1])
except (ValueError, IndexError):
    plan = {}

work = pathlib.Path(os.environ.get("SWARM_WORK_DIR") or os.getcwd())
out = {"type": "result", "subtype": "success", "is_error": False}

# Read back a file the platform was supposed to have staged into the working
# directory, and hand its CONTENT to the caller. This is the only evidence that
# survives the workspace being destroyed.
read_name = plan.get("read")
if read_name:
    source = work / read_name
    if source.exists():
        out["staged_content"] = source.read_text()
        out["staged_bytes"] = len(source.read_bytes())
    else:
        out["staged_content"] = None
        out["staged_missing"] = read_name

# Write a file where the platform said outputs go.
write = plan.get("write")
if write:
    variable = plan.get("artifacts_env", "SWARM_ARTIFACTS_DIR")
    target = os.environ.get(variable)
    if not target:
        out["result"] = (
            "The environment variable %s is not set in this environment, so I "
            "can't determine the target directory." % variable
        )
        out["wrote"] = None
    else:
        path = pathlib.Path(target) / write["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(write["text"])
        out["wrote"] = write["name"]

out.setdefault("result", plan.get("say", "done"))

usage = plan.get("usage")
if usage is not None:
    out["usage"] = usage
cost = plan.get("cost_usd")
if cost is not None:
    out["total_cost_usd"] = cost

print(json.dumps(out))
"""


@pytest.fixture
def agent_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the stand-in agent the way the deployed Job installs the real one.

    `CLAUDE_CODE_BIN` is read by `runners/cliagent._find_binary` and passed
    through by `lifecycle._build_child_env` as a PLATFORM-set variable. Setting
    it here is the same move the Cloud Run Job definition makes; nothing a
    caller sends can reach it.
    """
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    # The catalogue's default argv prefix (--print --output-format json
    # --dangerously-skip-permissions) is left alone: the stand-in ignores flags
    # and reads only the trailing prompt, exactly as the contract requires.
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def prompt_for(**plan: Any) -> str:
    return json.dumps(plan)


def seed_agent_attempt(db: Any, *, task_id: str, attempt_id: str, lease_id: str,
                       plan: dict[str, Any], input_from: dict[str, str] | None = None) -> None:
    """A dispatched claude-code attempt, with the tenant credential registered.

    `credentials` has to name the provider or `resolve_credentials` raises
    CredentialMissing and the task parks before the agent ever starts -- which
    would make every assertion below vacuous rather than failing.
    """
    seed_attempt(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        runner_profile=PROFILE,
        task_input={"prompt": prompt_for(**plan)},
    )
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    if input_from:
        db.doc(f"tasks/{task_id}")["metadata"] = {"input_from": dict(input_from)}


def run_agent_attempt(worker_factory: Any, *, task_id: str, attempt_id: str,
                      lease_id: str) -> int:
    worker, _config, _exporter = worker_factory(
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        runner_profile=PROFILE,
    )
    return worker.run()


def artifact_names(db: Any, task_id: str) -> set[str]:
    summary = db.doc(f"tasks/{task_id}")["result_summary"] or {}
    return {entry["name"] for entry in summary.get("artifacts", [])}


def structured_output(db: Any, task_id: str) -> dict[str, Any]:
    summary = db.doc(f"tasks/{task_id}")["result_summary"] or {}
    runner = summary.get("runner") or {}
    output = runner.get("output") or {}
    assert isinstance(output, dict), (
        "the runner envelope was truncated to a preview string; shrink the "
        f"stand-in agent's output. Got: {output!r}"
    )
    return output.get("structured_output") or {}


# ---------------------------------------------------------------------------
# 1. an agent-written artifact is harvested, and is not one of the runner's own
# ---------------------------------------------------------------------------


def test_the_agents_own_artifact_is_harvested_apart_from_the_runners(
    db, store, worker_factory, agent_cli
):
    """`result_summary.artifacts` must contain something the AGENT made.

    The assertion that matters is the set DIFFERENCE. `artifacts` was never
    empty while the defect was live -- the runner's stdout, stderr and
    transcript were always in it -- so `assert artifacts` passes on a platform
    where no agent can produce anything at all.
    """
    seed_agent_attempt(
        db,
        task_id="task_produce",
        attempt_id="att_produce",
        lease_id="lease_produce",
        plan={"write": {"name": FINDING_NAME, "text": FINDING_TEXT}},
    )

    assert run_agent_attempt(
        worker_factory,
        task_id="task_produce",
        attempt_id="att_produce",
        lease_id="lease_produce",
    ) == ExitCode.OK

    task = db.doc("tasks/task_produce")
    assert task["state"] == TaskState.SUCCEEDED.value

    names = artifact_names(db, "task_produce")
    assert RUNNER_OWN_ARTIFACTS <= names, (
        "the runner's own files are missing, so this test is no longer "
        f"distinguishing anything: {sorted(names)}"
    )
    agent_written = names - RUNNER_OWN_ARTIFACTS
    assert agent_written == {FINDING_NAME}, (
        "the agent wrote nothing that reached the harvest. artifacts contained "
        f"only the runner's own files: {sorted(names)}. That is the "
        "SWARM_ARTIFACTS_DIR defect -- a green attempt with an empty handoff."
    )

    # And the bytes really are in the bucket under this attempt's prefix, which
    # is where a downstream step's input_from resolves them from.
    key = f"tenants/{TENANT}/tasks/task_produce/attempts/att_produce/artifacts/{FINDING_NAME}"
    assert store.download_bytes(key).decode("utf-8") == FINDING_TEXT

    entry = next(
        e for e in task["result_summary"]["artifacts"] if e["name"] == FINDING_NAME
    )
    assert entry["bytes"] == FINDING_BYTES
    assert entry["uri"] == store.uri(key)


def test_the_agent_is_told_where_to_write_by_the_platform_not_by_the_caller(
    db, store, worker_factory, agent_cli
):
    """The variable is the contract's, and nothing in `input` sets it.

    The stand-in agent looks its output directory up in whatever variable the
    prompt names. Asking it for a name the platform does not export proves the
    directory is not arriving by some other route -- an inherited environment,
    a default, a guess -- which would make the test above pass for the wrong
    reason.
    """
    seed_agent_attempt(
        db,
        task_id="task_probe",
        attempt_id="att_probe",
        lease_id="lease_probe",
        plan={
            "write": {"name": "should-not-exist.md", "text": "x"},
            "artifacts_env": "SWARM_OUTPUT_DIR",
        },
    )

    assert run_agent_attempt(
        worker_factory, task_id="task_probe", attempt_id="att_probe",
        lease_id="lease_probe",
    ) == ExitCode.OK

    assert artifact_names(db, "task_probe") == RUNNER_OWN_ARTIFACTS
    assert structured_output(db, "task_probe")["wrote"] is None


# ---------------------------------------------------------------------------
# 2. the CONTENT reaches the next step's workspace
# ---------------------------------------------------------------------------


def test_step_two_reads_the_bytes_step_ones_agent_wrote(
    db, store, worker_factory, agent_cli
):
    """The whole handoff, agent to agent, asserted on content.

    A SUCCEEDED downstream state proves nothing here: the failing platform
    succeeded at step 1 too. The claim is that the downstream AGENT -- a
    separate process, in a separate workspace, on the far side of a GCS
    round trip -- read back exactly the bytes the upstream AGENT wrote.
    """
    seed_agent_attempt(
        db,
        task_id="task_produce",
        attempt_id="att_produce",
        lease_id="lease_produce",
        plan={"write": {"name": FINDING_NAME, "text": FINDING_TEXT}},
    )
    assert run_agent_attempt(
        worker_factory, task_id="task_produce", attempt_id="att_produce",
        lease_id="lease_produce",
    ) == ExitCode.OK

    seed_agent_attempt(
        db,
        task_id="task_consume",
        attempt_id="att_consume",
        lease_id="lease_consume",
        plan={"read": FINDING_NAME, "say": "read the upstream finding"},
        input_from={"task_produce": FINDING_NAME},
    )
    assert run_agent_attempt(
        worker_factory, task_id="task_consume", attempt_id="att_consume",
        lease_id="lease_consume",
    ) == ExitCode.OK

    consume = db.doc("tasks/task_consume")
    assert consume["state"] == TaskState.SUCCEEDED.value

    output = structured_output(db, "task_consume")
    assert output.get("staged_missing") is None, (
        "the downstream agent could not find the file it was promised: "
        f"{output.get('staged_missing')!r}"
    )
    assert output["staged_content"] == FINDING_TEXT
    assert output["staged_bytes"] == FINDING_BYTES

    # The platform's own record of the handoff agrees with what the agent saw.
    staged = consume["result_summary"]["staged_inputs"]
    assert staged == [
        {
            "task_id": "task_produce",
            "filename": FINDING_NAME,
            "path": FINDING_NAME,
            "bytes": FINDING_BYTES,
            "uri": store.uri(
                f"tenants/{TENANT}/tasks/task_produce/attempts/att_produce/"
                f"artifacts/{FINDING_NAME}"
            ),
        }
    ]


def test_an_agent_that_cannot_find_the_directory_breaks_the_handoff_silently(
    db, store, worker_factory, agent_cli
):
    """The defect, reproduced end to end, so its shape stays pinned.

    This is the regression in negative form: the upstream attempt is GREEN, its
    artifact list is non-empty, and the platform's headline feature is dead.
    Both halves are asserted, because it was the first half that stopped anyone
    noticing the second.
    """
    seed_agent_attempt(
        db,
        task_id="task_produce",
        attempt_id="att_produce",
        lease_id="lease_produce",
        plan={
            "write": {"name": FINDING_NAME, "text": FINDING_TEXT},
            # The agent looks up a variable the platform does not export: the
            # deployed failure, expressed through the one channel a caller has.
            "artifacts_env": "SWARM_OUTPUT_DIR",
        },
    )

    assert run_agent_attempt(
        worker_factory, task_id="task_produce", attempt_id="att_produce",
        lease_id="lease_produce",
    ) == ExitCode.OK

    produce = db.doc("tasks/task_produce")
    assert produce["state"] == TaskState.SUCCEEDED.value, (
        "the upstream attempt is supposed to SUCCEED here -- that is the whole "
        "problem"
    )
    assert artifact_names(db, "task_produce") == RUNNER_OWN_ARTIFACTS

    seed_agent_attempt(
        db,
        task_id="task_consume",
        attempt_id="att_consume",
        lease_id="lease_consume",
        plan={"read": FINDING_NAME},
        input_from={"task_produce": FINDING_NAME},
    )
    exit_code = run_agent_attempt(
        worker_factory, task_id="task_consume", attempt_id="att_consume",
        lease_id="lease_consume",
    )

    consume = db.doc("tasks/task_consume")
    assert exit_code != ExitCode.OK
    assert consume["state"] != TaskState.SUCCEEDED.value, (
        "a step whose promised input never arrived must not report success; "
        "its prompt is written as though the file is there"
    )
    assert FINDING_NAME in (consume["last_error"] or "")
    assert "task_produce" in (consume["last_error"] or "")

    # Capacity is still returned on the refusal path.
    assert db.doc("leases/lease_consume")["released_at"] is not None


# ---------------------------------------------------------------------------
# 3. spend reported by the agent survives all the way to the API's JSON
# ---------------------------------------------------------------------------


def test_spend_the_agent_reported_reaches_the_shape_the_api_serves(
    db, store, worker_factory, agent_cli
):
    """The whole chain, in one test, because every link had its own passing test.

    agent JSON -> cliagent's `structured_output` envelope -> `_usage_summary`
    -> `control.record_spend` -> the attempt document -> `attempt_from_dict`
    -> `attempt_to_api`.

    `test_usage_summary.py` covers the extractor and
    `test_attempt_spend_round_trip.py` covers the codec, but the two met
    nowhere: the codec test starts from a hand-written dict, so nothing checked
    that the keys `record_spend` WRITES are the keys `attempt_from_dict` READS.
    A rename on either side would have kept both suites green and put every
    cost figure in the product back to null.
    """
    usage = {
        "input_tokens": 1234,
        "output_tokens": 567,
        "cache_read_input_tokens": 89,
        "cache_creation_input_tokens": 10,
    }
    seed_agent_attempt(
        db,
        task_id="task_spend",
        attempt_id="att_spend",
        lease_id="lease_spend",
        plan={"say": "counted", "usage": usage, "cost_usd": 0.4213},
    )
    assert run_agent_attempt(
        worker_factory, task_id="task_spend", attempt_id="att_spend",
        lease_id="lease_spend",
    ) == ExitCode.OK

    document = db.doc("attempts/att_spend")
    assert document is not None, "no attempt document was written"

    served = attempt_to_api(attempt_from_dict(document))
    assert served["input_tokens"] == 1234
    assert served["output_tokens"] == 567
    assert served["cache_read_input_tokens"] == 89
    assert served["cache_creation_input_tokens"] == 10
    assert served["cost_usd"] == pytest.approx(0.4213)

    # Present-when-present, stated as the pair the original bug broke: the
    # absent-when-absent half passed throughout and proved nothing.
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cost_usd",
    ):
        assert served[field] is not None, (
            f"{field} was recorded on the attempt document and the API serves "
            "null for it -- this is the decoder dropping it"
        )


def test_a_task_that_reported_no_spend_serves_null_not_zero(
    db, store, worker_factory, agent_cli
):
    """The other half of the pair, kept so the fix cannot overshoot.

    "not reported" and "cost nothing" are different facts, and a decoder that
    turned the first into 0 would be just as wrong in the other direction.
    """
    seed_agent_attempt(
        db,
        task_id="task_nospend",
        attempt_id="att_nospend",
        lease_id="lease_nospend",
        plan={"say": "no usage block at all"},
    )
    assert run_agent_attempt(
        worker_factory, task_id="task_nospend", attempt_id="att_nospend",
        lease_id="lease_nospend",
    ) == ExitCode.OK

    served = attempt_to_api(attempt_from_dict(db.doc("attempts/att_nospend")))
    for field in ("input_tokens", "output_tokens", "cost_usd"):
        assert served[field] is None


def test_the_credential_never_reaches_an_artifact_or_the_summary(
    db, store, worker_factory, agent_cli
):
    """The agent holds the key, so the agent's output is where it would leak.

    Asserted on the whole harvested set rather than on the transcript alone: the
    value is registered for redaction once and every channel is scrubbed from
    the same list, so a channel added later without scrubbing shows up here.
    """
    secret = "test-key-for-swarm-tenant-eng-anthropic"
    seed_agent_attempt(
        db,
        task_id="task_leak",
        attempt_id="att_leak",
        lease_id="lease_leak",
        plan={"say": "configuration echoed below", "write": {
            "name": "notes.txt", "text": "the agent may write anything here"}},
    )
    assert run_agent_attempt(
        worker_factory, task_id="task_leak", attempt_id="att_leak",
        lease_id="lease_leak",
    ) == ExitCode.OK

    assert secret not in json.dumps(db.doc("tasks/task_leak")["result_summary"])
    prefix = f"tenants/{TENANT}/tasks/task_leak/attempts/att_leak/"
    for key in store.list_keys(prefix):
        assert secret.encode("utf-8") not in store.download_bytes(key), key
