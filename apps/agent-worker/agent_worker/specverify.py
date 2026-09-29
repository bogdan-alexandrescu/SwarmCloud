"""The worker runs only a spec swarm-api signed (contract request 34, #342).

WHY. A tenant's agents can write any task document of their tenant
(docs/multi-tenancy.md, "The Firestore row, in full"): the worker's service
account holds its Firestore role on the whole database, and an agent can mint
that account's token. So an implement step could rewrite a parked review
step's prompt, its `input_from`, its dispatch role or its repository, and the
review worker would run the rewrite with no prompt injection involved.
swarm-api now signs each step's canonical spec (`swarm_common.specsign`) with
a Cloud KMS key only its own account may use, and this module verifies that
signature over the document the worker fetched, before anything reads the
spec for what to do.

THE CHECKS, IN THIS ORDER, REFUSING AT THE FIRST FAILURE (section 5):

  1. `spec_signature` and `spec_key_version` are present -- or the legacy rule
     applies, and the task runs with checks 2 to 5 skipped. FIRST, because an
     unsigned legacy task has no `spec_format` either: the format check ahead
     of it refused every legacy task as `unknown_format`.
  2. `spec_format` is a format this worker knows.
  3. `spec_key_version` is `<SPEC_SIGNING_KEY>/cryptoKeyVersions/<digits>` and
     a key of `SPEC_VERIFY_KEYS` -- checked AS A STRING, before any key is
     used: the document can name any version of any key anywhere, and the
     worker never goes looking for one.
  4. the canonical form of the document verifies against the signature with
     that version's public key. A value with no canonical form (the document
     is tenant-writable, so it never went through swarm-api's check) is the
     refusal `not_canonical`, never a crash of this process.
  5. the execution's own environment agrees with the signed spec.

WHAT CHECK 5 PROVES, PER BACKEND -- NOT EQUALLY. On Cloud Run, CLOUD_RUN_JOB
is set by the PLATFORM, so comparing it with the Job the scheduler derives
from `tenant_id`, `runner_profile` and `resource_class` catches a dispatch
onto the wrong Job independently of the scheduler's arithmetic. On GKE there
is no platform-injected value: RUNNER_JOB_NAME and the Job's real
`metadata.name` come from ONE render by `GkeJobDispatcher._manifest`, so the
comparison only proves the dispatcher agrees with itself within that render.
It cannot catch a dispatcher that derives the wrong Job consistently. Closing
that needs the Downward API (`metadata.name` into the environment), which is
not done here; a wrong-Job dispatch on GKE still fails RUNNER_PROFILE's
comparison or the tenant-namespace boundary.

OUTAGES ARE NOT REFUSALS. Verification makes no network call: the public keys
are the Job's environment on Cloud Run and a read-only ConfigMap mount on GKE,
both rendered by Terraform. A worker with NO keys at all is misconfigured,
which is CANNOT_START (`ConfigError`), never a tenant's attack.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from swarm_common import specsign

from .errors import ConfigError, SpecSignatureInvalid

if TYPE_CHECKING:  # pragma: no cover
    from .config import WorkerConfig

#: THE END OF THE LEGACY WINDOW, IN THE CODE (decision 5). After it the worker
#: ignores SPEC_SIGNATURE_MODE=legacy, says it did, and enforces: a flag nobody
#: remembers to turn off stops working anyway.
SPEC_LEGACY_UNTIL = datetime(2026, 10, 20, tzinfo=timezone.utc)

#: The formats of the canonical form this worker can verify.
KNOWN_FORMATS = frozenset({specsign.SPEC_FORMAT})

#: Where a GKE pod finds `swarm-spec-verify-keys`, mounted read-only by
#: `GkeJobDispatcher._manifest` and kubernetes/worker-templates. Each key of
#: the ConfigMap is a file here. Read only when the environment has no keys.
VERIFY_KEYS_MOUNT = Path("/etc/swarm/spec-verify-keys")

MODES = ("enforce", "legacy")

_VERSION_SUFFIX = re.compile(r"/cryptoKeyVersions/[0-9]+")


@dataclass(frozen=True)
class SpecCheck:
    """What the check concluded, for the log and the task's event."""

    reason: str  # "verified" or "legacy_unsigned"
    task_id: str
    key_version: str | None = None
    digest: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "task_id": self.task_id,
            "key_version": self.key_version,
            "digest": self.digest,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def parse_verify_keys(raw: str) -> dict[str, str]:
    """SPEC_VERIFY_KEYS -> {full version name: PEM}. Malformed is ConfigError."""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"SPEC_VERIFY_KEYS is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigError("SPEC_VERIFY_KEYS must be a JSON object of version name -> PEM")
    return dict(value)


def parse_mode(raw: str) -> str:
    """SPEC_SIGNATURE_MODE; unset is `enforce`, the default (decision 5)."""
    mode = raw.strip().lower() or "enforce"
    if mode not in MODES:
        raise ConfigError(f"SPEC_SIGNATURE_MODE must be one of {MODES}, got {raw!r}")
    return mode


def parse_cutover(raw: str) -> datetime | None:
    """SPEC_LEGACY_CUTOVER, an RFC 3339 time WITH a zone; unset is None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"SPEC_LEGACY_CUTOVER is not an RFC 3339 time: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigError(f"SPEC_LEGACY_CUTOVER must carry a zone: {raw!r}")
    return value


def read_mount(mount: Path | None = None) -> tuple[str, str]:
    """(SPEC_VERIFY_KEYS, SPEC_SIGNING_KEY) from the GKE mount, or empty strings."""
    mount = mount or VERIFY_KEYS_MOUNT
    values = []
    for name in ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY"):
        try:
            values.append((mount / name).read_text())
        except OSError:
            values.append("")
    return values[0], values[1]


# ---------------------------------------------------------------------------
# The Job an execution should be running in
# ---------------------------------------------------------------------------

_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


def _sanitize_name(*parts: str, max_length: int = 63) -> str:
    """`scheduler.dispatch.sanitize_name`, restated.

    The worker image does not carry the scheduler (images/ ship apps/common
    and the service's own package only), so the rule is stated again here and
    held to the scheduler's by
    tests/unit/worker/test_spec_signature_worker.py
    (`test_the_workers_job_name_is_the_schedulers`).
    """
    import hashlib

    joined = "-".join(p for p in parts if p)
    slug = _NAME_SAFE.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    if len(slug) > max_length:
        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_length - 9].rstrip("-") + "-" + digest
    if not slug[0].isalpha():
        slug = "s" + slug[: max_length - 1]
    return slug


def cloud_run_job_id(tenant_id: str, profile_name: str, resource_class: str | None) -> str:
    """`scheduler.dispatch.job_id_for`, restated (see `_sanitize_name`)."""
    from swarm_common.profiles import RUNNER_PROFILES

    profile = RUNNER_PROFILES.get(profile_name)
    default_class = profile.resource_class if profile else None
    if resource_class and resource_class != default_class:
        return _sanitize_name("swarm", "job", tenant_id, profile_name, resource_class)
    return _sanitize_name("swarm", "job", tenant_id, profile_name)


def gke_job_name(task_id: str, generation: int) -> str:
    """The name `GkeJobDispatcher._manifest` gives a GKE Job, restated."""
    return _sanitize_name("swarm", task_id.replace("task_", ""), str(generation))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _legacy_admits(cfg: "WorkerConfig", create_time: Any, now: datetime, log: Any) -> bool:
    if cfg.spec_signature_mode != "legacy":
        return False
    if now >= SPEC_LEGACY_UNTIL:
        if log is not None:
            log.warning(
                "SPEC_SIGNATURE_MODE=legacy ignored: past SPEC_LEGACY_UNTIL, enforcing",
                spec_legacy_until=SPEC_LEGACY_UNTIL.isoformat(),
            )
        return False
    cutover = cfg.spec_legacy_cutover
    if cutover is None or not isinstance(create_time, datetime):
        return False
    created = create_time if create_time.tzinfo else create_time.replace(tzinfo=timezone.utc)
    return created < cutover


def _verify_signature(pem: str, signature_b64: Any, digest: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    if not isinstance(signature_b64, str):
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigError(f"a SPEC_VERIFY_KEYS entry is not a PEM public key: {exc}") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ConfigError("a SPEC_VERIFY_KEYS entry is not an EC public key")
    try:
        key.verify(signature, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except InvalidSignature:
        return False
    return True


def _environment_agrees(doc: Mapping[str, Any], cfg: "WorkerConfig") -> str | None:
    """Check 5. The name of the first disagreement, or None."""
    if cfg.runner_profile != doc.get("runner_profile"):
        return "RUNNER_PROFILE"
    if cfg.tenant_id != doc.get("tenant_id"):
        return "TENANT_ID"
    if cfg.task_timeout_env is not None and cfg.task_timeout_env != doc.get("timeout_seconds"):
        return "TASK_TIMEOUT_SECONDS"
    if cfg.repository_url is not None and cfg.repository_url != doc.get("repository_url"):
        return "REPOSITORY_URL"
    if cfg.repository_ref is not None and cfg.repository_ref != doc.get("repository_ref"):
        return "REPOSITORY_REF"
    if cfg.cloud_run_job is not None:
        # Independent: the platform sets CLOUD_RUN_JOB, not the scheduler.
        expected = cloud_run_job_id(
            str(doc.get("tenant_id")), str(doc.get("runner_profile")), doc.get("resource_class")
        )
        if cfg.cloud_run_job != expected:
            return "CLOUD_RUN_JOB"
    if cfg.runner_job_name is not None:
        # Self-consistency only: the same render wrote this and metadata.name.
        if cfg.runner_job_name != gke_job_name(cfg.task_id, cfg.generation):
            return "RUNNER_JOB_NAME"
    return None


def verify_step_spec(
    doc: Mapping[str, Any],
    *,
    create_time: Any,
    cfg: "WorkerConfig",
    now: datetime | None = None,
    log: Any = None,
) -> SpecCheck:
    """Checks 1 to 5 over the fetched document. Raises `SpecSignatureInvalid`."""
    task_id = cfg.task_id
    now = now or datetime.now(timezone.utc)
    signature = doc.get("spec_signature")
    version = doc.get("spec_key_version")

    # 1. Presence, or the legacy rule -- BEFORE the format.
    if not signature or not version:
        if _legacy_admits(cfg, create_time, now, log):
            return SpecCheck(reason="legacy_unsigned", task_id=task_id)
        raise SpecSignatureInvalid("unsigned", task_id=task_id)

    # 2. A format this worker knows.
    if doc.get("spec_format") not in KNOWN_FORMATS or isinstance(doc.get("spec_format"), bool):
        raise SpecSignatureInvalid(
            "unknown_format", task_id=task_id, key_version=str(version),
            detail=f"format {doc.get('spec_format')!r}",
        )

    # 3. The version, as a string, before any key is used.
    if not cfg.spec_verify_keys or not cfg.spec_signing_key:
        raise ConfigError(
            "SPEC_VERIFY_KEYS or SPEC_SIGNING_KEY is empty: this worker has no key to verify "
            "a signed spec with (terraform renders both onto every worker Job, and the "
            "swarm-spec-verify-keys ConfigMap on GKE)"
        )
    if not isinstance(version, str):
        raise SpecSignatureInvalid("foreign_key_version", task_id=task_id)
    suffix = version[len(cfg.spec_signing_key):] if version.startswith(cfg.spec_signing_key) else ""
    if not _VERSION_SUFFIX.fullmatch(suffix) or version not in cfg.spec_verify_keys:
        raise SpecSignatureInvalid("foreign_key_version", task_id=task_id, key_version=version)

    # 4. The signature over the canonical form. Not canonical is a refusal.
    try:
        digest = specsign.spec_digest(specsign.canonical_step_spec(doc, task_id=task_id))
    except specsign.SpecNotCanonical as exc:
        # Not `str(exc)`: it names the path to the value, and a path is made
        # of the spec's own keys. The reason is enough to act on.
        del exc
        raise SpecSignatureInvalid("not_canonical", task_id=task_id, key_version=version) from None
    hexdigest = digest.hex()
    if not _verify_signature(cfg.spec_verify_keys[version], signature, digest):
        raise SpecSignatureInvalid(
            "signature_mismatch", task_id=task_id, key_version=version, digest=hexdigest
        )

    # 5. The execution's environment agrees with what was signed.
    disagreement = _environment_agrees(doc, cfg)
    if disagreement is not None:
        raise SpecSignatureInvalid(
            "environment_mismatch", task_id=task_id, key_version=version, digest=hexdigest,
            detail=f"{disagreement} disagrees with the signed spec",
        )
    return SpecCheck(reason="verified", task_id=task_id, key_version=version, digest=hexdigest)
