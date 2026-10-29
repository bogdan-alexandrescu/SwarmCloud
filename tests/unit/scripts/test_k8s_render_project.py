"""Kubernetes rendering takes the project and the subjects from its caller,
never a default (DAYONE-K8S-1, part of #2688; design §7, lessons 29 and 32).

`kubernetes/render.py` defaulted `--project` to the live project, each
control-plane uniqueId to its email and the namespace quota to flat numbers,
and `kubernetes/apply.sh`'s tenant mode never passed a project at all. A
render that can reach a cluster -- one given the cluster network, which only
apply.sh reads -- now names all three or exits 2. The lint renders that run
with no cluster (`make lint`, security.yml, the worker's pre-publish checkov)
keep working and name no project.

No cloud credentials, no cluster: render.py runs in-process or as a child.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import io
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
RENDER = KUBERNETES / "render.py"
APPLY = KUBERNETES / "apply.sh"

#: A project id built at runtime, so no test reads as naming a real one.
PROJECT = f"k8s-render-{secrets.token_hex(4)}"
LIVE_PROJECT = "saga-" + "agents-staging"

#: A cluster network that passes network_values' checks: what apply.sh reads.
NETWORK = (
    "--pod-cidr", "10.200.0.0/14",
    "--service-cidr", "10.204.0.0/20",
    "--cluster-dns-ip", "10.204.0.10",
    "--node-local-dns-ip", "169.254.20.10",
    "--network-source", "gke/test-project/us-central1/test-cluster",
)


def _uid() -> str:
    """A numeric uniqueId in IAM's shape (21 digits), random per call."""
    return str(10**20 + secrets.randbelow(9 * 10**20))


@functools.cache
def _load():
    """render.py, loaded on first use: it imports the service stack, which a
    module-top load would make every collection of tests/unit/scripts pay for."""
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_project", RENDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module



def _main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = _load().main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def test_project_is_required() -> None:
    """A namespace and RBAC render for a cluster, with no --project, exits 2.

    It rendered the live project's scheduler and reconciler into every tenant
    namespace of any project (render.py `--project default=`), and apply.sh's
    tenant mode passed no project.
    """
    uids = ("--scheduler-uid", _uid(), "--reconciler-uid", _uid())
    quota = ("--quota-pods", "10", "--quota-cpu", "40", "--quota-memory", "80Gi",
             "--quota-jobs", "20", "--quota-ephemeral", "100Gi")
    # MUTATION: give tenant's --project a default again and this renders.
    code, out, err = _main("tenant", "--tenant", "eng", *NETWORK, *uids, *quota)
    assert code == 2, (code, out[:200])
    assert "--project" in err and out == ""

    code, out, _ = _main("tenant", "--tenant", "eng", "--project", PROJECT, *NETWORK, *uids, *quota)
    assert code == 0
    assert LIVE_PROJECT not in out
    emails = set(re.findall(r"[a-z-]+@([a-z0-9-]+)\.iam\.gserviceaccount\.com", out))
    assert emails == {PROJECT}, emails

    # apply.sh passes PROJECT_ID to every tenant-mode render, and refuses a
    # forwarded --project, which would come after it and win.
    text = APPLY.read_text()
    for command in ("identity", "verify-read", "tenant"):
        call = re.search(rf'render\.py" {command} --tenant "\$\{{TENANT\}}" --project "\$\{{PROJECT_ID\}}"', text)
        # MUTATION: drop `--project "${PROJECT_ID}"` from one of the three.
        assert call, f"apply.sh's `render.py {command}` does not pass --project \"${{PROJECT_ID}}\""
    assert "PROJECT_FLAGS=(--project)" in text


def test_apply_refuses_an_unset_project(tmp_path: Path) -> None:
    """apply.sh, with no PROJECT_ID in the environment or its env file, renders
    nothing and says why, before it contacts anything (design §7)."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "KUBECONFIG": str(tmp_path / "no-such-kubeconfig"),
        "TMPDIR": str(tmp_path),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        ["bash", str(APPLY), "--tenant", "eng"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    # MUTATION: delete the PROJECT_NOT_GIVEN check and common.sh's default
    # project is read and rendered.
    assert result.returncode != 0
    assert "PROJECT_ID is not set" in result.stderr, result.stderr[-2000:]


def test_subjects_carry_unique_id() -> None:
    """Every RoleBinding subject naming the scheduler or the reconciler is
    rendered by email AND by the numeric uniqueId passed in (lesson 29): GKE
    presents an access-token caller by uniqueId, and an email-only binding
    authorised nobody for eight months. Without the uniqueIds a cluster render
    exits 2 instead of falling back to the email."""
    scheduler, reconciler = _uid(), _uid()
    args = ("tenant", "--tenant", "eng", "--project", PROJECT, *NETWORK,
            "--quota-pods", "10", "--quota-cpu", "40", "--quota-memory", "80Gi",
            "--quota-jobs", "20", "--quota-ephemeral", "100Gi")
    # MUTATION: let the uniqueIds fall back to the email again.
    code, out, err = _main(*args)
    assert code == 2 and "--scheduler-uid" in err and "--reconciler-uid" in err, (code, err)

    code, out, _ = _main(*args, "--scheduler-uid", scheduler, "--reconciler-uid", reconciler)
    assert code == 0
    pairs = {
        f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com": scheduler,
        f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com": reconciler,
    }
    bindings = [d for d in _docs(out) if d.get("kind") == "RoleBinding"]
    checked = 0
    for binding in bindings:
        names = {s.get("name") for s in binding.get("subjects") or []}
        for email, uid in pairs.items():
            if email in names or uid in names:
                checked += 1
                assert {email, uid} <= names, (binding["metadata"]["name"], names)
    # Visited, not vacuous: the dispatcher's and the reaper's bindings at least.
    assert checked >= 2, f"only {checked} RoleBinding subjects named the scheduler or reconciler"


def test_quota_from_tenant_record(tmp_path: Path) -> None:
    """A namespace's ResourceQuota is the tenant record's, passed in, never a
    render.py default (#2039, lesson 32): a cluster render without all five
    --quota-* exits 2, and with the record's flags it renders exactly them."""
    record = tmp_path / "record.json"
    record.write_text(
        '{"workspace": {"workspace_id": "w-0a1b2c", "tenant_id": "u-carol", '
        '"principal": "carol@example.com", "kind": "person", "limits": {"max_active": 7}}}'
    )
    code, flags, err = _main("workspace-quota", "--tenant", "u-carol", "--record", str(record))
    assert code == 0, err
    flags_list = flags.split()
    assert len(flags_list) == 10, flags
    base = ("tenant", "--tenant", "u-carol", "--project", PROJECT, *NETWORK,
            "--scheduler-uid", _uid(), "--reconciler-uid", _uid())

    # MUTATION: give any --quota-* a default again and this renders.
    code, out, err = _main(*base)
    assert code == 2 and "--quota-pods" in err, (code, err)
    code, out, err = _main(*base, *flags_list[:8])
    assert code == 2 and "--quota-ephemeral" in err, (code, err)

    code, out, err = _main(*base, *flags_list)
    assert code == 0, err
    quotas = [d for d in _docs(out) if d.get("kind") == "ResourceQuota"]
    assert len(quotas) == 1
    hard = quotas[0]["spec"]["hard"]
    given = dict(zip(flags_list[::2], flags_list[1::2]))
    assert hard["pods"] == given["--quota-pods"]
    assert hard["requests.cpu"] == given["--quota-cpu"]
    assert hard["requests.memory"] == given["--quota-memory"]
    assert hard["count/jobs.batch"] == given["--quota-jobs"]
    assert hard["requests.ephemeral-storage"] == given["--quota-ephemeral"]
    # Seven agents is not the Terraform tenant's flat quota.
    assert hard["pods"] != str(_load().TERRAFORM_TENANT_QUOTA["quota_pods"])


def test_terraform_quota_is_the_named_constant() -> None:
    """`render.py terraform-quota` prints TERRAFORM_TENANT_QUOTA as five flags,
    in workspace-quota's order, which apply.sh's quota line indexes."""
    code, out, _ = _main("terraform-quota")
    assert code == 0
    lines = out.split()
    assert lines[::2] == ["--quota-pods", "--quota-cpu", "--quota-memory", "--quota-jobs", "--quota-ephemeral"]
    assert lines[1::2] == [str(v) for v in _load().TERRAFORM_TENANT_QUOTA.values()]


def test_policies_unchanged_without_project() -> None:
    """`render.py policies`, exactly as security.yml:235 and Makefile:343 run
    it, still renders, and its output names no project: every identity in it
    is OFFLINE_PROJECT's, which Google Cloud cannot issue (review finding 14)."""
    result = subprocess.run(
        [sys.executable, str(RENDER), "policies"],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert _docs(out), "policies rendered nothing"
    # MUTATION: put `default="saga-agents-staging"` back on policies' --project.
    assert LIVE_PROJECT not in out
    projects = set(re.findall(r"@([a-z0-9-]+)\.iam\.gserviceaccount\.com", out))
    assert projects == {_load().OFFLINE_PROJECT}, projects
    assert "google" in _load().OFFLINE_PROJECT  # a restricted string in a GCP project id

    # The lint tenant and job renders beside it (security.yml, Makefile, the
    # worker's pre-publish checkov) keep working offline and name no project.
    for argv in (
        ["tenant", "--tenant", "lint"],
        ["job", "--tenant", "lint", "--profile", "claude-code", "--task", "tsk_lint",
         "--attempt", "att_lint", "--lease", "lease_lint", "--generation", "1"],
    ):
        ran = subprocess.run(
            [sys.executable, str(RENDER), *argv],
            cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
        )
        assert ran.returncode == 0, (argv, ran.stderr)
        assert LIVE_PROJECT not in ran.stdout, argv


@pytest.mark.parametrize("command", ["identity", "verify-read"])
def test_apply_side_renders_take_the_project_given(command: str) -> None:
    """The identity apply.sh reads Workload Identity bindings for is the
    project's it passes, never the live one."""
    code, out, _ = _main(command, "--tenant", "eng", "--project", PROJECT)
    assert code == 0
    assert LIVE_PROJECT not in out
    if command == "identity":
        assert out.split()[1] == f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
