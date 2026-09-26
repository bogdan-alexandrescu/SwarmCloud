"""Adding one provider to a tenant keeps every provider it already has.

Two operator hints told people how to add a provider, and neither survived being
followed as printed:

  * `scripts/create-secrets.sh`, after storing a key nobody could read yet,
    printed `register-tenant.sh --tenant <t> --providers <p>`. The full
    registration needs `--group` or `--user`, so that command died on its first
    line.
  * `scripts/register-tenant.sh` closed every registration with
    `then re-run this  register-tenant.sh --<kind> <principal> --providers anthropic`.
    That one runs -- and `--providers` REPLACES the tenant's `credentials`
    (section 6's field mask names the whole list), so a tenant holding openai
    came out holding anthropic alone and every openai task parked as
    CREDENTIAL_MISSING. The same re-run reset max_active, capacity_units and
    display_name to their defaults and moved the tenant pool's ceiling with them.

The first two tests below take each hint off the script's own output and RUN it,
so what they hold is the property -- the command an operator is told to type
keeps the providers the tenant already has -- rather than the spelling of a
flag. The rest pin down `--add-provider`, the narrow path both hints now point
at: one secretAccessor binding on that one secret, then the provider added to
`credentials` under an update mask of `credentials` alone, conditional on the
document not having changed since it was read.

Driven the way test_register_tenant_grants.py drives the script: the real
scripts, with a fake `gcloud`, `curl` and `kubectl` first on PATH. Both fakes
append every call to one log, in order, so a test can say what was written and
what was not, and which came first. Nothing reaches a real project.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
REGISTER = REPO / "scripts" / "register-tenant.sh"
CREATE_SECRETS = REPO / "scripts" / "create-secrets.sh"

PROJECT = "swarm-test-project"
BUCKET = "swarm-test-artifacts"
WORKER = f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
UPDATE_TIME = "2026-09-25T23:09:12.345678Z"

# Not shaped like any provider's key on purpose: a secrets scanner reads this
# file too, and create-secrets.sh only warns about an unfamiliar prefix.
FAKE_KEY = "not-a-real-provider-key-0123456789\n"

pytestmark = pytest.mark.skipif(
    not REGISTER.exists() or shutil.which("jq") is None,
    reason="register-tenant.sh and jq are both required",
)


# Every call is appended to FAKE_LOG as one JSON line before it is answered.
# Reads answer as a healthy project would; FAKE_SECRET_DESCRIBE_ERR makes the
# secret lookup fail the way gcloud does.
#
# The argv goes to jq on stdin, one per line, not through `--args`: jq keeps
# parsing options after `--args` in some releases, and gcloud's own `--project`
# would be read as one of jq's.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" | jq -Rnc '{tool:"gcloud", argv:[inputs]}' >>"${FAKE_LOG}"
args="$*"
case "${args}" in
  *"auth print-access-token"*)        echo "fake-access-token" ;;
  *"identity groups describe"*)       echo "groups/1" ;;
  *"iam service-accounts describe"*)  echo "sa@example.iam.gserviceaccount.com" ;;
  *"iam roles describe"*)             echo "roles/custom" ;;
  *"storage buckets describe"*)       echo "swarm-test-artifacts" ;;
  *"storage cp"*)                     cat >/dev/null ;;
  *"secrets describe"*)
    if [[ -n "${FAKE_SECRET_DESCRIBE_ERR:-}" ]]; then
      printf '%s\n' "${FAKE_SECRET_DESCRIBE_ERR}" >&2
      exit 1
    fi
    echo "projects/x/secrets/whatever" ;;
  *"secrets versions add"*)           echo "projects/x/secrets/whatever/versions/2" ;;
  *"secrets get-iam-policy"*)         printf '%s' "${FAKE_SECRET_MEMBERS:-}" ;;
  *)                                  : ;;
esac
exit 0
"""

# The real fs_request calls curl as `-X <method> ... -o <file> -w '%{http_code}'`
# with the body in --data-binary, so stdout is the STATUS and the response goes
# to the file; fs_database_exists calls it with neither and reads the body from
# stdout. GET tenants/<id> serves FAKE_TENANT_DOC, or a 404 when that is empty.
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
cat >/dev/null 2>&1 || true
jq -nc --arg m "${method}" --arg u "${url}" --arg b "${body}" \
  '{tool:"curl", method:$m, url:$u, body:$b}' >>"${FAKE_LOG}"
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
  "PATCH "*)
    if [[ -n "${FAKE_PATCH_ERROR:-}" ]]; then
      status=400
      resp="${FAKE_PATCH_ERROR}"
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

# A client new enough for kubectl_bin, pointed at a context that is not the
# swarm's, so a full registration takes its "not connected" branch and never
# reaches a real kubeconfig.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
case "$*" in
  *"version --client"*)       echo "Client Version: v1.36.3" ;;
  *"config current-context"*) echo "not-the-swarm-cluster" ;;
esac
exit 0
"""


def _tenant_document(
    credentials: list[str],
    *,
    principal: str = "eng@saga.xyz",
    kind: str = "group",
    service_account: str = WORKER,
) -> str:
    """tenants/eng as Firestore's REST API returns it, with non-default limits.

    max_active 40 and capacity_units 80 are what a re-run without those flags
    would have reset to 20 and 40.
    """
    values = [{"stringValue": c} for c in credentials]
    fields = {
        "tenant_id": {"stringValue": "eng"},
        "kind": {"stringValue": kind},
        "principal": {"stringValue": principal},
        "display_name": {"stringValue": "Engineering"},
        "max_active": {"integerValue": "40"},
        "capacity_units": {"integerValue": "80"},
        "enabled": {"booleanValue": True},
        "credentials": {"arrayValue": {"values": values} if values else {}},
        "service_account": {"stringValue": service_account},
        "gcs_prefix": {"stringValue": "tenants/eng/"},
        "namespace": {"stringValue": "swarm-tenant-eng"},
    }
    return json.dumps({
        "name": f"projects/{PROJECT}/databases/swarm/documents/tenants/eng",
        "fields": fields,
        "createTime": "2026-09-20T10:00:00.000000Z",
        "updateTime": UPDATE_TIME,
    })


@dataclass(frozen=True)
class Write:
    """One Firestore PATCH, decoded."""

    doc: str
    mask: list[str]
    precondition: str | None
    credentials: list[str] | None


class Fakes:
    """A project that exists only on PATH, and the log of what was asked of it."""

    def __init__(self, tmp: Path, tenant_doc: str | None) -> None:
        self.tmp = tmp
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL), ("kubectl", FAKE_KUBECTL),
        ):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)

        # common.sh sources this as shell and refuses one anybody else can write.
        env_file = tmp / "env"
        env_file.write_text(
            f"PROJECT_ID={PROJECT}\n"
            "REGION=us-central1\n"
            "ENVIRONMENT=dev\n"
            "FIRESTORE_DATABASE=swarm\n"
            f"ARTIFACT_BUCKET={BUCKET}\n"
        )
        env_file.chmod(0o600)

        doc_file = tmp / "tenant.json"
        doc_file.write_text(tenant_doc or "")
        self.log = tmp / "calls.jsonl"
        self.log.write_text("")

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA")
        }
        env.update({
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SWARM_ENV_FILE": str(env_file),
            "SWARM_KUBECTL": str(bin_dir / "kubectl"),
            "KUBECONFIG": str(tmp / "kubeconfig"),
            "NO_COLOR": "1",
            "FAKE_LOG": str(self.log),
            "FAKE_TENANT_DOC": str(doc_file),
        })
        self.env = env

    def run(
        self, argv: list[str], *, stdin: str | None = None, **fake_env: str
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(fake_env)
        return subprocess.run(
            argv, cwd=REPO, env=env, input=stdin if stdin is not None else "",
            capture_output=True, text=True, timeout=180,
        )

    def forget(self) -> None:
        self.log.write_text("")

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def writes(self) -> list[Write]:
        out = []
        for call in self.calls():
            if call["tool"] != "curl" or call["method"] != "PATCH":
                continue
            url = urlsplit(call["url"])
            query = parse_qs(url.query)
            body = json.loads(call["body"])["fields"]
            creds = None
            if "credentials" in body:
                creds = [
                    v["stringValue"]
                    for v in body["credentials"]["arrayValue"].get("values", [])
                ]
            out.append(Write(
                doc=url.path.split("/documents/", 1)[1],
                mask=query.get("updateMask.fieldPaths", []),
                precondition=(query.get("currentDocument.updateTime") or [None])[0],
                credentials=creds,
            ))
        return out

    def gcloud_changes(self) -> list[list[str]]:
        """Every gcloud call that is not a read, in order."""
        reads = ("print-access-token", "describe", "get-iam-policy", "list")
        return [
            call["argv"] for call in self.calls()
            if call["tool"] == "gcloud" and not any(r in call["argv"] for r in reads)
        ]


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def _command_after(text: str, needle: str) -> list[str]:
    """The command on the first line of `text` that names `needle`, from there on."""
    for line in text.splitlines():
        if needle in line:
            return shlex.split(line[line.index(needle):])
    raise AssertionError(f"no line naming {needle}:\n{text}")


def _assert_kept_openai_and_added_anthropic(fakes: Fakes, hint: list[str], out: str) -> None:
    tenant_writes = [w for w in fakes.writes() if w.doc == "tenants/eng"]
    assert tenant_writes, f"the hint never listed anthropic for the tenant:\n  {shlex.join(hint)}\n{out}"
    for write in tenant_writes:
        assert write.credentials is None or "openai" in write.credentials, (
            "following the hint DROPPED openai from the tenant's credentials, so every "
            f"openai task would park as CREDENTIAL_MISSING:\n  {shlex.join(hint)}\n"
            f"  wrote credentials={write.credentials}\n{out}"
        )
    assert [w.mask for w in tenant_writes] == [["credentials"]], (
        "adding a provider must write `credentials` and nothing else; this also "
        f"rewrote limits and identity fields:\n  {shlex.join(hint)}\n"
        f"  masks={[w.mask for w in tenant_writes]}\n{out}"
    )
    assert tenant_writes[0].credentials == ["anthropic", "openai"], out


# ---------------------------------------------------------------------------
# The two hints, followed as printed
# ---------------------------------------------------------------------------


def test_the_hint_create_secrets_prints_keeps_the_tenants_other_providers(tmp_path) -> None:
    """Store a key nobody can read yet, then type exactly what the script says."""
    fakes = Fakes(tmp_path, _tenant_document(["openai"]))
    stored = fakes.run(
        [str(CREATE_SECRETS), "--tenant", "eng", "--provider", "anthropic", "--stdin"],
        stdin=FAKE_KEY,
    )
    assert stored.returncode == 0, _out(stored)
    hint = _command_after(_out(stored), "scripts/register-tenant.sh")

    fakes.forget()
    followed = fakes.run(hint)
    out = _out(followed)
    assert followed.returncode == 0, (
        f"create-secrets.sh's own hint fails when typed as printed:\n  {shlex.join(hint)}\n{out}"
    )
    _assert_kept_openai_and_added_anthropic(fakes, hint, out)


def test_the_next_step_register_tenant_prints_keeps_the_tenants_other_providers(tmp_path) -> None:
    """The closing 'Next:' block of a registration, followed as printed."""
    fakes = Fakes(tmp_path, _tenant_document(["openai"]))
    registered = fakes.run([str(REGISTER), "--group", "eng@saga.xyz", "--dry-run", "--skip-k8s"])
    assert registered.returncode == 0, _out(registered)
    assert "Next:" in _out(registered), _out(registered)
    hint = _command_after(_out(registered).split("Next:", 1)[1], "scripts/register-tenant.sh")

    fakes.forget()
    followed = fakes.run(hint)
    out = _out(followed)
    assert followed.returncode == 0, f"the printed next step fails:\n  {shlex.join(hint)}\n{out}"
    _assert_kept_openai_and_added_anthropic(fakes, hint, out)


def test_a_subscription_hint_never_sends_the_worker_to_the_refresh_half(tmp_path) -> None:
    """`--subscription` stores `-refresh`, which only the quota broker may read.

    terraform/modules/secret_manager says the tenant's worker is ABSENT from
    that secret's readers on purpose: with the refresh token a job could mint
    itself access for as long as it liked. So the hint must name the broker as
    that secret's reader, and the command it offers must bind the worker to the
    short-lived half the broker publishes -- never to `-refresh`.
    """
    fakes = Fakes(tmp_path, _tenant_document(["openai"]))
    credential = json.dumps({
        "claudeAiOauth": {
            "accessToken": "fake-access", "refreshToken": "fake-refresh", "expiresAt": 1,
        }
    })
    stored = fakes.run(
        [str(CREATE_SECRETS), "--tenant", "eng", "--provider", "anthropic",
         "--subscription", "--stdin"],
        stdin=credential,
    )
    out = _out(stored)
    assert stored.returncode == 0, out
    assert "quota broker" in out, f"the -refresh secret's one reader is not named:\n{out}"
    hint = _command_after(out, "scripts/register-tenant.sh")
    assert hint[hint.index("--add-provider") + 1] == "anthropic", hint

    fakes.forget()
    followed = fakes.run(hint)
    assert followed.returncode == 0, _out(followed)
    bindings = [argv for argv in fakes.gcloud_changes() if "add-iam-policy-binding" in argv]
    assert bindings, _out(followed)
    for argv in bindings:
        assert "swarm-tenant-eng-anthropic-refresh" not in argv, (
            f"the worker was granted the -refresh half:\n  {argv}"
        )
    _assert_kept_openai_and_added_anthropic(fakes, hint, _out(followed))


# ---------------------------------------------------------------------------
# --add-provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "who", [["--tenant", "eng"], ["--group", "eng@saga.xyz"]], ids=["by-id", "by-principal"]
)
def test_add_provider_writes_the_union_and_nothing_else(tmp_path, who: list[str]) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "openai"]))
    proc = fakes.run([str(REGISTER), *who, "--add-provider", "git"])
    out = _out(proc)
    assert proc.returncode == 0, out

    assert fakes.writes() == [
        Write(
            doc="tenants/eng",
            mask=["credentials"],
            precondition=UPDATE_TIME,
            credentials=["anthropic", "git", "openai"],
        )
    ], (
        "expected ONE write: tenants/eng, update mask `credentials` alone, the sorted "
        "union swarm_api's register_credential writes, conditional on the document "
        f"being unchanged since it was read:\n{fakes.writes()}\n{out}"
    )
    assert fakes.gcloud_changes() == [[
        "secrets", "add-iam-policy-binding", "swarm-tenant-eng-git",
        "--project", PROJECT,
        "--member", f"serviceAccount:{WORKER}",
        "--role", "roles/secretmanager.secretAccessor", "--quiet",
    ]], (
        "adding a provider must make exactly one grant -- the worker may read that one "
        f"secret -- and create nothing:\n{fakes.gcloud_changes()}\n{out}"
    )


def test_add_provider_grants_the_secret_before_it_lists_the_provider(tmp_path) -> None:
    """Listed-but-unreadable admits the provider's tasks and fails every one.

    Readable-but-unlisted is inert, so if the run stops between the two changes
    it must stop in that state.
    """
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git"])
    assert proc.returncode == 0, _out(proc)

    order = [
        "grant" if call["tool"] == "gcloud" and "add-iam-policy-binding" in call["argv"]
        else "list" if call["tool"] == "curl" and call["method"] == "PATCH"
        else None
        for call in fakes.calls()
    ]
    assert [o for o in order if o] == ["grant", "list"], order


def test_a_provider_already_listed_is_not_written_again(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.writes() == [], f"an already-listed provider was written again:\n{out}"
    assert "already lists git" in out, out


def test_add_provider_on_a_dry_run_changes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git", "--dry-run"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.writes() == [], out
    assert fakes.gcloud_changes() == [], out
    assert "would run: gcloud secrets add-iam-policy-binding swarm-tenant-eng-git" in out, out
    assert '["anthropic"] -> ["anthropic","git"]' in out, out


def test_add_provider_refuses_a_tenant_that_is_not_registered(tmp_path) -> None:
    """It changes a tenant; it never makes one. A PATCH on a missing document
    would create a record holding nothing but `credentials`."""
    fakes = Fakes(tmp_path, None)
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "does not exist" in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


def test_add_provider_refuses_a_secret_that_does_not_exist(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run(
        [str(REGISTER), "--tenant", "eng", "--add-provider", "git"],
        FAKE_SECRET_DESCRIBE_ERR=(
            "ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret "
            f"[projects/{PROJECT}/secrets/swarm-tenant-eng-git] not found or has no versions."
        ),
    )
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "scripts/create-secrets.sh --tenant eng --provider git --stdin" in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


def test_add_provider_does_not_read_a_denied_lookup_as_a_missing_secret(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run(
        [str(REGISTER), "--tenant", "eng", "--add-provider", "git"],
        FAKE_SECRET_DESCRIBE_ERR=(
            "ERROR: (gcloud.secrets.describe) PERMISSION_DENIED: Permission "
            "'secretmanager.secrets.get' denied for resource (or it may not exist)."
        ),
    )
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "PERMISSION_DENIED" in out, out
    assert "create-secrets.sh" not in out, (
        f"a denied lookup was reported as a secret to create:\n{out}"
    )
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


@pytest.mark.parametrize(
    "flag",
    [
        ["--providers", "git"],
        ["--max-active", "5"],
        ["--capacity-units", "8"],
        ["--budget", "100"],
        ["--display-name", "Eng"],
        ["--skip-k8s"],
    ],
    ids=lambda f: f[0],
)
def test_add_provider_refuses_the_flags_of_a_full_registration(tmp_path, flag: list[str]) -> None:
    """They would be silently ignored, and an operator who typed them meant them."""
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git", *flag])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert flag[0] in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        ("anthropic-refresh", "quota broker"),
        ("Git", "lowercase"),
        ("git/x", "lowercase"),
        ("", "needs a provider name"),
    ],
    ids=["refresh-half", "uppercase", "slash", "empty"],
)
def test_add_provider_refuses_a_provider_it_must_not_bind(
    tmp_path, provider: str, reason: str
) -> None:
    """`<p>-refresh` names the half only the quota broker may read.

    The reason is asserted, not only the exit code: a script that did not know
    --add-provider at all also exits non-zero here, having refused nothing.
    """
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", provider])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert reason in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


def test_add_provider_refuses_a_document_owned_by_another_principal(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"], principal="contractors@saga.xyz"))
    proc = fakes.run([str(REGISTER), "--group", "eng@saga.xyz", "--add-provider", "git"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "contractors@saga.xyz" in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


def test_add_provider_refuses_to_guess_which_identity_reads_the_key(tmp_path) -> None:
    """The document and the naming rule disagree about the worker's identity."""
    other = f"somebody-else@{PROJECT}.iam.gserviceaccount.com"
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"], service_account=other))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert other in out and WORKER in out, out
    assert fakes.writes() == [] and fakes.gcloud_changes() == [], out


def test_a_document_that_changed_after_it_was_read_is_not_overwritten(tmp_path) -> None:
    """Firestore refuses the conditional write; the run must stop and say why."""
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run(
        [str(REGISTER), "--tenant", "eng", "--add-provider", "git"],
        FAKE_PATCH_ERROR=json.dumps({"error": {
            "code": 400,
            "message": "the stored version does not match the required base version",
            "status": "FAILED_PRECONDITION",
        }}),
    )
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "FAILED_PRECONDITION" in out, out
    assert "run this again" in out, out
