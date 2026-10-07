"""The release's tenant-namespace apply runs render.py on the project's Python.

#346 (#354 review): the `deploy` job's "apply each tenant namespace with its
step-spec public keys" step calls `kubernetes/apply.sh`, which runs
`kubernetes/render.py` with `${PYTHON_BIN:-python3}` -- whatever python3 the
runner image ships, in a job that installed no Python. render.py imports
`quota_broker`, `scheduler` and `swarm_common` from apps/ by `sys.path`.

MEASURED 2026-10-07 on this checkout: that import chain is stdlib-only today,
and `python3 -S kubernetes/render.py tenant ...` (no site-packages) renders.
So the step would not fail on its first run as the review said; it would fail
the first time any of those three packages, at module level, imports a
third-party package -- and that would surface only in a release, on the
first run after the owner switched the step on, with no test to catch it.

WHAT IS PINNED. Before that step, in the same job, uv is set up and the
project is synced from the lockfile, and the step hands apply.sh the synced
interpreter through PYTHON_BIN -- the variable apply.sh actually reads.
The control: render.py still imports from apps/, and apply.sh still reads
PYTHON_BIN, so the premise is checked, not assumed.

WHAT THIS CANNOT PROVE: that the step succeeds against a cluster. It is off
until the owner switches it on, and it needs one.
"""

from __future__ import annotations

import re

from .test_release_reuses_ci_images import REPO, _workflow

STEP = "apply each tenant namespace with its step-spec public keys"
VENV_PYTHON = ".venv/bin/python"


def _deploy_steps() -> list[dict]:
    return _workflow("release.yml")["jobs"]["deploy"]["steps"]


def _index(steps: list[dict]) -> int:
    names = [s.get("name") for s in steps]
    assert STEP in names, f"release.yml's deploy job has no step named {STEP!r}"
    return names.index(STEP)


def test_the_premise_render_py_needs_the_projects_packages():
    render = (REPO / "kubernetes" / "render.py").read_text()
    assert re.search(r"^from quota_broker\b", render, re.MULTILINE), (
        "render.py no longer imports quota_broker; re-read #346 before relaxing this file"
    )
    apply_sh = (REPO / "kubernetes" / "apply.sh").read_text()
    assert 'PYTHON="${PYTHON_BIN:-python3}"' in apply_sh, (
        "apply.sh no longer reads PYTHON_BIN; the step below must set whatever it reads now"
    )


def test_uv_is_set_up_and_the_project_synced_before_the_step():
    steps = _deploy_steps()
    before = steps[: _index(steps)]
    assert any(str(s.get("uses", "")).startswith("astral-sh/setup-uv@") for s in before), (
        "the deploy job sets up no uv before the tenant-namespace apply"
    )
    runs = "\n".join(str(s.get("run", "")) for s in before) + "\n" + str(steps[_index(steps)].get("run", ""))
    assert re.search(r"\buv sync --frozen\b", runs), (
        "the project is not synced from the lockfile before render.py runs"
    )


def test_the_step_hands_apply_sh_the_synced_interpreter():
    step = _deploy_steps()[_index(_deploy_steps())]
    run = str(step.get("run", ""))
    env = step.get("env") or {}
    named = str(env.get("PYTHON_BIN", "")) + "\n" + run
    assert re.search(r"PYTHON_BIN=?.*" + re.escape(VENV_PYTHON), named) or (
        VENV_PYTHON in str(env.get("PYTHON_BIN", ""))
    ), "apply.sh would run render.py on the runner's bare python3"
    # Exported before apply.sh runs, so the child sees it.
    if "PYTHON_BIN" not in env:
        export = run.find("export PYTHON_BIN")
        assert export != -1, "PYTHON_BIN is set in the step but never exported to apply.sh"
        assert export < run.find("./kubernetes/apply.sh"), "PYTHON_BIN is exported after apply.sh runs"


def test_the_setup_steps_run_only_when_the_apply_does():
    """Off by default: a release that applies no tenant namespace spends nothing
    installing Python for it."""
    steps = _deploy_steps()
    for step in steps[: _index(steps)]:
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@") or "uv sync" in str(step.get("run", "")):
            assert "vars.APPLY_TENANT_NAMESPACES == 'true'" in str(step.get("if", "")), step
