"""The worker-template posture gate judges each CONTAINER, not each file.

THE DEFECT THIS PINS. `scripts/lib/validate-manifests.sh` asserted the posture
with `grep` over each template file, so a property set on ONE container
satisfied the check for EVERY container in that file. The audit that introduced
the per-class rule recorded it as a known residual
(docs/audits/2026-09-23/worker-job-v2-fails-the-posture-gate.md, "the check is
whole-file, not per-container"), and noted that its own first mutation table
produced three false greens for exactly this reason.

Three concrete ways the grep passed a template it should have refused, each
reproduced below against a copy of the real repository:

  * a second container in the same pod inherits nothing, yet the grep found the
    first container's lines and passed both (`worker-job-v2.yaml`'s init
    container losing `allowPrivilegeEscalation: false` or `drop: ["ALL"]`, and
    an unhardened sidecar added to `worker-job.yaml`);
  * `runAsNonRoot: true` at POD level satisfied the grep while the container
    overrode it to `false`, and the container's value is the one that runs;
  * the gVisor class was chosen by grepping for `runtimeClassName: gvisor`, so a
    COMMENT naming it moved an unsandboxed template onto the relaxed rules.

Each mutation is made to a copy of `scripts/`, `kubernetes/` and the two
`apps/` packages `render.py` imports, and the REAL validator is run over that
copy -- so what is under test is the shipped script, not a restatement of it.
The unmodified copy must pass, or the negative cases prove nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = Path("kubernetes") / "worker-templates"

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "node_modules", ".pytest_cache")


def _copy_tree(tmp_path: Path) -> Path:
    for rel in ("scripts", "kubernetes", "apps/common", "apps/quota-broker"):
        shutil.copytree(ROOT / rel, tmp_path / rel, ignore=IGNORE)
    return tmp_path


def _mutate(tree: Path, template: str, old: str, new: str) -> None:
    path = tree / TEMPLATES / template
    text = path.read_text()
    assert text.count(old) == 1, (
        f"the mutation anchor is not unique in {template} ({text.count(old)} matches); "
        "a mutation that changes nothing, or changes the wrong occurrence, is the "
        "false green this file exists to prevent"
    )
    path.write_text(text.replace(old, new))


def _validate(tree: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # The validator runs a bare `python3` that must be able to import yaml; the
    # interpreter running this test can, so put it first.
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["NO_COLOR"] = "1"
    env.pop("SWARM_ENV_FILE", None)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "lib" / "validate-manifests.sh")],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _refused(result: subprocess.CompletedProcess[str], *needles: str) -> None:
    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "the posture gate PASSED a template with an unhardened container:\n" + output
    )
    for needle in needles:
        assert needle in output, (
            f"the gate failed, but not naming {needle!r}; a refusal that does not say "
            f"which container is wrong sends the reader to the wrong one:\n{output}"
        )


def test_the_unmodified_templates_pass(tmp_path):
    result = _validate(_copy_tree(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_init_container_without_allow_privilege_escalation_false_is_refused(tmp_path):
    tree = _copy_tree(tmp_path)
    _mutate(
        tree,
        "worker-job-v2.yaml",
        "            runAsUser: 0\n"
        "            allowPrivilegeEscalation: false\n"
        "            capabilities:\n"
        '              drop: ["ALL"]\n'
        "          env:\n",
        "            runAsUser: 0\n"
        "            capabilities:\n"
        '              drop: ["ALL"]\n'
        "          env:\n",
    )
    _refused(_validate(tree), "install-credential", "allowPrivilegeEscalation")


def test_an_init_container_that_keeps_its_capabilities_is_refused(tmp_path):
    tree = _copy_tree(tmp_path)
    _mutate(
        tree,
        "worker-job-v2.yaml",
        "            allowPrivilegeEscalation: false\n"
        "            capabilities:\n"
        '              drop: ["ALL"]\n'
        "          env:\n",
        "            allowPrivilegeEscalation: false\n"
        "          env:\n",
    )
    _refused(_validate(tree), "install-credential", "ALL")


def test_a_container_that_overrides_the_pods_run_as_non_root_is_refused(tmp_path):
    tree = _copy_tree(tmp_path)
    _mutate(
        tree,
        "worker-job.yaml",
        "            readOnlyRootFilesystem: true\n            runAsNonRoot: true\n",
        "            readOnlyRootFilesystem: true\n            runAsNonRoot: false\n",
    )
    _refused(_validate(tree), "worker", "runAsNonRoot")


def test_an_unhardened_sidecar_is_refused(tmp_path):
    tree = _copy_tree(tmp_path)
    _mutate(
        tree,
        "worker-job.yaml",
        "      containers:\n",
        "      containers:\n"
        "        - name: sidecar\n"
        "          image: __IMAGE__\n"
        '          command: ["sleep", "3600"]\n',
    )
    _refused(_validate(tree), "sidecar")


def test_a_comment_naming_gvisor_does_not_relax_an_unsandboxed_template(tmp_path):
    tree = _copy_tree(tmp_path)
    _mutate(
        tree,
        "worker-job-browser.yaml",
        "            readOnlyRootFilesystem: true\n",
        "            # runtimeClassName: gvisor is what a sandboxed pod would say\n",
    )
    _refused(_validate(tree), "worker", "readOnlyRootFilesystem")
