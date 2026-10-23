"""QUOTA-ONE (#2039): a workspace tenant's namespace quota has one formula.

THE INCIDENT. u-bogdan's workspace (w-752763) read `failed` on 2026-10-23.
Its namespace had been applied by hand with `kubernetes/apply.sh --tenant
u-bogdan`, which rendered kubernetes/render.py's flat defaults (pods 100,
requests.cpu 400) where the record's 100 agents want pods 200, requests.cpu
800. The job's final check (A9) compared the two and failed the workspace as
"a ResourceQuota is missing", and `--mode limits`, the one run that re-applies
a quota, refused a workspace that was not `ready`.

Decided by BGD on 2026-10-23: ONE formula, and apply.sh renders a workspace
tenant's quota from its record. What is under test here is the shipped code:

  * the per-agent constants live once, in scripts/lib/workspace-quota.json,
    and neither register-tenant.sh nor render.py states a number of its own;
  * register-tenant.sh's shell formula (`ws_resolve_limits` + `ws_quota_args`,
    cut out of the script as it ships) and render.py's `workspace-quota` give
    the same five quota lines for 1, 50, 100 and 1000 agents;
  * `kubernetes/apply.sh --tenant <workspace tenant>` renders the record's
    quota, read from swarm-api, and says which numbers it used; a Terraform
    tenant keeps the renderer's defaults and reads nothing; explicit
    `--quota-*` flags still win, and the job's A7, which passes all five,
    reads nothing.

No real cluster, no credentials and no network: apply.sh runs against the fake
cluster of test_cluster_network_is_read_not_typed.py and a fake curl that
answers swarm-api's admin routes from files.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_cluster_network_is_read_not_typed import APPLY, FAKE, Cluster

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "scripts" / "register-tenant.sh"
RENDER = REPO / "kubernetes" / "render.py"
CONSTANTS = REPO / "scripts" / "lib" / "workspace-quota.json"
PEOPLE = REPO / "apps" / "swarm-api" / "swarm_api" / "people.py"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

WORKSPACE = "w-3f9a2c"
PRINCIPAL = "alice@example.com"
TENANT = "u-alice"

#: The issue's own numbers for 100 agents: what A9 asked of u-bogdan's
#: namespace, and what CLD applied by hand at 02:37Z (#2039, "Anything else").
HUNDRED = ["--quota-pods", "200", "--quota-cpu", "800", "--quota-memory", "1600Gi",
           "--quota-jobs", "800", "--quota-ephemeral", "2000Gi"]


# ---------------------------------------------------------------------------
# the two formulas
# ---------------------------------------------------------------------------


def _function(text: str, name: str) -> str:
    found = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert found, f"register-tenant.sh has no {name}()"
    return found.group(0)


def _shell_quota(max_active: int, quota_pods: str = "", quota_cpu: str = "") -> list[str]:
    """register-tenant.sh's own functions, cut out of the script as it ships,
    run over a record's limits: the arguments A7 passes kubernetes/apply.sh."""
    text = REGISTER.read_text()
    defaults = re.search(r"^WS_DEFAULT_MAX_ACTIVE=\d+\nWS_DEFAULT_CAPACITY_UNITS=\d+\n", text, flags=re.M)
    assert defaults, "register-tenant.sh no longer states WS_DEFAULT_MAX_ACTIVE beside WS_DEFAULT_CAPACITY_UNITS"
    body = "".join(_function(text, n) for n in
                   ("ws_whole", "ws_quota_constants", "ws_resolve_limits", "ws_quota_args"))
    script = "\n".join([
        "set -euo pipefail",
        "die() { printf 'die: %s\\n' \"$*\" >&2; exit 9; }",
        f"REPO_ROOT={shlex.quote(str(REPO))}",
        defaults.group(0), body,
        f"WS_MAX_ACTIVE={max_active}", "WS_CAPACITY_UNITS=''",
        f"WS_QUOTA_PODS={shlex.quote(quota_pods)}", f"WS_QUOTA_CPU={shlex.quote(quota_cpu)}",
        "ws_resolve_limits", "ws_quota_args", ""])
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def _record(max_active: int, **limits: int) -> dict[str, Any]:
    """A record as `GET /v1/admin/workspaces/{id}` returns it (people.admin_view)."""
    return {"workspace_id": WORKSPACE, "kind": "personal", "principal": PRINCIPAL, "state": "ready",
            "limits": {"max_active": max_active, "capacity_units": max_active, **limits}}


def _render_quota(tmp_path: Path, payload: dict[str, Any], tenant: str = TENANT) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload))
    return subprocess.run([sys.executable, str(RENDER), "workspace-quota", "--tenant", tenant, "--record", str(path)],
                          capture_output=True, text=True, timeout=120, check=False)


@pytest.mark.parametrize("max_active", [1, 50, 100, 1000])
@pytest.mark.parametrize("stated", [False, True], ids=["derived", "stated"])
def test_the_shell_and_the_renderer_give_one_quota(tmp_path: Path, max_active: int, stated: bool) -> None:
    """The parity table: the record's ceiling, with and without the quota
    numbers swarm-api writes beside it (people.ceiling_limits)."""
    limits = {"quota_pods": 2 * max_active, "quota_cpu": 8 * max_active} if stated else {}
    shell = _shell_quota(max_active, str(limits.get("quota_pods", "")), str(limits.get("quota_cpu", "")))
    rendered = _render_quota(tmp_path, {"workspace": _record(max_active, **limits)})
    assert rendered.returncode == 0, rendered.stderr
    # MUTATION: change memory_gi_per_cpu in render.py's formula (or read a
    # constant of its own) and this differs from the shell's line.
    assert rendered.stdout.split() == shell, (rendered.stdout, shell)
    expected = {"--quota-pods": str(2 * max_active), "--quota-cpu": str(8 * max_active),
                "--quota-memory": f"{16 * max_active}Gi", "--quota-jobs": str(8 * max_active),
                "--quota-ephemeral": f"{20 * max_active}Gi"}
    # MUTATION: set pods_per_agent to 1 in workspace-quota.json and both
    # formulas agree on a quota this table does not hold.
    assert dict(zip(shell[::2], shell[1::2])) == expected, shell


def test_a_hundred_agents_is_the_quota_the_issue_applied_by_hand(tmp_path: Path) -> None:
    rendered = _render_quota(tmp_path, {"workspace": _record(100)})
    assert rendered.returncode == 0, rendered.stderr
    # MUTATION: render.py's defaults (pods 100, cpu 400) for a workspace
    # tenant are the defect itself, and fail here.
    assert rendered.stdout.split() == HUNDRED, rendered.stdout


def test_the_record_s_own_quota_numbers_win_over_the_ratio(tmp_path: Path) -> None:
    rendered = _render_quota(tmp_path, {"workspace": _record(8, quota_pods=30, quota_cpu=100)})
    assert rendered.returncode == 0, rendered.stderr
    # MUTATION: derive pods from max_active alone and this reads 16.
    assert rendered.stdout.split() == _shell_quota(8, "30", "100") == [
        "--quota-pods", "30", "--quota-cpu", "100", "--quota-memory", "200Gi",
        "--quota-jobs", "120", "--quota-ephemeral", "300Gi"]


def test_the_renderer_finds_the_tenant_s_record_in_the_list_and_nothing_for_another(tmp_path: Path) -> None:
    other = {**_record(4), "workspace_id": "w-000001", "principal": "bob@example.com"}
    listed = {"workspaces": [other, _record(100)], "count": 2}
    found = _render_quota(tmp_path, listed)
    assert found.returncode == 0, found.stderr
    # MUTATION: take the first record of the list and this renders bob's 4.
    assert found.stdout.split() == HUNDRED, found.stdout
    none = _render_quota(tmp_path, listed, tenant="u-carol")
    assert none.returncode == 0, none.stderr
    # MUTATION: fall back to any record and u-carol gets someone else's quota.
    assert none.stdout == "", none.stdout


def test_a_record_of_another_tenant_named_by_id_is_refused(tmp_path: Path) -> None:
    other = {**_record(4), "principal": "bob@example.com"}
    refused = _render_quota(tmp_path, {"workspace": other})
    # MUTATION: skip the derived-tenant check and u-alice renders bob's quota.
    assert refused.returncode != 0 and "u-bob" in refused.stderr, refused.stderr


def test_the_constants_are_stated_once() -> None:
    """The per-agent numbers live in workspace-quota.json and nowhere else
    in the two places that compute a quota from them."""
    constants = json.loads(CONSTANTS.read_text())
    assert {k: constants[k] for k in ("pods_per_agent", "cpu_per_agent", "memory_gi_per_cpu",
                                      "jobs_per_pod", "ephemeral_gi_per_pod")} == {
        "pods_per_agent": 2, "cpu_per_agent": 8, "memory_gi_per_cpu": 2,
        "jobs_per_pod": 4, "ephemeral_gi_per_pod": 10}
    register = REGISTER.read_text()
    # MUTATION: put `WS_PODS_PER_AGENT=2` back in register-tenant.sh.
    assert not re.search(r"^WS_(PODS_PER_AGENT|CPU_PER_AGENT|MEMORY_GI_PER_CPU|JOBS_PER_POD|"
                         r"EPHEMERAL_GI_PER_POD)=\d", register, flags=re.M)
    assert "workspace-quota.json" in register and "workspace-quota.json" in RENDER.read_text()
    # swarm-api writes the record's quota_pods/quota_cpu beside the ceiling
    # (people.ceiling_limits) from its own two numbers: its image carries no
    # scripts/lib, so it cannot read the file. Held equal here instead.
    people = PEOPLE.read_text()
    # MUTATION: change PODS_PER_AGENT in people.py and the record's numbers
    # stop being the ones the job and apply.sh derive.
    assert re.search(rf"^PODS_PER_AGENT = {constants['pods_per_agent']}$", people, flags=re.M)
    assert re.search(rf"^CPU_PER_AGENT = {constants['cpu_per_agent']}$", people, flags=re.M)


# ---------------------------------------------------------------------------
# kubernetes/apply.sh --tenant renders a workspace tenant's quota from its record
# ---------------------------------------------------------------------------

FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_API_DIR:?}"
url="${*: -1}"
printf '%s\n' "${url}" >>"${d}/curl.log"
status="$(cat "${d}/status" 2>/dev/null || printf 200)"
case "${url}" in
  */v1/admin/workspaces) cat "${d}/list.json" ;;
  */v1/admin/workspaces/w-*) cat "${d}/${url##*/}.json" 2>/dev/null || { status=404; printf '{"detail":"not found"}'; } ;;
  *) printf 'fake curl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
printf '\n%s' "${status}"
"""


class Api:
    """swarm-api's two admin record routes, answered from files."""

    def __init__(self, cluster: Cluster, records: list[dict[str, Any]], status: int = 200) -> None:
        self.dir = cluster.tmp / "api"
        self.dir.mkdir()
        curl = cluster.bin / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        (self.dir / "list.json").write_text(json.dumps({"workspaces": records, "count": len(records)}))
        for record in records:
            (self.dir / f"{record['workspace_id']}.json").write_text(json.dumps({"workspace": record}))
        (self.dir / "status").write_text(str(status))
        self.env = {
            "FAKE_API_DIR": str(self.dir),
            # A Cloud Run address, not the front door, so the credential is an
            # ID token: the one given here, built at runtime.
            "API_URL": "https://swarm-api-quota-test.a.run.app",
            "API_HOST": "swarm.quota-test.example",
            "SWARM_ID_TOKEN": secrets.token_hex(16),
        }

    def calls(self) -> list[str]:
        log = self.dir / "curl.log"
        return log.read_text().split() if log.exists() else []


def _quota(cluster: Cluster) -> dict[str, str]:
    docs = [d for d in yaml.safe_load_all((cluster.dir / "applied-dry-run.yaml").read_text()) if d]
    quotas = [d for d in docs if d.get("kind") == "ResourceQuota"]
    assert len(quotas) == 1, f"expected one ResourceQuota in the render, found {len(quotas)}"
    return quotas[0]["spec"]["hard"]


def _apply(tmp_path: Path, records: list[dict[str, Any]], *args: str, status: int = 200):
    cluster = Cluster(tmp_path, FAKE)
    api = Api(cluster, records, status)
    result = cluster.run(APPLY, *args, **api.env)
    return cluster, api, result


def test_apply_renders_a_workspace_tenant_s_quota_from_its_record(tmp_path: Path) -> None:
    cluster, api, result = _apply(tmp_path, [_record(100)], "--tenant", TENANT)
    assert result.returncode == 0, result.stderr
    hard = _quota(cluster)
    # MUTATION: drop the record's flags from the render and this is 100/400,
    # u-bogdan's namespace on 2026-10-23.
    assert (hard["pods"], hard["requests.cpu"], hard["limits.cpu"]) == ("200", "800", "1600"), hard
    assert (hard["requests.memory"], hard["count/jobs.batch"], hard["requests.ephemeral-storage"]) \
        == ("1600Gi", "800", "2000Gi"), hard
    assert api.calls() == ["https://swarm-api-quota-test.a.run.app/v1/admin/workspaces"]
    out = result.stdout + result.stderr
    # MUTATION: render the record's numbers without saying so.
    assert f"workspace record {WORKSPACE}" in out and "pods 200, cpu 800" in out, out


def test_apply_reads_the_record_named_by_workspace_id(tmp_path: Path) -> None:
    group = {**_record(30), "workspace_id": "w-0a0b0c", "kind": "group", "principal": "research@example.com"}
    cluster, api, result = _apply(tmp_path, [group], "--tenant", "research", "--workspace-id", "w-0a0b0c")
    assert result.returncode == 0, result.stderr
    # MUTATION: list every record instead of reading the one named.
    assert api.calls() == ["https://swarm-api-quota-test.a.run.app/v1/admin/workspaces/w-0a0b0c"]
    # 30 agents, so the numbers are not the defaults (50 would render 100/400).
    assert (_quota(cluster)["pods"], _quota(cluster)["requests.cpu"]) == ("60", "240")


def test_apply_keeps_the_defaults_for_a_terraform_tenant_and_reads_nothing(tmp_path: Path) -> None:
    cluster, api, result = _apply(tmp_path, [_record(100)], "--tenant", "eng")
    assert result.returncode == 0, result.stderr
    hard = _quota(cluster)
    # MUTATION: change render.py's --quota-pods default and eng's quota moves.
    assert (hard["pods"], hard["requests.cpu"]) == ("100", "400"), hard
    # MUTATION: look eng up in swarm-api and the release, whose account is
    # no swarm-api admin, stops applying eng.
    assert api.calls() == []
    assert "Terraform tenant" in result.stdout + result.stderr


def test_apply_keeps_the_defaults_for_a_tenant_no_record_names(tmp_path: Path) -> None:
    cluster, api, result = _apply(tmp_path, [_record(100)], "--tenant", "u-carol")
    assert result.returncode == 0, result.stderr
    hard = _quota(cluster)
    assert (hard["pods"], hard["requests.cpu"]) == ("100", "400"), hard
    assert len(api.calls()) == 1
    # MUTATION: say nothing when no record is found, and the defaults read as
    # chosen.
    assert "no workspace record names u-carol" in result.stdout + result.stderr


def test_an_explicit_quota_flag_wins_over_the_record(tmp_path: Path) -> None:
    cluster, _, result = _apply(tmp_path, [_record(100)], "--tenant", TENANT, "--quota-pods", "300")
    assert result.returncode == 0, result.stderr
    hard = _quota(cluster)
    # MUTATION: put the record's flags after the command line's and pods is 200.
    assert (hard["pods"], hard["requests.cpu"]) == ("300", "800"), hard


def test_the_job_s_a7_reads_no_record(tmp_path: Path) -> None:
    """A7 passes all five quota flags (ws_quota_args), and runs under the call
    guard, which admits no call to swarm-api: apply.sh must ask nothing."""
    cluster, api, result = _apply(tmp_path, [_record(100)], "--tenant", TENANT,
                                  "--quota-pods", "16", "--quota-cpu", "64", "--quota-memory", "128Gi",
                                  "--quota-jobs", "64", "--quota-ephemeral=160Gi")
    assert result.returncode == 0, result.stderr
    # MUTATION: read the record whenever the tenant is not Terraform's.
    assert api.calls() == []
    assert (_quota(cluster)["pods"], _quota(cluster)["requests.cpu"]) == ("16", "64")


def test_an_unreadable_record_renders_nothing(tmp_path: Path) -> None:
    cluster, _, result = _apply(tmp_path, [_record(100)], "--tenant", TENANT, status=403)
    # MUTATION: fall back to the defaults when swarm-api refuses, which is
    # the defect again, silently.
    assert result.returncode != 0, result.stdout
    assert not (cluster.dir / "applied-dry-run.yaml").exists()
    assert "--quota-" in result.stderr, result.stderr
