"""A CLI agent's task with no repository: its deliverables reach Artifacts (#184).

Found in the post-deploy QA of #184 on 2026-09-26: `task_0e5b1f8b7bc1448fafdf`,
a claude-code task with no repository, wrote `answer.md` and `primes.txt` in its
working folder. Neither was uploaded -- only `$SWARM_ARTIFACTS_DIR` ever was --
so the task SUCCEEDED and its Artifacts tab showed the runner's logs and nothing
the agent made.

The owner's decision, recorded on #184 the same day, and what each test holds:

1. Every claude-code and codex prompt gets ONE line naming
   `$SWARM_ARTIFACTS_DIR`: files written there are uploaded and shown in
   Artifacts. Built in one place for both runners, and worded as the owner
   worded it -- information, not an order -- so a repository task is not told
   to put its deliverable anywhere but its diff (decision 3).
2. For a task with NO repository, the worker also uploads, when the attempt
   ends, the files the agent CREATED in its working folder: not files that were
   there before the runner started, not the artifacts folder a second time. At
   most 50 files and 25 MiB. Dot folders, caches, `node_modules`, `.venv`,
   `__pycache__` and symlinks are skipped, and a symlink is never followed out
   of the workspace. A file over a cap is listed as "not uploaded: over cap" in
   the attempt's result, never dropped silently.
3. A repository task is unchanged.

Every test here runs the production worker, the production claude-code runner
and a real stand-in agent executable started through `CLAUDE_CODE_BIN`, the way
`test_work_artifacts_link.py` does. The stand-in writes RELATIVE to the folder
the runner started it in, which is what the real agent did.

The uploaded files are named `workdir/<path>` in the manifest. The keys and the
wording are spelled out rather than imported: each is a field of a document that
outlives the process that wrote it, so a rename is a compatibility change a test
should notice.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from agent_worker.runners.base import RunnerContext
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

PROFILE = "claude-code"

#: The runner writes these into `artifacts/` itself on every claude-code attempt.
RUNNER_OWN_ARTIFACTS = {
    "claude-code.stdout.log",
    "claude-code.stderr.log",
    "claude-transcript.json",
}

#: `result_summary` key describing the working-folder upload.
WORKDIR_KEY = "workdir_outputs"

#: The owner's words for a file that did not fit.
OVER_CAP = "over cap"

#: The other reasons a created file is listed and not uploaded (#225 review).
HOLDS_SECRET = "holds a registered secret that could not be redacted"
CORE_DUMP = "a core dump"
NOT_UTF8 = "name is not valid UTF-8"
NAME_TOO_LONG = "name is longer than a storage object's 1024 bytes"

#: The worker's WARNING line naming the files it did not upload.
NOT_UPLOADED_LINE = "files the agent created in its working folder were not uploaded"

#: The QA task's two files, as the agent wrote them.
ANSWER = "# Answer\n\nThe first ten primes are listed in primes.txt.\n"
PRIMES = "2\n3\n5\n7\n11\n13\n17\n19\n23\n29\n"


def deliverables_line(artifacts_dir: Path | str) -> str:
    """The owner's line, as it must reach the agent. Spelled out, not imported.

    The owner's words on #184: "one line naming $SWARM_ARTIFACTS_DIR: files
    written there are uploaded and shown in Artifacts". Information, not an
    order: every repository task gets this line too, and decision 3 says its
    deliverable is its diff or pull request.
    """
    return (
        f"Files written to {os.path.abspath(artifacts_dir)} ($SWARM_ARTIFACTS_DIR) "
        "are uploaded and shown in Artifacts."
    )


#: How every copy of the line starts, and ends once the directory is named.
LINE_START = "Files written to "
LINE_END = "/artifacts ($SWARM_ARTIFACTS_DIR) are uploaded and shown in Artifacts."


#: A stand-in for `claude --print`. Its plan is the JSON value the prompt starts
#: with; the platform's instructions follow it, so only the leading value is
#: decoded. Everything it writes is relative to its working directory unless the
#: plan says `write_artifact`.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
try:
    plan, _end = json.JSONDecoder().raw_decode(prompt)
except ValueError:
    plan = {}


def put(rel, data):
    path = pathlib.Path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


for rel, text in sorted((plan.get("write") or {}).items()):
    put(rel, text.encode("utf-8"))
for rel, size in sorted((plan.get("write_bytes") or {}).items()):
    put(rel, b"x" * int(size))
for rel, text in sorted((plan.get("append") or {}).items()):
    with open(rel, "a") as fh:
        fh.write(text)
for rel, target in sorted((plan.get("symlink") or {}).items()):
    path = pathlib.Path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
for rel, text in sorted((plan.get("write_artifact") or {}).items()):
    path = artifacts / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
for rel in plan.get("write_key_into") or []:
    put(rel, ("token=" + key + "\n").encode("utf-8"))
# Bytes that are not UTF-8 around the key, the way a core dump's environment
# block holds it: the text rewrite cannot touch this file.
for rel in plan.get("write_key_binary_into") or []:
    put(rel, b"\x7fELF\x00\xff\xfe" + key.encode("utf-8") + b"\x00\xff")
for rel, hexdata in sorted((plan.get("write_binary") or {}).items()):
    put(rel, bytes.fromhex(hexdata))
# Names that are not UTF-8, given as hex: an unzipped CP437 archive, a clone
# with Latin-1 file names.
for hexname, text in sorted((plan.get("write_raw_name") or {}).items()):
    with open(bytes.fromhex(hexname), "wb") as fh:
        fh.write(text.encode("utf-8"))
for hexname, text in sorted((plan.get("write_raw_name_artifact") or {}).items()):
    with open(os.path.join(os.fsencode(artifacts), bytes.fromhex(hexname)), "wb") as fh:
        fh.write(text.encode("utf-8"))

out = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
if plan.get("echo_prompt"):
    out["prompt"] = prompt
print(json.dumps(out))
"""


@pytest.fixture
def agent_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed(db: Any, plan: dict[str, Any], *, task_id: str = "task_1", attempt_id: str = "att_1",
          lease_id: str = "lease_1", metadata: dict[str, Any] | None = None) -> None:
    seed_attempt(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        runner_profile=PROFILE,
        task_input={"prompt": json.dumps(plan)},
    )
    # Without the provider on the tenant, the task parks for a missing
    # credential before the agent starts, and every assertion below is vacuous.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    if metadata is not None:
        db.doc(f"tasks/{task_id}")["metadata"] = dict(metadata)


def _run(worker_factory: Any, **kwargs: Any) -> int:
    kwargs.setdefault("runner_profile", PROFILE)
    worker, _config, _exporter = worker_factory(**kwargs)
    return worker.run()


def _summary(db: Any, task_id: str = "task_1") -> dict[str, Any]:
    return db.doc(f"tasks/{task_id}")["result_summary"] or {}


def _names(db: Any, task_id: str = "task_1") -> list[str]:
    return [entry["name"] for entry in _summary(db, task_id).get("artifacts", [])]


def _workdir_names(db: Any, task_id: str = "task_1") -> set[str]:
    return {name for name in _names(db, task_id) if name.startswith("workdir/")}


def _object(store: LocalObjectStore, name: str, *, task_id: str = "task_1",
            attempt_id: str = "att_1") -> bytes:
    return store.download_bytes(
        f"tenants/{TENANT}/tasks/{task_id}/attempts/{attempt_id}/artifacts/{name}"
    )


def _agent_output(db: Any, task_id: str = "task_1") -> dict[str, Any]:
    output = (_summary(db, task_id).get("runner") or {}).get("output") or {}
    assert isinstance(output, dict), f"the runner envelope was truncated: {output!r}"
    return output.get("structured_output") or {}


def _succeeded(db: Any, task_id: str = "task_1") -> None:
    task = db.doc(f"tasks/{task_id}")
    assert task["state"] == TaskState.SUCCEEDED.value, (task["state"], task.get("last_error"))


# ---------------------------------------------------------------------------
# 2. the QA case: what the agent left in its working folder is uploaded
# ---------------------------------------------------------------------------


def test_the_qa_case_answer_md_and_primes_txt_are_both_uploaded(db, store, worker_factory, agent_cli):
    """task_0e5b1f8b7bc1448fafdf's two files, written where it wrote them."""
    _seed(db, {"write": {"answer.md": ANSWER, "primes.txt": PRIMES}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    names = _names(db)
    assert "workdir/answer.md" in names, names
    assert "workdir/primes.txt" in names, names
    # The runner's own files are still there, so this is not a manifest that
    # was replaced; and nothing else crept in from the working folder.
    assert RUNNER_OWN_ARTIFACTS <= set(names), names
    assert _workdir_names(db) == {"workdir/answer.md", "workdir/primes.txt"}
    # The bytes are in the bucket, under the attempt's artifacts prefix, which
    # is where the API's Artifacts routes read a manifest entry from.
    assert _object(store, "workdir/answer.md").decode("utf-8") == ANSWER
    assert _object(store, "workdir/primes.txt").decode("utf-8") == PRIMES
    entry = next(e for e in _summary(db)["artifacts"] if e["name"] == "workdir/primes.txt")
    assert entry["bytes"] == len(PRIMES.encode("utf-8"))

    block = _summary(db)[WORKDIR_KEY]
    assert block["uploaded"] == 2
    assert block["not_uploaded"] == []
    assert block["not_uploaded_count"] == 0


def test_a_file_the_agent_left_in_its_working_folder_does_not_satisfy_an_expected_output(
    db, worker_factory, agent_cli
):
    """#149's option (c) stays rejected: the working-folder copy is
    `workdir/scan-01.md`, and a later step staging `scan-01.md` still finds it
    missing. The attempt fails retryably exactly as before."""
    _seed(db, {"write": {"scan-01.md": "scan\n"}}, metadata={"expected_outputs": ["scan-01.md"]})

    assert _run(worker_factory) == ExitCode.FAILED
    assert "workdir/scan-01.md" in _names(db)
    assert "scan-01.md" not in _names(db)
    assert _summary(db).get("expected_outputs_missing") == ["scan-01.md"]


# ---------------------------------------------------------------------------
# 1. the prompt line, once, for both CLI runners
# ---------------------------------------------------------------------------


def test_a_claude_code_prompt_carries_the_deliverables_line_exactly_once(
    db, worker_factory, agent_cli
):
    plan = {"echo_prompt": True}
    _seed(db, plan)

    assert _run(worker_factory) == ExitCode.OK
    prompt = _agent_output(db)["prompt"]
    # The caller's own prompt first, whole.
    assert prompt.startswith(json.dumps(plan)), prompt
    lines = [line for line in prompt.splitlines() if line.startswith(LINE_START)]
    assert len(lines) == 1, prompt
    assert lines[0].endswith(LINE_END), lines[0]
    assert os.path.isabs(lines[0].split(" ")[3])
    # Information, in the owner's words, not an order.
    assert "Write deliverables" not in prompt, prompt


def test_the_deliverables_line_is_not_repeated_when_later_steps_expect_files(
    db, worker_factory, agent_cli
):
    plan = {"echo_prompt": True, "write_artifact": {"scan-01.md": "scan\n"}}
    _seed(db, plan, metadata={"expected_outputs": ["scan-01.md"]})

    assert _run(worker_factory) == ExitCode.OK
    prompt = _agent_output(db)["prompt"]
    assert prompt.count(LINE_START) == 1, prompt
    assert prompt.count("$SWARM_ARTIFACTS_DIR") == 1, prompt
    # The expected-output list is still there, with each file's full path.
    assert "Later steps of this workflow need these files from you: scan-01.md." in prompt
    assert "/artifacts/scan-01.md" in prompt


def _recording_cli(tmp_path: Path) -> Path:
    binary = tmp_path / "recording-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.path.join(os.environ['SWARM_WORK_DIR'], '.argv.json'), 'w') as fh:\n"
        "    json.dump(sys.argv, fh)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok'}))\n"
    )
    binary.chmod(0o755)
    return binary


def _ctx(tmp_path: Path, payload: dict[str, Any]) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


@pytest.mark.parametrize("runner", ["claude_code", "codex"])
@pytest.mark.parametrize(
    "payload",
    [{"prompt": "compute primes"}, {"prompt": "compute primes", "expected_outputs": ["p.txt"]}],
    ids=["nothing-expected", "expected-outputs"],
)
def test_both_cli_runners_end_the_prompt_with_the_line_once(tmp_path, monkeypatch, runner, payload):
    """The real claude-code and codex specs, through the one runner they share."""
    import importlib

    from agent_worker.runners.cliagent import run_cli_agent

    module = importlib.import_module(f"agent_worker.runners.{runner}")
    spec = module.SPEC
    monkeypatch.setenv(spec.binary_env, str(_recording_cli(tmp_path)))
    monkeypatch.delenv(spec.args_env, raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    for name in (spec.key_env, *spec.alt_key_envs):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(spec.key_env, "sk-test-value-0123456789")

    ctx = _ctx(tmp_path, dict(payload))
    run_cli_agent(ctx, spec)
    prompt = json.loads((ctx.work_dir / ".argv.json").read_text())[-1]

    assert prompt.startswith("compute primes\n\n"), prompt
    assert prompt.count(deliverables_line(ctx.artifacts_dir)) == 1, prompt
    assert prompt.count("$SWARM_ARTIFACTS_DIR") == 1, prompt
    if "expected_outputs" not in payload:
        # Nothing else is appended when no later step expects a file.
        assert prompt == f"compute primes\n\n{deliverables_line(ctx.artifacts_dir)}"


# ---------------------------------------------------------------------------
# 2. created, not pre-existing
# ---------------------------------------------------------------------------


def _run_upstream_mock(db: Any, worker_factory: Any, *, name: str, text: str) -> None:
    """A real upstream attempt that uploads one artifact, for `input_from`."""
    seed_attempt(
        db,
        task_id="task_up",
        attempt_id="att_up",
        lease_id="lease_up",
        task_input={
            "prompt": "produce",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": name,
            "artifact_text": text,
        },
    )
    worker, _config, _exporter = worker_factory(
        task_id="task_up", attempt_id="att_up", lease_id="lease_up"
    )
    assert worker.run() == ExitCode.OK
    _succeeded(db, "task_up")


def test_files_that_were_there_before_the_runner_started_are_not_uploaded(
    db, store, worker_factory, agent_cli
):
    """A staged input is the platform's, placed before the runner starts; the
    control files are the worker's. Neither is a deliverable, and an input the
    agent MODIFIED was still not created by it."""
    _run_upstream_mock(db, worker_factory, name="summary.md", text="upstream\n")
    _seed(
        db,
        {"append": {"summary.md": "edited by the agent\n"}, "write": {"answer.md": ANSWER}},
        metadata={"input_from": {"task_up": "summary.md"}},
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _summary(db)["staged_inputs"][0]["path"] == "summary.md", "nothing was staged"
    assert _workdir_names(db) == {"workdir/answer.md"}, _names(db)


def test_a_resumed_attempt_uploads_what_an_earlier_attempt_created(
    db, store, tmp_path, worker_factory, agent_cli
):
    """The manifest a reader sees is the FINAL attempt's. A file an earlier
    attempt's agent created comes back from the checkpoint before this runner
    starts, and is still the agent's deliverable; the control file beside it is
    not."""
    earlier = workspace_mod.create(tmp_path / "earlier-ws", "att_0")
    (earlier.work / "answer.md").write_text(ANSWER)
    (earlier.work / "input.json").write_text("{}")

    class _Quiet:
        def info(self, *a: Any, **k: Any) -> None: ...
        def warning(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_0",
        generation=1, logger=_Quiet(),
    ).create(earlier)
    _seed(db, {"write": {"primes.txt": PRIMES}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _summary(db).get("restored_from", {}).get("attempt_id") == "att_0", "nothing was restored"
    assert _workdir_names(db) == {"workdir/answer.md", "workdir/primes.txt"}, _names(db)
    assert _object(store, "workdir/answer.md").decode("utf-8") == ANSWER


def test_a_name_taken_in_the_artifacts_folder_keeps_its_place(db, store, worker_factory, agent_cli):
    """The artifacts folder is uploaded first and keeps its names. The
    working-folder file of the same manifest name is listed, not uploaded."""
    _seed(
        db,
        {
            "write_artifact": {"workdir/answer.md": "from the artifacts folder\n"},
            "write": {"answer.md": "from the working folder\n"},
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    names = _names(db)
    assert names.count("workdir/answer.md") == 1, names
    assert _object(store, "workdir/answer.md") == b"from the artifacts folder\n"
    listed = _summary(db)[WORKDIR_KEY]["not_uploaded"]
    assert [e["name"] for e in listed] == ["workdir/answer.md"], listed
    assert listed[0]["reason"].startswith("name taken"), listed


def test_a_registered_secret_in_a_working_folder_file_is_redacted_before_upload(
    db, store, worker_factory, agent_cli
):
    _seed(db, {"write_key_into": ["notes.md"]})

    assert _run(worker_factory) == ExitCode.OK
    body = _object(store, "workdir/notes.md").decode("utf-8")
    assert "test-key-for-" not in body, body
    assert "***REDACTED***" in body, body


def _artifact_objects(store: LocalObjectStore) -> list[str]:
    return store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/")


def test_a_working_folder_file_holding_a_secret_it_cannot_redact_is_not_uploaded(
    db, store, worker_factory, agent_cli
):
    """The artifacts folder's trade does not carry over (#225 review).

    A binary file in `$SWARM_ARTIFACTS_DIR` is uploaded as-is even when its raw
    bytes hold a registered secret: the agent chose to deliver it, and
    corrupting it to take the key out is the wrong trade. The working-folder
    upload is a net under files nobody chose to deliver, so a file the worker
    has MEASURED to hold the tenant's credential, and cannot rewrite, stays in
    the pod. A binary file with no key in it is uploaded exactly as before.
    """
    png = "89504e470d0a1a0a0000ff00"
    _seed(db, {"write_key_binary_into": ["dump.bin"], "write_binary": {"picture.png": png}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    names = _workdir_names(db)
    assert "workdir/dump.bin" not in names, names
    assert "workdir/picture.png" in names, names
    assert _object(store, "workdir/picture.png") == bytes.fromhex(png)

    listed = {e["name"]: e for e in _summary(db)[WORKDIR_KEY]["not_uploaded"]}
    assert listed["workdir/dump.bin"]["reason"] == HOLDS_SECRET, listed
    # It never left the pod, so it is not reported as a file that left unredacted.
    unredacted = _summary(db).get("redaction_skipped") or []
    assert not [e for e in unredacted if "dump.bin" in str(e.get("file"))], unredacted
    for key in _artifact_objects(store):
        assert b"test-key-for-" not in store.download_bytes(key), key


def test_a_core_dump_is_never_uploaded(db, store, worker_factory, agent_cli):
    """A program the agent built and ran, crashing with the working folder as
    its cwd, leaves `core` or `core.<pid>` there: its memory, environment
    block included, and the CLI's credential is in that environment. Never a
    deliverable, listed so a reader learns something crashed. A file whose name
    only starts with "core" is an ordinary file."""
    _seed(
        db,
        {
            "write_key_binary_into": ["core"],
            "write_binary": {"core.4242": "7f454c4602010100", "build/core.77": "7f454c46"},
            "write": {"answer.md": ANSWER, "core.txt": "notes on the core module\n"},
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == {"workdir/answer.md", "workdir/core.txt"}, _names(db)
    listed = {e["name"]: e["reason"] for e in _summary(db)[WORKDIR_KEY]["not_uploaded"]}
    assert listed == {
        "workdir/core": CORE_DUMP,
        "workdir/core.4242": CORE_DUMP,
        "workdir/build/core.77": CORE_DUMP,
    }, listed


# ---------------------------------------------------------------------------
# 2. the caps: 50 files, 25 MiB, and nothing dropped silently
# ---------------------------------------------------------------------------


def test_the_caps_are_the_owners_50_files_and_25_mib(worker_factory):
    _worker, config, _exporter = worker_factory(runner_profile=PROFILE)
    assert config.max_workdir_output_files == 50
    assert config.max_workdir_output_bytes == 25 * 1024 * 1024


def test_past_the_file_cap_the_rest_are_listed_as_not_uploaded_over_cap(
    db, store, worker_factory, agent_cli
):
    files = {f"f{index:02d}.txt": f"file {index}\n" for index in range(55)}
    _seed(db, {"write": files})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    uploaded = _workdir_names(db)
    assert uploaded == {f"workdir/f{index:02d}.txt" for index in range(50)}, sorted(uploaded)

    block = _summary(db)[WORKDIR_KEY]
    assert block["uploaded"] == 50
    assert block["not_uploaded_count"] == 5
    assert block["not_uploaded"] == [
        {"name": f"workdir/f{index:02d}.txt", "bytes": len(f"file {index}\n"), "reason": OVER_CAP}
        for index in range(50, 55)
    ]
    # NOT in `artifacts_skipped` (#225 review). Every reader of that list
    # calls its names dropped at the artifacts folder's SIZE cap -- the tab's
    # "dropped at the size cap", a dependant's "exceeded its artifact size
    # cap" -- and these were dropped at the working folder's FILE cap. The
    # tab reads them, with their reasons, from `workdir_outputs`.
    skipped = _summary(db).get("artifacts_skipped") or []
    assert not [name for name in skipped if name.startswith("workdir/")], skipped


def test_past_the_byte_cap_a_file_is_listed_and_smaller_ones_still_fit(
    db, store, worker_factory, agent_cli
):
    """Shallowest first, then by path: `a.txt`, `big.bin`, `sub/b.txt`."""
    _seed(db, {"write_bytes": {"a.txt": 10, "big.bin": 2000, "sub/b.txt": 10}})

    assert _run(worker_factory, max_workdir_output_bytes=1000) == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == {"workdir/a.txt", "workdir/sub/b.txt"}, _names(db)
    block = _summary(db)[WORKDIR_KEY]
    assert block["not_uploaded"] == [{"name": "workdir/big.bin", "bytes": 2000, "reason": OVER_CAP}]
    assert block["uploaded_bytes"] == 20
    assert "workdir/big.bin" not in (_summary(db).get("artifacts_skipped") or [])


def _logged(log_stream: Any, message: str) -> list[dict[str, Any]]:
    records = []
    for line in log_stream.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("message") == message:
            records.append(record)
    return records


def test_every_file_not_uploaded_is_named_in_the_worker_log_past_the_first_50(
    db, worker_factory, agent_cli, log_stream
):
    """The attempt's result lists 50 and counts the rest, because it is a
    Firestore document with a 1 MiB limit. The worker's log names EVERY one
    (#225 review: past 50 the names were nowhere, though a comment said the
    log carried them)."""
    files = {f"f{index:03d}.txt": "x\n" for index in range(130)}
    _seed(db, {"write": files})

    assert _run(worker_factory, max_workdir_output_files=2) == ExitCode.OK
    _succeeded(db)
    block = _summary(db)[WORKDIR_KEY]
    assert block["uploaded"] == 2
    assert block["not_uploaded_count"] == 128
    assert len(block["not_uploaded"]) == 50

    logged = [name for record in _logged(log_stream, NOT_UPLOADED_LINE) for name in record["files"]]
    assert sorted(logged) == [
        f"workdir/f{index:03d}.txt: not uploaded: {OVER_CAP}" for index in range(2, 130)
    ], logged


# ---------------------------------------------------------------------------
# 2. what is skipped, and symlinks
# ---------------------------------------------------------------------------


def test_dot_folders_caches_node_modules_venvs_and_pycache_are_skipped(
    db, worker_factory, agent_cli
):
    _seed(
        db,
        {
            "write": {
                # kept
                "report.md": "the report\n",
                "deep/dir/result.csv": "a,b\n1,2\n",
                # dot folders, and dot files: HOME is the working folder
                ".hidden/notes.txt": "x",
                ".claude.json": "{}",
                "deep/dir/.cache/y": "x",
                # named skips
                "node_modules/pkg/index.js": "x",
                ".venv/bin/activate": "x",
                "__pycache__/m.cpython-311.pyc": "x",
                "pkg/__pycache__/n.cpython-311.pyc": "x",
                # caches, by name and by CACHEDIR.TAG
                "build_cache/blob": "x",
                "pip-caches/wheel": "x",
                "tagged/CACHEDIR.TAG": "Signature: 8a477f597d28d172789f06886806bc55\n",
                "tagged/data.bin": "x",
                # a virtual environment under any name
                "env/pyvenv.cfg": "home = /usr/bin\n",
                "env/lib/site.py": "x",
            }
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == {"workdir/report.md", "workdir/deep/dir/result.csv"}, _names(db)


def test_a_symlink_is_never_followed_out_of_the_workspace(
    db, store, tmp_path, worker_factory, agent_cli
):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = "OUTSIDE-THE-WORKSPACE-7f3a91"
    (outside / "secret.txt").write_text(marker + "\n")
    (outside / "other.txt").write_text(marker + "\n")
    _seed(
        db,
        {
            "write": {"answer.md": ANSWER},
            "symlink": {
                "leak.txt": str(outside / "secret.txt"),
                "leakdir": str(outside),
                "nested/escape.txt": "../../../../outside/secret.txt",
            },
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == {"workdir/answer.md"}, _names(db)
    assert _summary(db)[WORKDIR_KEY]["symlinks_skipped"] == 3
    # Nothing under the task's prefix holds what the links point at.
    for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/"):
        if key.endswith(".tar.gz"):
            continue  # an archive records a link as a link, never its target's bytes
        assert marker.encode() not in store.download_bytes(key), key


def test_the_read_refuses_a_link_swapped_in_after_the_scan(tmp_path):
    """The scan skips links, but an orphaned agent process can still be running
    when the worker reads. Every component is opened without following."""
    from agent_worker import standalone_outputs as standalone

    work = tmp_path / "work"
    (work / "real").mkdir(parents=True)
    (work / "real" / "f.txt").write_text("inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("outside\n")
    os.symlink(outside, work / "linkdir")
    os.symlink(outside / "f.txt", work / "leaf.txt")
    os.mkfifo(work / "pipe")

    for relative in ("linkdir/f.txt", "leaf.txt", "pipe", "../outside/f.txt"):
        with pytest.raises(standalone.Refused):
            standalone.copy_without_following(work, relative, tmp_path / "copy", limit=1 << 20)
        assert not (tmp_path / "copy").exists(), relative

    assert standalone.copy_without_following(work, "real/f.txt", tmp_path / "copy", limit=100) == 7
    assert (tmp_path / "copy").read_text() == "inside\n"

    # Over the bytes left: refused, and the partial copy removed.
    (tmp_path / "copy").unlink()
    with pytest.raises(standalone.OverCap):
        standalone.copy_without_following(work, "real/f.txt", tmp_path / "copy", limit=3)
    assert not (tmp_path / "copy").exists()


def test_a_working_folder_replaced_by_a_link_is_not_walked(tmp_path):
    from agent_worker import standalone_outputs as standalone

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("outside\n")
    os.symlink(outside, tmp_path / "work")

    assert dict(standalone.scan(tmp_path / "work").files) == {}
    with pytest.raises(standalone.Refused):
        standalone.copy_without_following(tmp_path / "work", "f.txt", tmp_path / "copy", limit=100)


# ---------------------------------------------------------------------------
# names the result summary and the bucket cannot carry (#225 review)
# ---------------------------------------------------------------------------


def _filesystem_takes_non_utf8_names(tmp_path: Path) -> bool:
    probe = os.path.join(os.fsencode(tmp_path), b"probe-\xe9")
    try:
        with open(probe, "wb"):
            pass
    except OSError:
        return False
    os.unlink(probe)
    return True


def test_a_name_that_is_not_utf8_is_listed_escaped_and_the_result_is_still_written(
    db, store, tmp_path, worker_factory, agent_cli, log_stream
):
    """`os.walk` hands a name that is not UTF-8 over as a str holding lone
    surrogates. A protobuf string cannot carry one, so a single such name in
    `result_summary` made `finish` raise, `_safe_finish` raise again, and the
    reconciler re-run the task -- whose checkpoint brings the same file back,
    so every attempt ended the same way. The name is listed as the bytes it
    was, spelled the way `ls -b` and the API's checkpoint listing
    (`checkpoint_content.displayable`) spell it, and never used as a key. The
    artifacts folder had the same hole, and is held here too."""
    if not _filesystem_takes_non_utf8_names(tmp_path):
        # APFS refuses such names outright, so no agent on a Mac can make one.
        # CI runs on Linux, where they are ordinary, and must not skip.
        assert not sys.platform.startswith("linux"), "a Linux filesystem refused a non-UTF-8 name"
        pytest.skip("this filesystem refuses names that are not UTF-8")
    raw = b"caf\xe9.txt".hex()
    _seed(
        db,
        {
            "write": {"answer.md": ANSWER},
            "write_raw_name": {raw: "cafe\n"},
            "write_raw_name_artifact": {raw: "cafe\n"},
        },
    )

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    summary = _summary(db)
    # What Firestore needs: every string in the document is UTF-8.
    json.dumps(summary, ensure_ascii=False).encode("utf-8")
    # And the worker's log, written to stdout as UTF-8 under LC_ALL=C.UTF-8.
    for line in log_stream.getvalue().splitlines():
        line.encode("utf-8")

    listed = {e["name"]: e for e in summary[WORKDIR_KEY]["not_uploaded"]}
    assert listed["workdir/caf\\xe9.txt"] == {
        "name": "workdir/caf\\xe9.txt",
        "bytes": 5,
        "reason": NOT_UTF8,
    }, listed
    assert "workdir/answer.md" in _names(db)
    assert "caf\\xe9.txt" in (summary.get("artifacts_skipped") or []), summary.get("artifacts_skipped")
    assert not [name for name in _names(db) if name.endswith(".txt") and "caf" in name], _names(db)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="a 1,200-byte path is past macOS's PATH_MAX"
)
def test_a_path_longer_than_a_storage_object_name_is_listed_shortened(
    db, store, worker_factory, agent_cli
):
    """GCS names an object in at most 1,024 bytes of UTF-8, so a longer path
    cannot be uploaded under its name. Its whole spelling -- up to 4,096 bytes
    -- in each of 50 entries would also take a fifth of the 1 MiB Firestore
    allows the task document. It is listed with its name cut short."""
    deep = "/".join(["d" * 40] * 30) + "/f.txt"
    _seed(db, {"write": {"answer.md": ANSWER, deep: "deep\n"}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == {"workdir/answer.md"}, _names(db)
    (entry,) = _summary(db)[WORKDIR_KEY]["not_uploaded"]
    assert entry["reason"] == NAME_TOO_LONG, entry
    assert entry["bytes"] == 5
    assert entry["name"].startswith("workdir/" + "d" * 40 + "/"), entry
    assert len(entry["name"].encode("utf-8")) <= 300, len(entry["name"])


# ---------------------------------------------------------------------------
# 3. unchanged: a repository task, and runners that are not CLI agents
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_repository_task_is_unchanged(db, tmp_path, monkeypatch, worker_factory, agent_cli):
    """The diff or the pull request is a repository task's deliverable. A file
    it leaves beside the checkout is not uploaded, and the summary carries no
    working-folder block."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True)

    # `validate_repository_url` allows https and ssh only; a `file://` URL under
    # this test's own tmp_path is let through, every other URL is not.
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(
        gitops, "validate_repository_url", lambda url: url if url.startswith(allowed) else real(url)
    )

    plan = {"echo_prompt": True, "write": {"answer.md": ANSWER, "repo/agent.txt": "a change\n"}}
    _seed(db, plan)
    db.doc("tasks/task_1")["repository_url"] = f"file://{origin}"

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    summary = _summary(db)
    assert "git" in summary, "no repository was cloned, so this test proves nothing"
    assert summary["git"].get("dirty_count", 0) >= 1, summary["git"]
    assert _workdir_names(db) == set(), _names(db)
    assert WORKDIR_KEY not in summary

    # ITS PROMPT (#225 review). Decision 1 reaches every claude-code prompt,
    # this one included, and decision 3 says a repository task's deliverable
    # is its diff or pull request. So the line it gets is the owner's
    # information -- where files ARE uploaded from -- and nothing the platform
    # appends orders the agent to write its deliverable anywhere. An order
    # here is how "write docs/design.md" ends up in the artifacts folder and
    # out of the pull request.
    prompt = _agent_output(db)["prompt"]
    assert prompt.startswith(json.dumps(plan)), prompt
    appended = prompt[len(json.dumps(plan)):]
    lines = [line for line in appended.splitlines() if line.strip()]
    assert len(lines) == 1, appended
    assert lines[0].startswith(LINE_START) and lines[0].endswith(LINE_END), lines[0]
    assert "Write deliverables" not in prompt, prompt
    assert not any(line.lstrip().lower().startswith("write") for line in lines), lines


def test_a_mock_task_uploads_nothing_from_its_working_folder(db, worker_factory):
    """The mock writes `progress/` and `mock_state.json` into its working folder
    on every run; it is not an agent, and every smoke test runs it."""
    seed_attempt(db, task_input={"prompt": "hello", "steps": 2, "sleep_seconds": 0.05})

    assert _run(worker_factory, runner_profile="mock") == ExitCode.OK
    _succeeded(db)
    assert _workdir_names(db) == set(), _names(db)
    assert WORKDIR_KEY not in _summary(db)


# ---------------------------------------------------------------------------
# the scan, on its own
# ---------------------------------------------------------------------------


def test_the_scan_keeps_regular_files_and_counts_the_links_it_passed(tmp_path):
    from agent_worker import standalone_outputs as standalone

    work = tmp_path / "work"
    (work / "a" / "b").mkdir(parents=True)
    (work / "top.md").write_text("t")
    (work / "a" / "b" / "deep.txt").write_text("dd")
    (work / "input.json").write_text("{}")
    os.symlink(work / "top.md", work / "alias.md")
    os.symlink(work / "a", work / "alias-dir")

    found = standalone.scan(work, reserved={"input.json"})
    assert dict(found.files) == {"top.md": 1, "a/b/deep.txt": 2}
    assert found.symlinks == ("alias-dir", "alias.md")
    assert standalone.upload_order({"z/y.txt": 1, "b.txt": 1, "a/x.txt": 1}) == [
        ("b.txt", 1),
        ("a/x.txt", 1),
        ("z/y.txt", 1),
    ]
    assert standalone.created(found, {"top.md"}) == {"a/b/deep.txt": 2}
