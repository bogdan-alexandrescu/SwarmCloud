"""Every runner must tell its agent where to put work.

`runners/base.py` documents the contract in its module docstring:

    SWARM_ARTIFACTS_DIR   files written here are uploaded when the attempt ends

`runners/generic.py` exported it. `runners/cliagent.py` did not — so a
claude-code agent was never told where its output should go.

MEASURED, on the first real multi-agent workflow this platform ever ran
(wf_bcdc9180e4fb4a209f31, step `research`). The agent's own result:

    "The environment variable SWARM_ARTIFACTS_DIR is not set in this
     environment, so I can't determine the target directory. Could you
     provide the path or confirm how it should be set?"

It exited 0 and the attempt SUCCEEDED, because writing an artifact is not a
success condition. `result_summary.artifacts` contained exactly
claude-code.stdout.log, claude-code.stderr.log and claude-transcript.json —
the runner's own files, written by the runner using ctx.artifacts_dir — and
nothing the agent produced.

The consequence was the platform's headline feature. With no agent artifact
there is nothing for a downstream step's `input_from` to stage, so work could
not be routed between agents at all. `input_from` itself was correct and
tested; it was tested against the GENERIC runner, and the runner every
coding agent actually uses was missing the variable that makes it possible.

These tests are written against the whole runner registry rather than the two
known runners, so a third runner cannot be added without the same contract.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

RUNNERS_DIR = Path(__file__).resolve().parents[3] / "apps" / "agent-worker" / "agent_worker" / "runners"

#: Variables a runner must place in its child's environment. Each one is the
#: answer to a question the agent cannot otherwise answer about itself.
REQUIRED = ("SWARM_ARTIFACTS_DIR", "SWARM_WORK_DIR")


def _runner_modules() -> list[Path]:
    """Every module that spawns an agent child.

    Identified by calling run_child, not by a hand-written list: a new runner
    that spawns a child is subject to this contract whether or not anyone
    remembered to add it here.
    """
    out = []
    for p in sorted(RUNNERS_DIR.glob("*.py")):
        if p.name in ("__init__.py", "base.py", "limits.py"):
            continue
        text = p.read_text()
        if "run_child(" in text:
            out.append(p)
    return out


def test_there_are_runners_to_check():
    """Guards against the parametrised tests passing vacuously."""
    mods = _runner_modules()
    assert mods, "no runner modules found; the glob or the marker is wrong"
    names = {p.stem for p in mods}
    assert "cliagent" in names, f"cliagent not detected among {names}"
    assert "generic" in names, f"generic not detected among {names}"


@pytest.mark.parametrize("module", _runner_modules(), ids=lambda p: p.stem)
@pytest.mark.parametrize("var", REQUIRED)
def test_every_runner_exports_the_workspace_contract(module, var):
    text = module.read_text()
    assert f'"{var}"' in text, (
        f"{module.name} spawns an agent child and never puts {var} in its "
        f"environment. runners/base.py documents it as the contract; an agent "
        f"without it cannot write an artifact, and a downstream step's "
        f"input_from then has nothing to stage."
    )


def test_the_claude_code_runner_passes_the_real_directories():
    """Not just the NAME present somewhere, but bound to the context's paths.

    A string match alone would pass on a comment. This asserts the value comes
    from ctx, which is what makes the agent's writes land where the harvest
    looks.
    """
    src = (RUNNERS_DIR / "cliagent.py").read_text()
    assert '"SWARM_ARTIFACTS_DIR": str(ctx.artifacts_dir)' in src, (
        "SWARM_ARTIFACTS_DIR must be bound to ctx.artifacts_dir, which is where "
        "the attempt's harvest actually looks"
    )
    assert '"SWARM_WORK_DIR": str(ctx.work_dir)' in src


def test_the_runner_writes_its_own_logs_to_the_same_place_it_advertises():
    """Why the bug was invisible: the runner used ctx.artifacts_dir for its own
    stdout/stderr, so artifacts were never EMPTY -- they just never contained
    anything the agent made. An empty list would have been noticed."""
    from agent_worker.runners.claude_code import SPEC
    from agent_worker.runners.cliagent import cli_stream_files

    src = (RUNNERS_DIR / "cliagent.py").read_text()
    # Since #184 the names come from `cli_stream_files`, the one function the
    # worker's live publisher and final upload also read, and the runner joins
    # them onto ctx.artifacts_dir -- the directory it advertises.
    assert "ctx.artifacts_dir / files.stdout" in src
    assert "ctx.artifacts_dir / files.stderr" in src
    assert cli_stream_files(SPEC).stdout == f"{SPEC.name}.stdout.log"
    assert cli_stream_files(SPEC).stderr == f"{SPEC.name}.stderr.log"
