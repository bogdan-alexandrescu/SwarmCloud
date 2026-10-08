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
are the Job's environment on both backends (Terraform's or the scheduler's),
with a read-only ConfigMap mount as the fallback on GKE. A worker with NO keys at all is misconfigured,
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

from .errors import ConfigError, SpecSignatureInvalid, WorkerError

if TYPE_CHECKING:  # pragma: no cover
    from .config import WorkerConfig

#: THE END OF THE LEGACY WINDOW, IN THE CODE (decision 5). After it the worker
#: ignores SPEC_SIGNATURE_MODE=legacy, says it did, and enforces: a flag nobody
#: remembers to turn off stops working anyway.
SPEC_LEGACY_UNTIL = datetime(2026, 10, 20, tzinfo=timezone.utc)

#: THE LATEST CUTOVER ANY CONFIGURATION CAN SET (#355). The creation time of
#: swarm-api-00119-lcs, the first revision carrying SPEC_SIGNING_KEY_VERSION
#: (#353's release, 5cf0c9b), read from Cloud Run's
#: metadata.creationTimestamp on 2026-10-01 and truncated to the second; dev's
#: `spec_legacy_cutover` is the same value. Every task swarm-api has created
#: since is signed, so an unsigned task Firestore created at or after it had
#: its signature stripped, and no SPEC_LEGACY_CUTOVER -- however late it is
#: set, until SPEC_LEGACY_UNTIL -- admits one. Tasks parked before it are
#: still admitted in legacy, which is the constraint #355 names.
SPEC_SIGNING_RELEASED_AT = datetime(2026, 9, 30, 21, 24, 36, tzinfo=timezone.utc)

#: The formats of the canonical form this worker can verify: every one the
#: contract names (format 2 is contract request 42, a child's parent fields),
#: each under its own projection, so a task signed before format 2 still runs.
KNOWN_FORMATS = frozenset(specsign.SPEC_FORMATS)

#: Where a GKE pod finds `swarm-spec-verify-keys`, mounted read-only by
#: `GkeJobDispatcher._manifest` and kubernetes/worker-templates. Each key of
#: the ConfigMap is a file here. Read only when the environment has no keys --
#: the fallback since the GKE Job carries the scheduler's keys (2026-10-08).
VERIFY_KEYS_MOUNT = Path("/etc/swarm/spec-verify-keys")

#: The four settings, by the names both sources use: the Job's environment on
#: Cloud Run, and the ConfigMap's file keys on GKE (kubernetes/render.py writes
#: terraform's `spec_verify_keys_configmap` output key for key, the mode and
#: the cutover beside the keys).
SETTING_NAMES = (
    "SPEC_VERIFY_KEYS",
    "SPEC_SIGNING_KEY",
    "SPEC_SIGNATURE_MODE",
    "SPEC_LEGACY_CUTOVER",
)

MODES = ("enforce", "legacy")

_VERSION_SUFFIX = re.compile(r"/cryptoKeyVersions/[0-9]+")

#: THE LONGEST DOCUMENT VALUE A REFUSAL COPIES. `spec_format` and
#: `spec_key_version` are fields of a document the tenant can write, and a
#: refusal copies them into `result_summary.spec_check` and the log; unbounded,
#: a 1 MB `spec_format` became a 1 MB task document field and log line. 256,
#: not less, because a legitimate key version is long: the 30-character project,
#: the longest region and `swarm-<env>-specs` already reach about 150, and a
#: refusal that cut off the version number would hide which key was asked for.
DOCUMENT_VALUE_LIMIT = 256
_TRUNCATED = "...[truncated]"


def _bounded(value: object) -> str:
    """`str(value)` at most DOCUMENT_VALUE_LIMIT characters, marked when cut.

    Every `SpecSignatureInvalid` that carries a value read from the document
    goes through this, so no refusal can copy one unbounded.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= DOCUMENT_VALUE_LIMIT:
        return text
    return text[: DOCUMENT_VALUE_LIMIT - len(_TRUNCATED)] + _TRUNCATED


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


def read_mount(mount: Path | None = None) -> dict[str, str]:
    """Each of `SETTING_NAMES` from the GKE mount; an absent file reads as "".

    A missing ConfigMap (the volume is `optional`) reads as four empty
    strings. Called only when the environment has no keys either, so that is
    no keys at all, and `verify_step_spec` is CANNOT_START for every task."""
    mount = mount or VERIFY_KEYS_MOUNT
    values: dict[str, str] = {}
    for name in SETTING_NAMES:
        try:
            values[name] = (mount / name).read_text()
        except OSError:
            values[name] = ""
    return values


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


def legacy_cutover(cfg: "WorkerConfig") -> datetime | None:
    """The cutover the legacy rule judges against: the configured one, capped
    at SPEC_SIGNING_RELEASED_AT. None when none is configured."""
    if cfg.spec_legacy_cutover is None:
        return None
    return min(cfg.spec_legacy_cutover, SPEC_SIGNING_RELEASED_AT)


def _legacy_admits(
    cfg: "WorkerConfig", create_time: Any, now: datetime, log: Any
) -> tuple[bool, str]:
    """Whether legacy admits this unsigned task, and if not, the refusal's detail.

    The detail is "" in enforce, so an enforce refusal reads exactly as it
    always has; in legacy it says which rule refused, so an operator reading
    `last_error` knows a stripped signature from a late cutover.
    """
    if cfg.spec_signature_mode != "legacy":
        return False, ""
    if now >= SPEC_LEGACY_UNTIL:
        if log is not None:
            log.warning(
                "SPEC_SIGNATURE_MODE=legacy ignored: past SPEC_LEGACY_UNTIL, enforcing",
                spec_legacy_until=SPEC_LEGACY_UNTIL.isoformat(),
            )
        return False, f"legacy mode ignored past SPEC_LEGACY_UNTIL {SPEC_LEGACY_UNTIL.isoformat()}"
    cutover = legacy_cutover(cfg)
    if cutover is None:
        return False, "legacy mode has no SPEC_LEGACY_CUTOVER, so it admits no unsigned task"
    if not isinstance(create_time, datetime):
        return False, "legacy mode could not read the task's Firestore create_time"
    created = create_time if create_time.tzinfo else create_time.replace(tzinfo=timezone.utc)
    if created < cutover:
        return True, ""
    capped = " (SPEC_LEGACY_CUTOVER capped at the signing release)" if (
        cutover != cfg.spec_legacy_cutover
    ) else ""
    return False, (
        f"created {created.astimezone(timezone.utc).isoformat()}, at or after the legacy "
        f"cutover {cutover.astimezone(timezone.utc).isoformat()}{capped}"
    )


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

    # 0. A worker with no key cannot verify anything, and that is ITS
    # configuration, not the tenant's attack: CANNOT_START for every task,
    # signed or not, BEFORE the legacy rule. On GKE the mode and the cutover
    # come from the same mount as the keys, so a pod whose ConfigMap is
    # missing has read no mode either; it must neither admit an unsigned task
    # nor refuse one as a signature failure.
    if not cfg.spec_verify_keys or not cfg.spec_signing_key:
        raise ConfigError(
            "SPEC_VERIFY_KEYS or SPEC_SIGNING_KEY is empty: this worker has no key to verify "
            "a spec with (terraform renders both onto every worker Job, and the "
            "swarm-spec-verify-keys ConfigMap on GKE)"
        )

    # 1. Presence, or the legacy rule -- BEFORE the format.
    if not signature or not version:
        admitted, why_not = _legacy_admits(cfg, create_time, now, log)
        if admitted:
            return SpecCheck(reason="legacy_unsigned", task_id=task_id)
        raise SpecSignatureInvalid("unsigned", task_id=task_id, detail=why_not)

    key_version, hexdigest = _verify_signed(doc, task_id=task_id, cfg=cfg)

    # 5. The execution's environment agrees with what was signed.
    disagreement = _environment_agrees(doc, cfg)
    if disagreement is not None:
        raise SpecSignatureInvalid(
            "environment_mismatch", task_id=task_id, key_version=key_version, digest=hexdigest,
            detail=f"{disagreement} disagrees with the signed spec",
        )
    return SpecCheck(reason="verified", task_id=task_id, key_version=key_version, digest=hexdigest)


def _verify_signed(doc: Mapping[str, Any], *, task_id: str, cfg: "WorkerConfig") -> tuple[str, str]:
    """Checks 2 to 4 over a document that carries a signature and a version.

    Returns `(key version, hex digest)`; raises `SpecSignatureInvalid`.
    Shared by this execution's own spec and the upstream specs a worker
    action depends on (`verify_upstream_spec`), so the two can never verify
    by different rules.
    """
    signature = doc.get("spec_signature")
    version = doc.get("spec_key_version")

    # 2. A format this worker knows. An int first: a list or a map is
    # unhashable, and `in KNOWN_FORMATS` raised TypeError on one instead of
    # refusing it.
    spec_format = doc.get("spec_format")
    if (
        not isinstance(spec_format, int)
        or isinstance(spec_format, bool)
        or spec_format not in KNOWN_FORMATS
    ):
        raise SpecSignatureInvalid(
            "unknown_format", task_id=task_id, key_version=_bounded(version),
            detail=f"format {_bounded(repr(spec_format))}",
        )

    # 3. The version, as a string, before any key is used (step 0 made sure
    # there are keys).
    if not isinstance(version, str):
        raise SpecSignatureInvalid("foreign_key_version", task_id=task_id)
    suffix = version[len(cfg.spec_signing_key):] if version.startswith(cfg.spec_signing_key) else ""
    if not _VERSION_SUFFIX.fullmatch(suffix) or version not in cfg.spec_verify_keys:
        raise SpecSignatureInvalid(
            "foreign_key_version", task_id=task_id, key_version=_bounded(version)
        )

    # 4. The signature over the canonical form. Not canonical is a refusal.
    try:
        digest = specsign.spec_digest(
            specsign.canonical_step_spec(
                doc, task_id=task_id, spec_format=int(doc["spec_format"])
            )
        )
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
    return version, hexdigest


# ---------------------------------------------------------------------------
# The upstream specs a worker action depends on (#295)
# ---------------------------------------------------------------------------


class UpstreamSpecUnverified(WorkerError):
    """An upstream step's spec that a worker action depends on did not verify.

    NOT `SpecSignatureInvalid`: that cause is this execution's OWN spec.
    Contract request 34's own text gives an upstream failure the acting
    step's cause instead -- MERGE_REFUSED for `merge`, VERDICT_REFUSED for
    `post-verdict` -- with `result_summary.spec_check.reason` set to
    `upstream:<task id>:<why>`, `<why>` being CR 34's vocabulary verbatim
    (`unsigned`, `signature_mismatch`, ...), and `workflow_mismatch` for a
    verified spec of another workflow (docs/merge-step.md §6 row 42, §6a
    row 5).
    """

    def __init__(self, upstream_task_id: str, why: str, *, key_version: str | None = None) -> None:
        self.upstream_task_id = upstream_task_id
        self.why = why
        self.key_version = key_version
        self.reason = f"upstream:{upstream_task_id}:{why}"
        super().__init__(f"the signed spec of upstream task {upstream_task_id} did not verify: {why}")

    def spec_check(self) -> dict[str, str | None]:
        return {
            "reason": self.reason,
            "task_id": self.upstream_task_id,
            "key_version": self.key_version,
        }


def verify_upstream_spec(
    doc: Mapping[str, Any],
    *,
    upstream_task_id: str,
    workflow_id: str | None,
    cfg: "WorkerConfig",
) -> SpecCheck:
    """Checks 1 to 4 over an UPSTREAM task's document, and its workflow.

    Raises `UpstreamSpecUnverified`. Three differences from the own-spec
    check, each deliberate:

    * NO LEGACY WINDOW. An unsigned upstream spec is `unsigned`, whatever
      SPEC_SIGNATURE_MODE says: the merge chain is enabled only once #342 is
      enforced (docs/merge-step.md §10), and a merge or a posted verdict
      resting on a step nobody signed is exactly what it exists to refuse.
    * NO ENVIRONMENT CHECK (5): the upstream ran in another execution, whose
      environment this one cannot see.
    * THE WORKFLOW MUST BE THIS ONE. The signature makes the upstream's
      `workflow_id` a fact, and an upstream of another workflow -- a
      verified spec, honestly signed, of a step this chain never ran -- is
      `workflow_mismatch`.

    A worker with no keys is `ConfigError` (CANNOT_START), as for its own
    spec: its configuration, never a tenant's attack.
    """
    if not cfg.spec_verify_keys or not cfg.spec_signing_key:
        raise ConfigError(
            "SPEC_VERIFY_KEYS or SPEC_SIGNING_KEY is empty: this worker has no key to verify "
            "an upstream spec with"
        )
    if not doc.get("spec_signature") or not doc.get("spec_key_version"):
        raise UpstreamSpecUnverified(upstream_task_id, "unsigned")
    try:
        version, hexdigest = _verify_signed(doc, task_id=upstream_task_id, cfg=cfg)
    except SpecSignatureInvalid as exc:
        raise UpstreamSpecUnverified(
            upstream_task_id, exc.reason, key_version=exc.key_version
        ) from None
    if not workflow_id or doc.get("workflow_id") != workflow_id:
        raise UpstreamSpecUnverified(upstream_task_id, "workflow_mismatch", key_version=version)
    return SpecCheck(
        reason="verified", task_id=upstream_task_id, key_version=version, digest=hexdigest
    )
