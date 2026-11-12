"""Bundle builds: a bundled plugin's version built into an image version, in this deployment.

docs/design/managed-plugins.md sections 1.4.2, 1.4.3, 1.4.4 and 1.4.7 (lane 16
of section 17.1). gstack and security-audit are platform installs with
`build: "image"` (pluginstore.BUILD_IMAGE): their code must run before it
works, so a version is not unpacked per attempt but built, by the bundler
image, into one image per runner image a claude-code profile uses.

THE RECORD. `plugin_bundles/{bundle_id}`, written by swarm-api alone and
MAC'd under `swarm-settings-key` over the frozen contract's
`specsign.canonical_plugin_record("bundle", fields)` (contract request P6,
accepted by the owner 2026-11-09), through pluginstore's own `record_mac` and
`record_verifies`: a tenant identity can write any Firestore document, and a
forged `ready` record could otherwise move `current_version` to a pin nobody
built. A record whose MAC is missing or wrong is absent to every reader here.
Platform scope only, no tenant.

    key         every bundled install's {install_id, version_id, source_sha256}
                (a bundle carries both plugins: one step may load both, and
                runs on one image), sorted by install id
    key_sha256  the sha256 of the key's JCS: what a step's signed set names
    base        {runner image name: the release image it is built FROM, by digest}
    recipes     {install_id: the recipe digest the job was asked to build with},
                read back from the release image's own `.swarm-bundle.json`
    job         the one Cloud Run job it ran (configuration, never a caller's)
    execution   that job's execution, as `jobs.run` named it
    execution_read_at  the last `executions.get`, claimed in a transaction
    state       building -> ready | failed; ready -> retired (retention)
    outputs     {runner image name: the pushed image, by the digest swarm-api
                computed from the manifest it read and checked}, once ready
    plugins     {install_id: {recipe_digest, tree_sha256 per runner image}},
                each tree digest computed by swarm-api from the layer itself
    refusal     {code, check} once failed: fixed values, never the job's log text

THE LIFECYCLE (section 1.4.3). An Apply on a bundled install (`start_apply`)
writes one `building` record and runs ONE execution of the Cloud Run job
`swarm-plugin-bundle` (owner, 11 November 2026: a job under the builder
account, never Cloud Build), passing only `containerOverrides[0].args` in the
design's order (`job_args`); it never moves `current_version`. The status read
(`finish`) reads the record, and calls `executions.get` at most once per bundle
per READ_EVERY (`_claim_read`); a record still building BUILD_DEADLINE after it
started is failed `bundle_build_failed` without a call. A succeeded execution
is ready only after `check_bundle` read every pushed manifest and layer from
the bundle repository and found them to be the release image plus one layer
per plugin, under its own root, with the tree its manifest names (and, for
security-audit, its pinned source's files); then ONE transaction marks the
record ready and moves the install's `current_version`
(`PluginStore.move_current`), or marks it failed and moves nothing. Roll back
(`roll_back`, section 1.4.7) moves `current_version` to the previous version
at once when a retained ready bundle carries it.
Both moves first check, in that transaction, that the bundle's whole key is
the live pair once moved (`_stale_key`); a bundle that is not fails (or is
refused) `bundle_key_stale`. A building record whose start never recorded
its execution within START_GRACE is failed `bundle_build_not_started`, so a
lost start never holds later Applies. A ready finish then retires what
retention no longer keeps (`retire`).

A BUNDLE BUILD IS NOT AN AGENT ATTEMPT (invariant 1). It is one execution of
a platform job, outside every pool: it reserves no capacity, creates no lease
and no pod, and a queued step costs nothing while it waits for one. Nothing
here sleeps on it (invariant 4): the finish is read when it is asked for.

A MISSING GRANT IS A FIXED CODE. swarm-api's role on the job
(`swarmPluginBundleRunner`) and its read of the bundle repository are the
infrastructure lane's. Until they are applied, Cloud Run refuses the run, and
the Apply answers 503 `bundle_grant_missing` with the record failed under the
same code, never a 500.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import secrets
import tarfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.specsign import jcs

from . import pluginstore as ps
from .judgment import MAC_FIELD

log = logging.getLogger(__name__)

BUNDLES = "plugin_bundles"
KIND = "bundle"

STATE_BUILDING = "building"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_RETIRED = "retired"
STATES = (STATE_BUILDING, STATE_READY, STATE_FAILED, STATE_RETIRED)

#: Why a bundle was built: an admin's Apply, or `swarm upgrade` building a
#: retained key again on a new base before infra switches to it (1.4.4).
REASON_APPLY = "apply"
REASON_REBUILD = "rebuild"

BUNDLE_ID = re.compile(r"^b-[0-9a-f]{12}$")
DIGEST_REF = re.compile(r"^([a-z0-9-]+)-docker\.pkg\.dev/([a-z][a-z0-9-]{4,61}[a-z0-9])/"
                        r"([a-z0-9][a-z0-9-]{0,62})/([a-z0-9][a-z0-9._/-]{0,127})@sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^([a-z0-9-]+)-docker\.pkg\.dev/([a-z][a-z0-9-]{4,61}[a-z0-9])/"
                        r"([a-z0-9][a-z0-9-]{0,47})-plugin-bundles$")

#: The plugin each reserved install builds, and the folder it is built into
#: (the install path gstack's compiled `browse` embeds, section 1.4).
PLUGIN_OF = {"builtin:gstack": "gstack", "builtin:security-audit": "security-audit"}
PLUGIN_ROOT = "/opt/swarm/plugins"

#: The runner images a claude-code profile runs on: one bundle image each.
#: Read from the frozen profiles, never restated.
RUNNER_IMAGES: tuple[str, ...] = tuple(sorted({
    p.image for name, p in RUNNER_PROFILES.items() if name == "claude-code" or name.startswith("claude-code-")}))

#: How long a `building` record may go without the execution name its start
#: records. The start is one Cloud Run call; a record still without a name
#: after this lost its start (a write that failed after the execution was
#: created, or a process that died between the two) and is failed
#: `bundle_build_not_started`, so it cannot hold every later Apply at 409.
START_GRACE = timedelta(minutes=10)
#: At most one `executions.get` per bundle per this long (1.4.2's read cap),
#: the time of the last read claimed on the record in a transaction, so any
#: number of open pages and reloads make at most one call.
READ_EVERY = timedelta(seconds=15)
#: A record still `building` this long after it started is failed
#: `bundle_build_failed` without a call: the job's own agent timeout is
#: 3600 seconds, so its execution has ended by then whatever it says.
BUILD_DEADLINE = timedelta(hours=1)

#: What swarm-api reads from the registry, each bounded: a manifest or a
#: config, and one layer (1.4.2: a cap of 512 MiB per layer).
MANIFEST_CAP = 4 * 1024 * 1024
LAYER_CAP = 512 * 1024 * 1024
#: A pushed layer is written by the job, which runs upstream plugin code, so
#: it is read as hostile: LAYER_CAP bounds its gzip transfer and each file,
#: and these bound what unpacking it can cost. The total bytes gunzipped
#: from one layer: a small gzip of one repeated byte unpacks about a
#: thousandfold, so without it 512 MiB on the wire is hundreds of GiB to
#: read and hash. 2 GiB is four times the transfer cap and several times a
#: real plugin's tree (the largest, gstack with its runtime and browser, is
#: some hundreds of MiB).
LAYER_UNPACKED_CAP = 2 * 1024 * 1024 * 1024
#: The tar members in one layer. tarfile keeps every member's header while
#: it reads, and the check keeps a line per path, so this also bounds the
#: memory one read holds; a real plugin's tree is some tens of thousands of
#: paths at most.
LAYER_MEMBERS_CAP = 100_000
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"
LAYER_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: The one whiteout a plugin's layer may hold: the opaque marker on its own
#: folder, which hides the release's older copy.
OPAQUE = ".wh..wh..opq"
#: The manifest each built tree carries (images/plugin-bundler/swarm-bundle).
MANIFEST_NAME = ".swarm-bundle.json"
#: security-audit's validators, the part of its source the layer must equal.
AUDIT_SKILL = "skills/security-audit"

#: The closed patterns of the job's arguments (1.4.2), the entrypoint's own.
ARG_NAME = re.compile(r"^[a-z0-9-]{1,63}$")
ARG_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARG_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: An execution of a job, as Cloud Run names it.
EXECUTION = re.compile(r"^projects/[a-z][a-z0-9-]{4,61}[a-z0-9]/locations/[a-z0-9-]+/jobs/[a-z][a-z0-9-]{0,62}"
                       r"/executions/[a-z0-9][a-z0-9-]{0,62}$")

# Configuration. Read only here, each `(os.environ if environ is None else
# environ).get(...)`, the shape scripts/lib/check-env-parity.sh matches.
REPOSITORY_ENV = "PLUGIN_BUNDLE_REPOSITORY"
#: The job's name (`swarm-plugin-bundle`, terraform/infra/plugin-bundles.tf),
#: set by the infrastructure lane beside the repository.
JOB_ENV = "PLUGIN_BUNDLE_JOB"
BASE_ENV = "WORKER_IMAGE_REFS"
PLUGINS_BUCKET_ENV = "SWARM_PLUGINS_BUCKET"
JOB_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


# --- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class BundleSettings:
    project: str
    region: str
    repository: str
    job_name: str
    base: Mapping[str, str]
    plugins_bucket: str

    @property
    def job(self) -> str:
        """The one job swarm-api may run, in this deployment's region."""
        return f"projects/{self.project}/locations/{self.region}/jobs/{self.job_name}"


def ref_ok(ref: Any, project: str, region: str) -> bool:
    """`ref` is an image in this deployment's own registry, by digest."""
    match = DIGEST_REF.match(ref) if isinstance(ref, str) else None
    return bool(match) and match.group(1) == region and match.group(2) == project


def check_base(base: Any, project: str, region: str, *,
               like: Mapping[str, str] | None = None) -> dict[str, str]:
    """The base map, every claude-code runner image named by digest in this
    deployment's registry, or `bundle_base_invalid`. With `like` (the live
    base), each image must also be in the repository its live image is in:
    a caller names a release's digest, never another repository
    (invariant 10)."""
    if not isinstance(base, Mapping) or any(not ref_ok(base.get(name), project, region)
                                            for name in RUNNER_IMAGES):
        raise ps.PluginRefused("bundle_base_invalid")
    if like is not None and any(base[name].partition("@")[0] != str(like.get(name) or "").partition("@")[0]
                                for name in RUNNER_IMAGES):
        raise ps.PluginRefused("bundle_base_invalid")
    return {name: base[name] for name in RUNNER_IMAGES}


def bundle_settings(project: str, region: str, environ: Mapping[str, str] | None = None) -> BundleSettings:
    """This deployment's bundle build, or 503 `bundle_build_unconfigured`
    when any part is missing or not this deployment's: a build is started
    from configuration alone, never from a caller's words (invariant 10)."""
    repository = (os.environ if environ is None else environ).get(REPOSITORY_ENV, "").strip()
    job_name = (os.environ if environ is None else environ).get(JOB_ENV, "").strip()
    refs = (os.environ if environ is None else environ).get(BASE_ENV, "").strip()
    bucket = (os.environ if environ is None else environ).get(PLUGINS_BUCKET_ENV, "").strip()
    match = REPOSITORY.match(repository)
    try:
        base = json.loads(refs) if refs else None
    except ValueError:
        base = None
    if not (project and region and match and match.group(1) == region and match.group(2) == project
            and bucket and JOB_NAME.match(job_name)):
        raise ps.PluginRefused("bundle_build_unconfigured", status=503)
    try:
        checked = check_base(base, project, region)
    except ps.PluginRefused:
        raise ps.PluginRefused("bundle_build_unconfigured", status=503) from None
    return BundleSettings(project=project, region=region, repository=repository, job_name=job_name,
                          base=checked, plugins_bucket=bucket)


# --- the job, the registry and the sources ---------------------------------------


class GrantMissing(Exception):
    """Cloud Run or the registry refused swarm-api (401/403): a grant is not applied."""


class BuildNotStarted(Exception):
    """Cloud Run answered anything else; the status code only, never its text."""


class RegistryUnavailable(Exception):
    """The registry or the bucket could not be read now; the status code only.
    The record stays `building` and the next read tries again."""


class BundleJobs(Protocol):
    def run(self, job: str, args: list[str]) -> str: ...

    def get(self, execution: str) -> Mapping[str, Any] | None: ...


class BundleRegistry(Protocol):
    def manifest(self, image: str, reference: str) -> bytes | None: ...

    def blob(self, image: str, digest: str, cap: int) -> BinaryIO: ...


class BundleSources(Protocol):
    def source(self, source_sha256: str) -> BinaryIO | None: ...


def _session() -> Any:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


class CloudRunBundleJobs:
    """The Cloud Run Admin API's `jobs.run` (with overrides) and
    `executions.get`: the two calls swarm-api's role on the job allows."""

    API = "https://run.googleapis.com/v2"

    def __init__(self) -> None:
        self._session: Any = None

    def _s(self) -> Any:
        if self._session is None:
            self._session = _session()
        return self._session

    def run(self, job: str, args: list[str]) -> str:
        """One execution of `job`, overriding only the one container's
        arguments: never `env`, never a second container."""
        body = {"overrides": {"containerOverrides": [{"args": list(args)}]}}
        answer = self._s().post(f"{self.API}/{job}:run", json=body, timeout=30)
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code >= 300:
            raise BuildNotStarted(answer.status_code)
        name = (answer.json().get("metadata") or {}).get("name")
        if not isinstance(name, str) or not EXECUTION.match(name) or not name.startswith(job + "/executions/"):
            raise BuildNotStarted(answer.status_code)
        return name

    def get(self, execution: str) -> Mapping[str, Any] | None:
        answer = self._s().get(f"{self.API}/{execution}", timeout=30)
        if answer.status_code == 404:
            return None
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code >= 300:
            raise BuildNotStarted(answer.status_code)
        found = answer.json()
        return found if isinstance(found, Mapping) else None


class _Capped(io.RawIOBase):
    """A blob read as it arrives: refused past `cap` bytes, and refused at
    its end unless its sha256 is the digest it was asked for."""

    def __init__(self, chunks: Iterable[bytes], digest: str, cap: int) -> None:
        self._chunks = iter(chunks)
        self._digest = digest
        self._cap = cap
        self._hash = hashlib.sha256()
        self._size = 0
        self._left = b""
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        while not self._left and not self._done:
            chunk = next(self._chunks, None)
            if chunk is None:
                self._done = True
                if "sha256:" + self._hash.hexdigest() != self._digest:
                    raise CheckFailed("blob_digest")
                break
            self._size += len(chunk)
            if self._size > self._cap:
                raise CheckFailed("blob_too_large")
            self._hash.update(chunk)
            self._left = chunk
        count = min(len(buffer), len(self._left))
        buffer[:count] = self._left[:count]
        self._left = self._left[count:]
        return count


class ArtifactRegistryReader:
    """The Docker registry API of Artifact Registry, as a reader of the
    repositories: a manifest by tag or digest, a blob by digest."""

    ACCEPT = ", ".join((OCI_MANIFEST, DOCKER_MANIFEST))

    def __init__(self) -> None:
        self._session: Any = None

    def _s(self) -> Any:
        if self._session is None:
            self._session = _session()
        return self._session

    @staticmethod
    def _url(image: str, kind: str, reference: str) -> str:
        host, _, path = image.partition("/")
        return f"https://{host}/v2/{path}/{kind}/{reference}"

    def manifest(self, image: str, reference: str) -> bytes | None:
        answer = self._s().get(self._url(image, "manifests", reference), headers={"Accept": self.ACCEPT},
                               timeout=30, stream=True)
        if answer.status_code == 404:
            return None
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code >= 300:
            raise RegistryUnavailable(answer.status_code)
        body = b""
        for chunk in answer.iter_content(1 << 16):
            body += chunk
            if len(body) > MANIFEST_CAP:
                raise CheckFailed("manifest_too_large")
        return body

    def blob(self, image: str, digest: str, cap: int) -> BinaryIO:
        answer = self._s().get(self._url(image, "blobs", digest), timeout=300, stream=True)
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code == 404:
            raise CheckFailed("blob_missing")
        if answer.status_code >= 300:
            raise RegistryUnavailable(answer.status_code)
        return io.BufferedReader(_Capped(answer.iter_content(1 << 20), digest, cap), 1 << 20)


class GcsBundleSources:
    """The pinned source archives swarm-api's intake finish wrote
    (`platform/sources/<sha256>.tar` in the plugins bucket)."""

    def __init__(self, bucket: str, project: str) -> None:
        self._name = bucket
        self._project = project
        self._bucket: Any = None

    def source(self, source_sha256: str) -> BinaryIO | None:
        if self._bucket is None:
            from google.cloud import storage  # lazy: no client at import

            self._bucket = storage.Client(project=self._project).bucket(self._name)
        blob = self._bucket.get_blob(source_object(source_sha256))
        return None if blob is None else blob.open("rb")


def source_object(source_sha256: str) -> str:
    """Where a bundled version's checked source archive is kept (1.4.3)."""
    return f"platform/sources/{source_sha256}.tar"


def job_args(bundle_id: str, base: Mapping[str, str], versions: Mapping[str, Mapping[str, Any]],
             recipes: Mapping[str, str]) -> list[str]:
    """The job's arguments, in the design's order and form (1.4.2): the
    bundle id; then per bundled plugin, by name, its name, its source sha256
    and its recipe digest; then per runner image its name and its base
    digest. Each value is checked here against the closed pattern the
    entrypoint checks again; nothing else reaches the job."""
    args = [bundle_id]
    for install_id in sorted(versions, key=lambda i: PLUGIN_OF[i]):
        args += [PLUGIN_OF[install_id], versions[install_id]["source_sha256"], recipes[install_id]]
    for image in RUNNER_IMAGES:
        args += [image, base[image].partition("@")[2]]
    patterns = [ARG_NAME] + [p for _ in versions for p in (ARG_NAME, ARG_SHA256, ARG_SHA256)] \
        + [p for _ in RUNNER_IMAGES for p in (ARG_NAME, ARG_DIGEST)]
    if len(args) != len(patterns) or not all(isinstance(a, str) and p.match(a) for a, p in zip(args, patterns)):
        raise ps.PluginRefused("bundle_build_not_started", status=503, bundle_id=bundle_id)
    return args


# --- the check before ready (1.4.2) --------------------------------------------------


class CheckFailed(Exception):
    """A pushed bundle failed swarm-api's check: `code` (`bundle_check_failed`,
    or `bundle_config_changed` for the image config), with the fixed name of
    the check or field (never anything the job wrote)."""

    def __init__(self, check: str, code: str = "bundle_check_failed") -> None:
        super().__init__(check)
        self.check = check
        self.code = code


def _image_manifest(registry: BundleRegistry, image: str, reference: str) -> tuple[str, dict[str, Any]]:
    """(the digest swarm-api computed from the bytes it read, the manifest)."""
    raw = registry.manifest(image, reference)
    if raw is None:
        raise CheckFailed("manifest_missing")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if reference.startswith("sha256:") and reference != digest:
        raise CheckFailed("manifest_digest")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise CheckFailed("manifest_unreadable") from None
    layers = doc.get("layers") if isinstance(doc, Mapping) else None
    config = doc.get("config") if isinstance(doc, Mapping) else None
    if doc.get("schemaVersion") != 2 or doc.get("mediaType", OCI_MANIFEST) not in (OCI_MANIFEST, DOCKER_MANIFEST) \
            or not isinstance(layers, list) or not isinstance(config, Mapping) \
            or not all(isinstance(layer, Mapping) and isinstance(layer.get("digest"), str)
                       and LAYER_DIGEST.match(layer["digest"]) for layer in layers) \
            or not isinstance(config.get("digest"), str) or not LAYER_DIGEST.match(config["digest"]):
        raise CheckFailed("manifest_unreadable")
    return digest, doc


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    for name, value in pairs:
        if name in doc:
            # Two parsers may keep different copies of a repeated key: the
            # one compared here must be the one the runtime reads.
            raise ValueError("repeated key")
        doc[name] = value
    return doc


def _config(registry: BundleRegistry, image: str, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(the image config as read, its rootfs.diff_ids)."""
    with registry.blob(image, manifest["config"]["digest"], MANIFEST_CAP) as handle:
        raw = handle.read(MANIFEST_CAP + 1)
    try:
        doc = json.loads(raw, object_pairs_hook=_unique_keys)
        diff_ids = doc["rootfs"]["diff_ids"]
    except (ValueError, KeyError, TypeError):
        raise CheckFailed("config_unreadable") from None
    if not isinstance(doc, dict) or not isinstance(doc["rootfs"], dict) or not isinstance(diff_ids, list) \
            or not all(isinstance(d, str) and LAYER_DIGEST.match(d) for d in diff_ids):
        raise CheckFailed("config_unreadable")
    return doc, diff_ids


#: The history entry the bundle job appends for each plugin layer
#: (images/plugin-bundler/swarm-bundle, `append_layers`, on the `job` path):
#: a fixed time and a fixed text naming the plugin's folder. The job sets no
#: other field of the config; the top-level `created` stays the release's.
BUNDLE_HISTORY_CREATED = "1970-01-01T00:00:00Z"


def bundle_history(plugin: str) -> dict[str, str]:
    return {"created": BUNDLE_HISTORY_CREATED, "created_by": f"swarm-bundle job: {PLUGIN_ROOT}/{plugin}/"}


#: The image config's field names (OCI image-spec and Docker's own), the only
#: names a `bundle_config_changed` refusal records: a field the job added
#: under any other name is recorded as `other`, so the job's text never
#: reaches the record.
CONFIG_FIELDS = frozenset({
    "architecture", "author", "comment", "config", "container", "container_config", "created",
    "docker_version", "history", "id", "os", "os.features", "os.version", "parent", "rootfs", "variant"})
RUNTIME_FIELDS = frozenset({
    "ArgsEscaped", "AttachStderr", "AttachStdin", "AttachStdout", "Cmd", "CpuShares", "Domainname",
    "Entrypoint", "Env", "ExposedPorts", "Healthcheck", "Hostname", "Image", "Labels", "MacAddress",
    "Memory", "MemorySwap", "NetworkDisabled", "OnBuild", "OpenStdin", "Shell", "StdinOnce", "StopSignal",
    "StopTimeout", "Tty", "User", "Volumes", "WorkingDir"})
_ABSENT = object()


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON values, type for type: `1`, `1.0` and `true` differ."""
    if a is _ABSENT or b is _ABSENT:
        return a is b
    return json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(b, sort_keys=True, separators=(",", ":"))


def _first_difference(base: Mapping[str, Any], pushed: Mapping[str, Any], names: frozenset[str],
                      skip: Iterable[str] = ()) -> str | None:
    for name in sorted(set(base) | set(pushed)):
        if name not in skip and not _same(base.get(name, _ABSENT), pushed.get(name, _ABSENT)):
            return name if name in names else "other"
    return None


def check_config(base: Mapping[str, Any], pushed: Mapping[str, Any], plugins: list[str]) -> None:
    """The pushed config is the base's with exactly two differences:
    `rootfs.diff_ids` extended by one diff id per plugin layer (their values
    are held to the layers as each is read) and `history` extended by the
    job's fixed entry per plugin, in order. Anything else refused
    `bundle_config_changed` with the first differing field's name alone."""
    changed = _first_difference(base, pushed, CONFIG_FIELDS, skip=("config", "rootfs", "history"))
    if changed is None:
        runtime, pushed_runtime = base.get("config", _ABSENT), pushed.get("config", _ABSENT)
        if isinstance(runtime, Mapping) and isinstance(pushed_runtime, Mapping):
            inner = _first_difference(runtime, pushed_runtime, RUNTIME_FIELDS)
            changed = None if inner is None else f"config.{inner}"
        elif not _same(runtime, pushed_runtime):
            changed = "config"
    if changed is None:
        inner = _first_difference(base["rootfs"], pushed["rootfs"], frozenset({"type"}), skip=("diff_ids",))
        changed = None if inner is None else f"rootfs.{inner}"
    if changed is None:
        diff_ids, pushed_diff_ids = base["rootfs"]["diff_ids"], pushed["rootfs"]["diff_ids"]
        if len(pushed_diff_ids) != len(diff_ids) + len(plugins) or pushed_diff_ids[:len(diff_ids)] != diff_ids:
            changed = "rootfs.diff_ids"
    if changed is None:
        history = base.get("history", [])
        if not isinstance(history, list) \
                or not _same(pushed.get("history", _ABSENT), history + [bundle_history(p) for p in plugins]):
            changed = "history"
    if changed is not None:
        raise CheckFailed(changed, code="bundle_config_changed")


class _Hashing(io.RawIOBase):
    """The uncompressed layer, hashed as tarfile reads it: its diff_id.
    Refused `layer_too_large` once more than `cap` bytes have come out."""

    def __init__(self, inner: BinaryIO, cap: int) -> None:
        self._inner = inner
        self._cap = cap
        self._read = 0
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._inner.read(len(buffer))
        self._read += len(data)
        if self._read > self._cap:
            raise CheckFailed("layer_too_large")
        self.hash.update(data)
        buffer[:len(data)] = data
        return len(data)


def _file_sha256(handle: Any) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1 << 20), b""):
        digest.update(block)
    return digest.hexdigest()


@dataclass
class LayerTree:
    """What swarm-api read from one plugin's layer."""

    tree_sha256: str
    manifest: Mapping[str, Any]
    files: dict[str, str]  # path below the root -> content sha256
    diff_id: str  # the uncompressed layer's sha256, as read


def read_layer(handle: BinaryIO, plugin: str, media_type: str) -> LayerTree:
    """One plugin's layer, read entry by entry: only paths under
    `/opt/swarm/plugins/<plugin>/`, no `..`, no link, no device, no whiteout
    but the opaque marker on that folder; its uncompressed sha256 (the
    diff_id the config must name) and the tree digest computed as swarm-bundle computes it.
    Past LAYER_UNPACKED_CAP bytes unpacked or LAYER_MEMBERS_CAP members the
    read stops, refused by that fixed check alone."""
    if media_type in (OCI_LAYER_GZIP, DOCKER_LAYER_GZIP):
        inner: BinaryIO = gzip.GzipFile(fileobj=handle, mode="rb")  # type: ignore[assignment]
    elif media_type == OCI_LAYER:
        inner = handle
    else:
        raise CheckFailed("layer_media_type")
    root = f"{PLUGIN_ROOT.lstrip('/')}/{plugin}"
    hashing = _Hashing(inner, LAYER_UNPACKED_CAP)
    reader = io.BufferedReader(hashing, 1 << 20)
    entries: dict[str, tuple[str, str, str]] = {}
    files: dict[str, str] = {}
    manifest_raw: bytes | None = None
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for count, member in enumerate(tar, 1):
                if count > LAYER_MEMBERS_CAP:
                    raise CheckFailed("layer_too_many_members")
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                name = name.lstrip("/").rstrip("/")
                parts = name.split("/")
                if not name or ".." in parts or "." in parts or "" in parts or "\n" in name or "\r" in name:
                    raise CheckFailed("layer_path")
                if name == root:
                    if not member.isdir():
                        raise CheckFailed("layer_path")
                    continue
                if not name.startswith(root + "/"):
                    raise CheckFailed("layer_path")
                rel = name[len(root) + 1:]
                if parts[-1].startswith(".wh."):
                    if rel != OPAQUE or not member.isfile():
                        raise CheckFailed("layer_whiteout")
                    continue
                if member.issym() or member.islnk():
                    raise CheckFailed("layer_link")
                if not (member.isdir() or member.isfile()):
                    raise CheckFailed("layer_device")
                if rel in entries:
                    raise CheckFailed("layer_duplicate")
                mode = f"{member.mode & 0o7777:04o}"
                if member.isdir():
                    entries[rel] = ("d", mode, "")
                    continue
                if member.size > LAYER_CAP:
                    raise CheckFailed("blob_too_large")
                extracted = tar.extractfile(member)
                if rel == MANIFEST_NAME:
                    manifest_raw = extracted.read(MANIFEST_CAP + 1) if extracted else b""
                    continue
                sha = _file_sha256(extracted) if extracted else hashlib.sha256(b"").hexdigest()
                entries[rel] = ("f", mode, sha)
                files[rel] = sha
        while reader.read(1 << 20):
            pass
        while handle.read(1 << 20):
            pass
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise CheckFailed("layer_unreadable") from None
    digest = hashlib.sha256()
    for rel in sorted(entries):
        kind, mode, sha = entries[rel]
        line = f"d {mode} {rel}/\n" if kind == "d" else f"f {mode} {sha} {rel}\n"
        digest.update(line.encode())
    try:
        manifest = json.loads(manifest_raw) if manifest_raw is not None and len(manifest_raw) <= MANIFEST_CAP else None
    except ValueError:
        manifest = None
    if not isinstance(manifest, Mapping):
        raise CheckFailed("bundle_manifest_missing")
    return LayerTree(tree_sha256=digest.hexdigest(), manifest=manifest, files=files,
                     diff_id="sha256:" + hashing.hash.hexdigest())


def source_files(handle: BinaryIO) -> dict[str, str]:
    """security-audit's `skills/security-audit/` and `LICENSE` in its pinned
    source archive: path -> content sha256. A single top-level folder (the
    form a forge's archive takes) is taken off every path first."""
    found: dict[str, str] = {}
    names: list[tuple[str, str]] = []
    try:
        with tarfile.open(fileobj=handle, mode="r|*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                extracted = tar.extractfile(member)
                names.append((name.lstrip("/"), _file_sha256(extracted) if extracted else ""))
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise CheckFailed("source_unreadable") from None
    tops = {n.split("/", 1)[0] for n, _ in names}
    strip = len(tops) == 1 and all("/" in n for n, _ in names)
    for name, sha in names:
        rel = name.split("/", 1)[1] if strip else name
        if rel == "LICENSE" or rel.startswith(AUDIT_SKILL + "/"):
            found[rel] = sha
    return found


def release_recipes(registry: BundleRegistry, base_ref: str, wanted: Iterable[str]) -> dict[str, str]:
    """The recipe digest of each bundled install, from the live release
    image's own `.swarm-bundle.json` (1.4.2: the release image is the bundle
    of the first versions, built by the same recipes the bundler image
    holds). Its layers are read newest first until every one is found."""
    image, _, digest = base_ref.partition("@")
    _, manifest = _image_manifest(registry, image, digest)
    wanted = set(wanted)
    paths = {f"{PLUGIN_ROOT.lstrip('/')}/{PLUGIN_OF[i]}/{MANIFEST_NAME}": i for i in wanted}
    found: dict[str, str] = {}
    for layer in reversed(manifest["layers"]):
        if set(found) == wanted:
            break
        media = layer.get("mediaType")
        mode = "r|gz" if media in (OCI_LAYER_GZIP, DOCKER_LAYER_GZIP) else "r|" if media == OCI_LAYER else None
        if mode is None:
            continue
        with registry.blob(image, layer["digest"], LAYER_CAP) as handle:
            try:
                with tarfile.open(fileobj=handle, mode=mode) as tar:
                    for member in tar:
                        install_id = paths.get(member.name.lstrip("./").lstrip("/"))
                        if install_id is None or install_id in found or not member.isfile():
                            continue
                        extracted = tar.extractfile(member)
                        try:
                            doc = json.loads(extracted.read(MANIFEST_CAP)) if extracted else None
                        except ValueError:
                            doc = None
                        recipe = ((doc or {}).get("recipe") or {}).get("digest") if isinstance(doc, Mapping) else None
                        if isinstance(recipe, str) and ARG_SHA256.match(recipe):
                            found[install_id] = recipe
                # To its end, so the blob's digest is checked too.
                while handle.read(1 << 20):
                    pass
            except (tarfile.TarError, OSError, EOFError, zlib.error):
                raise CheckFailed("release_unreadable") from None
    if set(found) != wanted:
        raise CheckFailed("release_recipe_missing")
    return found


def check_bundle(bundle: Mapping[str, Any], registry: BundleRegistry, sources: BundleSources,
                 repository: str) -> dict[str, Any]:
    """Section 1.4.2's check of every pushed manifest, read by the tag
    `<runner image>:<bundle_id>`; {outputs, plugins} as swarm-api read and
    checked them, or CheckFailed. Nothing the job reported is used."""
    bundle_id = bundle["bundle_id"]
    key = {e["install_id"]: e for e in bundle.get("key") or ()}
    recipes = bundle.get("recipes") or {}
    order = sorted(key, key=lambda i: PLUGIN_OF.get(i, ""))
    if not key or any(i not in PLUGIN_OF for i in key) or set(recipes) != set(key):
        raise CheckFailed("bundle_key")
    outputs: dict[str, str] = {}
    trees: dict[str, dict[str, str]] = {i: {} for i in order}
    audit_source: dict[str, str] | None = None
    for image in RUNNER_IMAGES:
        base_image, _, base_digest = str((bundle.get("base") or {}).get(image) or "").partition("@")
        _, base = _image_manifest(registry, base_image, base_digest)
        out_image = f"{repository}/{image}"
        digest, pushed = _image_manifest(registry, out_image, bundle_id)
        n = len(base["layers"])
        if len(pushed["layers"]) != n + len(order) \
                or [layer["digest"] for layer in pushed["layers"][:n]] != [layer["digest"] for layer in base["layers"]]:
            raise CheckFailed("base_layers")
        base_config, base_diff_ids = _config(registry, base_image, base)
        config, diff_ids = _config(registry, out_image, pushed)
        if len(base_diff_ids) != n:
            raise CheckFailed("config_unreadable")
        # Entrypoint, Env, User and the rest run in every step of every
        # tenant on this image: the job may change none of them (1.4).
        check_config(base_config, config, [PLUGIN_OF[i] for i in order])
        for offset, install_id in enumerate(order):
            layer = pushed["layers"][n + offset]
            with registry.blob(out_image, layer["digest"], LAYER_CAP) as handle:
                tree = read_layer(handle, PLUGIN_OF[install_id], str(layer.get("mediaType") or ""))
            if tree.diff_id != diff_ids[n + offset]:
                # The config's added diff ids are exactly the layers', in order.
                raise CheckFailed("rootfs.diff_ids", code="bundle_config_changed")
            said = tree.manifest
            if (said.get("tree") or {}).get("sha256") != tree.tree_sha256:
                raise CheckFailed("tree_sha256")
            if said.get("install_id") != install_id \
                    or (said.get("source") or {}).get("sha256") != key[install_id]["source_sha256"] \
                    or (said.get("recipe") or {}).get("digest") != recipes[install_id]:
                raise CheckFailed("bundle_manifest")
            if PLUGIN_OF[install_id] == "security-audit":
                if audit_source is None:
                    handle = sources.source(key[install_id]["source_sha256"])
                    if handle is None:
                        raise CheckFailed("source_missing")
                    with handle:
                        audit_source = source_files(handle)
                # The layer holds exactly those files, byte for byte: the
                # recipe copies and tests, it compiles nothing.
                if not audit_source or tree.files != audit_source:
                    raise CheckFailed("security_audit_files")
            trees[install_id][image] = tree.tree_sha256
        outputs[image] = f"{out_image}@{digest}"
    plugins = {i: {"recipe_digest": recipes[i], "tree_sha256": trees[i]} for i in order}
    return {"outputs": outputs, "plugins": plugins}


# --- the record --------------------------------------------------------------


def new_bundle_id() -> str:
    return "b-" + secrets.token_hex(6)


def key_sha256(key: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(jcs(list(key))).hexdigest()


def bundle_key(versions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    return [{"install_id": install_id, "version_id": v["version_id"], "source_sha256": v["source_sha256"]}
            for install_id, v in sorted(versions.items())]


def bundle_view(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """What the console shows: ids, digests, the state and a fixed code."""
    refusal = bundle.get("refusal") or None
    code = refusal.get("code") if isinstance(refusal, Mapping) else None
    return {
        "bundle_id": bundle.get("bundle_id"), "state": bundle.get("state"), "reason": bundle.get("reason"),
        "install_id": bundle.get("install_id"), "version_id": bundle.get("version_id"),
        "previous_version": bundle.get("previous_version"), "key": bundle.get("key"),
        "key_sha256": bundle.get("key_sha256"), "base": bundle.get("base"), "outputs": bundle.get("outputs"),
        "plugins": bundle.get("plugins"), "execution": bundle.get("execution"),
        "refusal": {"code": code, "check": refusal.get("check"),
                    "sentence": ps.REFUSALS.get(code, "")} if code else None,
        "requested_by": bundle.get("requested_by"), "finished_at": bundle.get("finished_at"),
    }


class BundleStore:
    """`plugin_bundles`, each record MAC'd, through the catalogue's store."""

    def __init__(self, store: ps.PluginStore, db: Any, *, now: Callable[[], Any]) -> None:
        self._store = store
        self._db = db
        self._now = now

    def _ref(self, bundle_id: str) -> Any:
        return self._db.collection(BUNDLES).document(bundle_id)

    def _verified(self, snap: Any, bundle_id: str) -> dict[str, Any] | None:
        if not snap.exists:
            return None
        whole = dict(snap.to_dict() or {})
        whole["bundle_id"] = bundle_id
        return whole if ps.record_verifies(KIND, whole, self._store.keys) else None

    def _signed(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        stored = {k: v for k, v in fields.items() if k != MAC_FIELD}
        stored[MAC_FIELD] = ps.record_mac(KIND, stored, self._store.keys)
        return stored

    def bundle(self, bundle_id: str) -> dict[str, Any] | None:
        if not BUNDLE_ID.match(bundle_id or ""):
            return None
        return self._verified(self._ref(bundle_id).get(), bundle_id)

    def bundles(self, state: str | None = None) -> list[dict[str, Any]]:
        query = self._db.collection(BUNDLES)
        if state is not None:
            query = query.where(filter=FieldFilter("state", "==", state))
        found = []
        for snap in query.stream():
            whole = self._verified(snap, snap.id)
            if whole is not None:
                found.append(whole)
        return sorted(found, key=lambda b: (str(b.get("created_at") or ""), b["bundle_id"]))

    def bundled_installs(self) -> list[dict[str, Any]]:
        return [i for i in self._store.installs(ps.PLATFORM)
                if ps.is_bundled(i) and i.get("state") == ps.STATE_ACTIVE and i["install_id"] in PLUGIN_OF]

    def create(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        stored = self._signed(fields)
        self._ref(fields["bundle_id"]).set(stored)
        return stored

    def update(self, bundle_id: str, change: Callable[[dict[str, Any], Any], bool]) -> dict[str, Any] | None:
        """Read the verified record in a transaction, let `change` edit it
        (and write beside it in the same transaction); store it signed when
        `change` says so. None when the record does not verify."""
        ref = self._ref(bundle_id)
        result: dict[str, Any] = {}
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            whole = self._verified(_snapshot(txn.get(ref)), bundle_id)
            if whole is None:
                return
            if change(whole, txn):
                whole = self._signed(whole)
                txn.set(ref, whole)
            result.update(whole)

        _write(transaction)
        return result or None


# --- start a build -------------------------------------------------------------


def _versions_for(store: ps.PluginStore, installs: list[dict[str, Any]], *, target_install: str | None,
                  target_version: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Every bundled install's version for a bundle: the target's new one,
    the others' current."""
    versions = {}
    for install in installs:
        if install["install_id"] == target_install and target_version is not None:
            versions[install["install_id"]] = dict(target_version)
            continue
        current = store.current(install)
        if current is None:
            raise ps.PluginRefused("version_not_found", status=409, install_id=install["install_id"])
        versions[install["install_id"]] = current
    return versions


def _start(bundles: BundleStore, jobs: BundleJobs, registry: BundleRegistry, config: BundleSettings, *,
           base: Mapping[str, str], versions: Mapping[str, Mapping[str, Any]], reason: str, by: str,
           install_id: str | None, version_id: str | None, previous_version: str | None) -> dict[str, Any]:
    """Write one `building` record, then run its one execution of the job.
    A refused start fails the record under the start's fixed code and
    raises it."""
    bundle_id = new_bundle_id()
    key = bundle_key(versions)
    now = bundles._now()
    code = None
    recipes: dict[str, str] = {}
    try:
        # The recipe digest each plugin is built with: the one the live
        # release image carries, read back from its own manifest.
        recipes = _known_recipes(bundles, base, versions) \
            or release_recipes(registry, base[RUNNER_IMAGES[0]], versions)
    except GrantMissing as exc:
        code = "bundle_grant_missing"
        log.warning("plugin bundle recipes not read bundle_id=%s status=%s", bundle_id, exc.args[0])
    except Exception as exc:  # noqa: BLE001 -- a fixed code; the upstream's words are never passed on
        code = "bundle_build_not_started"
        log.warning("plugin bundle recipes not read bundle_id=%s (%s)", bundle_id, type(exc).__name__)
    bundles.create({
        "bundle_id": bundle_id, "scope": ps.PLATFORM, "tenant_id": None, "reason": reason,
        "install_id": install_id, "version_id": version_id, "previous_version": previous_version,
        "key": key, "key_sha256": key_sha256(key), "base": dict(base), "recipes": dict(recipes),
        "job": config.job, "state": STATE_BUILDING, "execution": None, "execution_read_at": None,
        "outputs": None, "plugins": None, "refusal": None,
        "requested_by": by, "finished_at": None, "created_at": now, "created_by": by,
    })
    execution = None
    if code is None:
        try:
            execution = jobs.run(config.job, job_args(bundle_id, base, versions, recipes))
        except GrantMissing as exc:
            code = "bundle_grant_missing"
            log.warning("plugin bundle job not run bundle_id=%s status=%s", bundle_id, exc.args[0])
        except Exception as exc:  # noqa: BLE001 -- a fixed code; the upstream's words are never passed on
            code = "bundle_build_not_started"
            log.warning("plugin bundle job not run bundle_id=%s (%s)", bundle_id, type(exc).__name__)

    def _record(whole: dict[str, Any], txn: Any) -> bool:
        if code is None:
            whole["execution"] = execution
        else:
            whole.update({"state": STATE_FAILED, "refusal": {"code": code},
                          "finished_at": bundles._now().isoformat()})
        return True

    stored = bundles.update(bundle_id, _record) or {}
    if code is not None:
        raise ps.PluginRefused(code, status=503, bundle_id=bundle_id)
    return stored


def _known_recipes(bundles: BundleStore, base: Mapping[str, str],
                   versions: Mapping[str, Any]) -> dict[str, str] | None:
    """The recipe digests a ready bundle on the same base was checked with
    (the same bundler image, so the same recipes), sparing the release
    image's read."""
    for ready in reversed(bundles.bundles(STATE_READY)):
        recipes = ready.get("recipes")
        if ready.get("base") == dict(base) and isinstance(recipes, Mapping) and set(recipes) == set(versions) \
                and all(isinstance(v, str) and ARG_SHA256.match(v) for v in recipes.values()):
            return dict(recipes)
    return None


def _time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _older(value: Any, now: Any, limit: timedelta) -> bool:
    """`value` is more than `limit` before `now`; True when either is not a
    time, so a record that cannot say when it started never holds."""
    then = _time(value)
    if then is None or not isinstance(now, datetime):
        return True
    try:
        return now - then > limit
    except TypeError:
        return True


def _unstarted(bundle: Mapping[str, Any], now: Any) -> bool:
    """A `building` record whose start never recorded an execution, older
    than START_GRACE."""
    if bundle.get("state") != STATE_BUILDING or bundle.get("execution"):
        return False
    return _older(bundle.get("created_at"), now, START_GRACE)


def fail_unstarted(bundles: BundleStore, bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Fail a building record whose start was lost (`_unstarted`) under
    `bundle_build_not_started`; any other record is returned unchanged."""
    if not _unstarted(bundle, bundles._now()):
        return dict(bundle)

    def _change(whole: dict[str, Any], txn: Any) -> bool:
        if not _unstarted(whole, bundles._now()):
            return False
        whole.update({"state": STATE_FAILED, "refusal": {"code": "bundle_build_not_started"},
                      "finished_at": bundles._now().isoformat()})
        return True

    return bundles.update(bundle["bundle_id"], _change) or dict(bundle)


def _building(bundles: BundleStore) -> list[dict[str, Any]]:
    """The building records, once every lost start among them is failed."""
    return [b for b in (fail_unstarted(bundles, b) for b in bundles.bundles(STATE_BUILDING))
            if b.get("state") == STATE_BUILDING]


def _refuse_while_building(bundles: BundleStore, install_id: str) -> None:
    """One Apply build at a time, across BOTH bundled installs: a bundle's
    key holds every bundled install's version, so a second build started
    from the same current pair would make current a pair no bundle carries.
    This read is not transactional: two Applies at once can both pass it,
    so the ready finish and Roll back each check, in the transaction that
    moves current, that the bundle's whole key is the live pair
    (`_stale_key`)."""
    if any(b.get("reason") == REASON_APPLY for b in _building(bundles)):
        raise ps.PluginRefused("bundle_building", status=409, install_id=install_id)


def start_apply(store: ps.PluginStore, bundles: BundleStore, jobs: BundleJobs, registry: BundleRegistry,
                config: BundleSettings, install_id: str, version_id: str, *, by: str) -> dict[str, Any]:
    """Apply `version_id` to a bundled install: run exactly one execution of
    the bundle carrying it on the live base. `current_version` does not
    move here; the ready finish moves it. A ready bundle with the same key
    on the live base is applied at once (nothing to build)."""
    install = store.install(install_id)
    if install is None or install.get("state") != ps.STATE_ACTIVE:
        raise ps.PluginRefused("install_not_active", status=409, install_id=install_id)
    if not ps.is_bundled(install) or install_id not in PLUGIN_OF:
        raise ps.PluginRefused("install_not_bundled", status=409, install_id=install_id)
    target = store.version(install_id, version_id)
    if target is None:
        raise ps.PluginRefused("version_not_found", status=404, install_id=install_id)
    if install.get("current_version") == version_id:
        raise ps.PluginRefused("version_is_current", status=409, install_id=install_id)
    _refuse_while_building(bundles, install_id)
    versions = _versions_for(store, bundles.bundled_installs(), target_install=install_id, target_version=target)
    key_digest = key_sha256(bundle_key(versions))
    for ready in bundles.bundles(STATE_READY):
        if ready.get("key_sha256") == key_digest and ready.get("base") == dict(config.base):
            return _move_to(store, bundles, ready, install, version_id, by=by)
    return _start(bundles, jobs, registry, config, base=config.base, versions=versions, reason=REASON_APPLY, by=by,
                  install_id=install_id, version_id=version_id, previous_version=install.get("current_version"))


def rebuild(store: ps.PluginStore, bundles: BundleStore, jobs: BundleJobs, registry: BundleRegistry,
            config: BundleSettings, base: Mapping[str, Any], *, by: str) -> list[dict[str, Any]]:
    """`swarm upgrade`'s step (section 1.4.4): at most ONE build per call, of
    the first retained key not yet ready or building on the new `base` (the
    new release's images, already copied into this deployment's registry),
    and none while a rebuild is still building on it, so the job runs one
    execution at a time. The caller waits for the build and calls again
    until a call starts nothing and nothing is building on the base (the
    owner's pick on PR #3873's question 1). Moves no `current_version`."""
    new_base = check_base(base, config.project, config.region, like=config.base)
    if any(b.get("reason") == REASON_REBUILD and b.get("base") == new_base for b in _building(bundles)):
        return []
    on_base = {b.get("key_sha256") for b in bundles.bundles() if b.get("base") == new_base
               and b.get("state") in (STATE_BUILDING, STATE_READY)}
    for kept in retention(bundles)["keep"]:
        if kept.get("key_sha256") in on_base or kept.get("state") != STATE_READY:
            continue
        versions = {}
        for entry in kept.get("key") or ():
            version = store.version(entry["install_id"], entry["version_id"])
            if version is None or version.get("source_sha256") != entry.get("source_sha256"):
                break
            versions[entry["install_id"]] = version
        else:
            return [_start(bundles, jobs, registry, config, base=new_base, versions=versions,
                           reason=REASON_REBUILD, by=by, install_id=None, version_id=None, previous_version=None)]
    return []


# --- the finish ------------------------------------------------------------------


def execution_outcome(execution: Mapping[str, Any] | None) -> str | None:
    """What a Cloud Run execution says: None while it runs, "succeeded"
    once its one task succeeded, "failed" otherwise (failed, cancelled,
    timed out, or gone). Only its counts and completion time are read."""
    if execution is None:
        return STATE_FAILED
    def count(name: str) -> int:
        value = execution.get(name)
        return value if isinstance(value, int) else 0
    if count("failedCount") or count("cancelledCount"):
        return STATE_FAILED
    if not execution.get("completionTime"):
        return None
    return "succeeded" if count("succeededCount") >= 1 else STATE_FAILED


def _stale_key(store: ps.PluginStore, txn: Any, key: Iterable[Mapping[str, Any]], install_id: str,
               version_id: str) -> bool:
    """Read inside the transaction that moves `install_id` to `version_id`:
    True unless, after that move, every bundled install's live current
    version is the one `key` names. Two builds started at once from the
    same current pair each carry the other install's old version; the
    second to finish (or a Roll back across a pair that has since changed)
    would make current a pair no ready bundle carries."""
    entries = {e.get("install_id"): e.get("version_id") for e in key}
    if entries.get(install_id) != version_id:
        return True
    for other, wanted in entries.items():
        if other == install_id:
            continue
        live = store._verified("install", _snapshot(txn.get(store._install_ref(other))), {"install_id": other})
        if live is None or live.get("state") != ps.STATE_ACTIVE or live.get("current_version") != wanted:
            return True
    return False


def mark_finished(store: ps.PluginStore, bundles: BundleStore, bundle_id: str, state: str,
                  detail: Mapping[str, Any]) -> dict[str, Any] | None:
    """End a building record, in ONE transaction: ready moves the applied
    install's `current_version` beside it; failed records its fixed code and
    moves nothing. A record that already ended is returned unchanged."""

    def _change(whole: dict[str, Any], txn: Any) -> bool:
        if whole.get("state") != STATE_BUILDING:
            return False
        finished = bundles._now().isoformat()
        if state == STATE_FAILED:
            refusal = {"code": detail["code"]}
            if detail.get("check"):
                refusal["check"] = detail["check"]
            whole.update({"state": STATE_FAILED, "refusal": refusal, "finished_at": finished})
            return True
        install = None
        if whole.get("reason") == REASON_APPLY:
            install_id = whole["install_id"]
            install = store._verified("install", _snapshot(txn.get(store._install_ref(install_id))),
                                      {"install_id": install_id})
            if install is None or install.get("state") != ps.STATE_ACTIVE or not ps.is_bundled(install):
                whole.update({"state": STATE_FAILED, "refusal": {"code": "install_not_active"},
                              "finished_at": finished})
                return True
            if _stale_key(store, txn, whole.get("key") or (), install_id, whole["version_id"]):
                whole.update({"state": STATE_FAILED, "refusal": {"code": "bundle_key_stale"},
                              "finished_at": finished})
                return True
        whole.update({"state": STATE_READY, "outputs": dict(detail["outputs"]),
                      "plugins": dict(detail["plugins"]), "finished_at": finished})
        if install is not None:
            store.move_current(txn, install, whole["version_id"], by=str(whole.get("requested_by") or ""),
                               bundle_id=bundle_id)
        return True

    return bundles.update(bundle_id, _change)


def _claim_read(bundles: BundleStore, bundle_id: str) -> bool:
    """Claim this bundle's one execution read for the next READ_EVERY, in a
    transaction on the record: any number of pages and reloads make at most
    one call per bundle per READ_EVERY (1.4.2)."""
    claimed: list[bool] = []

    def _change(whole: dict[str, Any], txn: Any) -> bool:
        now = bundles._now()
        if whole.get("state") != STATE_BUILDING or not whole.get("execution"):
            return False
        last = whole.get("execution_read_at")
        if last is not None and not _older(last, now, READ_EVERY - timedelta(microseconds=1)):
            return False
        whole["execution_read_at"] = now.isoformat()
        claimed.append(True)
        return True

    bundles.update(bundle_id, _change)
    return bool(claimed)


def finish(store: ps.PluginStore, bundles: BundleStore, jobs: BundleJobs, registry: BundleRegistry,
           sources: BundleSources, config: BundleSettings, bundle: Mapping[str, Any]) -> dict[str, Any]:
    """End a building record if its execution ended. Read from the record
    alone unless this bundle's read is claimed (`_claim_read`); a record
    still building BUILD_DEADLINE after it started is failed with no call.
    A succeeded execution is ready only after `check_bundle` passed."""
    if bundle.get("state") != STATE_BUILDING:
        return dict(bundle)
    bundle_id = bundle["bundle_id"]
    if _older(bundle.get("created_at"), bundles._now(), BUILD_DEADLINE):
        return mark_finished(store, bundles, bundle_id, STATE_FAILED, {"code": "bundle_build_failed"}) \
            or dict(bundle)
    if not bundle.get("execution"):
        return fail_unstarted(bundles, bundle)
    execution = str(bundle["execution"])
    if not execution.startswith(config.job + "/executions/") or not EXECUTION.match(execution):
        # Not this deployment's job: never read, never trusted.
        return mark_finished(store, bundles, bundle_id, STATE_FAILED, {"code": "bundle_build_failed"}) \
            or dict(bundle)
    if not _claim_read(bundles, bundle_id):
        return bundles.bundle(bundle_id) or dict(bundle)
    outcome = execution_outcome(jobs.get(execution))
    if outcome is None:
        return bundles.bundle(bundle_id) or dict(bundle)
    if outcome == STATE_FAILED:
        state, detail = STATE_FAILED, {"code": "bundle_build_failed"}
    else:
        try:
            state, detail = STATE_READY, check_bundle(bundle, registry, sources, config.repository)
        except CheckFailed as failed:
            log.warning("plugin bundle check failed bundle_id=%s code=%s check=%s", bundle_id, failed.code,
                        failed.check)
            state, detail = STATE_FAILED, {"code": failed.code, "check": failed.check}
    ended = mark_finished(store, bundles, bundle_id, state, detail) or dict(bundle)
    if ended.get("state") == STATE_READY:
        # A ready finish is when current (and so retention) changes: retire
        # what retention no longer keeps, so the records `swarm upgrade`
        # counts and rebuilds are the kept ones. Best effort: the next ready
        # finish retires whatever this one could not.
        try:
            retire(bundles)
        except Exception as exc:  # noqa: BLE001 -- the finish itself is committed
            log.warning("plugin bundle retention not applied (%s)", type(exc).__name__)
    return ended


# --- Roll back and retention -------------------------------------------------------


def _move_to(store: ps.PluginStore, bundles: BundleStore, ready: Mapping[str, Any], install: Mapping[str, Any],
             version_id: str, *, by: str) -> dict[str, Any]:
    install_id = install["install_id"]
    refused: list[str] = []

    def _change(whole: dict[str, Any], txn: Any) -> bool:
        if whole.get("state") != STATE_READY:
            refused.append("bundle_not_retained")
            return False
        live = store._verified("install", _snapshot(txn.get(store._install_ref(install_id))),
                               {"install_id": install_id})
        if live is None or live.get("state") != ps.STATE_ACTIVE:
            refused.append("install_not_active")
            return False
        if _stale_key(store, txn, whole.get("key") or (), install_id, version_id):
            refused.append("bundle_key_stale")
            return False
        store.move_current(txn, live, version_id, by=by, bundle_id=whole["bundle_id"])
        return False

    moved = bundles.update(ready["bundle_id"], _change)
    if refused or moved is None:
        raise ps.PluginRefused(refused[0] if refused else "bundle_not_retained", status=409,
                               install_id=install_id)
    return moved


def roll_back(store: ps.PluginStore, bundles: BundleStore, config: BundleSettings, install_id: str,
              version_id: str, *, by: str) -> dict[str, Any]:
    """Section 1.4.7: apply the previous version at once, through the
    retained ready bundle that carries it with the other installs' current
    versions on the live base. No retained bundle: 409 `bundle_not_retained`
    (Apply builds one)."""
    install = store.install(install_id)
    if install is None or install.get("state") != ps.STATE_ACTIVE:
        raise ps.PluginRefused("install_not_active", status=409, install_id=install_id)
    if not ps.is_bundled(install) or install_id not in PLUGIN_OF:
        raise ps.PluginRefused("install_not_bundled", status=409, install_id=install_id)
    target = store.version(install_id, version_id)
    if target is None:
        raise ps.PluginRefused("version_not_found", status=404, install_id=install_id)
    if install.get("current_version") == version_id:
        raise ps.PluginRefused("version_is_current", status=409, install_id=install_id)
    _refuse_while_building(bundles, install_id)
    versions = _versions_for(store, bundles.bundled_installs(), target_install=install_id, target_version=target)
    wanted = key_sha256(bundle_key(versions))
    for ready in bundles.bundles(STATE_READY):
        if ready.get("key_sha256") == wanted and ready.get("base") == dict(config.base):
            return _move_to(store, bundles, ready, install, version_id, by=by)
    raise ps.PluginRefused("bundle_not_retained", status=409, install_id=install_id)


#: The task states that still need their bundle (an unfinished step).
UNFINISHED = ("SUBMITTED", "QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING")


def _named_keys(db: Any) -> set[str]:
    """The bundle keys an unfinished step's signed plugin set names
    (`metadata.dispatch.plugins.bundle`, section 1.4.4)."""
    named: set[str] = set()
    query = db.collection("tasks").where(filter=FieldFilter("state", "in", list(UNFINISHED)))
    for snap in query.stream():
        plugins = ((((snap.to_dict() or {}).get("metadata") or {}).get("dispatch") or {}).get("plugins") or {})
        key = plugins.get("bundle") if isinstance(plugins, Mapping) else None
        if isinstance(key, str):
            named.add(key)
    return named


def retention(bundles: BundleStore, *, named: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Section 1.4.4's retention list: a bundle is kept while it is building,
    current (its key is every bundled install's current version), the
    previous current one (Roll back's), or named by an unfinished step; every
    other ready or failed record may be retired. A kept key is kept on every
    base, so the previous release's bundles stay for an upgrade's own
    rollback."""
    installs = {i["install_id"]: i.get("current_version") for i in bundles.bundled_installs()}
    current = [{"install_id": i, "version_id": v} for i, v in sorted(installs.items())]
    all_bundles = [b for b in bundles.bundles() if b.get("state") != STATE_RETIRED]
    current_keys = {b["key_sha256"] for b in all_bundles
                    if [{"install_id": e["install_id"], "version_id": e["version_id"]}
                        for e in b.get("key") or ()] == current}
    ready_current = [b for b in all_bundles if b.get("state") == STATE_READY and b["key_sha256"] in current_keys]
    previous_keys: set[str] = set()
    if ready_current:
        newest = ready_current[-1]
        if newest.get("reason") == REASON_APPLY and newest.get("previous_version"):
            want = [{"install_id": e["install_id"],
                     "version_id": newest["previous_version"] if e["install_id"] == newest["install_id"]
                     else e["version_id"]} for e in newest.get("key") or ()]
            previous_keys = {b["key_sha256"] for b in all_bundles
                             if [{"install_id": e["install_id"], "version_id": e["version_id"]}
                                 for e in b.get("key") or ()] == want}
    named = bundles_named(bundles) if named is None else named
    keep_keys = current_keys | previous_keys | named
    keep, retire = [], []
    for b in all_bundles:
        if b.get("state") == STATE_BUILDING or (b.get("state") == STATE_READY and b["key_sha256"] in keep_keys):
            keep.append(b)
        else:
            retire.append(b)
    return {"keep": keep, "retire": retire}


def bundles_named(bundles: BundleStore) -> set[str]:
    return _named_keys(bundles._db)


def retire(bundles: BundleStore, *, named: set[str] | None = None) -> list[str]:
    """Mark every record retention does not keep `retired` (a failed one
    too). Its images are deleted by section 9.3's sweep, not here."""
    retired = []
    for b in retention(bundles, named=named)["retire"]:
        def _change(whole: dict[str, Any], txn: Any) -> bool:
            if whole.get("state") not in (STATE_READY, STATE_FAILED):
                return False
            whole["state"] = STATE_RETIRED
            return True

        if (bundles.update(b["bundle_id"], _change) or {}).get("state") == STATE_RETIRED:
            retired.append(b["bundle_id"])
    return retired
