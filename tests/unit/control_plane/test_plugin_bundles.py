"""Bundle builds in swarm-api (docs/design/managed-plugins.md sections 1.4.2, 1.4.3, 1.4.4 and 1.4.7; lane 16).

A bundled install's (`build: "image"`) Apply runs exactly one execution of
the Cloud Run job `swarm-plugin-bundle`, passing only its arguments, and
never moves `current_version`; the status read calls `executions.get` at
most once per bundle per 15 seconds and fails an hour-old build without a
call; only a bundle whose every pushed manifest and layer passed swarm-api's
own check becomes ready, in the transaction that moves `current_version`; a
failed one moves nothing and records a fixed code; Roll back moves to the
retained previous bundle at once; a bundle record whose MAC is missing or
wrong is absent; the rebuild route `swarm upgrade` calls is a platform
admin's alone; and a missing grant is the fixed code `bundle_grant_missing`,
never a 500.

Offline: FakeFirestore, an in-memory Cloud Run job and an in-memory registry
holding real OCI layers on `app.state`, so the shipped routes run with no
credentials and no network.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import secrets
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from swarm_api import pluginbundle as pb
from swarm_api import roles
from swarm_api.auth import TENANT_HEADER
from swarm_api import pluginstore as ps
from swarm_api.judgment import MAC_FIELD, settings_mac
from swarm_api.roles import RoleStore
from swarm_api.routes import plugin_bundles as routes
from swarm_api.routes import plugin_catalogue as pc

from .conftest import PROJECT, REGION, auth_header, seed_tenant
from .test_settings_mac import configure, new_key

ROOT = "root@example.com"
GSTACK = "builtin:gstack"
AUDIT = "builtin:security-audit"
REGISTRY = f"{REGION}-docker.pkg.dev/{PROJECT}"
REPOSITORY = f"{REGISTRY}/swarm-plugin-bundles"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-plugin-bundle"
#: The release's recipe digests, as its image's `.swarm-bundle.json` carry them.
RECIPES = {GSTACK: hashlib.sha256(b"recipe gstack").hexdigest(),
           AUDIT: hashlib.sha256(b"recipe security-audit").hexdigest()}
#: security-audit's pinned source: what its layer must equal file by file.
AUDIT_FILES = {"skills/security-audit/SKILL.md": b"---\nname: security-audit\n---\n",
               "skills/security-audit/validators/check.py": b"print('checked')\n",
               "LICENSE": b"MIT\n"}

# The tree digest is images/plugin-bundler/swarm-bundle's own function, so
# swarm-api's computation from the layer is held to the bundler's format.
_BUNDLER = Path(__file__).resolve().parents[3] / "images" / "plugin-bundler" / "swarm-bundle"
_loader = importlib.machinery.SourceFileLoader("swarm_bundle_for_tests", str(_BUNDLER))
swarm_bundle = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(swarm_bundle)


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tar(entries: list[tuple[str, str, Any]]) -> bytes:
    """A reproducible tar: (name, "d" | "f" | "l", data or link target)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if kind == "d":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "l":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


#: Every blob and manifest the fake registry holds, content-addressed (so
#: shared across tests): {digest: bytes} and {(image, reference): bytes}.
BLOBS: dict[str, bytes] = {}
MANIFESTS: dict[tuple[str, str], bytes] = {}


#: A release image's runtime config: what every step on it runs with.
RELEASE_RUNTIME = {"User": "10001", "WorkingDir": "/workspace", "Entrypoint": ["/usr/local/bin/swarm-worker"],
                   "Cmd": ["run"], "Env": ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/agent"],
                   "Labels": {"org.opencontainers.image.title": "agent runtime"}, "StopSignal": "SIGTERM"}


def _put_image(image: str, tag: str | None, layers: list[bytes], *, diff_ids: list[str] | None = None,
               layer_digests: list[str] | None = None, config: Mapping[str, Any] | None = None) -> str:
    """Push an image of `layers` (raw tars, gzipped here); its digest. With
    no `config`, a release image's: its runtime config, one history entry per
    layer, and the layers' diff ids."""
    entries = []
    for raw in layers:
        packed = gzip.compress(raw, mtime=0)
        BLOBS[_sha(packed)] = packed
        entries.append({"mediaType": pb.OCI_LAYER_GZIP, "digest": _sha(packed), "size": len(packed)})
    if layer_digests is not None:
        for entry, digest in zip(entries, layer_digests):
            entry["digest"] = digest
    if config is None:
        config = {"architecture": "amd64", "os": "linux", "created": "2026-11-01T00:00:00Z",
                  "config": RELEASE_RUNTIME,
                  "rootfs": {"type": "layers", "diff_ids": diff_ids or [_sha(raw) for raw in layers]},
                  "history": [{"created": "2026-11-01T00:00:00Z", "created_by": f"release step {n}"}
                              for n in range(len(layers))]}
    config = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
    BLOBS[_sha(config)] = config
    manifest = json.dumps({"schemaVersion": 2, "mediaType": pb.OCI_MANIFEST,
                           "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                      "digest": _sha(config), "size": len(config)},
                           "layers": entries}).encode()
    MANIFESTS[(image, _sha(manifest))] = manifest
    if tag:
        MANIFESTS[(image, tag)] = manifest
    return _sha(manifest)


def _release_layers(label: str, image: str) -> list[bytes]:
    plugins = [("opt", "d", None), ("opt/swarm", "d", None), ("opt/swarm/plugins", "d", None)]
    for install_id, name in sorted(pb.PLUGIN_OF.items()):
        plugins += [(f"opt/swarm/plugins/{name}", "d", None),
                    (f"opt/swarm/plugins/{name}/.swarm-bundle.json", "f",
                     json.dumps({"install_id": install_id, "recipe": {"digest": RECIPES[install_id]}}).encode())]
    return [_tar([("etc", "d", None), ("etc/os-release", "f", f"{label} {image}".encode())]), _tar(plugins)]


def _base(label: str) -> dict[str, str]:
    """A release's runner images in this deployment's registry, pushed."""
    base = {}
    for image in pb.RUNNER_IMAGES:
        ref = f"{REGISTRY}/swarm/{image}"
        base[image] = f"{ref}@{_put_image(ref, None, _release_layers(label, image))}"
    return base


LIVE = _base("live")


class FakeRegistry:
    """The registry API: manifests by tag or digest, blobs by digest, each
    read recorded; a blob is served through pluginbundle's own capped,
    digest-checking reader."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def manifest(self, image: str, reference: str) -> bytes | None:
        self.reads.append((image, reference))
        return MANIFESTS.get((image, reference))

    def blob(self, image: str, digest: str, cap: int):
        self.reads.append((image, digest))
        if digest not in BLOBS:
            raise pb.CheckFailed("blob_missing")
        return io.BufferedReader(pb._Capped([BLOBS[digest]], digest, cap))


class FakeSources:
    """The plugins bucket's pinned source archives, by source sha256."""

    def __init__(self) -> None:
        self.archives: dict[str, bytes] = {}

    def source(self, source_sha256: str):
        found = self.archives.get(source_sha256)
        return None if found is None else io.BytesIO(found)


def _audit_archive(files: Mapping[str, bytes]) -> bytes:
    top = "example-security-audit-0123abc"
    entries = [(top, "d", None), (f"{top}/README.md", "f", b"readme\n")]
    entries += [(f"{top}/{rel}", "f", data) for rel, data in sorted(files.items())]
    return _tar(entries)


class FakeJobs:
    """The Cloud Run job: each run recorded with its job and arguments, each
    execution's state set by a test, each `executions.get` counted."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, list[str]]] = []
        self.executions: dict[str, dict[str, Any]] = {}
        self.gets: list[str] = []
        self.refuse: str | None = None

    def run(self, job: str, args: list[str]) -> str:
        if self.refuse == "grant":
            raise pb.GrantMissing(403)
        if self.refuse == "other":
            raise pb.BuildNotStarted(500)
        name = f"{job}/executions/swarm-plugin-bundle-{secrets.token_hex(3)}"
        self.runs.append((job, list(args)))
        self.executions[name] = {"name": name, "taskCount": 1}
        return name

    def get(self, execution: str) -> Mapping[str, Any] | None:
        self.gets.append(execution)
        return self.executions.get(execution)


def _plugin_layer(tmp: Path, name: str, install_id: str, source_sha256: str, recipe: str, *,
                  files: Mapping[str, bytes], extra: list[tuple[str, str, Any]] = (),
                  tree: str | None = None) -> bytes:
    """What the job appends for one plugin: its root, the opaque marker, the
    tree and `.swarm-bundle.json`, whose tree digest is swarm-bundle's."""
    folder = tmp / name
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
        (folder / rel).chmod(0o644)
    for path in [folder, *folder.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
    digest = tree or swarm_bundle.tree_digest([folder])[0]
    manifest = json.dumps({"schema": 1, "plugin": name, "install_id": install_id,
                           "source": {"sha256": source_sha256}, "recipe": {"digest": recipe},
                           "tree": {"sha256": digest}}).encode()
    root = f"opt/swarm/plugins/{name}"
    entries: list[tuple[str, str, Any]] = [(root, "d", None), (f"{root}/{pb.OPAQUE}", "f", b"")]
    folders = sorted({str(Path(rel).parent) for rel in files} - {"."})
    for rel in folders:
        parts = Path(rel).parts
        for n in range(1, len(parts) + 1):
            entry = (f"{root}/{'/'.join(parts[:n])}", "d", None)
            if entry not in entries:
                entries.append(entry)
    entries += [(f"{root}/{rel}", "f", data) for rel, data in sorted(files.items())]
    entries.append((f"{root}/.swarm-bundle.json", "f", manifest))
    return _tar(entries + list(extra))


def _push_bundle(w, bundle_id: str, *, foreign_base: bool = False, foreign_diff_ids: bool = False,
                 extra: list = (), tree: str | None = None,
                 audit: Mapping[str, bytes] | None = None, source: Mapping[str, str] | None = None,
                 spoil_config: Callable[[dict[str, Any]], None] | None = None) -> None:
    """Do what the job does for `bundle_id` from its recorded arguments, then
    end its execution well. Each keyword spoils one thing a check refuses.
    The pushed config is the base's as images/plugin-bundler/swarm-bundle's
    `append_layers` writes it: each plugin layer's diff id appended to
    `rootfs.diff_ids` and one fixed history entry per plugin, nothing else."""
    record = _built(w, bundle_id)
    job, args = next((j, a) for j, a in w["jobs"].runs if a[0] == bundle_id)
    tmp = w["tmp"] / bundle_id
    for image in pb.RUNNER_IMAGES:
        base_ref = record["base"][image]
        base_image, _, base_digest = base_ref.partition("@")
        base = json.loads(MANIFESTS[(base_image, base_digest)])
        base_raw = [gzip.decompress(BLOBS[layer["digest"]]) for layer in base["layers"]]
        layers = list(base_raw)
        config = json.loads(BLOBS[base["config"]["digest"]])
        history = list(config.get("history", []))
        for entry in sorted(record["key"], key=lambda e: pb.PLUGIN_OF[e["install_id"]]):
            install_id = entry["install_id"]
            name = pb.PLUGIN_OF[install_id]
            files = dict(audit or AUDIT_FILES) if install_id == AUDIT else {
                "SKILL.md": f"gstack {entry['version_id']}\n".encode(), "bin/browse": b"\x7fELF"}
            layers.append(_plugin_layer(tmp / image, name, install_id,
                                        (source or {}).get(install_id, entry["source_sha256"]),
                                        record["recipes"][install_id], files=files,
                                        extra=list(extra) if install_id == GSTACK else [], tree=tree))
            config["rootfs"]["diff_ids"].append(_sha(layers[-1]))
            history.append({"created": "1970-01-01T00:00:00Z",
                            "created_by": f"swarm-bundle job: /opt/swarm/plugins/{name}/"})
        config["history"] = history
        digests = None
        if foreign_base:
            other = gzip.compress(_tar([("etc", "d", None), ("etc/os-release", "f", b"not the release")]), mtime=0)
            BLOBS[_sha(other)] = other
            digests = [_sha(other)]
        if foreign_diff_ids:
            # The release's base layer blobs, but a config that says the
            # first of them unpacks to something else.
            config["rootfs"]["diff_ids"][0] = _digest("not the release")
        if spoil_config is not None:
            spoil_config(config)
        _put_image(f"{REPOSITORY}/{image}", bundle_id, layers, layer_digests=digests, config=config)
    for name, execution in w["jobs"].executions.items():
        if name == record["execution"]:
            execution.update({"completionTime": "2026-11-11T00:00:00Z", "succeededCount": 1})
    _tick(w)


def _fail(w, bundle_id: str) -> None:
    execution = w["jobs"].executions[_built(w, bundle_id)["execution"]]
    execution.update({"completionTime": "2026-11-11T00:00:00Z", "failedCount": 1,
                      "conditions": [{"type": "Completed", "message": "upstream words"}]})
    _tick(w)


def _tick(w, seconds: int = 16) -> None:
    w["clock"][0] += timedelta(seconds=seconds)


def _version(install_id: str, version_id: str, label: str) -> dict[str, Any]:
    return {
        "install_id": install_id, "version_id": version_id, "scope": "platform", "tenant_id": None,
        "intake_id": secrets.token_hex(16),
        "source": {"kind": "github", "repository": "example-org/" + install_id.split(":")[1],
                   "ref": label, "commit": hashlib.sha1(label.encode()).hexdigest()},
        "source_sha256": hashlib.sha256(("source" + label).encode()).hexdigest(), "source_bytes": 10,
        "runnable_sha256": hashlib.sha256(("runnable" + label).encode()).hexdigest(), "runnable_bytes": 10,
        "object": "platform/x.tar", "files": 3, "unpacked_bytes": 100,
        "manifest": {"name": install_id.split(":")[1], "version": label, "description": "d", "user_config": []},
        "manifest_source": "plugin",
        "components": {"skills": [{"name": "s"}], "commands": [], "agents": [], "hooks": [], "mcp_servers": []},
        "overlay": None, "overlay_requested": None,
        "requires": {"suggested": {"hosts": [], "runtimes": []}, "declared": None},
    }


def _set(db: Any, path: str, kind: str, fields: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    stored = dict(fields)
    stored[MAC_FIELD] = ps.record_mac(kind, stored, keys)
    collection, _, rest = path.partition("/")
    ref = db.collection(collection)
    parts = rest.split("/")
    ref = ref.document(parts[0])
    if len(parts) > 1:
        ref = ref.collection(parts[1]).document(parts[2])
    ref.set(stored)
    return stored


@pytest.fixture
def world(client, api_context, db, monkeypatch, tmp_path):
    configure(api_context, settings_key=new_key())
    keys = settings_mac(api_context.settings).keys
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(api_context, "now", lambda: clock[0])
    jobs, registry, sources = FakeJobs(), FakeRegistry(), FakeSources()
    client.app.state.plugin_bundle_jobs = jobs
    client.app.state.plugin_bundle_registry = registry
    client.app.state.plugin_bundle_sources = sources
    monkeypatch.setenv(pb.REPOSITORY_ENV, REPOSITORY)
    monkeypatch.setenv(pb.JOB_ENV, "swarm-plugin-bundle")
    monkeypatch.setenv(pb.BASE_ENV, json.dumps(LIVE))
    monkeypatch.setenv(pb.PLUGINS_BUCKET_ENV, "plugins-bucket-test")
    versions = {}
    for install_id, labels in ((GSTACK, ("1.0", "2.0")), (AUDIT, ("a1",))):
        for n, label in enumerate(labels):
            version_id = f"v-{'a' if install_id == GSTACK else 'b'}{n:011d}"
            versions[(install_id, label)] = _set(db, f"plugin_installs/{install_id}/versions/{version_id}",
                                                  "version", _version(install_id, version_id, label), keys)
        current = versions[(install_id, labels[0])]["version_id"]
        _set(db, f"plugin_installs/{install_id}", "install", {
            "install_id": install_id, "scope": "platform", "tenant_id": None, "name": install_id.split(":")[1],
            "display_name": install_id.split(":")[1], "source": {"kind": "github"}, "current_version": current,
            "built_in": True, "build": "image", "state": "active", "removed_at": None}, keys)
    sources.archives[versions[(AUDIT, "a1")]["source_sha256"]] = _audit_archive(AUDIT_FILES)
    # The release image is the bundle of the first versions (section 1.4.2).
    first = [{"install_id": i, "version_id": versions[(i, label)]["version_id"],
              "source_sha256": versions[(i, label)]["source_sha256"]} for i, label in ((GSTACK, "1.0"), (AUDIT, "a1"))]
    _set(db, "plugin_bundles/b-000000000001", "bundle", {
        "bundle_id": "b-000000000001", "scope": "platform", "tenant_id": None, "reason": "apply",
        "install_id": None, "version_id": None, "previous_version": None, "key": first,
        "key_sha256": pb.key_sha256(first), "base": LIVE, "recipes": None, "job": None, "state": "ready",
        "execution": None, "execution_read_at": None, "outputs": LIVE, "plugins": {}, "refusal": None,
        "requested_by": "release", "finished_at": "2026-11-01T00:00:00+00:00"}, keys)
    return {"client": client, "db": db, "ctx": api_context, "jobs": jobs, "registry": registry,
            "sources": sources, "keys": keys, "versions": versions, "clock": clock, "tmp": tmp_path}


def _store(w) -> ps.PluginStore:
    return ps.PluginStore(w["db"], now=w["ctx"].now, mac=settings_mac(w["ctx"].settings))


def _current(w, install_id: str = GSTACK) -> str:
    return _store(w).install(install_id)["current_version"]


def _diff_sha(w, install_id: str, version_id: str) -> str:
    store = _store(w)
    install = store.install(install_id)
    return ps.diff_digest(ps.version_diff(store.current(install) or {}, store.version(install_id, version_id)))


def _apply(w, version_id: str, *, user: str = "root", path: str = "apply"):
    body = {"version_id": version_id, "diff_sha256": _diff_sha(w, GSTACK, version_id)}
    return w["client"].post(f"{routes.BASE}/installs/{GSTACK}/{path}", json=body, headers=auth_header(user))


def _status(w, bundle_id: str):
    return w["client"].get(f"{routes.BASE}/{bundle_id}", headers=auth_header("root"))


def _built(w, bundle_id: str) -> dict[str, Any]:
    return pb.BundleStore(_store(w), w["db"], now=w["ctx"].now).bundle(bundle_id)


# --- Apply ------------------------------------------------------------------------


def test_an_apply_on_a_bundled_install_starts_exactly_one_build_and_leaves_current_version(world):
    w = world
    before = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    answer = _apply(w, v2)
    assert answer.status_code == 202, answer.text
    assert answer.json()["state"] == "building"
    assert len(w["jobs"].runs) == 1
    assert _current(w) == before
    # A second Apply while it builds starts nothing.
    again = _apply(w, v2)
    assert again.status_code == 409
    assert again.json()["code"] == "bundle_building"
    assert len(w["jobs"].runs) == 1
    assert _current(w) == before


def test_an_apply_on_the_other_bundled_install_waits_for_the_running_build(world):
    w = world
    _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"])
    store = _store(w)
    bundles = pb.BundleStore(store, w["db"], now=w["ctx"].now)
    config = pb.bundle_settings(PROJECT, REGION)
    other = _set(w["db"], f"plugin_installs/{AUDIT}/versions/v-b00000000009", "version",
                 _version(AUDIT, "v-b00000000009", "a9"), w["keys"])
    with pytest.raises(ps.PluginRefused) as refused:
        pb.start_apply(store, bundles, w["jobs"], w["registry"], config, AUDIT, other["version_id"], by=ROOT)
    assert refused.value.code == "bundle_building"
    assert len(w["jobs"].runs) == 1


def test_two_applies_at_once_never_make_current_a_pair_no_ready_bundle_carries(world, monkeypatch):
    # Both Applies read "nothing building" before either writes its record:
    # the guard is a plain read, so the finish must check the whole key.
    w = world
    monkeypatch.setattr(pb, "_refuse_while_building", lambda bundles, install_id: None)
    store = _store(w)
    bundles = pb.BundleStore(store, w["db"], now=w["ctx"].now)
    config = pb.bundle_settings(PROJECT, REGION)
    a1 = _current(w, AUDIT)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    a9_version = _set(w["db"], f"plugin_installs/{AUDIT}/versions/v-b00000000009", "version",
                      _version(AUDIT, "v-b00000000009", "a9"), w["keys"])
    a9 = a9_version["version_id"]
    w["sources"].archives[a9_version["source_sha256"]] = _audit_archive(AUDIT_FILES)
    first = pb.start_apply(store, bundles, w["jobs"], w["registry"], config, GSTACK, v2, by=ROOT)
    second = pb.start_apply(store, bundles, w["jobs"], w["registry"], config, AUDIT, a9, by=ROOT)
    assert len(w["jobs"].runs) == 2
    for started in (first, second):
        record = _built(w, started["bundle_id"])
        _push_bundle(w, record["bundle_id"])
    assert _status(w, first["bundle_id"]).json()["state"] == "ready"
    answer = _status(w, second["bundle_id"]).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_key_stale"
    assert (_current(w), _current(w, AUDIT)) == (v2, a1)


def test_a_building_record_whose_start_was_lost_is_failed_and_holds_nothing(world):
    w = world
    bundles = pb.BundleStore(_store(w), w["db"], now=w["ctx"].now)
    lost = {"bundle_id": "b-0000000000aa", "scope": "platform", "tenant_id": None, "reason": "apply",
            "install_id": GSTACK, "version_id": w["versions"][(GSTACK, "2.0")]["version_id"],
            "previous_version": _current(w), "key": [], "key_sha256": pb.key_sha256([]), "base": LIVE,
            "recipes": RECIPES, "job": JOB, "state": "building", "execution": None, "execution_read_at": None, "outputs": None, "plugins": None,
            "refusal": None, "requested_by": ROOT, "finished_at": None, "created_by": ROOT}
    # Within the grace a start may still be recording its id: it holds.
    bundles.create({**lost, "created_at": w["ctx"].now()})
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    assert _apply(w, v2).json()["code"] == "bundle_building"
    # Past it, the start was lost: the record fails and Apply goes on.
    bundles.create({**lost, "created_at": w["ctx"].now() - pb.START_GRACE - timedelta(minutes=1)})
    answer = _apply(w, v2)
    assert answer.status_code == 202, answer.text
    assert _built(w, "b-0000000000aa")["state"] == "failed"
    assert _built(w, "b-0000000000aa")["refusal"] == {"code": "bundle_build_not_started"}
    assert len(w["jobs"].runs) == 1


def test_a_start_runs_exactly_one_execution_of_the_job_with_only_its_arguments_in_order(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]
    a1 = w["versions"][(AUDIT, "a1")]
    bundle_id = _apply(w, v2["version_id"]).json()["bundle_id"]
    assert len(w["jobs"].runs) == 1
    job, args = w["jobs"].runs[0]
    assert job == JOB
    # The bundle id; per plugin, by name, its name, source sha256 and recipe
    # digest; per runner image its name and base digest. Nothing else.
    expected = [bundle_id, "gstack", v2["source_sha256"], RECIPES[GSTACK],
                "security-audit", a1["source_sha256"], RECIPES[AUDIT]]
    for image in pb.RUNNER_IMAGES:
        expected += [image, LIVE[image].partition("@")[2]]
    assert args == expected
    record = _built(w, bundle_id)
    assert record["execution"].startswith(JOB + "/executions/")
    assert record["recipes"] == RECIPES


def test_the_job_call_overrides_only_the_one_containers_arguments(monkeypatch):
    sent: list[tuple[str, Any]] = []

    class Answer:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"metadata": {"name": f"{JOB}/executions/swarm-plugin-bundle-abc12"}}

    class Session:
        def post(self, url: str, json: Any, timeout: int) -> Answer:
            sent.append((url, json))
            return Answer()

    jobs = pb.CloudRunBundleJobs()
    monkeypatch.setattr(jobs, "_session", Session())
    name = jobs.run(JOB, ["b-0123456789ab", "gstack"])
    assert name == f"{JOB}/executions/swarm-plugin-bundle-abc12"
    assert sent == [(f"https://run.googleapis.com/v2/{JOB}:run",
                     {"overrides": {"containerOverrides": [{"args": ["b-0123456789ab", "gstack"]}]}})]


def test_no_cloud_build_client_is_imported_anywhere_in_swarm_api():
    # No Cloud Build library is imported, its API is never called and its
    # builder images are never named: a bundle build is a Cloud Run job.
    api = Path(pb.__file__).resolve().parent
    for path in api.rglob("*.py"):
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any("cloudbuild" in n or "cloud_build" in n for n in names), path.relative_to(api)
        for word in ("cloudbuild.googleapis.com", "gcr.io/cloud-builders", "CloudBuild"):
            assert word not in text, f"{path.relative_to(api)} names {word}"


def test_the_catalogues_own_apply_starts_the_bundle_build_and_leaves_current_version(world):
    # The owner's pick on Q21: the catalogue's Apply delegates a bundled
    # install to the bundle build, the same one start as the bundle route.
    w = world
    before = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    answer = w["client"].post(f"{pc.PLATFORM_BASE}/installs/{GSTACK}/apply",
                              json={"version_id": v2, "diff_sha256": _diff_sha(w, GSTACK, v2)},
                              headers=auth_header("root"))
    assert answer.status_code == 202, answer.text
    assert answer.json()["state"] == "building"
    assert len(w["jobs"].runs) == 1
    assert _built(w, answer.json()["bundle_id"])["version_id"] == v2
    assert _current(w) == before
    # The store under it still refuses to move a bundled install directly.
    with pytest.raises(ps.PluginRefused) as refused:
        _store(w).apply(GSTACK, v2, by=ROOT)
    assert refused.value.code == "install_bundled"
    assert _current(w) == before


def test_the_catalogues_own_apply_on_a_bundled_install_checks_the_diff_it_was_shown(world):
    w = world
    before = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    answer = w["client"].post(f"{pc.PLATFORM_BASE}/installs/{GSTACK}/apply",
                              json={"version_id": v2, "diff_sha256": "0" * 64},
                              headers=auth_header("root"))
    assert answer.status_code == 409
    assert answer.json()["code"] == "diff_changed"
    assert w["jobs"].runs == []
    assert _current(w) == before


def test_the_catalogues_own_apply_on_a_bundled_install_is_a_platform_admins_alone(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    answer = w["client"].post(f"{pc.PLATFORM_BASE}/installs/{GSTACK}/apply",
                              json={"version_id": v2, "diff_sha256": _diff_sha(w, GSTACK, v2)},
                              headers=auth_header("carol"))
    assert answer.status_code in (401, 403)
    assert w["jobs"].runs == []


# --- the finish ---------------------------------------------------------------------


def test_a_ready_finish_moves_current_version(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    assert _status(w, bundle_id).json()["state"] == "building"
    _push_bundle(w, record["bundle_id"])
    answer = _status(w, bundle_id)
    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "ready"
    assert set(answer.json()["outputs"]) == set(pb.RUNNER_IMAGES)
    assert _current(w) == v2


def test_a_failed_finish_leaves_current_version_and_records_why(world):
    w = world
    before = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    _fail(w, bundle_id)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_build_failed"
    assert "upstream words" not in json.dumps(answer)
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_build_failed"}
    assert _current(w) == before


def test_a_build_whose_output_is_not_the_bundle_asked_for_fails_its_check(world):
    w = world
    before = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    _push_bundle(w, record["bundle_id"], source={GSTACK: "0" * 64})
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_check_failed"
    assert _current(w) == before


def test_a_second_read_inside_15_seconds_makes_no_execution_call(world):
    w = world
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    assert _status(w, bundle_id).json()["state"] == "building"
    assert len(w["jobs"].gets) == 1
    # Pages and reloads inside the window read the record alone.
    for _ in range(5):
        _tick(w, 2)
        assert _status(w, bundle_id).json()["state"] == "building"
    assert len(w["jobs"].gets) == 1
    _tick(w, 6)
    assert _status(w, bundle_id).json()["state"] == "building"
    assert len(w["jobs"].gets) == 2
    assert _built(w, bundle_id)["execution_read_at"] == w["clock"][0].isoformat()


def test_an_hour_old_building_record_is_failed_without_a_call(world):
    w = world
    before = _current(w)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _tick(w, int(pb.BUILD_DEADLINE.total_seconds()) + 1)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_build_failed"
    assert w["jobs"].gets == []
    assert _current(w) == before


def test_a_passing_check_moves_current_version_and_records_the_digest_it_checked(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    _push_bundle(w, bundle_id)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "ready", answer
    assert _current(w) == v2
    for image in pb.RUNNER_IMAGES:
        pushed = MANIFESTS[(f"{REPOSITORY}/{image}", bundle_id)]
        assert answer["outputs"][image] == f"{REPOSITORY}/{image}@{_sha(pushed)}"
        assert (f"{REPOSITORY}/{image}", bundle_id) in w["registry"].reads
    plugins = _built(w, bundle_id)["plugins"]
    assert plugins[AUDIT]["recipe_digest"] == RECIPES[AUDIT]
    assert set(plugins[GSTACK]["tree_sha256"]) == set(pb.RUNNER_IMAGES)


@pytest.mark.parametrize("spoil, check", [
    ({"foreign_base": True}, "base_layers"),
    ({"extra": [("opt/swarm/plugins/security-audit/skills/x", "f", b"x")]}, "layer_path"),
    ({"extra": [("opt/swarm/elsewhere", "f", b"x")]}, "layer_path"),
    ({"extra": [("opt/swarm/plugins/gstack/../../../etc/passwd", "f", b"x")]}, "layer_path"),
    ({"extra": [("opt/swarm/plugins/gstack/.wh.SKILL.md", "f", b"")]}, "layer_whiteout"),
    ({"extra": [("opt/swarm/plugins/gstack/out", "l", "/etc/passwd")]}, "layer_link"),
    ({"tree": "0" * 64}, "tree_sha256"),
    ({"audit": {**AUDIT_FILES, "skills/security-audit/validators/check.py": b"print('always pass')\n"}},
     "security_audit_files"),
    ({"audit": {**AUDIT_FILES, "skills/security-audit/extra.py": b"x\n"}}, "security_audit_files"),
    ({"source": {GSTACK: "0" * 64}}, "bundle_manifest"),
])
def test_each_failed_layer_check_leaves_current_version_and_records_its_code(world, spoil, check):
    w = world
    before = _current(w)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, **spoil)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_check_failed"
    assert answer["refusal"]["check"] == check
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_check_failed", "check": check}
    assert _built(w, bundle_id)["outputs"] is None
    assert _current(w) == before


def _set_runtime(name: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def spoil(config: dict[str, Any]) -> None:
        config["config"] = {**config["config"], name: value}
    return spoil


def _add_env(config: dict[str, Any]) -> None:
    config["config"] = {**config["config"], "Env": [*config["config"]["Env"], "LD_PRELOAD=/tmp/x.so"]}


def _add_label(config: dict[str, Any]) -> None:
    config["config"] = {**config["config"], "Labels": {**config["config"]["Labels"], "added": "yes"}}


def _extra_diff_id(config: dict[str, Any]) -> None:
    config["rootfs"]["diff_ids"].append(_digest("one more layer"))


def _drop_history(config: dict[str, Any]) -> None:
    config["history"].pop()


def _swap_diff_ids(config: dict[str, Any]) -> None:
    ids = config["rootfs"]["diff_ids"]
    ids[-2], ids[-1] = ids[-1], ids[-2]


@pytest.mark.parametrize("spoil, field", [
    (_set_runtime("Entrypoint", ["/bin/sh", "-c"]), "config.Entrypoint"),
    (_set_runtime("Cmd", ["sh"]), "config.Cmd"),
    (_add_env, "config.Env"),
    (_set_runtime("User", "0"), "config.User"),
    (_set_runtime("WorkingDir", "/"), "config.WorkingDir"),
    (_add_label, "config.Labels"),
    (_extra_diff_id, "rootfs.diff_ids"),
    (_drop_history, "history"),
    (_swap_diff_ids, "rootfs.diff_ids"),
])
def test_a_pushed_config_other_than_the_bases_is_refused_and_moves_nothing(world, spoil, field):
    """The pushed config is the release's plus the plugin layers' diff ids
    and the job's history entries, nothing else (1.4: an Entrypoint or Env
    the job changed would run in every step of every tenant)."""
    w = world
    before = _current(w)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, spoil_config=spoil)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_config_changed"
    assert answer["refusal"]["check"] == field
    assert answer["refusal"]["sentence"] == ps.REFUSALS["bundle_config_changed"]
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_config_changed", "check": field}
    assert _built(w, bundle_id)["outputs"] is None
    assert _current(w) == before


def test_a_config_whose_base_diff_id_differs_is_refused_as_changed(world):
    w = world
    before = _current(w)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, foreign_diff_ids=True)
    assert _status(w, bundle_id).json()["state"] == "failed"
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_config_changed", "check": "rootfs.diff_ids"}
    assert _current(w) == before


def test_a_refused_config_records_a_field_name_never_a_value(world):
    """A field the job added under a name of its own is recorded `other`:
    neither its name nor its value reaches the record or the answer."""
    w = world
    marker = "x" + secrets.token_hex(8)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, spoil_config=lambda config: config.update({marker: marker}))
    answer = _status(w, bundle_id).json()
    assert answer["refusal"] == {**answer["refusal"], "code": "bundle_config_changed", "check": "other"}
    assert marker not in json.dumps(answer) and marker not in json.dumps(_built(w, bundle_id), default=str)


def test_a_config_that_is_the_bases_plus_the_layers_and_history_the_job_writes_passes(world):
    """The pushed config as the job writes it (`_push_bundle`, after
    swarm-bundle's `append_layers`) becomes ready; so does the same check
    on a release config with no history of its own."""
    w = world
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id)
    assert _status(w, bundle_id).json()["state"] == "ready"
    base = {"rootfs": {"type": "layers", "diff_ids": [_digest("base")]}, "config": dict(RELEASE_RUNTIME)}
    pushed = {"rootfs": {"type": "layers", "diff_ids": [_digest("base"), _digest("g"), _digest("s")]},
              "config": dict(RELEASE_RUNTIME),
              "history": [{"created": "1970-01-01T00:00:00Z",
                           "created_by": "swarm-bundle job: /opt/swarm/plugins/gstack/"},
                          {"created": "1970-01-01T00:00:00Z",
                           "created_by": "swarm-bundle job: /opt/swarm/plugins/security-audit/"}]}
    pb.check_config(base, pushed, ["gstack", "security-audit"])


def test_a_value_of_another_json_type_is_a_change():
    base = {"rootfs": {"type": "layers", "diff_ids": []}, "config": {"Tty": False}}
    pushed = {"rootfs": {"type": "layers", "diff_ids": []}, "config": {"Tty": 0}}
    with pytest.raises(pb.CheckFailed) as failed:
        pb.check_config(base, pushed, [])
    assert (failed.value.code, failed.value.check) == ("bundle_config_changed", "config.Tty")


def test_the_job_sets_no_config_created_so_the_releases_must_stay(world):
    w = world
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, spoil_config=lambda config: config.update({"created": "2026-11-11T00:00:00Z"}))
    assert _status(w, bundle_id).json()["state"] == "failed"
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_config_changed", "check": "created"}


@pytest.mark.parametrize("cap, value, extra, check", [
    # 64 KiB of one byte, a few hundred bytes gzipped, past a 32 KiB cap:
    # the total unpacked is capped, not only each file or the transfer.
    ("LAYER_UNPACKED_CAP", 32 * 1024, [("opt/swarm/plugins/gstack/pad", "f", b"\0" * (64 * 1024))],
     "layer_too_large"),
    ("LAYER_MEMBERS_CAP", 20, [(f"opt/swarm/plugins/gstack/m{n}", "f", b"") for n in range(30)],
     "layer_too_many_members"),
])
def test_a_layer_past_a_read_cap_is_refused_with_its_code_and_moves_nothing(world, monkeypatch, cap, value,
                                                                             extra, check):
    w = world
    monkeypatch.setattr(pb, cap, value)
    before = _current(w)
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    _push_bundle(w, bundle_id, extra=extra)
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_check_failed", "check": check}
    assert answer["refusal"]["check"] == check
    assert _built(w, bundle_id)["outputs"] is None
    assert _current(w) == before


def test_a_layer_at_the_read_caps_is_read(world, monkeypatch):
    """The caps refuse only past them: each layer is read with both caps set
    to exactly what it holds, unpacked bytes and members, and passes."""
    w = world
    bundle_id = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"]).json()["bundle_id"]
    seen: list[tuple[int, int]] = []
    real = pb.read_layer

    def at_the_caps(handle, plugin, media_type, *rest):
        data = handle.read()
        raw = gzip.decompress(data)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            count = len(tar.getmembers())
        seen.append((len(raw), count))
        monkeypatch.setattr(pb, "LAYER_UNPACKED_CAP", len(raw))
        monkeypatch.setattr(pb, "LAYER_MEMBERS_CAP", count)
        return real(io.BytesIO(data), plugin, media_type, *rest)

    monkeypatch.setattr(pb, "read_layer", at_the_caps)
    _push_bundle(w, bundle_id)
    assert _status(w, bundle_id).json()["state"] == "ready"
    assert len(seen) == len(pb.RUNNER_IMAGES) * 2


# --- Roll back --------------------------------------------------------------------------


def test_roll_back_moves_to_the_retained_previous_bundle(world):
    w = world
    v1 = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    _push_bundle(w, record["bundle_id"])
    _status(w, bundle_id)
    assert _current(w) == v2
    kept = pb.retention(pb.BundleStore(_store(w), w["db"], now=w["ctx"].now), named=set())
    assert {b["bundle_id"] for b in kept["keep"]} == {bundle_id, "b-000000000001"}
    answer = _apply(w, v1, path="roll-back")
    assert answer.status_code == 200, answer.text
    assert answer.json()["bundle"]["bundle_id"] == "b-000000000001"
    assert _current(w) == v1
    assert len(w["jobs"].runs) == 1


def test_a_ready_finish_retires_what_retention_no_longer_keeps(world):
    w = world
    w["jobs"].refuse = "grant"
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    failed = _apply(w, v2).json()["detail"]["bundle_id"]
    w["jobs"].refuse = None
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    _push_bundle(w, record["bundle_id"])
    assert _status(w, bundle_id).json()["state"] == "ready"
    assert _built(w, failed)["state"] == "retired"
    # Current and Roll back's previous stay.
    assert _built(w, bundle_id)["state"] == "ready"
    assert _built(w, "b-000000000001")["state"] == "ready"


def test_roll_back_refuses_a_bundle_whose_other_install_has_moved(world):
    w = world
    store = _store(w)
    bundles = pb.BundleStore(store, w["db"], now=w["ctx"].now)
    ready = bundles.bundle("b-000000000001")
    a9 = _set(w["db"], f"plugin_installs/{AUDIT}/versions/v-b00000000009", "version",
              _version(AUDIT, "v-b00000000009", "a9"), w["keys"])["version_id"]
    install = dict(store.install(AUDIT))
    install["current_version"] = a9
    install.pop(MAC_FIELD, None)
    _set(w["db"], f"plugin_installs/{AUDIT}", "install", install, w["keys"])
    gstack = store.install(GSTACK)
    with pytest.raises(ps.PluginRefused) as refused:
        pb._move_to(store, bundles, ready, gstack, gstack["current_version"], by=ROOT)
    assert refused.value.code == "bundle_key_stale"
    assert _current(w, AUDIT) == a9


def test_roll_back_with_no_retained_bundle_is_refused_and_moves_nothing(world):
    w = world
    w["db"].docs.pop("plugin_bundles/b-000000000001")
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    _push_bundle(w, record["bundle_id"])
    _status(w, bundle_id)
    answer = _apply(w, w["versions"][(GSTACK, "1.0")]["version_id"], path="roll-back")
    assert answer.status_code == 409
    assert answer.json()["code"] == "bundle_not_retained"
    assert _current(w) == v2


# --- the MAC ----------------------------------------------------------------------------


@pytest.mark.parametrize("forge", ["missing", "wrong"])
def test_a_bundle_record_with_a_missing_or_wrong_mac_is_refused(world, forge):
    w = world
    doc = w["db"].docs["plugin_bundles/b-000000000001"]
    if forge == "missing":
        doc.pop(MAC_FIELD)
    else:
        doc["outputs"] = _base("forged")
    assert _built(w, "b-000000000001") is None
    assert _status(w, "b-000000000001").status_code == 404
    # Nor can a forged ready record move current_version.
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    _push_bundle(w, record["bundle_id"])
    _status(w, bundle_id)
    answer = _apply(w, w["versions"][(GSTACK, "1.0")]["version_id"], path="roll-back")
    assert answer.status_code == 409
    assert answer.json()["code"] == "bundle_not_retained"
    assert _current(w) == v2


def test_a_bundle_record_is_signed_in_the_contracts_bundle_kind(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    stored = dict(w["db"].docs[f"plugin_bundles/{bundle_id}"])
    stored["bundle_id"] = bundle_id
    assert ps.record_verifies("bundle", stored, w["keys"])
    assert not ps.record_verifies("install", stored, w["keys"])


# --- the rebuild route -------------------------------------------------------------------


def test_the_rebuild_route_refuses_anyone_but_a_platform_admin(world):
    w = world
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": _base("next")},
                              headers=auth_header("carol"))
    assert answer.status_code == 403
    assert w["jobs"].runs == []
    assert w["client"].post(f"{routes.BASE}/rebuild", json={"base": _base("next")}).status_code == 401
    # Nor a workspace's own admin.
    seed_tenant(w["db"], "eng")
    # The context's role store was built before the fixture gave it a key.
    w["ctx"].roles = store = RoleStore(w["db"], w["ctx"].settings, metrics=w["ctx"].metrics, now=w["ctx"].now)
    store.write(tenant_id="eng", subject="alice@example.com", role=roles.ADMIN, by=ROOT, via="platform_admin",
                reason="", previous=store.newest_valid("eng", "alice@example.com", fresh=True),
                action=roles.AUDIT_GRANTED, detail={})
    me = w["client"].get("/v1/tenants/me", headers={**auth_header("alice"), TENANT_HEADER: "eng"})
    assert me.json()["role"]["name"] == roles.ADMIN, me.text
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": _base("next")},
                              headers={**auth_header("alice"), TENANT_HEADER: "eng"})
    assert answer.status_code == 403
    assert w["jobs"].runs == []


def test_the_rebuild_route_builds_each_retained_key_once_on_the_new_base(world):
    w = world
    nxt = _base("next")
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": nxt}, headers=auth_header("root"))
    assert answer.status_code == 202, answer.text
    assert len(answer.json()["started"]) == 1
    args = w["jobs"].runs[0][1]
    for image in pb.RUNNER_IMAGES:
        assert args[args.index(image) + 1] == nxt[image].partition("@")[2]
    again = w["client"].post(f"{routes.BASE}/rebuild", json={"base": nxt}, headers=auth_header("root"))
    assert again.json()["started"] == []
    assert len(w["jobs"].runs) == 1
    assert _current(w) == w["versions"][(GSTACK, "1.0")]["version_id"]


def test_the_rebuild_route_refuses_a_base_outside_this_deployment(world):
    w = world
    elsewhere = {image: f"{REGION}-docker.pkg.dev/other-project-1/swarm/{image}@{_digest(image)}"
                 for image in pb.RUNNER_IMAGES}
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": elsewhere}, headers=auth_header("root"))
    assert answer.status_code == 422
    assert answer.json()["code"] == "bundle_base_invalid"
    assert w["jobs"].runs == []
    # Nor one in another repository of this deployment's own registry.
    mirror = {image: f"{REGISTRY}/docker-hub-mirror/{image}@{_digest(image)}" for image in pb.RUNNER_IMAGES}
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": mirror}, headers=auth_header("root"))
    assert answer.status_code == 422
    assert answer.json()["code"] == "bundle_base_invalid"
    assert w["jobs"].runs == []


# --- a missing grant ----------------------------------------------------------------------


def test_a_missing_grant_gives_the_fixed_code_and_moves_nothing(world):
    w = world
    before = _current(w)
    w["jobs"].refuse = "grant"
    answer = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"])
    assert answer.status_code == 503
    assert answer.json()["code"] == "bundle_grant_missing"
    bundle_id = answer.json()["detail"]["bundle_id"]
    assert _built(w, bundle_id)["state"] == "failed"
    assert _built(w, bundle_id)["refusal"] == {"code": "bundle_grant_missing"}
    assert _current(w) == before


def test_an_unconfigured_deployment_gives_a_fixed_code(world, monkeypatch):
    w = world
    monkeypatch.delenv(pb.REPOSITORY_ENV)
    answer = _apply(w, w["versions"][(GSTACK, "2.0")]["version_id"])
    assert answer.status_code == 503
    assert answer.json()["code"] == "bundle_build_unconfigured"
    assert w["jobs"].runs == []
