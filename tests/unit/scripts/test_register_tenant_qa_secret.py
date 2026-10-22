"""`scripts/register-tenant.sh` makes the tenant's QA target credential secret.

terraform/infra/qa_targets.tf (VQ3-QA-SECRET, PR 1943) declares
`swarm-tenant-<t>-qa` for every tenant in `var.tenants`: empty, labelled
`component=qa-target-credential`, and readable and writable by swarm-api's
account alone (rows V-1 and V-2 of docs/design/visual-qa-any-app.md §7). A
tenant this script registers is not in `var.tenants`, so it got none, and no
own-login credential could be stored for it. BGD, 2026-10-22 08:44Z:
"register-tenant.sh creates it".

What has to hold, for a full registration (`--group`, `--user`):

  * the secret is created EMPTY -- no version is ever added from here;
  * it carries the labels Terraform gives its own tenants' QA secrets, read
    off qa_targets.tf rather than restated in this test;
  * it is pinned to the workload region, as Terraform pins it;
  * swarm-api, and nobody else, gets secretVersionAdder (V-1) and
    secretAccessor (V-2), each on that one secret, never on the project;
  * no worker account gets anything on it;
  * a second run creates nothing and binds nothing;
  * one already labelled for another tenant, or one some other member can
    read, is refused rather than adopted;
  * `--dry-run` creates and binds nothing.

The harness is test_register_tenant_merge_step's (the real script, a fake
`gcloud` and `curl` first on PATH), with Secret Manager's `-qa` secrets
answered by a fake that remembers what the run created and bound, so a
second run sees the first run's work. Nothing reaches a real project.
"""

from __future__ import annotations

import json
import re

from .test_register_tenant_merge_step import (
    FULL,
    REGISTER,
    REPO,
    WORKER,
    Fakes,
    _out,
    _sa,
)

QA_TF = REPO / "terraform" / "infra" / "qa_targets.tf"
TENANT = "eng"
QA = f"swarm-tenant-{TENANT}-qa"
API_MEMBER = f"serviceAccount:{_sa('swarm-api')}"
ADDER = "roles/secretmanager.secretVersionAdder"
ACCESSOR = "roles/secretmanager.secretAccessor"

# Secret Manager for `swarm-tenant-*-qa`, stateful: FAKE_QA_DIR/<name>.json is
# the secret as created, <name>.policy.json its IAM policy. Every other call
# goes to the merge-step fake, which logs it. Calls answered here are logged
# here, in the same shape, so Fakes.gcloud_changes() sees them all.
QA_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
name=""
[[ "${1:-}" == "secrets" && $# -ge 3 ]] && name="$3"
case "${name}" in
  swarm-tenant-*-qa) ;;
  *) exec "${here}/gcloud-base" "$@" ;;
esac
printf '%s\n' "$@" | jq -Rnc '{tool:"gcloud", argv:[inputs], condition:""}' >>"${FAKE_LOG}"
secret="${FAKE_QA_DIR}/${name}.json"
policy="${FAKE_QA_DIR}/${name}.policy.json"
flag() {
  local want="$1" prev=""
  shift
  for a in "$@"; do
    [[ "${prev}" == "${want}" ]] && { printf '%s' "${a}"; return 0; }
    prev="${a}"
  done
  return 1
}
case "$2" in
  describe)
    if [[ -f "${secret}" ]]; then cat "${secret}"; exit 0; fi
    echo "ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [${name}] not found or has no versions." >&2
    exit 1 ;;
  create)
    if [[ -f "${secret}" ]]; then
      echo "ERROR: (gcloud.secrets.create) ALREADY_EXISTS: Secret [${name}] already exists." >&2
      exit 1
    fi
    labels="$(flag --labels "$@" || true)"
    jq -nc --arg n "projects/x/secrets/${name}" --arg l "${labels}" \
      '{name:$n, labels:($l | split(",") | map(select(. != "") | split("=") | {key:.[0], value:(.[1:] | join("="))}) | from_entries)}' \
      >"${secret}"
    echo "Created secret [${name}]." >&2 ;;
  get-iam-policy)
    if [[ -f "${policy}" ]]; then cat "${policy}"; else echo '{}'; fi ;;
  add-iam-policy-binding)
    member="$(flag --member "$@")"; role="$(flag --role "$@")"
    [[ -f "${policy}" ]] || echo '{}' >"${policy}"
    jq --arg r "${role}" --arg m "${member}" '
      .bindings = ((.bindings // []) as $b
        | if any($b[]; .role == $r) then
            $b | map(if .role == $r then .members = ((.members + [$m]) | unique) else . end)
          else $b + [{role:$r, members:[$m]}] end)' "${policy}" >"${policy}.new"
    mv "${policy}.new" "${policy}"
    cat "${policy}" ;;
  *)
    echo "ERROR: the QA fake does not answer: $*" >&2
    exit 1 ;;
esac
"""


class QaFakes(Fakes):
    def __init__(self, tmp) -> None:
        super().__init__(tmp)
        bin_dir = tmp / "bin"
        (bin_dir / "gcloud").rename(bin_dir / "gcloud-base")
        wrapper = bin_dir / "gcloud"
        wrapper.write_text(QA_GCLOUD)
        wrapper.chmod(0o755)
        self.qa_dir = tmp / "qa-secrets"
        self.qa_dir.mkdir()
        self.env["FAKE_QA_DIR"] = str(self.qa_dir)

    def qa_calls(self, verb: str) -> list[list[str]]:
        return [
            c["argv"] for c in self.gcloud_changes()
            if c["argv"][:2] == ["secrets", verb] and c["argv"][2].endswith("-qa")
        ]

    def plant(self, name: str, labels: dict[str, str], bindings: list[dict] | None = None) -> None:
        (self.qa_dir / f"{name}.json").write_text(json.dumps({"name": f"projects/x/secrets/{name}", "labels": labels}))
        if bindings is not None:
            (self.qa_dir / f"{name}.policy.json").write_text(json.dumps({"version": 1, "bindings": bindings}))

    def policy_of(self, name: str) -> dict[str, list[str]]:
        path = self.qa_dir / f"{name}.policy.json"
        if not path.exists():
            return {}
        return {b["role"]: sorted(b["members"]) for b in json.loads(path.read_text()).get("bindings", [])}


def _terraform_qa_labels(tenant: str) -> dict[str, str]:
    """The labels qa_targets.tf merges over local.labels on its qa_target secret."""
    text = QA_TF.read_text()
    block = re.search(r'resource "google_secret_manager_secret" "qa_target" \{(.*?)\n\}', text, re.S)
    assert block, "terraform/infra/qa_targets.tf no longer declares google_secret_manager_secret.qa_target"
    merged = re.search(r"labels\s*=\s*merge\(local\.labels,\s*\{(.*?)\}\)", block.group(1), re.S)
    assert merged, "qa_target's labels are no longer merge(local.labels, {...}); this test reads them that way"
    pairs = re.findall(r'"([a-z0-9_-]+)"\s*=\s*("[^"]*"|each\.key)', merged.group(1))
    assert pairs, merged.group(1)
    return {k: (tenant if v == "each.key" else v.strip('"')) for k, v in pairs}


def _register(fakes: QaFakes, *extra: str):
    return fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", *extra])


def test_the_terraform_labels_are_read_the_way_this_test_reads_them() -> None:
    # MUTATION: rename "component" in qa_targets.tf's qa_target labels and this reads it differently.
    assert _terraform_qa_labels(TENANT) == {
        "component": "qa-target-credential",
        "tenant": TENANT,
        "swarm-tenant": TENANT,
    }


def test_a_registration_creates_the_qa_secret_empty_with_terraforms_labels(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = _register(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    creates = fakes.qa_calls("create")
    # MUTATION: drop the qa_secret_ensure call from register-tenant.sh's section 4b and nothing is created.
    assert [argv[2] for argv in creates] == [QA], f"{QA} must be created exactly once:\n{out}"
    created = json.loads((fakes.qa_dir / f"{QA}.json").read_text())
    # MUTATION: drop `swarm-tenant=` from qa_secret_labels in scripts/lib/forge-slot.sh and this fails.
    for key, value in _terraform_qa_labels(TENANT).items():
        assert created["labels"].get(key) == value, (key, created["labels"], out)
    # Terraform's local.labels carries swarm-env; the script's environment is dev here.
    # MUTATION: drop `swarm-env=` from qa_secret_labels and this fails.
    assert created["labels"].get("swarm-env") == "dev", created["labels"]
    # Not Terraform's: destroy.sh and the plan guard read managed-by=swarm-terraform as "in state".
    # MUTATION: label it managed-by=swarm-terraform and this fails.
    assert created["labels"].get("managed-by") == "swarm-register-tenant", created["labels"]
    argv = creates[0]
    # Pinned to the workload region, as Terraform's user_managed replication is.
    # MUTATION: create with --replication-policy automatic and this fails.
    assert argv[argv.index("--replication-policy") + 1] == "user-managed", argv
    assert argv[argv.index("--locations") + 1] == "us-central1", argv
    # Empty: no version, no data file.
    # MUTATION: pass --data-file to the create, or add a `secrets versions add`, and this fails.
    assert "--data-file" not in argv, argv
    assert not any(
        c["argv"][:3] == ["secrets", "versions", "add"] for c in fakes.calls() if c["tool"] == "gcloud"
    ), out


def test_swarm_api_alone_adds_and_reads_it(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = _register(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    # MUTATION: drop the secretVersionAdder binding (V-1) from qa_secret_ensure and this fails.
    # MUTATION: drop the secretAccessor binding (V-2) from qa_secret_ensure and this fails.
    assert fakes.policy_of(QA) == {ADDER: [API_MEMBER], ACCESSOR: [API_MEMBER]}, out
    binds = fakes.qa_calls("add-iam-policy-binding")
    assert sorted((a[a.index("--role") + 1], a[a.index("--member") + 1]) for a in binds) == [
        (ACCESSOR, API_MEMBER), (ADDER, API_MEMBER),
    ], binds
    # Never the worker, on any secret's role list for this one, nor a project-level grant of either role.
    # MUTATION: bind serviceAccount:${GSA_EMAIL} on the -qa secret and this fails.
    assert all(f"serviceAccount:{WORKER}" not in a for a in binds), binds
    assert not any(
        c["argv"][:2] == ["projects", "add-iam-policy-binding"]
        and c["argv"][c["argv"].index("--role") + 1] in (ADDER, ACCESSOR)
        for c in fakes.gcloud_changes()
    ), out


def test_a_second_run_changes_nothing(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    first = _register(fakes)
    assert first.returncode == 0, _out(first)
    fakes.log.write_text("")
    second = _register(fakes)
    out = _out(second)
    assert second.returncode == 0, out
    # MUTATION: skip the describe in qa_secret_ensure (create unconditionally) and the second run creates again.
    assert fakes.qa_calls("create") == [], out
    # MUTATION: skip the iam_policy_binds_member check in qa_secret_ensure and the second run binds again.
    assert fakes.qa_calls("add-iam-policy-binding") == [], out
    assert fakes.policy_of(QA) == {ADDER: [API_MEMBER], ACCESSOR: [API_MEMBER]}, out


def test_a_present_secret_missing_one_binding_gains_only_that_one(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    labels = {**_terraform_qa_labels(TENANT), "managed-by": "swarm-terraform", "swarm-env": "dev"}
    fakes.plant(QA, labels, [{"role": ACCESSOR, "members": [API_MEMBER]}])
    proc = _register(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.qa_calls("create") == [], out
    binds = fakes.qa_calls("add-iam-policy-binding")
    # MUTATION: bind both roles without reading the policy first and this sees two.
    assert [(a[a.index("--role") + 1], a[a.index("--member") + 1]) for a in binds] == [(ADDER, API_MEMBER)], binds


def test_a_secret_labelled_for_another_tenant_is_refused_and_not_bound(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    fakes.plant(QA, {"component": "qa-target-credential", "tenant": "eng-x", "swarm-tenant": "eng-x"})
    proc = _register(fakes)
    out = _out(proc)
    # MUTATION: drop the labels.tenant check in qa_secret_present and the foreign secret is adopted.
    assert proc.returncode != 0, out
    assert fakes.qa_calls("add-iam-policy-binding") == [], out
    assert "eng-x" in out, out


def test_a_tenant_secret_that_is_not_a_qa_credential_is_refused(tmp_path) -> None:
    # A provider key named `qa` would be swarm-tenant-eng-qa too, read by the worker.
    fakes = QaFakes(tmp_path)
    fakes.plant(QA, {"component": "tenant-credential", "tenant": TENANT, "provider": "qa"})
    proc = _register(fakes)
    out = _out(proc)
    # MUTATION: drop the labels.component check in qa_secret_present and a provider key is adopted.
    assert proc.returncode != 0, out
    assert fakes.qa_calls("add-iam-policy-binding") == [], out


def test_a_qa_secret_some_other_member_can_read_is_refused_not_repaired(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    labels = {**_terraform_qa_labels(TENANT), "managed-by": "swarm-register-tenant", "swarm-env": "dev"}
    worker = f"serviceAccount:{WORKER}"
    fakes.plant(QA, labels, [{"role": ACCESSOR, "members": [API_MEMBER, worker]}])
    proc = _register(fakes)
    out = _out(proc)
    # MUTATION: drop the foreign-member check in qa_secret_ensure and the run passes over a worker reader.
    assert proc.returncode != 0, out
    assert WORKER in out, out
    assert fakes.qa_calls("add-iam-policy-binding") == [], out


def test_a_dry_run_creates_and_binds_nothing(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = _register(fakes, "--dry-run")
    out = _out(proc)
    assert proc.returncode == 0, out
    # MUTATION: pass FORGE_SLOT_DRY_RUN=0 from section 4b regardless of --dry-run and this fails.
    assert fakes.qa_calls("create") == [] and fakes.qa_calls("add-iam-policy-binding") == [], out
    assert f"would create {QA}" in out, out


def test_a_personal_registration_creates_its_own_qa_secret(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    # No --providers: the merge-step fake labels every provider secret tenant=eng.
    # A tenant holding no provider key still gets its QA secret.
    proc = fakes.run([str(REGISTER), "--user", "ada@example.com", "--skip-k8s"])
    out = _out(proc)
    assert proc.returncode == 0, out
    created = [argv[2] for argv in fakes.qa_calls("create")]
    # MUTATION: run section 4b for --group only and the personal tenant gets none.
    assert len(created) == 1 and created[0].startswith("swarm-tenant-u-") and created[0].endswith("-qa"), (created, out)
    tenant = created[0][len("swarm-tenant-"):-len("-qa")]
    labels = json.loads((fakes.qa_dir / f"{created[0]}.json").read_text())["labels"]
    assert labels["tenant"] == tenant and labels["swarm-tenant"] == tenant, labels
    assert fakes.policy_of(created[0]) == {ADDER: [API_MEMBER], ACCESSOR: [API_MEMBER]}, out

