"""`MAX_DECLARED_NAMES` is enforced, and tied to the frozen workflow-step limit (#227).

Found in review of #232: `artifact_manifest.MAX_DECLARED_NAMES` (49) is the
number the manifest's 1 MiB budget is computed from -- up to that many declared
names are exempt from `MAX_NAME_BYTES` and held only to GCS's 1,024-byte limit
-- and nothing enforced it. It held only while `swarm_common`'s
`max_workflow_steps` stayed 50. These tests hold both ends:

* the constant is at least `max_workflow_steps - 1`, read from the FROZEN
  default, so a raised step limit fails here instead of quietly starving a
  legitimate declared output of its exemption;
* `plan()` exempts at most `MAX_DECLARED_NAMES` declared names and returns the
  ones past it, so the budget's arithmetic holds whatever reaches the worker;
* the worker names those in its own log, scrubbed before they are cut (review
  of this change: a stdlib logger in `plan()` wrote caller-supplied names to
  stderr unscrubbed, cut first, with no task context).

The `plan()` tests are pure. The log test runs the production worker's
`_upload_outputs` against the fakes, with no cloud and no emulator.
"""

from __future__ import annotations

import dataclasses
import io
import json

from agent_worker import artifact_manifest as manifest_mod
from agent_worker import workspace as workspace_mod
from swarm_common.config import Settings

from worker_seeds import TENANT, build_worker, seed_attempt, seed_tenant
from fakes import FakeSecretClient

#: The worker's WARNING line naming declared outputs past the bound: spelled
#: out, as the line an operator searches for.
PAST_BOUND_LINE = (
    "declared outputs past the manifest's bound are held to its name length like any other file"
)


def _frozen_default(name: str) -> int:
    """A field's default on the frozen `Settings`, without building one."""
    for f in dataclasses.fields(Settings):
        if f.name == name:
            assert f.default is not dataclasses.MISSING, name
            return int(f.default)
    raise AssertionError(f"swarm_common.config.Settings has no field {name!r}")


def _long(i: int) -> str:
    """A declared name past `MAX_NAME_BYTES` and under GCS's own limit."""
    name = f"{i:03d}-" + "x" * (manifest_mod.MAX_NAME_BYTES + 10) + ".md"
    assert manifest_mod.MAX_NAME_BYTES < len(name.encode()) < 1024
    return name


def test_the_declared_bound_covers_every_upstream_step_a_workflow_can_have() -> None:
    """Each dependant maps one upstream step to one filename, and a workflow
    has at most `max_workflow_steps` steps including this one."""
    max_steps = _frozen_default("max_workflow_steps")
    assert manifest_mod.MAX_DECLARED_NAMES >= max_steps - 1, (
        f"max_workflow_steps is {max_steps}, so a task can have {max_steps - 1} "
        f"declared outputs, but MAX_DECLARED_NAMES is {manifest_mod.MAX_DECLARED_NAMES}: "
        "raise it AND redo the manifest's 1 MiB budget in artifact_manifest.py"
    )


def test_up_to_the_bound_every_declared_long_name_is_taken() -> None:
    names = [_long(i) for i in range(manifest_mod.MAX_DECLARED_NAMES)]
    plan = manifest_mod.plan(
        {name: 1 for name in names}, first=names, cap=500, declared=names
    )
    assert sorted(plan.take) == sorted(names)
    assert plan.not_uploaded == ()
    assert plan.declared_past_bound == ()


def test_declared_names_past_the_bound_lose_the_exemption() -> None:
    """The budget assumes at most `MAX_DECLARED_NAMES` long names; past it a
    declared name is held to `MAX_NAME_BYTES` like any other, and returned."""
    bound = manifest_mod.MAX_DECLARED_NAMES
    names = [_long(i) for i in range(bound + 3)]
    plan = manifest_mod.plan({name: 1 for name in names}, first=names, cap=500, declared=names)

    assert list(plan.take) == names[:bound]
    assert [e["name"] for e in plan.not_uploaded] == names[bound:]
    assert {e["reason"] for e in plan.not_uploaded} == {manifest_mod.NAME_TOO_LONG}
    assert plan.declared_past_bound == tuple(names[bound:])
    assert len(plan.declared_past_bound) == 3


def test_a_short_declared_name_past_the_bound_is_still_uploaded() -> None:
    """Losing the exemption is not losing the upload: a name that fits is taken."""
    bound = manifest_mod.MAX_DECLARED_NAMES
    names = [f"out-{i:03d}.md" for i in range(bound + 5)]
    plan = manifest_mod.plan({name: 1 for name in names}, first=names, cap=500, declared=names)
    assert sorted(plan.take) == sorted(names)
    assert plan.not_uploaded == ()
    assert plan.declared_past_bound == tuple(names[bound:])


def test_repeated_declared_names_count_once() -> None:
    name = _long(0)
    plan = manifest_mod.plan(
        {name: 1}, first=[name], cap=500, declared=[name] * (manifest_mod.MAX_DECLARED_NAMES + 5)
    )
    assert plan.take == (name,)
    assert plan.declared_past_bound == ()


def _past_bound_lines(log: io.StringIO) -> list[dict]:
    records = []
    for line in log.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("message") == PAST_BOUND_LINE:
            records.append(record)
    return records


def test_the_worker_logs_declared_names_past_the_bound_scrubbed_before_cut(db, store, tmp_path):
    """The names past the bound go to the worker's JSON log, every one of them,
    with the whole count, and a registered secret crossing the 256-character
    cut leaves no fragment: `shown(self._scrub(name))`, not `shown(name)`."""
    key = "sk-ant-api03-" + "S" * 40
    secret_name = "a" * 230 + key + "z" * 36
    start = secret_name.index(key)
    assert start < manifest_mod.MAX_NAME_BYTES < start + len(key)
    leaked_fragment = key[: manifest_mod.MAX_NAME_BYTES - start]
    assert 0 < len(leaked_fragment) < len(key)

    bound = manifest_mod.MAX_DECLARED_NAMES
    declared = [f"in-{i:03d}.md" for i in range(bound)] + [secret_name, "late-1.md", "late-2.md"]

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    log = io.StringIO()
    worker, _config, _ = build_worker(
        db,
        store,
        tmp_path,
        log,
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": key}),
    )
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()  # registers the key for redaction
    worker._expected_outputs = tuple(declared)
    (ws.artifacts / "late-1.md").write_text("x\n")

    worker._upload_outputs()

    lines = _past_bound_lines(log)
    assert len(lines) == 1, lines
    record = lines[0]
    assert record["count"] == 3
    assert record["declared_exempt"] == bound
    assert len(record["names"]) == 3
    assert record["names"][1:] == ["late-1.md", "late-2.md"]
    assert leaked_fragment not in log.getvalue()
    # The control: the name was logged, scrubbed, not dropped.
    assert record["names"][0].startswith("a" * 230)


def test_the_worker_writes_no_past_bound_line_within_the_bound(db, store, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    log = io.StringIO()
    worker, _config, _ = build_worker(db, store, tmp_path, log, runner_profile="claude-code")
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._expected_outputs = tuple(
        f"in-{i:03d}.md" for i in range(manifest_mod.MAX_DECLARED_NAMES)
    )

    worker._upload_outputs()

    assert _past_bound_lines(log) == []
