"""The workspace job verifies swarm-api's signature on an approval before anything grants.

Lane WK-SIGNED-APPROVAL of #847; owner decision 2026-10-19, closing risk R8 of
docs/design/workspace-kinds.md §4.5. Tenant workers can write any document of
the database, so a worker can write `workspaces/{id}` with `decision.by` set
to the platform owner. swarm-api now signs every approval and retry with the
step-spec KMS key (`swarm_api.workspaces.sign_approval`), and
images/workspace-apply/entry.py checks that signature for every group and
service record before step 1 -- before S2, the front-door admit, and before
the group and service steps.

Lane WK-APPROVAL-KEYS (owner decision 2026-10-19: no new IAM for approval
verification): the job verifies against the public keys in its own
environment, `SPEC_VERIFY_KEYS` as the bootstrap renders it, never against
Cloud KMS at run time, and that value counts only when its SHA-256 is the one
the job's fixed command carries -- an override may set an environment
variable, never the command. What these tests hold, against the REAL entry.py
and the REAL swarm-api signing code:

* an approval swarm-api signed verifies in the job, which then runs step 1
  with the verified record pinned in SWARM_APPROVAL_PIN;
* a record edited after signing -- `decision.by`, the principal, the kind, the
  tenant, the request id -- is refused APPROVAL_UNSIGNED: the record is moved
  to `needs_owner` with a `front_door_audit` entry, and step 1 never runs, so
  nothing is granted;
* a group or service record with no signature, a signature by any other key,
  a key version the image does not trust, or a version absent from
  SPEC_VERIFY_KEYS is refused the same way;
* no Cloud KMS client is built or called: the fakes fail if one is;
* a SPEC_VERIFY_KEYS an override replaced, or none at all, runs nothing and
  writes nothing, whatever the record;
* a personal record with no signature keeps working (the lane's
  questions.json recommendation: personal steps never touch the front door);
* a cloud that does not answer writes nothing and runs nothing;
* the call guard writes no expectation for a record that differs from the pin.

The signing key is made here with `cryptography`; the job verifies with its own
standard-library P-256 code, so these tests are also that code's check against
an independent implementation. No cloud, no metadata server, no credentials.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

# swarm_api is imported inside the functions that use it, never at module top:
# test_collection_stays_cheap.py holds every module in this directory to that.

REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / "images" / "workspace-apply" / "entry.py"
GUARD = REPO / "scripts" / "lib" / "workspace-guard.sh"
CASES = json.loads((REPO / "scripts" / "lib" / "workspace-guard-cases.json").read_text())
SPEC_KEY_MODULE = REPO / "terraform" / "modules" / "spec_signing_key" / "main.tf"

WORKSPACE = "w-3f9a2c"
EXECUTION = "swarm-workspace-apply-x7k2p"
PROJECT = "fixture-project-1"
REGION = "us-central1"
PIN_PREFIX = "--spec-verify-keys-sha256="
SIGNING_PREFIX = "--spec-signing-key="
KEY = f"projects/{PROJECT}/locations/{REGION}/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
VERSION = KEY + "/cryptoKeyVersions/1"
OWNER = "owner@example.com"
SERVICE = f"reports-bot@{PROJECT}.iam.gserviceaccount.com"
DOC_NAME = f"projects/{PROJECT}/databases/swarm/documents/workspaces/u-re-75c2e5"


def _load():
    spec = importlib.util.spec_from_file_location("workspace_apply_entry_approval", ENTRY)
    assert spec and spec.loader, ENTRY
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def entry():
    return _load()


class KmsKey:
    """The step-spec key as Cloud KMS holds it: signs digests; its PEM is what
    the bootstrap renders into the job's SPEC_VERIFY_KEYS."""

    def __init__(self, version: str = VERSION) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.version = version

    def sign(self, digest: bytes) -> Any:
        from swarm_api.specsigning import SpecSignature

        der = self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return SpecSignature(signature=der, key_version=self.version)

    @property
    def pem(self) -> str:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _record(kind: str = "service", principal: str = SERVICE, tenant: str = "u-re-75c2e5") -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "workspace_id": WORKSPACE,
        "kind": kind,
        "principal": principal,
        "state": "approved",
        "request_id": "5d0c6a3e-1b7f-4c55-9a51-0f1c2b3d4e5f",
        "decision": {"by": OWNER, "at": datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc),
                     "verdict": "approved", "reason": None, "auto": False},
        "retry": None,
        "failure": None,
    }


def _signed(key: KmsKey, record: dict[str, Any]) -> dict[str, Any]:
    """The record as swarm-api's approval writes it: the decision, then the signature."""
    from swarm_api import workspaces as ws

    out = copy.deepcopy(record)
    out["approval"] = ws.sign_approval(key, out, decided_by=out["decision"]["by"],
                                       decided_at=out["decision"]["at"],
                                       request_id=out["request_id"])
    return out


def _rest(value: Any) -> dict[str, Any]:
    """A Python value as Firestore's REST API returns it."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, datetime):
        return {"timestampValue": value.isoformat().replace("+00:00", "Z")}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: _rest(v) for k, v in value.items()}}}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_rest(v) for v in value]}}
    raise TypeError(type(value))


class FakeCloud:
    """Firestore as the job's identity reaches it: the REAL `Cloud.record` /
    `commit` code over a fake transport. Cloud KMS is not reachable from it:
    a request to it, or a `public_key` lookup, fails the test."""

    def __init__(self, entry, records: list[dict[str, Any]], *, down: bool = False) -> None:
        self.cloud = entry.Cloud(PROJECT)
        self.cloud.call = self.call  # the transport, nothing else
        # A KMS client the job must never build or ask: it fails if called.
        self.cloud.public_key = self.kms
        self.records = records
        self.down = down
        self.calls: list[tuple[str, str]] = []
        self.commits: list[dict[str, Any]] = []

    def __call__(self, project: str):
        assert project == PROJECT
        return self.cloud

    def kms(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the job asked Cloud KMS for a public key; it must read SPEC_VERIFY_KEYS")

    def call(self, method: str, url: str, body: Any = None) -> Any:
        self.calls.append((method, url))
        assert "cloudkms" not in url, f"the job called Cloud KMS: {method} {url}"
        if self.down:
            raise OSError("the cloud did not answer")
        if url.endswith("/documents:runQuery"):
            assert body["structuredQuery"]["where"]["fieldFilter"]["value"] == {"stringValue": WORKSPACE}
            return [{"readTime": "2026-10-19T10:00:00Z"}] + [
                {"document": {"name": DOC_NAME, "updateTime": "2026-10-19T09:31:00.000001Z",
                              "fields": {k: _rest(v) for k, v in r.items()}}} for r in self.records]
        if url.endswith("/documents:commit"):
            self.commits.append(body)
            return {"writeResults": [{}]}
        raise AssertionError(f"unexpected call {method} {url}")


class Run:
    def __init__(self) -> None:
        self.execs: list[tuple[str, list[str], dict[str, str]]] = []

    def execve(self, program: str, args: list[str], env: dict[str, str]) -> None:
        self.execs.append((program, args, env))


def _keys(*keys: KmsKey) -> str:
    """SPEC_VERIFY_KEYS as the bootstrap renders it: {full version name: PEM}."""
    return json.dumps({key.version: key.pem for key in keys})


def _pin(raw: str) -> str:
    """The job command's last element: the SHA-256 of the SPEC_VERIFY_KEYS it sets."""
    return PIN_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _main(entry, tmp_path: Path, cloud: FakeCloud, keys: str, mode: str = "create", *,
          pinned: str | None = None, environ: dict[str, str] | None = None,
          signing_key: str = KEY) -> tuple[int, Run]:
    """One execution: the command's pin of PINNED (default KEYS) and its
    SIGNING_KEY (DAYONE-BOOT-1), then the two arguments; SPEC_VERIFY_KEYS=KEYS
    in its environment."""
    run = Run()
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    if environ is None:
        environ = {"CLOUD_RUN_EXECUTION": EXECUTION, "CLOUD_RUN_TASK_COUNT": "1",
                   "CLOUD_RUN_TASK_INDEX": "0", "SPEC_VERIFY_KEYS": keys}
    answers = {"project/project-id": PROJECT, "instance/region": f"projects/1/regions/{REGION}"}
    rc = entry.main([_pin(keys if pinned is None else pinned), SIGNING_PREFIX + signing_key, WORKSPACE, mode],
                    environ,
                    metadata=answers.__getitem__, scratch_root=str(scratch),
                    target=Path("/opt/swarm/scripts/workspace-apply.sh"), execve=run.execve, cloud=cloud)
    return rc, run


def _refused_unsigned(rc: int, run: Run, cloud: FakeCloud) -> dict[str, Any]:
    """Stopped for the owner: no step ran, one conditional write recorded why."""
    assert rc == 0, "a needs_owner stop is 'ended, not failed', as workspace-apply.sh reads it"
    assert run.execs == [], "the approval did not verify, but step 1 ran: something could be granted"
    [commit] = cloud.commits
    [write] = commit["writes"]
    assert write["update"]["name"] == DOC_NAME
    assert write["currentDocument"] == {"updateTime": "2026-10-19T09:31:00.000001Z"}
    assert sorted(write["updateMask"]["fieldPaths"]) == ["failure", "state"]
    fields = write["update"]["fields"]
    assert fields["state"] == {"stringValue": "needs_owner"}
    failure = fields["failure"]["mapValue"]["fields"]
    assert failure["code"] == {"stringValue": "APPROVAL_UNSIGNED"}
    # A retry will not re-sign it (People.retry): the remedy is deny, then approve.
    assert failure["retryable"] == {"booleanValue": False}
    [transform] = write["updateTransforms"]
    assert transform["fieldPath"] == "front_door_audit"
    [audit] = transform["appendMissingElements"]["values"]
    audit = audit["mapValue"]["fields"]
    assert audit["admitted"] == {"booleanValue": False}
    assert audit["code"] == {"stringValue": "APPROVAL_UNSIGNED"}
    assert audit["execution"] == {"stringValue": EXECUTION}
    return audit


# ---------------------------------------------------------------------------
# An approval swarm-api signed verifies, and step 1 runs with it pinned.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,principal,tenant", [
    ("service", SERVICE, "u-re-75c2e5"),
    ("group", "research@example.com", "research"),
])
def test_an_approval_signed_by_swarm_api_verifies_in_the_job(entry, tmp_path, kind, principal, tenant):
    key = KmsKey()
    record = _signed(key, _record(kind, principal, tenant))
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    assert rc == 0 and cloud.commits == []
    [(_, args, env)] = run.execs
    assert args[-2:] == [WORKSPACE, "create"]
    assert json.loads(env["SWARM_APPROVAL_PIN"]) == {
        "workspace_id": WORKSPACE, "tenant_id": tenant, "kind": kind, "principal": principal,
        "verified": True}
    # The keys were the job's own; nothing but Firestore was asked.
    assert all(url.startswith("https://firestore.googleapis.com/") for _, url in cloud.calls)
    assert "SPEC_VERIFY_KEYS" not in env, "the keys are step 0's, not the child's"


def test_a_version_among_several_in_spec_verify_keys_verifies(entry, tmp_path):
    """Mid-rotation the job holds versions 1 and 2; swarm-api signs with 2."""
    old, new = KmsKey(), KmsKey(version=KEY + "/cryptoKeyVersions/2")
    cloud = FakeCloud(entry, [_signed(new, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(old, new))
    assert rc == 0 and len(run.execs) == 1 and cloud.commits == []


def test_a_retry_signed_by_swarm_api_verifies_in_the_job(entry, tmp_path):
    """A retry runs a new request id, signed by whoever retried it."""
    from swarm_api import workspaces as ws

    key = KmsKey()
    record = _record()
    record.update(state="approved", request_id="0b9e1f7a-3c2d-4e5f-8a9b-7c6d5e4f3a2b",
                  retry={"by": OWNER, "at": datetime(2026, 10, 19, 11, 0, tzinfo=timezone.utc),
                         "from_state": "failed"})
    record["approval"] = ws.sign_approval(key, record, decided_by=OWNER,
                                          decided_at=record["retry"]["at"],
                                          request_id=record["request_id"])
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    assert rc == 0 and len(run.execs) == 1 and cloud.commits == []


# ---------------------------------------------------------------------------
# A record edited after signing is refused, and nothing is granted.
# ---------------------------------------------------------------------------
def _edit_decided_by(r: dict) -> None:
    r["decision"]["by"] = "mallory@example.com"
    r["approval"]["decided_by"] = "mallory@example.com"


EDITS = [
    pytest.param(_edit_decided_by, id="decision.by-and-its-copy"),
    pytest.param(lambda r: r["decision"].update(by="mallory@example.com"), id="decision.by"),
    pytest.param(lambda r: r.update(principal=f"intruder@{PROJECT}.iam.gserviceaccount.com"), id="principal"),
    pytest.param(lambda r: r.update(kind="group"), id="kind"),
    pytest.param(lambda r: r.update(tenant_id="u-in-0a1b2c"), id="tenant"),
    pytest.param(lambda r: r.update(request_id="ffffffff-0000-4000-8000-000000000000"), id="request-id"),
    pytest.param(lambda r: r["approval"].update(request_id="ffffffff-0000-4000-8000-000000000000"),
                 id="signed-request-id"),
    pytest.param(lambda r: r["approval"].update(decided_at="2026-10-19T09:30:01+00:00"), id="decided-at"),
]


@pytest.mark.parametrize("edit", EDITS)
def test_a_record_edited_after_signing_is_refused_and_nothing_is_granted(entry, tmp_path, edit):
    key = KmsKey()
    record = _signed(key, _record())
    edit(record)
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    _refused_unsigned(rc, run, cloud)


def test_the_control_the_unedited_record_runs(entry, tmp_path):
    """The positive control for every edit above: the same record, unedited."""
    key = KmsKey()
    cloud = FakeCloud(entry, [_signed(key, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    assert rc == 0 and len(run.execs) == 1 and cloud.commits == []


@pytest.mark.parametrize("kind,principal,tenant", [
    ("service", SERVICE, "u-re-75c2e5"),
    ("group", "research@example.com", "research"),
])
def test_a_group_or_service_record_with_no_signature_is_refused(entry, tmp_path, kind, principal, tenant):
    """What a worker can write: the decision naming the owner, and no signature."""
    cloud = FakeCloud(entry, [_record(kind, principal, tenant)])
    rc, run = _main(entry, tmp_path, cloud, _keys(KmsKey()))
    _refused_unsigned(rc, run, cloud)


@pytest.mark.parametrize("mode", ["create", "limits"])
def test_every_mode_is_checked(entry, tmp_path, mode):
    cloud = FakeCloud(entry, [_record()])
    rc, run = _main(entry, tmp_path, cloud, _keys(KmsKey()), mode=mode)
    _refused_unsigned(rc, run, cloud)


def test_a_signature_by_any_key_but_the_trusted_one_is_refused(entry, tmp_path):
    """A worker can make its own P-256 key and name the trusted key's version:
    the job takes the public key from its own SPEC_VERIFY_KEYS, never from
    the record."""
    trusted, forger = KmsKey(), KmsKey()
    record = _signed(forger, _record())
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(trusted))
    _refused_unsigned(rc, run, cloud)


@pytest.mark.parametrize("held", [
    pytest.param(("1",), id="the-job-holds-only-version-1"),
    pytest.param((), id="the-job-holds-no-version"),
])
def test_a_version_absent_from_spec_verify_keys_is_refused_and_nothing_is_granted(entry, tmp_path, held):
    """swarm-api signed with version 2, a real version of the trusted key, but
    the bootstrap has not been re-applied since it was enabled: the job does
    not hold it, so the record stops for the owner (fail closed) -- the
    rotation runbook's re-apply step is what prevents this."""
    signer = KmsKey(version=KEY + "/cryptoKeyVersions/2")
    others = [KmsKey(version=KEY + "/cryptoKeyVersions/" + n) for n in held]
    cloud = FakeCloud(entry, [_signed(signer, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(*others))
    audit = _refused_unsigned(rc, run, cloud)
    assert audit["reason"] == {"stringValue": "the approval names a key version this job was not given"}


def test_the_control_the_same_version_held_verifies(entry, tmp_path):
    """The positive control for the refusal above: the job holds version 2."""
    signer = KmsKey(version=KEY + "/cryptoKeyVersions/2")
    cloud = FakeCloud(entry, [_signed(signer, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(KmsKey(), signer))
    assert rc == 0 and len(run.execs) == 1 and cloud.commits == []


@pytest.mark.parametrize("version", [
    pytest.param(KEY.replace("swarm-dev-specs", "swarm-prod-specs") + "/cryptoKeyVersions/1", id="prod-key"),
    pytest.param(f"projects/{PROJECT}/locations/{REGION}/keyRings/mine/cryptoKeys/k/cryptoKeyVersions/1",
                 id="a-worker-made-key"),
    pytest.param(KEY + "/cryptoKeyVersions/0", id="version-zero"),
    pytest.param(KEY + "/cryptoKeyVersions/1/../../x", id="path"),
    pytest.param(KEY, id="no-version"),
])
def test_a_key_version_the_image_does_not_trust_is_refused_even_when_held(entry, tmp_path, version):
    """The command's signing key still bounds which key's versions count: a
    version of another key is refused even if SPEC_VERIFY_KEYS carried it."""
    key = KmsKey(version=version)
    cloud = FakeCloud(entry, [_signed(key, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    _refused_unsigned(rc, run, cloud)


def test_a_held_key_that_is_not_p256_stops_for_the_owner(entry, tmp_path):
    """A malformed PEM in the job's keys verifies nothing: fail closed."""
    key = KmsKey()
    other = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cloud = FakeCloud(entry, [_signed(key, _record())])
    rc, run = _main(entry, tmp_path, cloud, json.dumps({VERSION: other}))
    _refused_unsigned(rc, run, cloud)


# ---------------------------------------------------------------------------
# The keys are the job spec's: an override cannot choose them.
# ---------------------------------------------------------------------------
def test_spec_verify_keys_an_override_replaced_runs_nothing_and_writes_nothing(entry, tmp_path):
    """Anyone holding runWithOverrides may set SPEC_VERIFY_KEYS on an
    execution -- here to a forger's key, over a record the forger signed. Its
    SHA-256 is not the one the fixed command carries, so the execution ends
    before it reads the record."""
    trusted, forger = KmsKey(), KmsKey()
    cloud = FakeCloud(entry, [_signed(forger, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(forger), pinned=_keys(trusted))
    assert rc == 1 and run.execs == [] and cloud.commits == [] and cloud.calls == []


def test_no_spec_verify_keys_runs_nothing(entry, tmp_path):
    key = KmsKey()
    cloud = FakeCloud(entry, [_signed(key, _record())])
    environ = {"CLOUD_RUN_EXECUTION": EXECUTION, "CLOUD_RUN_TASK_COUNT": "1", "CLOUD_RUN_TASK_INDEX": "0"}
    rc, run = _main(entry, tmp_path, cloud, _keys(key), environ=environ)
    assert rc == 1 and run.execs == [] and cloud.commits == [] and cloud.calls == []


@pytest.mark.parametrize("raw", ["not json", "[]", '{"v": 1}', '"x"'])
def test_spec_verify_keys_that_are_not_a_map_of_pems_run_nothing(entry, tmp_path, raw):
    """Pinned or not, a value of the wrong shape trusts nothing and runs nothing."""
    cloud = FakeCloud(entry, [_record()])
    rc, run = _main(entry, tmp_path, cloud, raw)
    assert rc == 1 and run.execs == [] and cloud.commits == [] and cloud.calls == []


def test_no_cloud_kms_client_is_left_in_the_job():
    """Nothing reads Cloud KMS at run time, so the deployer needs no grant on
    the key (owner decision 2026-10-19): no endpoint, no lookup."""
    module = _load()
    text = ENTRY.read_text()
    assert "cloudkms" not in text and "publicKey" not in text
    assert not hasattr(module, "KMS")
    assert not hasattr(module.Cloud, "public_key")


def test_a_cloud_that_does_not_answer_writes_nothing_and_runs_nothing(entry, tmp_path):
    key = KmsKey()
    cloud = FakeCloud(entry, [_signed(key, _record())], down=True)
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    assert rc == 1 and run.execs == [] and cloud.commits == []


@pytest.mark.parametrize("count", [0, 2])
def test_no_single_record_runs_nothing(entry, tmp_path, count):
    key = KmsKey()
    cloud = FakeCloud(entry, [_signed(key, _record())] * count)
    rc, run = _main(entry, tmp_path, cloud, _keys(key))
    assert rc == 1 and run.execs == [] and cloud.commits == []


def test_a_key_the_command_does_not_name_is_not_trusted(entry, tmp_path):
    """The command names prod's ring: a version of dev's key, held in
    SPEC_VERIFY_KEYS and signing correctly, still stops for the owner."""
    key = KmsKey()
    cloud = FakeCloud(entry, [_signed(key, _record())])
    rc, run = _main(entry, tmp_path, cloud, _keys(key),
                    signing_key=KEY.replace("swarm-dev-specs", "swarm-prod-specs"))
    _refused_unsigned(rc, run, cloud)


def test_no_kms_grant_in_terraform_names_the_workspace_deployer():
    """Every Cloud KMS IAM resource in terraform/, by type, not by name: a
    grant added under a new resource name is found too. The deployer verifies
    with SPEC_VERIFY_KEYS and holds nothing on any key."""
    blocks = []
    for path in sorted((REPO / "terraform").rglob("*.tf")):
        text = path.read_text()
        for found in re.finditer(r'^resource "(google_kms_[a-z_]*iam[a-z_]*)" "([^"]+)" \{\n(.*?)^\}', text,
                                 re.M | re.S):
            blocks.append((path, found.group(1), found.group(2), found.group(3)))
    assert blocks, "the control: terraform/ grants on the step-spec key, so this looked at something"
    for path, kind, name, body in blocks:
        assert "workspace_deployer" not in body and "swarm-workspace-deployer" not in body, (
            f"{path.relative_to(REPO)}: {kind}.{name} names the workspace deployer")


# ---------------------------------------------------------------------------
# Personal records: not verified (questions.json), and pinned all the same.
# ---------------------------------------------------------------------------
def test_a_personal_record_with_no_signature_keeps_working(entry, tmp_path):
    record = _record("personal", "alice@example.com", "u-alice")
    record["decision"]["by"] = "admin@example.com"
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(KmsKey()))
    assert rc == 0 and cloud.commits == []
    [(_, _, env)] = run.execs
    assert json.loads(env["SWARM_APPROVAL_PIN"]) == {
        "workspace_id": WORKSPACE, "tenant_id": "u-alice", "kind": "personal",
        "principal": "alice@example.com", "verified": False}


def test_an_unapproved_record_is_left_to_a1_and_pinned_unverified(entry, tmp_path):
    """A1 refuses it without a write, as before; the job must not move a
    record nobody approved to needs_owner on a caller's say-so."""
    record = _record()
    record["decision"] = None
    record["state"] = "requested"
    cloud = FakeCloud(entry, [record])
    rc, run = _main(entry, tmp_path, cloud, _keys(KmsKey()))
    assert rc == 0 and cloud.commits == []
    [(_, _, env)] = run.execs
    assert json.loads(env["SWARM_APPROVAL_PIN"])["verified"] is False


# ---------------------------------------------------------------------------
# The two halves agree, and the job's ECDSA is right.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("principal", [
    "plain@example.com", "quote\"and\\back@example.com", "tab\tnew\nline@x", "ünï©ødé@example.com", "😀@example.com",
    "  @x", "\x00\x1f\x7f@x",
])
def test_the_job_signs_over_the_same_bytes_as_swarm_api(entry, principal):
    from swarm_api import workspaces as ws

    fields = {"workspace_id": WORKSPACE, "tenant_id": "u-x", "kind": "service", "principal": principal,
              "decided_by": OWNER, "decided_at": "2026-10-19T09:30:00+00:00", "request_id": "r-1"}
    assert entry.canonical_approval(fields) == ws.canonical_approval(fields)
    assert entry.APPROVAL_PURPOSE == ws.APPROVAL_PURPOSE and entry.APPROVAL_FIELDS == ws.APPROVAL_FIELDS


def test_the_approval_bytes_can_never_be_a_step_spec():
    from swarm_api import workspaces as ws
    from swarm_common import specsign

    fields = {k: "x" for k in ws.APPROVAL_FIELDS}
    canonical = json.loads(ws.canonical_approval(fields))
    assert canonical["purpose"] == ws.APPROVAL_PURPOSE != specsign.SPEC_PURPOSE


def test_the_job_verifies_exactly_what_cryptography_signs(entry):
    key = KmsKey()
    point = entry.p256_public_key(key.pem)
    for i in range(8):
        digest = hashlib.sha256(f"message {i}".encode()).digest()
        der = key.sign(digest).signature
        assert entry.p256_verify(point, digest, der)
        assert not entry.p256_verify(point, hashlib.sha256(b"other").digest(), der)
        r, s = utils.decode_dss_signature(der)
        assert not entry.p256_verify(point, digest, utils.encode_dss_signature(r, (s + 1) % entry._N))
        assert not entry.p256_verify(point, digest, utils.encode_dss_signature(0, s))
        assert not entry.p256_verify(point, digest, utils.encode_dss_signature(r, 0))
        assert not entry.p256_verify(point, digest, der + b"\x00")
    assert not entry.p256_verify(entry.p256_public_key(KmsKey().pem), digest, der)


def test_a_public_key_that_is_not_p256_is_refused(entry):
    other = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with pytest.raises(ValueError):
        entry.p256_public_key(other)
    with pytest.raises(ValueError):
        entry.p256_public_key("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----")


def test_the_trusted_key_is_the_one_terraform_names():
    """`<name_prefix>-<environment>-specs` / `step-spec`, in dev, the
    environment the job runs as: the job's command names the key of the
    module its verify keys come from (DAYONE-BOOT-1), and the image names no
    project's key of its own."""
    module = _load()
    text = SPEC_KEY_MODULE.read_text()
    assert 'key_ring_name   = "${var.name_prefix}-${var.environment}-specs"' in text
    assert 'crypto_key_name = "step-spec"' in text
    deployer = (REPO / "terraform" / "bootstrap" / "workspace_deployer.tf").read_text()
    assert ("workspace_spec_signing_key = var.enable_workspace_deployer ? "
            "module.workspace_spec_keys[0].crypto_key_id : \"\"") in deployer
    assert module.SIGNING_KEY_RE.fullmatch(SIGNING_PREFIX + KEY)
    assert not hasattr(module, "SIGNING_KEYS"), "the image trusts a key of its own again"


def test_entry_imports_the_standard_library_and_the_frozen_specsign_only():
    """The canonical bytes and the digest are the frozen contract's, as the
    workers' are (swarm_common.specsign.jcs, spec_digest): one spelling of
    RFC 8785 for signer and verifier. Nothing else outside the standard
    library: the image's Debian python3 has nothing installed."""
    text = ENTRY.read_text()
    imported = set(re.findall(r"^(?:from|import) ([a-z0-9_.]+)", text, re.M))
    assert {name.split(".")[0] for name in imported} <= {
        "__future__", "base64", "hashlib", "json", "os", "re", "sys", "tempfile", "urllib",
        "collections", "datetime", "pathlib", "typing", "swarm_common"}
    assert re.search(r"^from swarm_common import specsign\b", text, re.M)
    assert "specsign.jcs(" in text and "specsign.spec_digest(" in text
    assert "json.dumps(payload" not in text, "a second RFC 8785 spelling beside the frozen one"


def test_the_image_carries_the_frozen_package_where_entry_looks():
    """`python3 -I` puts nothing of /opt/swarm on sys.path; entry.py adds the
    one directory the Dockerfile copies the frozen package into."""
    dockerfile = (REPO / "images" / "workspace-apply" / "Dockerfile").read_text()
    assert "COPY apps/common/swarm_common/ /opt/swarm/apps/common/swarm_common/" in dockerfile
    assert str(_load().CONTRACT_PATH) == "/opt/swarm/apps/common"


# ---------------------------------------------------------------------------
# The guard writes no expectation for a record that is not the pinned one.
# ---------------------------------------------------------------------------
def _guard_env(tmp: Path, pin: dict | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SWARM_", "CLOUDSDK_", "GOOGLE_")) and k != "KUBECONFIG"}
    env.update(SWARM_ENV_FILE=str(tmp / "no.env"), PROJECT_ID="saga-agents-staging", REGION="us-central1",
               ENVIRONMENT="dev", FIRESTORE_DATABASE="swarm", GKE_CLUSTER="swarm-autopilot",
               GKE_LOCATION="us-central1", SWARM_CALL_GUARD=str(tmp / "expect.json"), NO_COLOR="1")
    env.pop("ARTIFACT_BUCKET", None)
    if pin is not None:
        env["SWARM_APPROVAL_PIN"] = json.dumps(pin)
    return env


def _expect(tmp: Path, record: dict, pin: dict | None) -> subprocess.CompletedProcess[str]:
    env = _guard_env(tmp, pin)
    init = subprocess.run([str(GUARD), "init", "--workspace-id", WORKSPACE], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert init.returncode == 0, init.stderr
    path = tmp / "record.json"
    path.write_text(json.dumps(record))
    return subprocess.run([str(GUARD), "expect", "--record", str(path)], env=env,
                          capture_output=True, text=True, timeout=120, check=False)


def _service_pin(**change: Any) -> dict[str, Any]:
    fields = CASES["kind_records"]["service"]["fields"]
    pin = {"workspace_id": fields["workspace_id"]["stringValue"], "tenant_id": fields["tenant_id"]["stringValue"],
           "kind": "service", "principal": fields["principal"]["stringValue"], "verified": True}
    pin.update(change)
    return pin


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


@needs_jq
def test_the_guard_expects_a_service_record_that_is_the_verified_pin(tmp_path):
    done = _expect(tmp_path, CASES["kind_records"]["service"], _service_pin())
    assert done.returncode == 0, done.stderr
    assert json.loads((tmp_path / "expect.json").read_text())["service_principal"] == _service_pin()["principal"]


@needs_jq
@pytest.mark.parametrize("change", [
    pytest.param({"principal": "intruder@saga-agents-staging.iam.gserviceaccount.com"}, id="principal"),
    pytest.param({"tenant_id": "u-in-0a1b2c"}, id="tenant"),
    pytest.param({"kind": "group"}, id="kind"),
    pytest.param({"workspace_id": "w-000000"}, id="workspace"),
    pytest.param({"verified": False}, id="unverified"),
])
def test_the_guard_refuses_a_record_that_is_not_the_verified_pin(tmp_path, change):
    done = _expect(tmp_path, CASES["kind_records"]["service"], _service_pin(**change))
    assert done.returncode != 0
    assert "APPROVAL_UNSIGNED" in done.stderr
    assert "service_principal" not in json.loads((tmp_path / "expect.json").read_text())


@needs_jq
def test_the_guard_refuses_an_unreadable_pin(tmp_path):
    env_pin = "not json"
    env = _guard_env(tmp_path, None)
    env["SWARM_APPROVAL_PIN"] = env_pin
    subprocess.run([str(GUARD), "init", "--workspace-id", WORKSPACE], env=env, check=True,
                   capture_output=True, timeout=120)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(CASES["kind_records"]["service"]))
    done = subprocess.run([str(GUARD), "expect", "--record", str(path)], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode != 0 and "APPROVAL_UNSIGNED" in done.stderr


@needs_jq
def test_the_guard_takes_a_personal_record_pinned_unverified(tmp_path):
    fields = CASES["record"]["fields"]
    pin = {"workspace_id": WORKSPACE, "tenant_id": fields["tenant_id"]["stringValue"], "kind": "personal",
           "principal": fields["principal"]["stringValue"], "verified": False}
    done = _expect(tmp_path, CASES["record"], pin)
    assert done.returncode == 0, done.stderr



def test_the_pin_carries_identity_fields_only(entry):
    """The pin is what the guard compares, and nothing else: no signature, no approver."""
    pin = json.loads(entry.pin_of(_record(), verified=True))
    assert set(pin) == {"workspace_id", "tenant_id", "kind", "principal", "verified"}


def test_the_cloud_sends_the_metadata_token_to_google_and_reads_json(entry):
    """The real transport: the token from the metadata server, sent as a
    bearer header on the API request and never anywhere else."""
    token = secrets.token_hex(16)
    seen: list = []

    def opened(request, limit):
        seen.append(request)
        if request.full_url.startswith(entry.METADATA):
            assert request.get_header("Metadata-flavor") == "Google"
            assert request.get_header("Authorization") is None
            return json.dumps({"access_token": token}).encode()
        return b'{"ok": true}'

    cloud = entry.Cloud(PROJECT)
    cloud._open = opened
    assert cloud.call("POST", cloud.documents() + ":runQuery", {"q": 1}) == {"ok": True}
    assert cloud.call("POST", cloud.documents() + ":commit", {"writes": []}) == {"ok": True}
    metadata, first, second = seen
    assert metadata.full_url == entry.METADATA + "instance/service-accounts/default/token"
    for request in (first, second):
        assert request.get_header("Authorization") == "Bearer " + token
    assert first.full_url == f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/swarm/documents:runQuery"
    assert json.loads(first.data) == {"q": 1}
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in cloud._opener.handlers)
