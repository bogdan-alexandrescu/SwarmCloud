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
    bundler     the bundler image the build ran, by digest
    state       building -> ready | failed; ready -> retired (retention)
    outputs     {runner image name: the pushed image, by digest}, once ready
    plugins     {install_id: {tree_sha256, recipe_digest}}, read back from the
                build's own `.swarm-bundle.json` files, once ready
    refusal     {code} once failed: a fixed code, never the build's log text

THE LIFECYCLE (section 1.4.3). An Apply on a bundled install (`start_apply`)
writes one `building` record and starts one Cloud Build build as the builder
account; it never moves `current_version`. The finish (`finish`), reached by
the build status read or by the Cloud Build notification, reads the build from
Cloud Build itself, never from the notification's body, and in ONE transaction
marks the record ready and moves the install's `current_version`
(`PluginStore.move_current`), or marks it failed and moves nothing. Roll back
(`roll_back`, section 1.4.7) moves `current_version` to the previous version
at once when a retained ready bundle carries it.
Both moves first check, in that transaction, that the bundle's whole key is
the live pair once moved (`_stale_key`); a bundle that is not fails (or is
refused) `bundle_key_stale`. A building record whose start never recorded
its build id within START_GRACE is failed `bundle_build_not_started`, so a
lost start never holds later Applies. A ready finish then retires what
retention no longer keeps (`retire`).

A BUNDLE BUILD IS NOT AN AGENT ATTEMPT (invariant 1). It is a Cloud Build
build in the deployment's project, outside every pool: it reserves no
capacity, creates no lease and no pod, and a queued step costs nothing while
it waits for one. Nothing here sleeps on it (invariant 4): the finish is read
when it is asked for.

A MISSING GRANT IS A FIXED CODE. The builder account's IAM rows (swarm-api's
`serviceAccountUser` on it, and the right to create a build) are bootstrap's,
applied by the owner (lane 15). Until they are, Cloud Build refuses the start,
and the Apply answers 503 `bundle_grant_missing` with the record failed under
the same code, never a 500.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol

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
#: gstack's file list per runner image (images/plugin-bundler/recipes/gstack):
#: the browser image lays its list over the base's; every other image, base.
GSTACK_LISTS = {"agent-runtime-browser": "browser"}

#: Cloud Build's terminal states, and the ones a build ends well in.
BUILD_SUCCESS = "SUCCESS"
BUILD_FAILED = frozenset({"FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"})
#: The build's own timeout: gstack's setup compiles with Bun, three times.
BUILD_TIMEOUT = "3600s"
#: How long a `building` record may go without the build id its start
#: records. The start is one Cloud Build call; a record still without an id
#: after this lost its start (a write that failed after the build was
#: created, or a process that died between the two) and is failed
#: `bundle_build_not_started`, so it cannot hold every later Apply at 409.
START_GRACE = timedelta(minutes=10)

# Configuration. Read only here, each `(os.environ if environ is None else
# environ).get(...)`, the shape scripts/lib/check-env-parity.sh matches.
REPOSITORY_ENV = "PLUGIN_BUNDLE_REPOSITORY"
BUNDLER_ENV = "SWARM_PLUGIN_BUNDLER_IMAGE"
BASE_ENV = "WORKER_IMAGE_REFS"
PLUGINS_BUCKET_ENV = "SWARM_PLUGINS_BUCKET"
#: The builder account terraform/bootstrap creates (plugin-bundles.tf reads it).
BUILDER_ACCOUNT = "swarm-plugin-bundler"


# --- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class BundleSettings:
    project: str
    region: str
    repository: str
    bundler: str
    base: Mapping[str, str]
    plugins_bucket: str

    @property
    def builder(self) -> str:
        return f"{BUILDER_ACCOUNT}@{self.project}.iam.gserviceaccount.com"

    @property
    def prefix(self) -> str:
        match = REPOSITORY.match(self.repository)
        return match.group(3) if match else ""

    @property
    def npm_registry(self) -> str:
        """The deployment's npm mirror (plugin-bundles.tf, Q19)."""
        return f"https://{self.region}-npm.pkg.dev/{self.project}/{self.prefix}-mirror-npm/"

    @property
    def mirror_bucket(self) -> str:
        """Bun's pinned zip (build_mirror.tf's bucket)."""
        return f"{self.prefix}-build-mirror-{self.project}"


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
    bundler = (os.environ if environ is None else environ).get(BUNDLER_ENV, "").strip()
    refs = (os.environ if environ is None else environ).get(BASE_ENV, "").strip()
    bucket = (os.environ if environ is None else environ).get(PLUGINS_BUCKET_ENV, "").strip()
    match = REPOSITORY.match(repository)
    try:
        base = json.loads(refs) if refs else None
    except ValueError:
        base = None
    if not (project and region and match and match.group(1) == region and match.group(2) == project
            and bucket and ref_ok(bundler, project, region)):
        raise ps.PluginRefused("bundle_build_unconfigured", status=503)
    try:
        checked = check_base(base, project, region)
    except ps.PluginRefused:
        raise ps.PluginRefused("bundle_build_unconfigured", status=503) from None
    return BundleSettings(project=project, region=region, repository=repository, bundler=bundler,
                          base=checked, plugins_bucket=bucket)


# --- Cloud Build -------------------------------------------------------------


class GrantMissing(Exception):
    """Cloud Build refused the caller (401/403): a grant is not applied."""


class BuildNotStarted(Exception):
    """Cloud Build answered anything else; the status code only, never its text."""


class BundleBuilds(Protocol):
    def start(self, build: Mapping[str, Any]) -> str: ...

    def get(self, build_id: str) -> Mapping[str, Any] | None: ...


class CloudBuildBundleBuilds:
    """`projects.locations.builds` create and get, in this deployment's region."""

    def __init__(self, project: str, region: str) -> None:
        self._base = f"https://cloudbuild.googleapis.com/v1/projects/{project}/locations/{region}/builds"
        self._session: Any = None

    def _s(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def start(self, build: Mapping[str, Any]) -> str:
        answer = self._s().post(self._base, json=dict(build), timeout=30)
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code >= 300:
            raise BuildNotStarted(answer.status_code)
        build_id = ((answer.json().get("metadata") or {}).get("build") or {}).get("id")
        if not isinstance(build_id, str) or not build_id:
            raise BuildNotStarted(answer.status_code)
        return build_id

    def get(self, build_id: str) -> Mapping[str, Any] | None:
        answer = self._s().get(f"{self._base}/{build_id}", timeout=30)
        if answer.status_code == 404:
            return None
        if answer.status_code in (401, 403):
            raise GrantMissing(answer.status_code)
        if answer.status_code >= 300:
            raise BuildNotStarted(answer.status_code)
        found = answer.json()
        return found if isinstance(found, Mapping) else None


#: The fetch step: the pins file and the mirror folder the recipes read.
#: Runs in the bundler image (python3, no extra tool), with the builder's own
#: token from the metadata server, reading only the two objects it names. The
#: pins file is the image's release pins with each plugin's version and
#: source replaced by the bundle key's, so the toolchain stays the release's.
_FETCH = r"""
import json, sys, urllib.parse, urllib.request
spec = json.loads(sys.argv[1])
meta = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={"Metadata-Flavor": "Google"})
grant = json.load(urllib.request.urlopen(meta, timeout=30))
headers = {"Authorization": grant["token_type"] + " " + grant["access_token"]}
def fetch(bucket, name, to):
    import os
    url = ("https://storage.googleapis.com/storage/v1/b/" + bucket + "/o/"
           + urllib.parse.quote(name, safe="") + "?alt=media")
    request = urllib.request.Request(url, headers=headers)
    os.makedirs(os.path.dirname(to), exist_ok=True)
    with urllib.request.urlopen(request, timeout=300) as got, open(to, "wb") as out:
        while True:
            chunk = got.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
pins = json.load(open("/opt/swarm-bundler/bundled-plugins.json"))
for name, plugin in spec["plugins"].items():
    pins["plugins"][name].update({"version": plugin["version"], "source": plugin["source"]})
    fetch(spec["sources_bucket"], plugin["object"], "/workspace/mirror/" + plugin["source"]["object"])
    for tool in (pins["plugins"][name].get("toolchain") or {}).values():
        fetch(spec["mirror_bucket"], tool["object"], "/workspace/mirror/" + tool["object"])
json.dump(pins, open("/workspace/pins.json", "w"), indent=2, sort_keys=True)
"""

#: The last step: each built tree's manifest, as the build's step output
#: (4 KiB, base64 in `results.buildStepOutputs`), the one place swarm-api
#: reads the tree digests from. The build log is never read.
_REPORT = r"""
import json, os, sys
images = json.loads(sys.argv[1])
found = {}
for image in images:
    folder = "/workspace/out/" + image
    for name in sorted(n for n in os.listdir(folder) if os.path.isdir(folder + "/" + n)):
        manifest = json.load(open("/workspace/out/" + image + "/" + name + "/.swarm-bundle.json"))
        found.setdefault(manifest["install_id"], {})[image] = {
            "source_sha256": manifest["source"]["sha256"], "recipe_digest": manifest["recipe"]["digest"],
            "tree_sha256": manifest["tree"]["sha256"]}
json.dump(found, open(os.environ["BUILDER_OUTPUT"] + "/output", "w"), sort_keys=True, separators=(",", ":"))
"""


def _version_label(version: Mapping[str, Any]) -> str:
    label = str(((version.get("manifest") or {}).get("version")) or "")
    commit = str((version.get("source") or {}).get("commit") or "")
    return label if re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$", label) else commit


def source_object(source_sha256: str) -> str:
    """Where a bundled version's checked source archive is kept (1.4.3)."""
    return f"platform/sources/{source_sha256}.tar"


def build_request(bundle_id: str, config: BundleSettings, base: Mapping[str, str],
                  versions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The Cloud Build build for one bundle: fetch, one recipe run per plugin
    and runner image, one image per runner image `FROM` its base by digest,
    pushed as `<repository>/<image>:<bundle_id>`, and the manifests' digests
    as the last step's output. Every value is the deployment's or a MAC'd
    record's; nothing comes from a caller."""
    spec = {"sources_bucket": config.plugins_bucket, "mirror_bucket": config.mirror_bucket, "plugins": {}}
    for install_id, version in sorted(versions.items()):
        sha = version["source_sha256"]
        spec["plugins"][PLUGIN_OF[install_id]] = {
            "object": source_object(sha), "version": _version_label(version),
            "source": {"object": source_object(sha), "sha256": sha,
                       "commit": (version.get("source") or {}).get("commit")}}
    steps: list[dict[str, Any]] = [{"id": "fetch", "name": config.bundler, "entrypoint": "python3",
                                    "args": ["-c", _FETCH, json.dumps(spec, sort_keys=True)]}]
    recipes = "/opt/swarm-bundler/recipes"
    inputs = "/opt/swarm-bundler/inputs"
    for image in RUNNER_IMAGES:
        out = f"/workspace/out/{image}"
        lines = [f"mkdir -p {out}"]
        for install_id in sorted(versions):
            plugin = PLUGIN_OF[install_id]
            if plugin == "gstack":
                lines.append(
                    f"{recipes}/gstack/build --pins /workspace/pins.json --mirror /workspace/mirror"
                    f" --layers {inputs}/gstack-layers --measure {inputs}/gstack_packaging_measure.py"
                    f" --base-list {inputs}/gstack-base-files.txt --browser-list {inputs}/gstack-browser-files.txt"
                    f" --list {GSTACK_LISTS.get(image, 'base')} --bun install"
                    f" --bun-path /opt/swarm-bundler/bun/bun --registry {config.npm_registry}"
                    f" --out {out}/{plugin}")
            else:
                lines.append(f"{recipes}/{plugin}/build --pins /workspace/pins.json --mirror /workspace/mirror"
                             f" --validate-with /usr/local/bin/node --out {out}/{plugin}")
        copies = "".join(f"COPY {p} {PLUGIN_ROOT}/{p}/\\n" for p in sorted(PLUGIN_OF[i] for i in versions))
        lines.append(f"printf 'FROM {base[image]}\\n{copies}' > {out}/Dockerfile")
        steps.append({"id": f"recipes-{image}", "name": config.bundler, "entrypoint": "bash",
                      "args": ["-c", "set -euo pipefail\n" + "\n".join(lines)]})
        steps.append({"id": f"image-{image}", "name": "gcr.io/cloud-builders/docker",
                      "args": ["build", "--pull=false", "-t", f"{config.repository}/{image}:{bundle_id}", out]})
    steps.append({"id": "report", "name": config.bundler, "entrypoint": "python3",
                  "args": ["-c", _REPORT, json.dumps(list(RUNNER_IMAGES))]})
    return {
        "steps": steps,
        "images": [f"{config.repository}/{image}:{bundle_id}" for image in RUNNER_IMAGES],
        "serviceAccount": f"projects/{config.project}/serviceAccounts/{config.builder}",
        "timeout": BUILD_TIMEOUT,
        "tags": ["swarm-plugin-bundle", bundle_id],
        "options": {"logging": "CLOUD_LOGGING_ONLY", "requestedVerifyOption": "VERIFIED"},
    }


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
        "plugins": bundle.get("plugins"), "build_id": bundle.get("build_id"),
        "refusal": {"code": code, "sentence": ps.REFUSALS.get(code, "")} if code else None,
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


def _start(bundles: BundleStore, builds: BundleBuilds, config: BundleSettings, *, base: Mapping[str, str],
           versions: Mapping[str, Mapping[str, Any]], reason: str, by: str, install_id: str | None,
           version_id: str | None, previous_version: str | None) -> dict[str, Any]:
    """Write one `building` record, then start its one build. A refused
    start fails the record under the start's fixed code and raises it."""
    bundle_id = new_bundle_id()
    key = bundle_key(versions)
    now = bundles._now()
    bundles.create({
        "bundle_id": bundle_id, "scope": ps.PLATFORM, "tenant_id": None, "reason": reason,
        "install_id": install_id, "version_id": version_id, "previous_version": previous_version,
        "key": key, "key_sha256": key_sha256(key), "base": dict(base), "bundler": config.bundler,
        "state": STATE_BUILDING, "build_id": None, "outputs": None, "plugins": None, "refusal": None,
        "requested_by": by, "finished_at": None, "created_at": now, "created_by": by,
    })
    code = None
    build_id = None
    try:
        build_id = builds.start(build_request(bundle_id, config, base, versions))
    except GrantMissing as exc:
        code = "bundle_grant_missing"
        log.warning("plugin bundle build not started bundle_id=%s status=%s", bundle_id, exc.args[0])
    except Exception as exc:  # noqa: BLE001 -- a fixed code; the upstream's words are never passed on
        code = "bundle_build_not_started"
        log.warning("plugin bundle build not started bundle_id=%s (%s)", bundle_id, type(exc).__name__)

    def _record(whole: dict[str, Any], txn: Any) -> bool:
        if code is None:
            whole["build_id"] = build_id
        else:
            whole.update({"state": STATE_FAILED, "refusal": {"code": code},
                          "finished_at": bundles._now().isoformat()})
        return True

    stored = bundles.update(bundle_id, _record) or {}
    if code is not None:
        raise ps.PluginRefused(code, status=503, bundle_id=bundle_id)
    return stored


def _unstarted(bundle: Mapping[str, Any], now: Any) -> bool:
    """A `building` record whose start never recorded a build id, older
    than START_GRACE."""
    if bundle.get("state") != STATE_BUILDING or bundle.get("build_id"):
        return False
    created = bundle.get("created_at")
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return True
    if not isinstance(created, datetime) or not isinstance(now, datetime):
        return True
    try:
        return now - created > START_GRACE
    except TypeError:
        return True


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


def start_apply(store: ps.PluginStore, bundles: BundleStore, builds: BundleBuilds, config: BundleSettings,
                install_id: str, version_id: str, *, by: str) -> dict[str, Any]:
    """Apply `version_id` to a bundled install: start exactly one build of
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
    return _start(bundles, builds, config, base=config.base, versions=versions, reason=REASON_APPLY, by=by,
                  install_id=install_id, version_id=version_id, previous_version=install.get("current_version"))


def rebuild(store: ps.PluginStore, bundles: BundleStore, builds: BundleBuilds, config: BundleSettings,
            base: Mapping[str, Any], *, by: str) -> list[dict[str, Any]]:
    """`swarm upgrade`'s step (section 1.4.4): one build per retained key on
    the new `base` (the new release's images, already copied into this
    deployment's registry), none for a key already ready or building on it.
    Moves no `current_version`."""
    new_base = check_base(base, config.project, config.region, like=config.base)
    _building(bundles)
    on_base = {b.get("key_sha256") for b in bundles.bundles() if b.get("base") == new_base
               and b.get("state") in (STATE_BUILDING, STATE_READY)}
    started: list[dict[str, Any]] = []
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
            on_base.add(kept.get("key_sha256"))
            started.append(_start(bundles, builds, config, base=new_base, versions=versions,
                                  reason=REASON_REBUILD, by=by, install_id=None, version_id=None,
                                  previous_version=None))
    return started


# --- the finish ------------------------------------------------------------------


def _step_output(build: Mapping[str, Any]) -> Any:
    outputs = (build.get("results") or {}).get("buildStepOutputs") or []
    if not outputs or not isinstance(outputs[-1], str):
        return None
    try:
        return json.loads(base64.b64decode(outputs[-1], validate=True))
    except (ValueError, TypeError):
        return None


def read_outcome(bundle: Mapping[str, Any], build: Mapping[str, Any] | None,
                 repository: str) -> tuple[str, dict[str, Any]] | None:
    """What a build's own record says about `bundle`: None while it runs,
    ("failed", {code}) or ("ready", {outputs, plugins}). Every value read is
    checked by shape and against the record; nothing upstream is kept."""
    if build is None:
        return STATE_FAILED, {"code": "bundle_build_failed"}
    status = build.get("status")
    if status in BUILD_FAILED:
        return STATE_FAILED, {"code": "bundle_build_failed"}
    if status != BUILD_SUCCESS:
        return None
    bundle_id = bundle["bundle_id"]
    outputs: dict[str, str] = {}
    for image in (build.get("results") or {}).get("images") or ():
        name, digest = image.get("name"), image.get("digest")
        for runner in RUNNER_IMAGES:
            if name == f"{repository}/{runner}:{bundle_id}" and isinstance(digest, str) \
                    and re.match(r"^sha256:[0-9a-f]{64}$", digest):
                outputs[runner] = f"{repository}/{runner}@{digest}"
    report = _step_output(build)
    if set(outputs) != set(RUNNER_IMAGES) or not isinstance(report, Mapping):
        return STATE_FAILED, {"code": "bundle_check_failed"}
    plugins: dict[str, dict[str, str]] = {}
    for entry in bundle.get("key") or ():
        per_image = report.get(entry["install_id"])
        if not isinstance(per_image, Mapping) or set(per_image) != set(RUNNER_IMAGES):
            return STATE_FAILED, {"code": "bundle_check_failed"}
        trees = {}
        for runner in RUNNER_IMAGES:
            got = per_image[runner]
            if not isinstance(got, Mapping) or got.get("source_sha256") != entry["source_sha256"] \
                    or not all(isinstance(got.get(k), str) and ps.SHA256.match(got[k])
                               for k in ("recipe_digest", "tree_sha256")):
                return STATE_FAILED, {"code": "bundle_check_failed"}
            trees[runner] = got["tree_sha256"]
        plugins[entry["install_id"]] = {"recipe_digest": per_image[RUNNER_IMAGES[0]]["recipe_digest"],
                                        "tree_sha256": trees}
    return STATE_READY, {"outputs": outputs, "plugins": plugins}


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
            whole.update({"state": STATE_FAILED, "refusal": {"code": detail["code"]}, "finished_at": finished})
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


def finish(store: ps.PluginStore, bundles: BundleStore, builds: BundleBuilds, bundle: Mapping[str, Any],
           repository: str) -> dict[str, Any]:
    """Read a building record's build from Cloud Build and end it if it
    ended. The one path a notification and a status read both take."""
    if bundle.get("state") != STATE_BUILDING:
        return dict(bundle)
    if not bundle.get("build_id"):
        return fail_unstarted(bundles, bundle)
    outcome = read_outcome(bundle, builds.get(str(bundle["build_id"])), repository)
    if outcome is None:
        return dict(bundle)
    ended = mark_finished(store, bundles, bundle["bundle_id"], *outcome) or dict(bundle)
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
