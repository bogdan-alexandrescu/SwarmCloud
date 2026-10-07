"""`scripts/register-tenant.sh` and what is left of the #295 identities (docs/merge-step.md §4.3, §10 item 5).

Two things the script has to do exactly as terraform/modules/tenancy does,
because it is the second path that grants a tenant's worker:

  * THE WORKER'S BUCKET GRANT IS SPLIT. Read and list on tenants/<t>/, write
    on tenants/<t>/ EXCEPT tenants/<t>/verdicts/. The old single objectUser
    binding must be found and replaced -- the new pair created first, the old
    one removed after, under a typed confirmation -- and a re-run on a tenant
    already split changes nothing. The split is KEPT after the merge,
    post-verdict and review accounts were retired (owner decision MS0-Q4,
    2026-10-06): collapsing it back into one binding is a create, on every
    tenant, which that retirement does not make.
  * NO ACCOUNT IS GIVEN AN APP KEY. `git-merge` and `git-review` are retired:
    nothing reads either key, so the script refuses both, on every path, before
    it reads anything.

Driven end to end: the real script with a fake `gcloud` and `curl` first on
PATH, each appending every call to one log in order. Nothing reaches a real
project.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "scripts" / "register-tenant.sh"
TENANCY = REPO / "terraform" / "modules" / "tenancy" / "main.tf"

PROJECT = "swarm-test-project"
BUCKET = "swarm-test-artifacts"
UPDATE_TIME = "2026-09-25T23:09:12.345678Z"


def _sa(account_id: str) -> str:
    return f"{account_id}@{PROJECT}.iam.gserviceaccount.com"


WORKER = _sa("swarm-agent-worker-eng")

pytestmark = pytest.mark.skipif(
    not REGISTER.exists() or shutil.which("jq") is None,
    reason="register-tenant.sh and jq are both required",
)


# Every call is appended to FAKE_LOG as one JSON line before it is answered.
# A --condition-from-file is read when the call is made and logged beside the
# argv, because the script removes the file afterwards. A secret's IAM policy is
# FAKE_SECRET_POLICIES/<secret>.json when that file exists, and empty otherwise.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
cond=""; name=""; prev=""
for arg in "$@"; do
  case "${prev}" in
    describe|list|get-iam-policy) name="${arg}" ;;
    --condition-from-file) cond="$(cat "${arg}")" ;;
  esac
  prev="${arg}"
done
printf '%s\n' "$@" | jq -Rnc --arg c "${cond}" '{tool:"gcloud", argv:[inputs], condition:$c}' >>"${FAKE_LOG}"
args="$*"
case "${args}" in
  *"auth print-access-token"*)        echo "fake-access-token" ;;
  *"identity groups describe"*)       echo "groups/1" ;;
  *"firestore databases describe"*)   echo "projects/x/databases/swarm" ;;
  *"iam service-accounts describe"*)
    if [[ " ${FAKE_MISSING_ACCOUNTS:-} " == *" ${name} "* ]]; then
      echo "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: Unknown service account" >&2
      exit 1
    fi
    echo "${name}" ;;
  *"iam service-accounts get-iam-policy"*) echo '{}' ;;
  *"iam service-accounts keys list"*) : ;;
  *"iam roles describe"*)             echo "roles/custom" ;;
  *"storage buckets describe"*)       echo "swarm-test-artifacts" ;;
  *"storage buckets get-iam-policy"*) cat "${FAKE_BUCKET_POLICY:-/dev/null}" ;;
  *"storage cp"*)                     cat >/dev/null ;;
  *"secrets describe"*)
    tenant="eng"
    provider="${name#swarm-tenant-"${tenant}"-}"
    jq -nc --arg n "projects/x/secrets/${name}" --arg t "${tenant}" --arg p "${provider}" \
      '{name:$n, labels:{tenant:$t, provider:$p}}' ;;
  *"secrets get-iam-policy"*)
    if [[ -n "${FAKE_SECRET_POLICIES:-}" && -f "${FAKE_SECRET_POLICIES}/${name}.json" ]]; then
      cat "${FAKE_SECRET_POLICIES}/${name}.json"
    else
      echo '{}'
    fi ;;
  *"secrets versions list"*)          echo "projects/x/secrets/${name}/versions/1" ;;
  *)                                  : ;;
esac
exit 0
"""

# Firestore as the real fs_request calls it (status on stdout, body in -o).
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
out=""; want_status=0; method="GET"; body=""; url=""; prev=""
for arg in "$@"; do
  case "${prev}" in
    -o) out="${arg}" ;;
    -w) [[ "${arg}" == *http_code* ]] && want_status=1 ;;
    -X) method="${arg}" ;;
    --data-binary) body="${arg}" ;;
  esac
  case "${arg}" in https://*) url="${arg}" ;; esac
  prev="${arg}"
done
[[ -t 0 ]] || cat >/dev/null 2>&1 || true
printf '%s\n' "$@" | jq -Rnc --arg m "${method}" --arg u "${url}" --arg b "${body}" \
  '{tool:"curl", method:$m, url:$u, body:$b, argv:[inputs]}' >>"${FAKE_LOG}"
status=200
resp='{}'
case "${method} ${url}" in
  "GET "*"/documents/tenants/"*)
    if [[ -s "${FAKE_TENANT_DOC:-/dev/null}" ]]; then
      resp="$(cat "${FAKE_TENANT_DOC}")"
    else
      status=404
      resp='{"error":{"code":404,"message":"Document not found","status":"NOT_FOUND"}}'
    fi ;;
esac
if [[ -n "${out}" ]]; then
  printf '%s' "${resp}" >"${out}"
  if [[ "${want_status}" -eq 1 ]]; then printf '%s' "${status}"; fi
else
  printf '%s' "${resp}"
fi
exit 0
"""

def _tenant_document(credentials: list[str]) -> str:
    values = [{"stringValue": c} for c in credentials]
    fields = {
        "tenant_id": {"stringValue": "eng"},
        "kind": {"stringValue": "group"},
        "principal": {"stringValue": "eng@saga.xyz"},
        "credentials": {"arrayValue": {"values": values} if values else {}},
        "service_account": {"stringValue": WORKER},
    }
    return json.dumps({
        "name": f"projects/{PROJECT}/databases/swarm/documents/tenants/eng",
        "fields": fields,
        "updateTime": UPDATE_TIME,
    })


class Fakes:
    def __init__(self, tmp: Path, tenant_doc: str | None = None) -> None:
        self.tmp = tmp
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)

        env_file = tmp / "env"
        env_file.write_text(
            f"PROJECT_ID={PROJECT}\n"
            "REGION=us-central1\n"
            "ENVIRONMENT=dev\n"
            "FIRESTORE_DATABASE=swarm\n"
            f"ARTIFACT_BUCKET={BUCKET}\n"
        )
        env_file.chmod(0o600)

        self.policy = tmp / "bucket-policy.json"
        self.policy.write_text("")
        doc_file = tmp / "tenant.json"
        doc_file.write_text(tenant_doc or "")
        self.log = tmp / "calls.jsonl"
        self.log.write_text("")
        self.scratch = tmp / "scratch"
        self.scratch.mkdir()

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA", "SWARM_ASSUME_YES")
        }
        env.update({
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SWARM_ENV_FILE": str(env_file),
            "NO_COLOR": "1",
            "TMPDIR": str(self.scratch),
            "FAKE_LOG": str(self.log),
            "FAKE_TENANT_DOC": str(doc_file),
            "FAKE_BUCKET_POLICY": str(self.policy),
        })
        self.env = env

    def run(self, argv: list[str], *, tty_answer: str | None = None, **fake_env: str):
        """Run the script. `tty_answer` gives it a terminal on stdin and types that."""
        env = dict(self.env)
        env.update(fake_env)
        if tty_answer is None:
            return subprocess.run(
                argv, cwd=REPO, env=env, input="", capture_output=True, text=True, timeout=180,
            )
        master, slave = pty.openpty()
        try:
            os.write(master, (tty_answer + "\n").encode())
            return subprocess.run(
                argv, cwd=REPO, env=env, stdin=slave, capture_output=True, text=True,
                timeout=180,
            )
        finally:
            os.close(slave)
            os.close(master)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def gcloud_changes(self) -> list[dict]:
        reads = ("print-access-token", "describe", "get-iam-policy", "list", "access")
        return [
            c for c in self.calls()
            if c["tool"] == "gcloud" and not any(r in c["argv"] for r in reads)
        ]

    def secret_grants(self) -> list[tuple[str, str]]:
        """(secret, member) for every secret accessor binding the run made."""
        out = []
        for c in self.gcloud_changes():
            argv = c["argv"]
            if argv[:2] == ["secrets", "add-iam-policy-binding"]:
                out.append((argv[2], argv[argv.index("--member") + 1]))
        return out

    def bucket_changes(self) -> list[tuple[str, str, dict | None]]:
        """(verb, role, condition) for every bucket policy change, in order."""
        out = []
        for c in self.gcloud_changes():
            argv = c["argv"]
            if argv[:2] != ["storage", "buckets"]:
                continue
            if argv[2] not in ("add-iam-policy-binding", "remove-iam-policy-binding"):
                continue
            if argv[argv.index("--member") + 1] != f"serviceAccount:{WORKER}":
                continue
            role = argv[argv.index("--role") + 1]
            if not role.startswith("roles/storage.object"):
                continue
            if "--condition-from-file" in argv:
                cond = json.loads(c["condition"])
            else:
                cond = None
            out.append((argv[2].split("-", 1)[0], role, cond))
        return out

    def patches(self) -> list[dict]:
        return [c for c in self.calls() if c["tool"] == "curl" and c["method"] == "PATCH"]


def _out(proc) -> str:
    return proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Terraform is the source of truth for the split: read its conditions from it
# ---------------------------------------------------------------------------


def _tf_string(text: str, name: str) -> str:
    """The quoted template of `name = { for t, _ in var.tenants : t => "<template>" }`."""
    m = re.search(rf'^\s*{name}\s*=\s*\{{\s*\n\s*for t, _ in var\.tenants :\s*\n\s*t => "((?:[^"\\]|\\.)*)"', text, re.M)
    assert m, f"terraform/modules/tenancy no longer spells {name} the way this test reads it"
    return m.group(1).replace('\\"', '"')


def _render(template: str, tenant: str) -> str:
    return template.replace("${var.artifact_bucket}", BUCKET).replace("${t}", tenant)


def _terraform_split(tenant: str = "eng") -> dict[str, dict]:
    """The worker's two bucket bindings as terraform/modules/tenancy declares them."""
    text = TENANCY.read_text()
    obj = _render(_tf_string(text, "object_prefix"), tenant)
    lst = _render(_tf_string(text, "list_prefix"), tenant)
    ver = _render(_tf_string(text, "verdicts_prefix"), tenant)
    flat = " ".join(text.split())
    # The two joins, pinned so a change to either fails here, not in production.
    assert 'read_expression = { for t, _ in var.tenants : t => join(" || ", [local.object_prefix[t], local.list_prefix[t]]) }' in flat
    assert 'write_expression = { for t, _ in var.tenants : t => "${local.object_prefix[t]} && !${local.verdicts_prefix[t]}" }' in flat

    def resource(name: str) -> dict:
        m = re.search(
            rf'resource "google_storage_bucket_iam_member" "{name}" \{{(.*?)\n\}}', text, re.S
        )
        assert m, f"terraform/modules/tenancy has no {name}"
        body = m.group(1)
        role = re.search(r'role\s*=\s*"([^"]+)"', body).group(1)
        title = re.search(r'title\s*=\s*"([^"]+)"', body).group(1)
        desc = re.search(r'description\s*=\s*"([^"]+)"', body).group(1)
        return {
            "role": role,
            "title": title.replace("${each.key}", tenant),
            "description": desc.replace("${each.key}", tenant),
        }

    read = resource("worker_objects_read")
    write = resource("worker_objects_write")
    read["expression"] = f"{obj} || {lst}"
    write["expression"] = f"{obj} && !{ver}"
    return {"read": read, "write": write}


def _policy(*bindings: dict) -> str:
    return json.dumps({"kind": "storage#policy", "version": 3, "etag": "CAE=", "bindings": list(bindings)})


def _split_bindings() -> list[dict]:
    split = _terraform_split()
    return [
        {
            "role": b["role"],
            "members": [f"serviceAccount:{WORKER}"],
            "condition": {k: b[k] for k in ("title", "description", "expression")},
        }
        for b in (split["read"], split["write"])
    ]


OLD_BINDING = {
    "role": "roles/storage.objectUser",
    "members": [f"serviceAccount:{WORKER}"],
    "condition": {
        "title": "tenant_prefix_only",
        "description": "Only this tenant's object prefix, listing included",
        "expression": (
            f"resource.name.startsWith('projects/_/buckets/{BUCKET}/objects/tenants/eng/') || "
            "api.getAttribute('storage.googleapis.com/objectListPrefix', '').startsWith('tenants/eng/')"
        ),
    },
}

FULL = ["--group", "eng@saga.xyz", "--skip-k8s"]


# ---------------------------------------------------------------------------
# The bucket split
# ---------------------------------------------------------------------------


def test_a_new_tenant_gets_exactly_terraforms_two_bindings(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL])
    out = _out(proc)
    assert proc.returncode == 0, out
    split = _terraform_split()
    expected = [
        ("add", b["role"], {k: b[k] for k in ("title", "description", "expression")})
        for b in (split["read"], split["write"])
    ]
    assert fakes.bucket_changes() == expected, (
        "the worker's bucket grant must be terraform/modules/tenancy's worker_objects_read "
        f"and worker_objects_write, condition for condition:\n{fakes.bucket_changes()}\n{out}"
    )


def test_the_old_single_binding_is_replaced_new_first_then_old_removed(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(OLD_BINDING))
    proc = fakes.run([str(REGISTER), *FULL], tty_answer="eng")
    out = _out(proc)
    assert proc.returncode == 0, out
    changes = fakes.bucket_changes()
    split = _terraform_split()
    assert [(v, r) for v, r, _ in changes] == [
        ("add", "roles/storage.objectViewer"),
        ("add", "roles/storage.objectUser"),
        ("remove", "roles/storage.objectUser"),
    ], f"the new pair must exist before the old binding is removed:\n{changes}\n{out}"
    assert changes[1][2]["expression"] == split["write"]["expression"], changes
    # The removal names the old binding's condition EXACTLY, as read off the
    # policy: remove-iam-policy-binding matches the triple, not the role.
    assert changes[2][2] == OLD_BINDING["condition"], changes


def test_the_old_binding_is_not_removed_without_a_typed_confirmation(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(OLD_BINDING))
    # SWARM_ASSUME_YES must not answer a destructive prompt (CLAUDE.md).
    proc = fakes.run([str(REGISTER), *FULL], SWARM_ASSUME_YES="1")
    out = _out(proc)
    assert proc.returncode != 0, out
    verbs = [v for v, _, _ in fakes.bucket_changes()]
    assert "remove" not in verbs, f"the old binding was removed with no typed confirmation:\n{out}"
    assert verbs == ["add", "add"], f"the new pair should be in place before the prompt:\n{out}"
    assert "tenant_prefix_only" in out, out


def test_a_wrong_answer_removes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(OLD_BINDING))
    proc = fakes.run([str(REGISTER), *FULL], tty_answer="yes")
    assert proc.returncode != 0, _out(proc)
    assert "remove" not in [v for v, _, _ in fakes.bucket_changes()], _out(proc)


def test_an_already_split_tenant_reruns_as_a_no_op(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(*_split_bindings()))
    proc = fakes.run([str(REGISTER), *FULL])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.bucket_changes() == [], f"a tenant already split was changed:\n{out}"
    assert "already split" in out, out


def test_the_split_is_found_by_expression_not_by_role(tmp_path) -> None:
    """objectUser alone is the OLD shape's role too, so it cannot mean 'done'."""
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(_split_bindings()[0], OLD_BINDING))
    proc = fakes.run([str(REGISTER), *FULL, "--dry-run"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "objectUser already granted" not in out and "already split" not in out, out
    would = [
        line for line in out.splitlines()
        if "would run: gcloud storage buckets" in line and WORKER in line and "roles/storage.object" in line
    ]
    assert len(would) == 2, would
    assert "add-iam-policy-binding" in would[0] and "roles/storage.objectUser" in would[0], would
    assert "remove-iam-policy-binding" in would[1], would


def test_a_dry_run_on_the_old_shape_changes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    fakes.policy.write_text(_policy(OLD_BINDING))
    proc = fakes.run([str(REGISTER), *FULL, "--dry-run"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [], out
    assert "would remove" in out and "tenant_prefix_only" in out, out


# ---------------------------------------------------------------------------
# App keys: retired, and refused for every account (owner decision MS0-Q4)
# ---------------------------------------------------------------------------
#
# Nothing reads a `-git-merge` or `-git-review` App key any more: the merge
# runs as the worker on `-git` (contract request 47) and the merge, post-verdict
# and review accounts are gone from terraform. So the script no longer binds
# either key to any account, and still never to the worker. FAILS WITHOUT THE
# CHANGE: `--add-provider git-review` bound the post-verdict and review
# accounts and listed the provider; `--providers git-merge` named the merge
# account as the key's owner.


@pytest.mark.parametrize("provider", ["git-merge", "git-review"])
def test_a_full_registration_refuses_an_app_provider(tmp_path, provider) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", f"anthropic,{provider}", "--dry-run"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "retired" in out and "#295" in out, f"the refusal must say the provider is retired:\n{out}"
    for account in ("swarm-eng-merge", "swarm-eng-post-verdict", "swarm-eng-review", "--add-provider"):
        assert account not in out, f"the refusal sends the operator to a path that no longer exists:\n{out}"
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    assert f"swarm-tenant-eng-{provider}" not in " ".join(
        line for line in out.splitlines() if "would run" in line
    ), out


@pytest.mark.parametrize("provider", ["git-merge", "git-review"])
def test_add_provider_refuses_an_app_provider_before_reading_anything(tmp_path, provider) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", provider])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "retired" in out and "Nothing was changed" in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    # Refused on the name: not a single secret, account or GitHub read.
    assert not [c for c in fakes.calls() if c["tool"] == "curl"], out
    assert not [c for c in fakes.calls() if "secrets" in c.get("argv", [])], out


def test_the_tfvars_flag_went_with_the_forge_record(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "openai", "--tfvars", "x.tfvars", "--dry-run"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "--tfvars" in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


# ---------------------------------------------------------------------------
# Probing common.sh against a terraform module (test_register_tenant_forge_reader)
# ---------------------------------------------------------------------------

SA_IDS = REPO / "terraform" / "modules" / "service_account_ids" / "main.tf"


def _derive(tmp_path: Path, module: Path, *calls: str) -> list[str]:
    fakes = Fakes(Path(tempfile.mkdtemp(dir=tmp_path)))
    probe = "\n".join([
        "set -euo pipefail",
        f"source {REPO / 'scripts' / 'lib' / 'common.sh'}",
        f"SA_IDS_MODULE={module}",
        *calls,
    ])
    proc = subprocess.run(["bash", "-c", probe], env=fakes.env, capture_output=True, text=True, check=True)
    return proc.stdout.splitlines()
