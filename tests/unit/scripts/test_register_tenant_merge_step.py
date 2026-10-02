"""`scripts/register-tenant.sh` and the #295 identities (docs/merge-step.md §1.3, §4.3, §10 item 5).

Three things the script has to produce exactly as terraform/modules/tenancy and
terraform/modules/service_account_ids (M2, #480) do, because it is the second
path that grants them:

  * NO WORKER ACCOUNT EVER READS AN APP KEY. `swarm-tenant-<t>-git-merge` is
    read by `swarm-<t>-merge` alone and `-git-review` by `swarm-<t>-post-verdict`
    alone; the review account reads its profile's own provider key beside the
    worker. Any agent of the tenant can mint the worker's token (§0), so a
    worker binding on an App key hands every agent the merge or verdict App.
  * THE WORKER'S BUCKET GRANT IS SPLIT. Read and list on tenants/<t>/, write
    on tenants/<t>/ EXCEPT tenants/<t>/verdicts/, which only the review
    account may create in. The old single objectUser binding still lets a
    worker write a verdict, so a re-run must find it and replace it -- the new
    pair created first, the old one removed after, under a typed
    confirmation -- and a re-run on a tenant already split changes nothing.
  * `review_app_bot_id` IS PINNED AT REGISTRATION (§5.2a): resolved once with
    the review App's own JWT and written into the tenant's tfvars entry, so the
    merge Job never asks GitHub who the review App is.

Driven end to end: the real script with a fake `gcloud` and `curl` first on
PATH, each appending every call to one log in order. Nothing reaches a real
project or GitHub. The App key is generated per test with openssl, so no key
material is written into this file.
"""

from __future__ import annotations

import base64
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
APP_ID = 123456
BOT_ID = 4242
APP_SLUG = "swarm-review-eng"


def _sa(account_id: str) -> str:
    return f"{account_id}@{PROJECT}.iam.gserviceaccount.com"


WORKER = _sa("swarm-agent-worker-eng")
MERGE = _sa("swarm-eng-merge")
POST_VERDICT = _sa("swarm-eng-post-verdict")
REVIEW = _sa("swarm-eng-review")

pytestmark = pytest.mark.skipif(
    not REGISTER.exists()
    or shutil.which("jq") is None
    or shutil.which("openssl") is None,
    reason="register-tenant.sh, jq and openssl are all required",
)


# Every call is appended to FAKE_LOG as one JSON line before it is answered.
# A --condition-from-file is read when the call is made and logged beside the
# argv, because the script removes the file afterwards.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
cond=""; name=""; prev=""
for arg in "$@"; do
  case "${prev}" in
    describe|list) name="${arg}" ;;
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
  *"secrets versions list"*)          echo "projects/x/secrets/${name}/versions/1" ;;
  *"secrets versions access"*)        cat "${FAKE_APP_SECRET}" ;;
  *)                                  : ;;
esac
exit 0
"""

# Firestore as the real fs_request calls it (status on stdout, body in -o),
# plus the two GitHub reads. A `-H @file` header is read at call time and
# appended to FAKE_GH_AUTH, so a test can check the JWT the script signed
# without it ever having been on a command line.
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
out=""; want_status=0; method="GET"; body=""; url=""; prev=""
for arg in "$@"; do
  case "${prev}" in
    -o) out="${arg}" ;;
    -w) [[ "${arg}" == *http_code* ]] && want_status=1 ;;
    -X) method="${arg}" ;;
    --data-binary) body="${arg}" ;;
    -H) if [[ "${arg}" == @* ]]; then cat "${arg#@}" >>"${FAKE_GH_AUTH}"; fi ;;
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
  "GET https://api.github.com/repos/"*"/installation")
    resp="$(jq -nc --argjson a "${FAKE_GH_APP_ID}" --arg s "${FAKE_GH_SLUG}" '{id:99, app_id:$a, app_slug:$s}')" ;;
  "GET https://api.github.com/users/"*)
    resp="$(jq -nc --argjson i "${FAKE_GH_BOT_ID}" --arg s "${FAKE_GH_SLUG}" '{id:$i, login:($s + "[bot]"), type:"Bot"}')" ;;
esac
if [[ -n "${out}" ]]; then
  printf '%s' "${resp}" >"${out}"
  if [[ "${want_status}" -eq 1 ]]; then printf '%s' "${status}"; fi
else
  printf '%s' "${resp}"
fi
exit 0
"""

TFVARS = """\
tenants = {
  eng = {
    kind      = "group"
    principal = "eng@saga.xyz"
    providers = ["anthropic", "git-review"]
    forge = {
      owner         = "saga-xyz"
      repo          = "swarm"
      review_app_id = 123456
    }
  }
  # A brace in a comment must not move the parser: {
  other = {
    kind      = "group"
    principal = "other@saga.xyz"
    forge = {
      owner             = "saga-xyz"
      repo              = "other"
      review_app_id     = 7
      review_app_bot_id = 8
    }
  }
}
"""

ENG_FORGE_AFTER = """\
    forge = {
      owner             = "saga-xyz"
      repo              = "swarm"
      review_app_id     = 123456
      review_app_bot_id = 4242
    }
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


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


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

        # The App's key, made here so no key is ever a literal in the repository.
        self.key = tmp / "app.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(self.key), "2048"],
            check=True, capture_output=True,
        )
        self.pem = self.key.read_text()
        self.public = tmp / "app.pub"
        subprocess.run(
            ["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(self.public)],
            check=True, capture_output=True,
        )
        self.app_secret = tmp / "app-secret.json"
        self.app_secret.write_text(json.dumps({"app_id": APP_ID, "private_key": self.pem}))

        self.tfvars = tmp / "dev.tfvars"
        self.tfvars.write_text(TFVARS)
        self.policy = tmp / "bucket-policy.json"
        self.policy.write_text("")
        doc_file = tmp / "tenant.json"
        doc_file.write_text(tenant_doc or "")
        self.log = tmp / "calls.jsonl"
        self.log.write_text("")
        self.gh_auth = tmp / "gh-auth"
        self.gh_auth.write_text("")
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
            "FAKE_APP_SECRET": str(self.app_secret),
            "FAKE_GH_AUTH": str(self.gh_auth),
            "FAKE_GH_APP_ID": str(APP_ID),
            "FAKE_GH_SLUG": APP_SLUG,
            "FAKE_GH_BOT_ID": str(BOT_ID),
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
# App keys never reach the worker account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "owner"), [("git-merge", "swarm-eng-merge"), ("git-review", "swarm-eng-post-verdict")]
)
def test_a_full_registration_refuses_an_app_provider_for_the_worker(tmp_path, provider, owner) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", f"anthropic,{provider}", "--dry-run"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert owner in out, f"the refusal must name the account the key belongs to:\n{out}"
    assert fakes.gcloud_changes() == [] and f"swarm-tenant-eng-{provider}" not in " ".join(
        line for line in out.splitlines() if "would run" in line
    ), out


def test_add_provider_git_merge_binds_the_merge_account_alone(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git-review"]))
    fakes.tfvars.write_text(TFVARS.replace("review_app_id = 123456", "review_app_id = 123456\n      review_app_bot_id = 4242"))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-merge", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.secret_grants() == [("swarm-tenant-eng-git-merge", f"serviceAccount:{MERGE}")], out
    assert WORKER not in json.dumps(fakes.secret_grants()), out


def test_add_provider_git_merge_needs_the_review_bot_pinned_first(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-merge", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "review_app_bot_id" in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


def test_add_provider_git_review_binds_post_verdict_and_review_never_the_worker(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert sorted(fakes.secret_grants()) == sorted([
        ("swarm-tenant-eng-git-review", f"serviceAccount:{POST_VERDICT}"),
        ("swarm-tenant-eng-anthropic", f"serviceAccount:{REVIEW}"),
    ]), out
    patches = fakes.patches()
    assert len(patches) == 1 and "git-review" in patches[0]["body"], out


def test_a_missing_action_account_stops_before_anything_changes(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run(
        [str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)],
        FAKE_MISSING_ACCOUNTS=POST_VERDICT,
    )
    out = _out(proc)
    assert proc.returncode != 0, out
    assert POST_VERDICT in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    assert fakes.tfvars.read_text() == TFVARS


# ---------------------------------------------------------------------------
# review_app_bot_id
# ---------------------------------------------------------------------------


def test_review_app_bot_id_is_resolved_with_the_apps_jwt_and_written(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode == 0, out
    text = fakes.tfvars.read_text()
    assert ENG_FORGE_AFTER in text, f"review_app_bot_id not written into eng's forge block:\n{text}"
    assert "review_app_bot_id = 8" in text, "another tenant's entry was changed"
    assert text.count("review_app_bot_id") == 2, text

    # The installation was asked with a JWT the App's own key signed, iss = app id.
    auth = fakes.gh_auth.read_text().strip()
    m = re.fullmatch(r"Authorization: Bearer ([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)", auth)
    assert m, f"the installation call did not carry a JWT header from a file: {auth[:40]!r}"
    header, payload = (json.loads(_b64url_decode(p)) for p in m.groups()[:2])
    assert header == {"alg": "RS256", "typ": "JWT"}, header
    assert str(payload["iss"]) == str(APP_ID) and payload["exp"] > payload["iat"], payload
    signed = tmp_path / "signed"
    signed.write_bytes(f"{m.group(1)}.{m.group(2)}".encode())
    sig = tmp_path / "sig"
    sig.write_bytes(_b64url_decode(m.group(3)))
    verify = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(fakes.public), "-signature", str(sig), str(signed)],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0, f"the JWT is not signed by the App's key: {verify.stdout}{verify.stderr}"


def test_a_rerun_with_the_same_bot_id_writes_nothing_new(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git-review"]))
    argv = [str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)]
    assert fakes.run(argv).returncode == 0
    first = fakes.tfvars.read_text()
    proc = fakes.run(argv)
    assert proc.returncode == 0, _out(proc)
    assert fakes.tfvars.read_text() == first


def test_a_different_pinned_bot_id_is_not_overwritten(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    pinned = TFVARS.replace("review_app_id = 123456", "review_app_id = 123456\n      review_app_bot_id = 1")
    fakes.tfvars.write_text(pinned)
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "already 1" in out and f"answers {BOT_ID}" in out, f"refused, but not for the pinned id:\n{out}"
    assert fakes.tfvars.read_text() == pinned
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


def test_an_installation_of_another_app_is_refused(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run(
        [str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)],
        FAKE_GH_APP_ID="999",
    )
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "belongs to App" in out and "999" in out, f"refused, but not for the App mismatch:\n{out}"
    assert fakes.tfvars.read_text() == TFVARS
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


def test_add_provider_git_review_dry_run_changes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([
        str(REGISTER), "--tenant", "eng", "--add-provider", "git-review",
        "--tfvars", str(fakes.tfvars), "--dry-run",
    ])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    assert fakes.tfvars.read_text() == TFVARS
    assert fakes.gh_auth.read_text() == "", "a dry run minted a JWT and asked GitHub"
    assert not any("versions" in c.get("argv", []) and "access" in c["argv"] for c in fakes.calls()), (
        "a dry run read the App key"
    )
    assert "would resolve" in out and "review_app_bot_id" in out, out
    assert "would run: gcloud secrets add-iam-policy-binding swarm-tenant-eng-git-review" in out, out


def test_no_key_or_jwt_reaches_output_argv_or_disk(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git-review", "--tfvars", str(fakes.tfvars)])
    out = _out(proc)
    assert proc.returncode == 0, out
    jwt = fakes.gh_auth.read_text().strip().rsplit(" ", 1)[-1]
    body_lines = [line for line in fakes.pem.splitlines() if line and not line.startswith("-----")]
    argv_text = json.dumps([c.get("argv") for c in fakes.calls()])
    for label, haystack in (("output", out), ("argv", argv_text), ("tfvars", fakes.tfvars.read_text())):
        assert jwt not in haystack, f"the JWT reached the {label}"
        for line in body_lines[:3]:
            assert line not in haystack, f"the App key reached the {label}"
    leftovers = list(fakes.scratch.iterdir())
    assert leftovers == [], f"temporary files were left behind: {leftovers}"


# ---------------------------------------------------------------------------
# The account names come from terraform/modules/service_account_ids
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


def test_the_account_table_is_read_off_terraform(tmp_path) -> None:
    rows = _derive(tmp_path, SA_IDS, "tf_action_accounts")
    assert rows == [
        "merge|-merge|git-merge|true|",
        "post-verdict|-post-verdict|git-review|true|",
        "claude-code-review|-review|git-review|false|anthropic",
    ], rows
    ids = _derive(
        tmp_path, SA_IDS,
        *(f'tenant_action_account_id eng {p}; echo' for p in ("merge", "post-verdict", "claude-code-review")),
    )
    assert ids == ["swarm-eng-merge", "swarm-eng-post-verdict", "swarm-eng-review"], ids


def test_a_renamed_suffix_in_terraform_moves_the_script_with_it(tmp_path) -> None:
    changed = tmp_path / "main.tf"
    text = SA_IDS.read_text()
    assert 'suffix = "-post-verdict"' in text
    changed.write_text(text.replace('suffix = "-post-verdict"', 'suffix = "-pv"'))
    ids = _derive(tmp_path, changed, "tenant_action_account_id eng post-verdict; echo")
    assert ids == ["swarm-eng-pv"], ids
