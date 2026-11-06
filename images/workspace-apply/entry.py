"""Step 0 of the personal-workspace job: the scrub (docs/workspaces.md §2.2).

The Cloud Run job `swarm-workspace-apply` runs this file as its fixed command,
`python3 -I /opt/swarm/entry.py --spec-verify-keys-sha256=<hex>
--spec-signing-key=<key>`, with
`<w-id> <mode>` as each execution's arguments, as swarm-workspace-deployer:
the identity with project-wide account-IAM power Google cannot narrow (§2.4).
Lane W6b of #847.

WHAT THIS STANDS BETWEEN. Anyone holding `run.jobs.runWithOverrides` on the
job may start an execution and choose its arguments, its environment
variables, its task count and its timeout. The scheduler, the acceptance
account and the release deployer hold that project-wide and unscopably
(§2.1), and none of them may steer the identity. The job's command is fixed in
the job spec, so the boundary is what THIS file lets an override reach:

  * THE ENVIRONMENT, NONE OF IT. Every variable handed in is discarded before
    anything else happens -- `os.environ` is emptied, so no library read below
    sees one either -- and step 1 is `execve`d with a FIXED environment written
    in this file. That is what keeps a caller from reaching the run through
    `PATH`, an exported bash function (`BASH_FUNC_*`), `BASH_ENV`,
    `SWARM_CALL_GUARD_ENFORCE`, `CLOUDSDK_*`, `http_proxy` or anything else.
    `-I` keeps Python itself from reading `PYTHON*`. What `-I` cannot stop is
    the dynamic loader reading `LD_*` before Python starts: §2.4 R6 states
    that residual risk and what would close it.
  * THE ARGUMENTS, EXACTLY TWO, after the command's own. `[<pin>, <key>,
    <w-id>, <mode>]`: the pin of the verify keys and the trusted signing key
    (below), which the command carries and no override can move, then the id
    `w-<6 hex>` and the mode `create` or `limits`, or the execution ends
    here, having run nothing. Step 1
    (scripts/workspace-apply.sh) checks the id and mode again; neither check
    alone is load-bearing.
  * THE TASK COUNT, ONE. `CLOUD_RUN_TASK_COUNT` must be `1` and this task must
    be index `0`. A1's claim would stop a second task anyway; this stops it
    before it reads anything.
  * THE TIMEOUT, OURS. Step 1 runs under coreutils `timeout` with this file's
    1800-second deadline, whatever timeout the execution was given.

What survives from the execution is two values. `CLOUD_RUN_EXECUTION`, checked
against register-tenant.sh's `WS_BUILD_RE` and passed on as `BUILD_ID`, which
the script uses for nothing but the record and the log. And `SPEC_VERIFY_KEYS`,
which step 0 reads itself and never passes on, and which counts only when its
SHA-256 is the one the fixed command carries (below). The project and the
region come from the metadata server, never from the environment, and the
metadata request goes direct: no proxy, whatever was set.

THE APPROVAL, VERIFIED BEFORE ANYTHING GRANTS (lane WK-SIGNED-APPROVAL of
#847; owner decision 2026-10-19, closing risk R8 of
docs/design/workspace-kinds.md §4.5). Tenant workers can write any document
of the database, so a record's `decision.by` alone proves nothing: a worker
could name the platform owner on a service record it wrote itself, and the
job would admit that account at the front door (S2). So swarm-api signs every
approval and retry with the step-spec KMS key -- the key only swarm-api may
sign with -- over a canonical payload, and this file checks that signature
for every `group` and `service` record before step 1 runs:

  * THE KEY IS THE JOB SPEC'S, NEVER THE CALLER'S. Which key is trusted is
    the fixed command's `--spec-signing-key=` (DAYONE-BOOT-1, part of #2688),
    which the bootstrap renders from the deployment's own step-spec key and
    which must be a `step-spec` key of the metadata server's project; no
    environment variable names one. The public key is the one
    `SPEC_VERIFY_KEYS` holds for the exact
    version the record names, which must be a version of that key (lane
    WK-APPROVAL-KEYS; owner decision 2026-10-19: no Cloud KMS grant for this
    identity, so nothing reads KMS at run time). `SPEC_VERIFY_KEYS` is the
    `{full version name: PEM}` map workers get, which the owner's bootstrap
    apply reads from Cloud KMS at plan and writes into the job spec
    (terraform/bootstrap/workspace_deployer.tf). An override CAN set an
    environment variable, so the value counts only when its SHA-256 is the
    `--spec-verify-keys-sha256=` the job's fixed command carries -- the
    command is the one part of an execution no override reaches. Any other
    value (or none) ends the execution here, having read nothing. Nothing on
    the record (a worker writes it) chooses the key.
  * A VERSION THE JOB WAS NOT GIVEN IS AN UNSIGNED APPROVAL. The keys refresh
    only when the bootstrap is applied, unlike workers', which refresh on
    every release; a version enabled since stops every group and service run
    at `APPROVAL_UNSIGNED` (fail closed) until it is
    (docs/runbooks/spec-signing-rollout.md, "Rotation").
  * A MISSING OR BAD SIGNATURE, OR A PAYLOAD THAT IS NOT THE RECORD, STOPS
    THE RUN FOR THE OWNER: `needs_owner` with failure code
    `APPROVAL_UNSIGNED` and a `front_door_audit` entry saying nothing was
    admitted, and step 1 never runs, so nothing is granted. Not retryable:
    swarm-api's retry re-signs only an approval that already verifies, so the
    remedy is a fresh decision -- deny, then approve the record as shown.
  * A CLOUD THAT DID NOT ANSWER (no token, Firestore down) is not a bad
    signature: the execution ends here having written nothing, as any refusal
    above does.
  * WHAT WAS VERIFIED IS PINNED. `SWARM_APPROVAL_PIN` in the fixed
    environment carries the record's workspace id, tenant id, kind and
    principal as read here, and whether they were verified. The call guard
    (scripts/lib/workspace-guard.sh `expect`) writes no expectation for a
    record that differs, so a record swapped after this check grants nothing.

A `personal` record is not verified: a person's steps never touch the front
door, and a personal approval written before signatures existed keeps working
(questions.json of the lane; new personal approvals are signed all the same,
so verifying them later is one entry in `VERIFIED_KINDS`).

The standard library, and the frozen contract's `swarm_common.specsign` for
the canonical bytes and their digest, as workers use it (swarm-api signs over
`specsign.jcs` too): this runs on the image's Debian python3, before anything
is installed or trusted, and both are stdlib-only. The P-256 ECDSA check below
is written out here rather than imported (workers verify with `cryptography`,
which the image does not carry), and checks public values only. Nothing here may
print a credential: the access token it holds is sent to Google's APIs and
nowhere else, and is never printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Where the image puts the frozen contract (images/workspace-apply/Dockerfile,
#: `COPY apps/common/swarm_common/`). `python3 -I` puts nothing of /opt/swarm
#: on sys.path, so this one root-owned directory goes first; elsewhere (the
#: unit tests) it does not exist and the installed package is found.
CONTRACT_PATH = Path("/opt/swarm/apps/common")
if str(CONTRACT_PATH) not in sys.path:
    sys.path.insert(0, str(CONTRACT_PATH))

from swarm_common import specsign  # noqa: E402  (after the path it is found on)

#: Where the image puts the code, read-only and root-owned
#: (images/workspace-apply/Dockerfile).
SWARM_ROOT = Path("/opt/swarm")
#: Steps 1 to 3: validate, install the guard, run register-tenant.sh.
APPLY_SCRIPT = SWARM_ROOT / "scripts" / "workspace-apply.sh"

#: Absolute, so nothing is looked up on a PATH before the fixed one exists.
BASH = "/bin/bash"
TIMEOUT = "/usr/bin/timeout"

#: The run's own deadline (§2.1: "a task timeout of 1800 seconds, as the build
#: had"). An override may lengthen the execution's timeout; it cannot lengthen
#: this. The grace after it is what `timeout` gives the script's process group
#: to exit on SIGTERM before SIGKILL.
DEADLINE_SECONDS = 1800
KILL_AFTER_SECONDS = 30

#: The image's own tool locations: Debian's, /usr/local/bin for kubectl and uv,
#: and the SDK's bin for anything the gke-gcloud-auth-plugin .deb put only
#: there. The Dockerfile's self-test resolves every tool the job needs on
#: exactly this value, so a tool that moves fails the image build.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/google-cloud-sdk/bin"
#: The real kubectl the guard runs after an allow (workspace-guard.sh).
KUBECTL = "/usr/local/bin/kubectl"
#: The Dockerfile removes gcloud's bundled interpreter and names this one, by
#: ENV; that ENV is discarded with the rest, so it is restated here as a fixed
#: value.
CLOUDSDK_PYTHON = "/usr/bin/python3"

#: §2.2 step 1. register-tenant.sh's WS_ID_RE, without its anchors.
WORKSPACE_RE = re.compile(r"w-[0-9a-f]{6}", re.ASCII)
MODES = ("create", "limits")
#: register-tenant.sh's WS_BUILD_RE, verbatim; a unit test holds the two equal.
BUILD_RE = "^[A-Za-z0-9._:-]{1,80}$"
#: A project id as Google defines it, and a region's last path segment.
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", re.ASCII)
REGION_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+", re.ASCII)

METADATA = "http://metadata.google.internal/computeMetadata/v1/"
METADATA_TIMEOUT_SECONDS = 5


#: The run's Firestore database, as scripts/lib/common.sh fixes it for the job
#: (FIRESTORE_DATABASE is not in the fixed environment, so its default holds).
FIRESTORE_DATABASE = "swarm"
FIRESTORE = "https://firestore.googleapis.com/v1/"
API_TIMEOUT_SECONDS = 15

#: The key whose signatures this job trusts: the fixed command's
#: `--spec-signing-key=`, which terraform/bootstrap/workspace_deployer.tf
#: renders from the step-spec key of the environment the job serves
#: (terraform/modules/spec_signing_key: `<name_prefix>-<environment>-specs` /
#: `step-spec`; dev, one key per environment being the point of that module).
#: Only swarm-api holds `signer` on it (terraform/bootstrap/spec_signing.tf,
#: swarm_api_signer). It is a command element and never an environment
#: variable, because an execution override can set any variable but cannot
#: touch the command; and it must be a key of the metadata server's project,
#: so a job spec rendered for another project runs nothing. A key name of any
#: other shape ends the execution before anything runs.
SIGNING_KEY_RE = re.compile(
    r"--spec-signing-key=(projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/locations/[a-z0-9-]+"
    r"/keyRings/[A-Za-z0-9_-]+/cryptoKeys/step-spec)", re.ASCII)
#: The public keys of the step-spec key's ENABLED versions, `{full version
#: name: PEM}`, as the bootstrap writes them into the job spec
#: (terraform/bootstrap/workspace_deployer.tf), and the fixed command's last
#: element, which carries the SHA-256 of exactly that value. P-256
#: (terraform/bootstrap/spec_signing.tf version_template), as p256_verify reads.
VERIFY_KEYS_ENV = "SPEC_VERIFY_KEYS"
VERIFY_KEYS_PIN_RE = re.compile(r"--spec-verify-keys-sha256=([0-9a-f]{64})", re.ASCII)

#: The kinds whose approval is verified: the two whose steps grant beyond a
#: person's own (a group's team slot and label, a service's front door).
VERIFIED_KINDS = ("group", "service")
KINDS = ("personal", "group", "service")

#: swarm-api's canonical approval (swarm_api.workspaces.canonical_approval):
#: the purpose, inside the signed bytes, keeps an approval from ever being
#: read as a step spec, or a step spec as an approval. A unit test holds this
#: file's bytes equal to swarm-api's.
APPROVAL_PURPOSE = "swarm.workspace-approval/v1"
APPROVAL_FIELDS = ("workspace_id", "tenant_id", "kind", "principal",
                   "decided_by", "decided_at", "request_id")
#: The failure code a refused approval records (docs/workspaces.md §4.3).
APPROVAL_UNSIGNED = "APPROVAL_UNSIGNED"
#: The name the verified record is pinned under in the fixed environment.
APPROVAL_PIN = "SWARM_APPROVAL_PIN"


class Refused(Exception):
    """The execution ends here, having run nothing."""


class Unsigned(Exception):
    """The record's approval is not swarm-api's: stop for the owner."""


# --- P-256 ECDSA, verification only (FIPS 186-4, SEC 1) ----------------------
# Public inputs only -- a public key, a digest, a signature -- so nothing here
# needs to run in constant time.
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)
#: DER of the two object identifiers an EC P-256 SubjectPublicKeyInfo names.
_OID_EC_PUBLIC_KEY = bytes.fromhex("06072a8648ce3d0201")
_OID_P256 = bytes.fromhex("06082a8648ce3d030107")


def _on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        slope = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _multiply(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _der_read(data: bytes, offset: int, tag: int) -> tuple[bytes, int]:
    """One DER element of TAG at OFFSET: (its contents, the offset after it)."""
    if offset + 2 > len(data) or data[offset] != tag:
        raise ValueError("unexpected DER element")
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count not in (1, 2) or offset + count > len(data):
            raise ValueError("bad DER length")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise ValueError("DER element runs past its container")
    return data[offset:offset + length], offset + length


def p256_public_key(pem: str) -> tuple[int, int]:
    """The point of a PEM SubjectPublicKeyInfo that names EC P-256, or ValueError."""
    lines = [line.strip() for line in pem.strip().splitlines()]
    if len(lines) < 3 or lines[0] != "-----BEGIN PUBLIC KEY-----" or lines[-1] != "-----END PUBLIC KEY-----":
        raise ValueError("not a PEM public key")
    der = base64.b64decode("".join(lines[1:-1]), validate=True)
    info, end = _der_read(der, 0, 0x30)
    if end != len(der):
        raise ValueError("trailing bytes after the public key")
    algorithm, offset = _der_read(info, 0, 0x30)
    if algorithm != _OID_EC_PUBLIC_KEY + _OID_P256:
        raise ValueError("the public key is not EC P-256")
    bits, offset = _der_read(info, offset, 0x03)
    if offset != len(info) or len(bits) != 66 or bits[0] != 0 or bits[1] != 0x04:
        raise ValueError("the public key is not an uncompressed P-256 point")
    point = (int.from_bytes(bits[2:34], "big"), int.from_bytes(bits[34:66], "big"))
    if not _on_curve(point):
        raise ValueError("the public key is not on P-256")
    return point


def p256_verify(public_key: tuple[int, int], digest: bytes, signature: bytes) -> bool:
    """True when SIGNATURE (DER ECDSA) is PUBLIC_KEY's over the SHA-256 DIGEST."""
    if len(digest) != 32 or not _on_curve(public_key):
        return False
    try:
        body, end = _der_read(signature, 0, 0x30)
        r_bytes, offset = _der_read(body, 0, 0x02)
        s_bytes, offset = _der_read(body, offset, 0x02)
    except ValueError:
        return False
    if end != len(signature) or offset != len(body):
        return False
    r, s = int.from_bytes(r_bytes, "big"), int.from_bytes(s_bytes, "big")
    if not (0 < r < _N and 0 < s < _N):
        return False
    w = pow(s, -1, _N)
    e = int.from_bytes(digest, "big")
    point = _add(_multiply(e * w % _N, _G), _multiply(r * w % _N, public_key))
    return point is not None and point[0] % _N == r


# --- the approval -----------------------------------------------------------

def canonical_approval(fields: Mapping[str, Any]) -> bytes:
    """The bytes swarm-api signs: RFC 8785 of {purpose, APPROVAL_FIELDS}, by
    the frozen contract's own encoder (`specsign.jcs`, as swarm-api's
    `canonical_approval` calls it).

    A value that is not a non-empty string has no canonical form here, and the
    approval is refused rather than encoded some other way; a lone surrogate
    is `specsign.SpecNotCanonical`, a ValueError.
    """
    payload = {"purpose": APPROVAL_PURPOSE}
    for key in APPROVAL_FIELDS:
        value = fields.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"the approval's {key} is not a non-empty string")
        payload[key] = value
    return specsign.jcs(payload)


def firestore_value(value: Mapping[str, Any]) -> Any:
    """A Firestore REST value as Python: maps, arrays and scalars as JSON has them."""
    if "mapValue" in value:
        return {k: firestore_value(v) for k, v in (value["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in value:
        return [firestore_value(v) for v in (value["arrayValue"].get("values") or [])]
    if "integerValue" in value:
        return int(value["integerValue"])
    for key in ("stringValue", "booleanValue", "doubleValue", "timestampValue", "referenceValue"):
        if key in value:
            return value[key]
    return None


def kind_of(record: Mapping[str, Any]) -> str:
    """A record with no kind is personal (§4.1); anything else is kept as read."""
    kind = record.get("kind")
    return "personal" if kind is None else str(kind)


def pin_of(record: Mapping[str, Any], *, verified: bool) -> str:
    """The value of SWARM_APPROVAL_PIN: the record's identity, as read here."""
    return json.dumps({
        "workspace_id": str(record.get("workspace_id") or ""),
        "tenant_id": str(record.get("tenant_id") or ""),
        "kind": kind_of(record),
        "principal": str(record.get("principal") or ""),
        "verified": verified,
    }, sort_keys=True, separators=(",", ":"))


def approval_payload(record: Mapping[str, Any]) -> tuple[dict[str, str], str, bytes]:
    """(payload, key version, DER signature) from a record, or Unsigned naming why.

    Every signed field that is also the record's is taken FROM THE RECORD, so a
    payload that does not match it is a signature that does not verify. The
    two that are the signature's own -- who decided and when -- must still be
    the record's: `decided_by` is the record's `decision.by` (an approval) or
    `retry.by` (a retry), and the signed request id is the record's own.
    """
    approval = record.get("approval")
    if not isinstance(approval, Mapping):
        raise Unsigned("the record carries no approval signature")
    decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else {}
    retry = record.get("retry") if isinstance(record.get("retry"), Mapping) else {}
    decided_by = str(approval.get("decided_by") or "")
    deciders = {str(who).strip().lower() for who in (decision.get("by"), retry.get("by")) if who}
    if not decided_by or decided_by not in deciders:
        raise Unsigned("the signed approver is not the record's approver")
    if approval.get("request_id") != record.get("request_id"):
        raise Unsigned("the signature is for another request than the record's")
    payload = {
        "workspace_id": record.get("workspace_id"),
        "tenant_id": record.get("tenant_id"),
        "kind": kind_of(record),
        "principal": record.get("principal"),
        "decided_by": decided_by,
        "decided_at": approval.get("decided_at"),
        "request_id": record.get("request_id"),
    }
    for key, value in payload.items():
        if not isinstance(value, str) or not value:
            raise Unsigned(f"the record's {key} is not a string the approval can cover")
    key_version = approval.get("key_version")
    signature = approval.get("signature")
    if not isinstance(key_version, str) or not isinstance(signature, str) or not signature:
        raise Unsigned("the approval names no key version or no signature")
    try:
        der = base64.b64decode(signature, validate=True)
    except ValueError:
        raise Unsigned("the approval's signature is not base64") from None
    return payload, key_version, der


def trusted_version(signing_key: str, key_version: str) -> bool:
    """True when KEY_VERSION is a version of SIGNING_KEY, the command's key."""
    return re.fullmatch(re.escape(signing_key) + r"/cryptoKeyVersions/[1-9][0-9]{0,5}",
                        key_version, re.ASCII) is not None


def parse_verify_keys(raw: str) -> dict[str, str]:
    """SPEC_VERIFY_KEYS -> {full version name: PEM}, the shape workers parse
    (agent_worker.specverify.parse_verify_keys); anything else is ValueError."""
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("SPEC_VERIFY_KEYS is not a JSON object of version name -> PEM")
    return dict(value)


def verify_keys_pin(raw: str) -> str:
    """The fixed command's element that pins RAW: its SHA-256, as Terraform's
    `sha256()` spells it over the same string."""
    return "--spec-verify-keys-sha256=" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_approval(record: Mapping[str, Any], *, signing_key: str,
                    verify_keys: Mapping[str, str]) -> None:
    """Returns when the record's approval is swarm-api's; raises Unsigned when not.

    `signing_key` is the fixed command's `--spec-signing-key=`, the one key
    whose versions count. `verify_keys` is the job spec's SPEC_VERIFY_KEYS, already held to the
    command's pin: the PEM for each version this job may verify with. A
    version absent from it -- one enabled since the bootstrap was last
    applied -- is refused like a bad signature, and so is a PEM that is not a
    P-256 public key: nothing here asks anyone else for one.
    """
    payload, key_version, der = approval_payload(record)
    if not trusted_version(signing_key, key_version):
        raise Unsigned("the approval names a key this job does not trust")
    pem = verify_keys.get(key_version)
    if pem is None:
        raise Unsigned("the approval names a key version this job was not given")
    try:
        canonical = canonical_approval(payload)
    except (ValueError, UnicodeEncodeError):
        raise Unsigned("the approval has no canonical form") from None
    try:
        point = p256_public_key(pem)
    except ValueError:
        raise Unsigned("the job's key for that version is not a P-256 public key") from None
    if not p256_verify(point, specsign.spec_digest(canonical), der):
        raise Unsigned("the approval's signature does not verify over the record")


class Cloud:
    """Firestore, which the approval check reads and writes, as the job's own
    identity: the token from the metadata server, every request direct (no
    proxy, whatever was set). Every failure is an OSError. No Cloud KMS: the
    public keys are the job spec's (SPEC_VERIFY_KEYS)."""

    def __init__(self, project: str) -> None:
        self.project = project
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._token = ""

    def _open(self, request: urllib.request.Request, limit: int) -> bytes:
        with self._opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
            return response.read(limit)

    def token(self) -> str:
        if not self._token:
            request = urllib.request.Request(
                METADATA + "instance/service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"})
            try:
                self._token = str(json.loads(self._open(request, 65536))["access_token"])
            except (KeyError, TypeError, ValueError):
                raise OSError("the metadata server returned no access token") from None
        return self._token

    def call(self, method: str, url: str, body: Mapping[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", " ".join(("Bearer", self.token())))
        try:
            return json.loads(self._open(request, 4 * 1024 * 1024) or b"null")
        except ValueError:
            raise OSError(f"{method} answered with something that is not JSON") from None

    def documents(self) -> str:
        return f"{FIRESTORE}projects/{self.project}/databases/{FIRESTORE_DATABASE}/documents"

    def record(self, workspace_id: str) -> tuple[str, str, dict[str, Any]]:
        """(document name, update time, the record) of the one record naming the id."""
        query = {"structuredQuery": {
            "from": [{"collectionId": "workspaces"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "workspace_id"}, "op": "EQUAL",
                                      "value": {"stringValue": workspace_id}}},
            "limit": 2}}
        rows = self.call("POST", self.documents() + ":runQuery", query)
        docs = [row["document"] for row in rows or [] if isinstance(row, dict) and row.get("document")]
        if len(docs) != 1:
            raise Refused(f"{len(docs)} workspace records name {workspace_id}, not one")
        doc = docs[0]
        record = {k: firestore_value(v) for k, v in (doc.get("fields") or {}).items()}
        return str(doc.get("name") or ""), str(doc.get("updateTime") or ""), record

    def commit(self, write: Mapping[str, Any]) -> None:
        self.call("POST", self.documents() + ":commit", {"writes": [write]})


def unsigned_write(name: str, update_time: str, record: Mapping[str, Any], *,
                   execution: str, mode: str, reason: str, at: str) -> dict[str, Any]:
    """The one write a refused approval makes: `needs_owner`, the failure, and
    a `front_door_audit` entry saying nothing was admitted -- conditional on
    the record being the one read, so a change since is never overwritten.

    `reason` is one of this file's own fixed sentences, never record text.
    """
    def text(value: str) -> dict[str, str]:
        return {"stringValue": value}

    entry = {"mapValue": {"fields": {
        "admitted": {"booleanValue": False},
        "code": text(APPROVAL_UNSIGNED),
        "reason": text(reason),
        "kind": text(kind_of(record)),
        "request_id": text(str(record.get("request_id") or "")),
        "execution": text(execution),
        "mode": text(mode),
        "at": {"timestampValue": at},
    }}}
    return {
        "update": {"name": name, "fields": {
            "state": text("needs_owner"),
            "failure": {"mapValue": {"fields": {
                "step": text("A1"),
                "code": text(APPROVAL_UNSIGNED),
                # A retry re-signs only a verified approval (People.retry).
                "retryable": {"booleanValue": False},
                "at": {"timestampValue": at},
            }}},
        }},
        "updateMask": {"fieldPaths": ["state", "failure"]},
        "updateTransforms": [{"fieldPath": "front_door_audit",
                              "appendMissingElements": {"values": [entry]}}],
        "currentDocument": {"updateTime": update_time},
    }


def approval_gate(workspace_id: str, mode: str, signing_key: str, execution: str, cloud: Any,
                  verify_keys: Mapping[str, str]) -> str | None:
    """The pin for step 1's environment, or None when the run stopped for the owner.

    Raises Refused when the cloud did not answer or the record is not one.
    """
    try:
        name, update_time, record = cloud.record(workspace_id)
        if record.get("workspace_id") != workspace_id:
            raise Refused("the record found does not name this workspace")
        kind = kind_of(record)
        decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else {}
        # Unapproved, or of a kind this build does not make: A1 refuses it
        # without a write, as before. Personal: see the module docstring.
        if kind not in VERIFIED_KINDS or decision.get("verdict") != "approved":
            return pin_of(record, verified=False)
        try:
            verify_approval(record, signing_key=signing_key, verify_keys=verify_keys)
        except Unsigned as why:
            at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            cloud.commit(unsigned_write(name, update_time, record, execution=execution,
                                        mode=mode, reason=str(why), at=at))
            print(f"workspace {workspace_id}: {APPROVAL_UNSIGNED}: {why}; stopped for the "
                  "owner (needs_owner), nothing granted", file=sys.stderr, flush=True)
            return None
        return pin_of(record, verified=True)
    except OSError as exc:
        raise Refused(f"the approval could not be checked ({type(exc).__name__}); "
                      "nothing was written") from None


def read_metadata(path: str) -> str:
    """One value from the metadata server, with no proxy whatever was set."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(METADATA + path, headers={"Metadata-Flavor": "Google"})
    with opener.open(request, timeout=METADATA_TIMEOUT_SECONDS) as response:
        return response.read(256).decode("ascii", "replace").strip()


def plan(
    argv: list[str],
    environ: MutableMapping[str, str],
    *,
    metadata: Callable[[str], str] = read_metadata,
    scratch_root: str = "/tmp",
    target: Path = APPLY_SCRIPT,
) -> tuple[str, list[str], dict[str, str], dict[str, str], str]:
    """The program, its arguments, its environment, the verify keys and the
    trusted signing key, or Refused.

    `environ` is EMPTIED first, whatever happens after: given `os.environ`, that
    is this process's own environment, so nothing later in it reads a value a
    caller chose. ARGV is the command's pin and signing key, then the
    execution's two.
    """
    execution = environ.get("CLOUD_RUN_EXECUTION")
    task_count = environ.get("CLOUD_RUN_TASK_COUNT")
    task_index = environ.get("CLOUD_RUN_TASK_INDEX")
    raw_keys = environ.get(VERIFY_KEYS_ENV)
    environ.clear()

    if len(argv) != 4:
        raise Refused("the job takes exactly two arguments, <w-id> <mode>, after its command's "
                      f"verify-key pin and signing key; it was given {len(argv) - 2}")
    keys_pin, signing_arg, workspace_id, mode = argv
    if not VERIFY_KEYS_PIN_RE.fullmatch(keys_pin):
        raise Refused("the first argument is not the command's --spec-verify-keys-sha256 pin")
    signing = SIGNING_KEY_RE.fullmatch(signing_arg)
    if signing is None:
        raise Refused("the second argument is not the command's --spec-signing-key, a step-spec key")
    signing_key, signing_project = signing.group(1), signing.group(2)
    if not WORKSPACE_RE.fullmatch(workspace_id):
        raise Refused("the first argument is not a workspace id of the form w-<6 hex digits>")
    if mode not in MODES:
        raise Refused("the second argument is neither create nor limits")
    if task_count != "1":
        raise Refused("the execution has a task count other than one")
    if task_index != "0":
        raise Refused("this is not the execution's first task")
    if execution is None or not re.fullmatch(BUILD_RE, execution):
        raise Refused("CLOUD_RUN_EXECUTION is missing or is not an execution name")
    # The job spec's keys, or an override's: only the command can tell. Public
    # values, so a plain comparison.
    if raw_keys is None or verify_keys_pin(raw_keys) != keys_pin:
        raise Refused(f"{VERIFY_KEYS_ENV} is missing or is not the one the job's command pins")
    try:
        verify_keys = parse_verify_keys(raw_keys)
    except ValueError:
        raise Refused(f"{VERIFY_KEYS_ENV} is not a JSON object of version name -> PEM") from None

    try:
        project = metadata("project/project-id")
        region = metadata("instance/region").rsplit("/", 1)[-1]
    except OSError as exc:
        raise Refused(f"the metadata server did not answer ({type(exc).__name__})") from None
    if not PROJECT_RE.fullmatch(project):
        raise Refused("the metadata server's project id is not one")
    if not REGION_RE.fullmatch(region):
        raise Refused("the metadata server's region is not one")
    if signing_project != project:
        raise Refused("the command's signing key is not a key of the job's own project")

    # Private to this run, in the execution's in-memory filesystem and outside
    # /opt/swarm: gcloud's config, the job's kubeconfig and the guard's
    # expectation file (§2.2 A1) all live under it.
    work = Path(tempfile.mkdtemp(prefix="swarm-workspace-apply.", dir=scratch_root))
    home = work / "home"
    guard = work / "guard"
    home.mkdir(mode=0o700)
    guard.mkdir(mode=0o700)

    env = {
        "PATH": SAFE_PATH,
        "HOME": str(home),
        "PROJECT_ID": project,
        "REGION": region,
        # load_env (scripts/lib/common.sh) sources this if it exists. It never
        # does: nothing writes to it.
        "SWARM_ENV_FILE": str(work / "no-env-file"),
        "SWARM_CALL_GUARD": str(guard / "expect.json"),
        # A report-only request is ignored while this is set (workspace-guard.sh):
        # the deployer identity never runs unguarded.
        "SWARM_CALL_GUARD_ENFORCE": "1",
        "SWARM_KUBECTL": KUBECTL,
        "CLOUDSDK_PYTHON": CLOUDSDK_PYTHON,
        "BUILD_ID": execution,
        "NO_COLOR": "1",
    }
    args = [
        TIMEOUT,
        f"--kill-after={KILL_AFTER_SECONDS}s",
        f"{DEADLINE_SECONDS}s",
        BASH,
        str(target),
        workspace_id,
        mode,
    ]
    return TIMEOUT, args, env, verify_keys, signing_key


def main(
    argv: list[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
    *,
    metadata: Callable[[str], str] = read_metadata,
    scratch_root: str = "/tmp",
    target: Path = APPLY_SCRIPT,
    execve: Callable[[str, list[str], dict[str, str]], object] = os.execve,
    cloud: Callable[[str], Any] = Cloud,
) -> int:
    try:
        program, args, env, verify_keys, signing_key = plan(
            sys.argv[1:] if argv is None else argv,
            os.environ if environ is None else environ,
            metadata=metadata,
            scratch_root=scratch_root,
            target=target,
        )
        # The approval, before step 1: nothing past here grants for a group
        # or service record whose approval swarm-api did not sign.
        pin = approval_gate(args[-2], args[-1], signing_key, env["BUILD_ID"],
                            cloud(env["PROJECT_ID"]), verify_keys)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr, flush=True)
        return 1
    if pin is None:
        # Stopped for the owner and recorded: "ended, not failed", as
        # scripts/workspace-apply.sh reads register-tenant.sh's needs_owner.
        return 0
    env[APPROVAL_PIN] = pin
    # The workspace id and the execution only (§2.6): both are opaque.
    print(f"workspace {args[-2]}: mode {args[-1]}, execution {env['BUILD_ID']}", flush=True)
    execve(program, args, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
