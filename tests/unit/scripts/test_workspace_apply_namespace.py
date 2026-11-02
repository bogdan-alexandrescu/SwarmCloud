"""What a team's namespace is made of when `swarm-workspace-apply` makes it
(DAYONE-TEAM-1, part of #2688; docs/design/day-one-plan.md, lane TEAM-1).

`swarm team add <group>` and the console's team creation drive one path:
swarm-api's team route writes the workspace record, then the job runs
`scripts/workspace-apply.sh` -> `scripts/register-tenant.sh --workspace`.
Three lessons hold that path (docs/design/day-one/lessons.md):

  * 32 -- a hand-applied namespace took kubernetes/render.py's default quota
    instead of the tenant record's (#2039): the quota is rendered from the
    record's `limits`, whatever they are;
  * 31 -- a new tenant could run nothing until its namespace and its
    `swarm-spec-verify-keys` ConfigMap were made by hand (#1590): the job
    holds no ConfigMap grant (kubernetes/rbac/provisioner-rbac.yaml), so the
    team verb applies the keys as the installer, from the keys the scheduler
    hands every Job, and checks the ConfigMap is there before it says done;
  * 40 -- re-running `register-tenant.sh --providers` REPLACED the tenant's
    `credentials`, dropping every provider the run did not name: a re-run
    adds what it names and never takes one away.

The harnesses are the existing ones: test_register_tenant_workspace's fake
world behind the real call guard, and test_register_tenant_merge_step's fake
gcloud and curl. Nothing reaches a real project.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from .test_register_tenant_merge_step import FULL, REGISTER, Fakes, _tenant_document
from .test_register_tenant_workspace import NAMESPACE, TENANT, Job, _out, _record

# ---------------------------------------------------------------------------
# lesson 32: the quota is the record's
# ---------------------------------------------------------------------------


def test_namespace_quota_from_tenant_record(tmp_path: Path) -> None:
    """A record whose limits are NOT render.py's defaults (nor §8's 16/64)
    gets exactly its own numbers on the namespace's ResourceQuota."""
    job = Job(tmp_path)
    record = _record()
    record["fields"]["limits"] = {"mapValue": {"fields": {
        "max_active": {"integerValue": "3"}, "capacity_units": {"integerValue": "3"},
        "quota_pods": {"integerValue": "6"}, "quota_cpu": {"integerValue": "24"}}}}
    job.edit(lambda world: world["firestore"].__setitem__(f"workspaces/{TENANT}", record))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 0, out[-6000:]
    quota = job.read()["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    # MUTATION: drop ws_quota_args from ws_a7 and render.py's defaults land here.
    assert (quota["pods"], quota["requests.cpu"]) == ("6", "24"), quota
    # The other lines keep §8's ratios to those two (scripts/lib/workspace-quota.json).
    assert quota["requests.memory"] == "48Gi", quota
    assert quota["limits.cpu"] == "48", quota


# ---------------------------------------------------------------------------
# lesson 31: the verify-keys ConfigMap is there before the team is done
# ---------------------------------------------------------------------------


class _Cluster:
    """The team namespace as the job leaves it: no ConfigMap of keys."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], dict] = {("Namespace", "", NAMESPACE): {}}
        self.applied: list[str] = []

    def get(self, kind: str, name: str, namespace: str = "") -> dict | None:
        return self.objects.get((kind, namespace, name))

    def apply(self, manifest: str) -> None:
        self.applied.append(manifest)
        import yaml

        for doc in yaml.safe_load_all(manifest):
            meta = doc["metadata"]
            self.objects[(doc["kind"], meta.get("namespace", ""), meta["name"])] = doc


def _pem() -> str:
    body = secrets.token_hex(32)
    return f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----\n"


def _spec_settings(project: str) -> dict[str, str]:
    key = f"projects/{project}/locations/us-central1/keyRings/swarm-prod-specs/cryptoKeys/step-spec"
    return {
        "SPEC_VERIFY_KEYS": json.dumps({f"{key}/cryptoKeyVersions/1": _pem()}),
        "SPEC_SIGNING_KEY": key,
        "SPEC_SIGNATURE_MODE": "enforce",
    }


def test_verify_keys_configmap_present() -> None:
    from swarm_install import team as team_mod

    cluster = _Cluster()
    project = "acme-swarm-1"
    settings = _spec_settings(project)
    team_mod.ensure_verify_keys(cluster, tenant=TENANT, settings=settings)
    found = cluster.get("ConfigMap", "swarm-spec-verify-keys", NAMESPACE)
    # MUTATION: skip the apply and the check below raises instead.
    assert found is not None, cluster.applied
    assert found["data"]["SPEC_VERIFY_KEYS"] == settings["SPEC_VERIFY_KEYS"]
    assert found["data"]["SPEC_SIGNING_KEY"] == settings["SPEC_SIGNING_KEY"]
    assert found["metadata"]["labels"]["swarm-tenant"] == TENANT
    # A second run with the keys in place applies nothing.
    before = len(cluster.applied)
    team_mod.ensure_verify_keys(cluster, tenant=TENANT, settings=settings)
    assert len(cluster.applied) == before


def test_verify_keys_refused_without_the_scheduler_s_keys() -> None:
    """No keys to copy is a stop with a reason, never a namespace left without
    them (lesson 31's CANNOT_START)."""
    from swarm_install import team as team_mod

    cluster = _Cluster()
    try:
        team_mod.ensure_verify_keys(cluster, tenant=TENANT, settings={})
    except team_mod.TeamError as exc:
        assert "SPEC_VERIFY_KEYS" in str(exc)
    else:
        raise AssertionError("an empty key set was applied")
    assert cluster.applied == []


# ---------------------------------------------------------------------------
# lesson 40: a re-run with --providers adds, never replaces
# ---------------------------------------------------------------------------


def _tenant_patch(fakes: Fakes) -> dict:
    patches = [p for p in fakes.patches() if "/documents/tenants/eng" in p["url"]]
    assert len(patches) == 1, patches
    return json.loads(patches[0]["body"])


def test_add_provider_never_replaces(tmp_path: Path) -> None:
    """eng already lists anthropic and git; a full re-run naming openai keeps
    both and adds openai."""
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git"]))
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "openai"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    body = _tenant_patch(fakes)
    creds = [v["stringValue"] for v in body["fields"]["credentials"]["arrayValue"].get("values", [])]
    # MUTATION: write CREDENTIALS_JSON alone (the old replace) and this reads ["openai"].
    assert creds == ["anthropic", "git", "openai"], out


def test_a_first_registration_lists_exactly_what_it_names(tmp_path: Path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    body = _tenant_patch(fakes)
    creds = [v["stringValue"] for v in body["fields"]["credentials"]["arrayValue"].get("values", [])]
    assert creds == ["anthropic"], out

