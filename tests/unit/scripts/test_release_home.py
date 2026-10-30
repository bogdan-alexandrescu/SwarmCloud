"""scripts/release-home.sh stands up the release home, idempotently and guarded
(DAYONE-RELHOME-1; part of #2688).

The release home is project `swarmcloud-prod` (D25; display name `swarmcloud`; `swarmcloud-releases` if that
id had been taken, D23): an Artifact Registry repository `swarmcloud` (us,
Docker), a bucket `gs://swarmcloud` (uniform access, versioning) and a KMS key
`swarmcloud-release/cosign` (asymmetric P-256) that signs the release, each
labelled `managed-by=swarm-release`. BGD runs it; CLD does not create projects.

The public read (`allUsers` as `roles/storage.objectViewer` on the bucket and
`roles/artifactregistry.reader` on the repository) is the one IAM change that
makes the release downloadable by any company. It is granted only when the
effective `iam.allowedPolicyMemberDomains` policy on the project already admits
it -- the Q4 org-policy exception, an org-admin act -- and only after the
operator types the project id, which `SWARM_ASSUME_YES` does not skip.

`--ci-publisher EMAIL` (DAYONE-RELEASE-CI, BGD 2026-10-30: CI publishes) grants
the publish-release workflow's identity exactly three roles, each on one
resource and none on the project: `roles/artifactregistry.writer` on the
repository, `roles/storage.objectAdmin` on the bucket and
`roles/cloudkms.signerVerifier` on the cosign key. The same typed project id
gates them.

Every test runs the script against a fake `gcloud` first on `PATH`, which logs
each call and answers describes from a JSON state file: no cloud credentials.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "release-home.sh"
COMMON = REPO / "scripts" / "lib" / "common.sh"

PROJECT = "swarmcloud-prod"
BUCKET = "gs://swarmcloud"
ALL_USERS = "allUsers"

#: The verbs that change something. Anything else the script calls is a read.
WRITE_VERBS = ("create", "update", "enable", "add-iam-policy-binding")

FAKE_GCLOUD = r'''#!{python}
"""A fake gcloud: logs argv, answers reads from $FAKE_GCLOUD_STATE."""
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state = json.load(open(os.environ["FAKE_GCLOUD_STATE"]))
exists = state.get("exists", {})


def answer(kind):
    if kind in exists:
        print(json.dumps(exists[kind]))
        sys.exit(0)
    print(f"ERROR: (gcloud) NOT_FOUND: {kind}", file=sys.stderr)
    sys.exit(1)


positional = [a for a in args if not a.startswith("-")]
if positional[:3] == ["resource-manager", "org-policies", "describe"]:
    if "policy" not in state:
        print("ERROR: (gcloud) PERMISSION_DENIED", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(state["policy"]))
elif positional[:3] == ["services", "list"] or positional[:2] == ["services", "list"]:
    for name in state.get("services", []):
        print(name)
elif positional[-1:] == ["get-iam-policy"] or "get-iam-policy" in positional:
    kind = {"storage": "bucket-iam", "kms": "key-iam"}.get(positional[0], "repo-iam")
    print(json.dumps(state.get(kind, {"bindings": []})))
elif "get-public-key" in positional:
    print("-----BEGIN PUBLIC KEY-----")
    print("fake")
    print("-----END PUBLIC KEY-----")
elif "describe" in positional:
    if positional[0] == "artifacts":
        answer("repo")
    elif positional[0] == "storage":
        answer("bucket")
    elif positional[:2] == ["kms", "keyrings"]:
        answer("keyring")
    elif positional[:2] == ["kms", "keys"]:
        answer("key")
    else:
        sys.exit(1)
sys.exit(0)
'''

LABELS = {"managed-by": "swarm-release"}
EXISTING = {
    "repo": {
        "name": f"projects/{PROJECT}/locations/us/repositories/swarmcloud",
        "format": "DOCKER",
        "labels": LABELS,
    },
    "bucket": {
        "name": "swarmcloud",
        "location": "US",
        "uniform_bucket_level_access": True,
        "versioning_enabled": True,
        "labels": LABELS,
    },
    "keyring": {"name": f"projects/{PROJECT}/locations/us/keyRings/swarmcloud-release"},
    "key": {
        "name": f"projects/{PROJECT}/locations/us/keyRings/swarmcloud-release/cryptoKeys/cosign",
        "purpose": "ASYMMETRIC_SIGN",
        "versionTemplate": {"algorithm": "EC_SIGN_P256_SHA256", "protectionLevel": "SOFTWARE"},
        "labels": LABELS,
    },
}
SERVICES = [
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "storage.googleapis.com",
]
#: The effective policy once the Q4 exception exists on the project.
EXCEPTION = {
    "constraint": "constraints/iam.allowedPolicyMemberDomains",
    "listPolicy": {"allValues": "ALLOW"},
}
#: The effective policy an org with Domain Restricted Sharing hands down.
RESTRICTED = {
    "constraint": "constraints/iam.allowedPolicyMemberDomains",
    "listPolicy": {"allowedValues": ["C0" + "abc123"]},
    "inheritFromParent": True,
}


def _public(role: str) -> dict:
    return {"bindings": [{"role": role, "members": [ALL_USERS]}]}


def _env(tmp_path: Path, state: dict) -> dict[str, str]:
    # A refusal test must not pass because there is no script to refuse.
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} does not exist"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    log = tmp_path / "gcloud.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDSDK_", "GOOGLE_"))}
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        FAKE_GCLOUD_STATE=str(state_file),
        FAKE_GCLOUD_LOG=str(log),
        HOME=str(tmp_path),
        NO_COLOR="1",
        # Which project the platform's own scripts point at must not matter.
        SWARM_ENV_FILE=str(tmp_path / "absent.env"),
    )
    return env


def _calls(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "gcloud.log").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _writes(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if any(v in c for v in WRITE_VERBS)]


def _run(tmp_path: Path, state: dict, *args: str, extra_env: dict | None = None,
         stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
    env = _env(tmp_path, state)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, stdin=stdin,
        capture_output=True, text=True, timeout=120, check=False,
    )


def _run_typed(tmp_path: Path, state: dict, typed: str, *args: str,
               extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run with a terminal on stdin and `typed` already typed into it."""
    env = _env(tmp_path, state)
    env.update(extra_env or {})
    master, slave = pty.openpty()
    try:
        os.write(master, (typed + "\n").encode())
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, stdin=slave,
            capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        os.close(slave)
        os.close(master)
    return proc


def _all_exist(**overrides) -> dict:
    state = {"exists": json.loads(json.dumps(EXISTING)), "services": SERVICES, "policy": EXCEPTION}
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Red-first tests named by the plan's entry
# ---------------------------------------------------------------------------


def test_dry_run_prints_every_call(tmp_path: Path) -> None:
    """From an empty project a dry run names every write it would make, in
    order, and makes none of them."""
    state = {"exists": {}, "services": [], "policy": EXCEPTION}
    proc = _run(tmp_path, state, "--dry-run", "--public")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    expected = [
        "gcloud services enable artifactregistry.googleapis.com cloudkms.googleapis.com storage.googleapis.com",
        "gcloud artifacts repositories create swarmcloud",
        "--repository-format=docker",
        "--location=us",
        f"gcloud storage buckets create {BUCKET}",
        "--uniform-bucket-level-access",
        f"gcloud storage buckets update {BUCKET}",
        "--versioning",
        "gcloud kms keyrings create swarmcloud-release",
        "gcloud kms keys create cosign",
        "--purpose=asymmetric-signing",
        "--default-algorithm=ec-sign-p256-sha256",
        "--labels=managed-by=swarm-release",
        f"gcloud storage buckets add-iam-policy-binding {BUCKET} --member=allUsers --role=roles/storage.objectViewer",
        "gcloud artifacts repositories add-iam-policy-binding swarmcloud --location=us "
        f"--project={PROJECT} --member=allUsers --role=roles/artifactregistry.reader",
    ]
    for piece in expected:
        assert piece in out, f"dry run did not print {piece!r}:\n{out}"
    order = [out.index(p) for p in (
        "services enable", "repositories create", "buckets create",
        "keyrings create", "keys create", "buckets add-iam-policy-binding",
        "repositories add-iam-policy-binding",
    )]
    assert order == sorted(order), out
    assert out.count("would run: gcloud") == 8, out
    assert _writes(_calls(tmp_path)) == [], "a dry run called a write"
    # Every call it did make names the release project explicitly.
    for call in _calls(tmp_path):
        if call[:1] != ["storage"]:
            assert f"--project={PROJECT}" in call, call


def test_public_grant_refused_without_exception(tmp_path: Path) -> None:
    """Domain Restricted Sharing in force: refused before ANY write, naming
    the exception the org admin has to make."""
    proc = _run(tmp_path, _all_exist(policy=RESTRICTED), "--public")
    assert proc.returncode != 0
    assert "iam.allowedPolicyMemberDomains" in proc.stderr
    assert "exception" in proc.stderr
    assert _writes(_calls(tmp_path)) == []
    assert any(c[:3] == ["resource-manager", "org-policies", "describe"] and "--effective" in c
               for c in _calls(tmp_path))


def test_public_grant_refused_when_policy_unreadable(tmp_path: Path) -> None:
    """An effective policy it cannot read is not an exception: fail closed."""
    state = _all_exist()
    del state["policy"]
    proc = _run(tmp_path, state, "--public")
    assert proc.returncode != 0
    assert _writes(_calls(tmp_path)) == []


@pytest.mark.parametrize("policy", [
    {"constraint": "constraints/iam.allowedPolicyMemberDomains", "listPolicy": {"allValues": "DENY"}},
    {"constraint": "constraints/iam.allowedPolicyMemberDomains", "listPolicy": {"deniedValues": ["x"]}},
])
def test_public_grant_refused_for_other_restrictions(tmp_path: Path, policy: dict) -> None:
    proc = _run(tmp_path, _all_exist(policy=policy), "--public")
    assert proc.returncode != 0
    assert _writes(_calls(tmp_path)) == []


def test_public_grant_needs_typed_confirmation(tmp_path: Path) -> None:
    """SWARM_ASSUME_YES does not skip it, and no terminal means no grant."""
    proc = _run(tmp_path, _all_exist(), "--public", extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode != 0, proc.stderr
    assert not [c for c in _calls(tmp_path) if "add-iam-policy-binding" in c]


def test_public_grant_refused_on_wrong_typed_answer(tmp_path: Path) -> None:
    proc = _run_typed(tmp_path, _all_exist(), "yes", "--public",
                      extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode != 0, proc.stderr
    assert not [c for c in _calls(tmp_path) if "add-iam-policy-binding" in c]


def test_public_grant_runs_after_typed_project_id(tmp_path: Path) -> None:
    proc = _run_typed(tmp_path, _all_exist(), PROJECT, "--public")
    assert proc.returncode == 0, proc.stderr
    grants = [c for c in _calls(tmp_path) if "add-iam-policy-binding" in c]
    assert len(grants) == 2, grants
    assert ["storage", "buckets", "add-iam-policy-binding", BUCKET,
            f"--member={ALL_USERS}", "--role=roles/storage.objectViewer"] == grants[0][:6]
    assert "--role=roles/artifactregistry.reader" in grants[1]
    assert f"--member={ALL_USERS}" in grants[1]
    assert f"Type {PROJECT} to continue" in proc.stderr


def test_refuses_shared_project(tmp_path: Path) -> None:
    """The shared project, or any name on the deny-list, is refused before
    gcloud is called at all."""
    for project in ("saga-agents-staging", "agents-staging", "saga-agents-files-staging"):
        proc = _run(tmp_path, _all_exist(), "--project", project, "--dry-run")
        assert proc.returncode != 0, project
        assert "refus" in proc.stderr.lower(), proc.stderr
        assert _calls(tmp_path) == [], f"gcloud was called for {project}"


def test_refuses_a_project_that_is_not_the_release_home(tmp_path: Path) -> None:
    proc = _run(tmp_path, _all_exist(), "--project", "someone-elses-project", "--dry-run")
    assert proc.returncode != 0
    assert _calls(tmp_path) == []


def test_alternate_release_project_is_admitted(tmp_path: Path) -> None:
    """D23: `swarmcloud-releases` if `swarmcloud` had been taken."""
    proc = _run(tmp_path, {"exists": {}, "services": []}, "--project", "swarmcloud-releases", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "--project=swarmcloud-releases" in proc.stdout


# ---------------------------------------------------------------------------
# --ci-publisher: the three release roles for publish-release.yml's identity
# ---------------------------------------------------------------------------

PUBLISHER = "swarm-deployer@a-ci-project.iam.gserviceaccount.com"
PUBLISHER_MEMBER = f"serviceAccount:{PUBLISHER}"
#: (resource kind, role) -- the whole of what --ci-publisher may grant.
PUBLISHER_GRANTS = {
    ("artifacts", "roles/artifactregistry.writer"),
    ("storage", "roles/storage.objectAdmin"),
    ("kms", "roles/cloudkms.signerVerifier"),
}


def _grants(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "add-iam-policy-binding" in c]


def _role(call: list[str]) -> str:
    return next(a.split("=", 1)[1] for a in call if a.startswith("--role="))


def _bound(role: str, member: str = PUBLISHER_MEMBER) -> dict:
    return {"bindings": [{"role": role, "members": [member]}]}


def test_ci_publisher_dry_run_shows_the_three_grants(tmp_path: Path) -> None:
    proc = _run(tmp_path, _all_exist(), "--ci-publisher", PUBLISHER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for piece in (
        "gcloud artifacts repositories add-iam-policy-binding swarmcloud --location=us "
        f"--project={PROJECT} --member={PUBLISHER_MEMBER} --role=roles/artifactregistry.writer",
        f"gcloud storage buckets add-iam-policy-binding {BUCKET} --member={PUBLISHER_MEMBER} "
        f"--role=roles/storage.objectAdmin --project={PROJECT}",
        "gcloud kms keys add-iam-policy-binding cosign --keyring=swarmcloud-release --location=us "
        f"--project={PROJECT} --member={PUBLISHER_MEMBER} --role=roles/cloudkms.signerVerifier",
    ):
        assert piece in out, f"dry run did not print {piece!r}:\n{out}"
    assert out.count("would run: gcloud") == 3, out
    assert _writes(_calls(tmp_path)) == [], "a dry run called a write"
    assert "to continue" not in proc.stderr


def test_ci_publisher_needs_typed_confirmation(tmp_path: Path) -> None:
    """SWARM_ASSUME_YES does not answer it, and no terminal means no grant."""
    proc = _run(tmp_path, _all_exist(), "--ci-publisher", PUBLISHER, extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode != 0, proc.stderr
    assert _grants(_calls(tmp_path)) == []


def test_ci_publisher_refused_on_wrong_typed_answer(tmp_path: Path) -> None:
    proc = _run_typed(tmp_path, _all_exist(), "yes", "--ci-publisher", PUBLISHER,
                      extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode != 0, proc.stderr
    assert _grants(_calls(tmp_path)) == []


def test_ci_publisher_grants_exactly_three_roles_after_typed_project_id(tmp_path: Path) -> None:
    proc = _run_typed(tmp_path, _all_exist(), PROJECT, "--ci-publisher", PUBLISHER)
    assert proc.returncode == 0, proc.stderr
    grants = _grants(_calls(tmp_path))
    assert {(c[0], _role(c)) for c in grants} == PUBLISHER_GRANTS, grants
    assert len(grants) == 3, grants
    for call in grants:
        assert f"--member={PUBLISHER_MEMBER}" in call, call
        assert f"--project={PROJECT}" in call, call
    # Each on its one resource; never the project.
    assert not [c for c in _calls(tmp_path) if c[:1] == ["projects"]]
    assert [c[:4] for c in grants if c[0] == "kms"] == [["kms", "keys", "add-iam-policy-binding", "cosign"]]
    assert "--keyring=swarmcloud-release" in next(c for c in grants if c[0] == "kms")
    # Every grant is printed before the project id is asked for.
    asked = proc.stderr.index(f"Type {PROJECT} to continue")
    for role in ("roles/artifactregistry.writer", "roles/storage.objectAdmin", "roles/cloudkms.signerVerifier"):
        assert -1 < proc.stderr.find(role) < asked, proc.stderr
    # --ci-publisher is not --public: nothing reaches allUsers.
    assert not [c for c in grants if f"--member={ALL_USERS}" in c]


def test_ci_publisher_second_run_grants_nothing(tmp_path: Path) -> None:
    state = _all_exist(**{
        "repo-iam": _bound("roles/artifactregistry.writer"),
        "bucket-iam": _bound("roles/storage.objectAdmin"),
        "key-iam": _bound("roles/cloudkms.signerVerifier"),
    })
    proc = _run(tmp_path, state, "--ci-publisher", PUBLISHER)
    assert proc.returncode == 0, proc.stderr
    assert _writes(_calls(tmp_path)) == []
    assert "to continue" not in proc.stderr


def test_ci_publisher_grants_only_what_is_missing(tmp_path: Path) -> None:
    """A role held by someone else, or another role held by the publisher, is
    not the grant: only the bucket's is in place here."""
    state = _all_exist(**{
        "repo-iam": _bound("roles/artifactregistry.writer", "serviceAccount:other@x.iam.gserviceaccount.com"),
        "bucket-iam": _bound("roles/storage.objectAdmin"),
        "key-iam": _bound("roles/cloudkms.viewer"),
    })
    proc = _run_typed(tmp_path, state, PROJECT, "--ci-publisher", PUBLISHER)
    assert proc.returncode == 0, proc.stderr
    grants = _grants(_calls(tmp_path))
    assert {(c[0], _role(c)) for c in grants} == {
        ("artifacts", "roles/artifactregistry.writer"), ("kms", "roles/cloudkms.signerVerifier")}, grants


@pytest.mark.parametrize("email", [
    "",
    "someone@example.com",
    "user:someone@example.com",
    "serviceAccount:swarm-deployer@a-ci-project.iam.gserviceaccount.com",
    "allUsers",
    "swarm-deployer@saga-agents-staging.iam.gserviceaccount.com",
])
def test_ci_publisher_refuses_anything_but_a_service_account(tmp_path: Path, email: str) -> None:
    """A person, a group, allUsers or the shared project's identity is never
    the publisher; refused before gcloud is called."""
    proc = _run(tmp_path, _all_exist(), "--ci-publisher", email, "--dry-run")
    assert proc.returncode != 0, proc.stdout
    assert _calls(tmp_path) == []


def test_ci_publisher_refuses_a_deny_listed_service_account(tmp_path: Path) -> None:
    text = COMMON.read_text()
    listed = sorted({w.strip('"\'') for w in text.split() if w.strip('"\'').endswith(".iam.gserviceaccount.com")})
    assert listed, "SHARED_DENY_LIST names no service account"
    proc = _run(tmp_path, _all_exist(), "--ci-publisher", listed[0], "--dry-run")
    assert proc.returncode != 0, proc.stdout
    assert _calls(tmp_path) == []


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    proc = subprocess.run(
        ["shellcheck", "-x", str(SCRIPT)], cwd=REPO / "scripts",
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Idempotency and shape
# ---------------------------------------------------------------------------


def test_second_run_writes_nothing(tmp_path: Path) -> None:
    """Everything in place, public read already granted: no write at all, and
    no confirmation asked for."""
    state = _all_exist(**{
        "bucket-iam": _public("roles/storage.objectViewer"),
        "repo-iam": _public("roles/artifactregistry.reader"),
    })
    proc = _run(tmp_path, state, "--public")
    assert proc.returncode == 0, proc.stderr
    assert _writes(_calls(tmp_path)) == []
    assert "to continue" not in proc.stderr


def test_run_without_public_grants_nothing(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"exists": {}, "services": []})
    assert proc.returncode == 0, proc.stderr
    writes = _writes(_calls(tmp_path))
    assert writes, "nothing was created"
    assert not [c for c in writes if "add-iam-policy-binding" in c]
    assert not [c for c in _calls(tmp_path) if "org-policies" in c]


def test_missing_label_and_versioning_are_converged(tmp_path: Path) -> None:
    state = _all_exist()
    state["exists"]["bucket"]["versioning_enabled"] = False
    state["exists"]["bucket"]["labels"] = {}
    state["exists"]["repo"]["labels"] = {}
    state["exists"]["key"]["labels"] = {}
    proc = _run(tmp_path, state)
    assert proc.returncode == 0, proc.stderr
    writes = _writes(_calls(tmp_path))
    assert not [c for c in writes if "create" in c], writes
    bucket = [c for c in writes if c[:3] == ["storage", "buckets", "update"]]
    assert bucket and "--versioning" in bucket[0]
    assert "--update-labels=managed-by=swarm-release" in bucket[0]
    assert [c for c in writes if c[:3] == ["artifacts", "repositories", "update"]
            and "--update-labels=managed-by=swarm-release" in c]
    assert [c for c in writes if c[:3] == ["kms", "keys", "update"]
            and "--update-labels=managed-by=swarm-release" in c]


@pytest.mark.parametrize("kind,field,value", [
    ("repo", "format", "MAVEN"),
    ("bucket", "location", "US-CENTRAL1"),
    ("key", "versionTemplate", {"algorithm": "RSA_SIGN_PSS_2048_SHA256"}),
    ("key", "purpose", "ENCRYPT_DECRYPT"),
])
def test_existing_resource_of_the_wrong_shape_is_refused(tmp_path: Path, kind: str, field: str, value) -> None:
    """Location, format and algorithm cannot be changed in place: say so
    rather than signing the release with the wrong key."""
    state = _all_exist()
    state["exists"][kind][field] = value
    proc = _run(tmp_path, state)
    assert proc.returncode != 0
    assert _writes(_calls(tmp_path)) == []


def test_prints_the_public_key(tmp_path: Path) -> None:
    """ENGINE-3 pins this key in the wheel (swarm_install/release_key.pub)."""
    proc = _run(tmp_path, _all_exist())
    assert proc.returncode == 0, proc.stderr
    assert "-----BEGIN PUBLIC KEY-----" in proc.stdout


def test_ignores_assume_yes_and_starts_strict() -> None:
    text = SCRIPT.read_text()
    lines = [ln for ln in text.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines[0] == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert 'source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"' in text
    assert "unset SWARM_ASSUME_YES" in text


def test_release_scripts_never_wait_on_a_gcloud_prompt():
    """A first run against a project with Artifact Registry not yet enabled hung
    ten minutes on gcloud's "enable it and retry? (y/N)" (2026-10-30). Both
    release scripts turn gcloud's prompts off before their first call."""
    for name in ("release-home.sh", "release/publish.sh"):
        text = (REPO / "scripts" / name).read_text()
        assert "export CLOUDSDK_CORE_DISABLE_PROMPTS=1" in text, name

