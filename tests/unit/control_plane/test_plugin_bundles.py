"""Bundle builds in swarm-api (docs/design/managed-plugins.md sections 1.4.3, 1.4.4 and 1.4.7; lane 16).

A bundled install's (`build: "image"`) Apply starts exactly one build and
never moves `current_version`; only the ready finish moves it, in the
transaction that marks the bundle ready; a failed finish moves nothing and
records a fixed code; Roll back moves to the retained previous bundle at
once; a bundle record whose MAC is missing or wrong is absent; the rebuild
route `swarm upgrade` calls is a platform admin's alone; and a missing grant
is the fixed code `bundle_grant_missing`, never a 500.

Offline: FakeFirestore, and an in-memory Cloud Build on `app.state`, so the
shipped routes run with no credentials and no network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any, Mapping

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
CALLER = "cloud-build-push@example.com"
AUDIENCE = "https://api.example.com/v1/internal/plugin-bundles/finish"


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _base(label: str) -> dict[str, str]:
    return {image: f"{REGISTRY}/swarm/{image}@{_digest(label + image)}" for image in pb.RUNNER_IMAGES}


LIVE = _base("live")


class FakeBuilds:
    """Cloud Build: each started build recorded, each one's state set by a test."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.builds: dict[str, dict[str, Any]] = {}
        self.refuse: str | None = None

    def start(self, build: Mapping[str, Any]) -> str:
        if self.refuse == "grant":
            raise pb.GrantMissing(403)
        if self.refuse == "other":
            raise pb.BuildNotStarted(500)
        build_id = secrets.token_hex(8)
        self.started.append(dict(build))
        self.builds[build_id] = {"id": build_id, "status": "WORKING"}
        return build_id

    def get(self, build_id: str) -> Mapping[str, Any] | None:
        return self.builds.get(build_id)

    def succeed(self, build_id: str, bundle: Mapping[str, Any], *, source: Mapping[str, str] | None = None) -> None:
        repository = REPOSITORY
        images = [{"name": f"{repository}/{image}:{bundle['bundle_id']}", "digest": _digest(build_id + image)}
                  for image in pb.RUNNER_IMAGES]
        report = {}
        for entry in bundle["key"]:
            sha = (source or {}).get(entry["install_id"], entry["source_sha256"])
            report[entry["install_id"]] = {image: {
                "source_sha256": sha, "recipe_digest": hashlib.sha256(b"recipe").hexdigest(),
                "tree_sha256": hashlib.sha256((entry["version_id"] + image).encode()).hexdigest()}
                for image in pb.RUNNER_IMAGES}
        output = base64.b64encode(json.dumps(report).encode()).decode()
        self.builds[build_id] = {"id": build_id, "status": "SUCCESS",
                                 "results": {"images": images, "buildStepOutputs": ["", output]}}

    def fail(self, build_id: str) -> None:
        self.builds[build_id] = {"id": build_id, "status": "FAILURE", "statusDetail": "upstream words"}


class FakeVerifier:
    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self.claims = claims

    def verify(self, token: str) -> dict[str, Any]:
        if token not in self.claims:
            raise ValueError("token refused")
        return self.claims[token]


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
def world(client, api_context, db, monkeypatch):
    configure(api_context, settings_key=new_key())
    keys = settings_mac(api_context.settings).keys
    builds = FakeBuilds()
    client.app.state.plugin_bundle_builds = builds
    push = "push-" + secrets.token_hex(8)
    client.app.state.plugin_bundle_verifier = FakeVerifier({push: {"email": CALLER, "email_verified": True}})
    monkeypatch.setenv(pb.REPOSITORY_ENV, REPOSITORY)
    monkeypatch.setenv(pb.BUNDLER_ENV, f"{REGISTRY}/swarm/swarm-plugin-bundler@{_digest('bundler')}")
    monkeypatch.setenv(pb.BASE_ENV, json.dumps(LIVE))
    monkeypatch.setenv(pb.PLUGINS_BUCKET_ENV, "plugins-bucket-test")
    monkeypatch.setenv(routes.AUDIENCE_ENV, AUDIENCE)
    monkeypatch.setenv(routes.CALLERS_ENV, CALLER)
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
    # The release image is the bundle of the first versions (section 1.4.2).
    first = [{"install_id": i, "version_id": versions[(i, label)]["version_id"],
              "source_sha256": versions[(i, label)]["source_sha256"]} for i, label in ((GSTACK, "1.0"), (AUDIT, "a1"))]
    _set(db, "plugin_bundles/b-000000000001", "bundle", {
        "bundle_id": "b-000000000001", "scope": "platform", "tenant_id": None, "reason": "apply",
        "install_id": None, "version_id": None, "previous_version": None, "key": first,
        "key_sha256": pb.key_sha256(first), "base": LIVE, "bundler": "release", "state": "ready",
        "build_id": None, "outputs": LIVE, "plugins": {}, "refusal": None, "requested_by": "release",
        "finished_at": "2026-11-01T00:00:00+00:00"}, keys)
    return {"client": client, "db": db, "ctx": api_context, "builds": builds, "keys": keys,
            "versions": versions, "push": push}


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
    assert len(w["builds"].started) == 1
    assert _current(w) == before
    # A second Apply while it builds starts nothing.
    again = _apply(w, v2)
    assert again.status_code == 409
    assert again.json()["code"] == "bundle_building"
    assert len(w["builds"].started) == 1
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
        pb.start_apply(store, bundles, w["builds"], config, AUDIT, other["version_id"], by=ROOT)
    assert refused.value.code == "bundle_building"
    assert len(w["builds"].started) == 1


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
    a9 = _set(w["db"], f"plugin_installs/{AUDIT}/versions/v-b00000000009", "version",
              _version(AUDIT, "v-b00000000009", "a9"), w["keys"])["version_id"]
    first = pb.start_apply(store, bundles, w["builds"], config, GSTACK, v2, by=ROOT)
    second = pb.start_apply(store, bundles, w["builds"], config, AUDIT, a9, by=ROOT)
    assert len(w["builds"].started) == 2
    for started in (first, second):
        record = _built(w, started["bundle_id"])
        w["builds"].succeed(record["build_id"], record)
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
            "bundler": "b", "state": "building", "build_id": None, "outputs": None, "plugins": None,
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
    assert len(w["builds"].started) == 1


def test_the_build_runs_as_the_builder_account_and_pushes_only_to_the_bundle_repository(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    build = w["builds"].started[0]
    assert build["serviceAccount"] == (
        f"projects/{PROJECT}/serviceAccounts/swarm-plugin-bundler@{PROJECT}.iam.gserviceaccount.com")
    assert build["images"] == [f"{REPOSITORY}/{image}:{bundle_id}" for image in pb.RUNNER_IMAGES]
    text = json.dumps(build)
    for image in pb.RUNNER_IMAGES:
        assert f"FROM {LIVE[image]}" in text
    assert w["versions"][(GSTACK, "2.0")]["source_sha256"] in text


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
    assert len(w["builds"].started) == 1
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
    assert w["builds"].started == []
    assert _current(w) == before


def test_the_catalogues_own_apply_on_a_bundled_install_is_a_platform_admins_alone(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    answer = w["client"].post(f"{pc.PLATFORM_BASE}/installs/{GSTACK}/apply",
                              json={"version_id": v2, "diff_sha256": _diff_sha(w, GSTACK, v2)},
                              headers=auth_header("carol"))
    assert answer.status_code in (401, 403)
    assert w["builds"].started == []


# --- the finish ---------------------------------------------------------------------


def test_a_ready_finish_moves_current_version(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    assert _status(w, bundle_id).json()["state"] == "building"
    w["builds"].succeed(record["build_id"], record)
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
    w["builds"].fail(_built(w, bundle_id)["build_id"])
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
    w["builds"].succeed(record["build_id"], record, source={GSTACK: "0" * 64})
    answer = _status(w, bundle_id).json()
    assert answer["state"] == "failed"
    assert answer["refusal"]["code"] == "bundle_check_failed"
    assert _current(w) == before


def test_the_notification_finishes_a_build_from_cloud_builds_own_record(world):
    w = world
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    w["builds"].succeed(record["build_id"], record)
    # The message's own status is never read: Cloud Build's record is.
    data = base64.b64encode(json.dumps({"id": record["build_id"], "status": "FAILURE",
                                        "tags": ["swarm-plugin-bundle", bundle_id]}).encode()).decode()
    envelope = {"message": {"data": data}}
    refused = w["client"].post(routes.FINISH_ROUTE, json=envelope)
    assert refused.status_code == 401
    answer = w["client"].post(routes.FINISH_ROUTE, json=envelope,
                              headers={"Authorization": f"Bearer {w['push']}"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "ready"
    assert _current(w) == v2


# --- Roll back --------------------------------------------------------------------------


def test_roll_back_moves_to_the_retained_previous_bundle(world):
    w = world
    v1 = _current(w)
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    w["builds"].succeed(record["build_id"], record)
    _status(w, bundle_id)
    assert _current(w) == v2
    kept = pb.retention(pb.BundleStore(_store(w), w["db"], now=w["ctx"].now), named=set())
    assert {b["bundle_id"] for b in kept["keep"]} == {bundle_id, "b-000000000001"}
    answer = _apply(w, v1, path="roll-back")
    assert answer.status_code == 200, answer.text
    assert answer.json()["bundle"]["bundle_id"] == "b-000000000001"
    assert _current(w) == v1
    assert len(w["builds"].started) == 1


def test_a_ready_finish_retires_what_retention_no_longer_keeps(world):
    w = world
    w["builds"].refuse = "grant"
    v2 = w["versions"][(GSTACK, "2.0")]["version_id"]
    failed = _apply(w, v2).json()["detail"]["bundle_id"]
    w["builds"].refuse = None
    bundle_id = _apply(w, v2).json()["bundle_id"]
    record = _built(w, bundle_id)
    w["builds"].succeed(record["build_id"], record)
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
    w["builds"].succeed(record["build_id"], record)
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
    w["builds"].succeed(record["build_id"], record)
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
    assert w["builds"].started == []
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
    assert w["builds"].started == []


def test_the_rebuild_route_builds_each_retained_key_once_on_the_new_base(world):
    w = world
    nxt = _base("next")
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": nxt}, headers=auth_header("root"))
    assert answer.status_code == 202, answer.text
    assert len(answer.json()["started"]) == 1
    assert f"FROM {nxt['agent-runtime-base']}" in json.dumps(w["builds"].started[0])
    again = w["client"].post(f"{routes.BASE}/rebuild", json={"base": nxt}, headers=auth_header("root"))
    assert again.json()["started"] == []
    assert len(w["builds"].started) == 1
    assert _current(w) == w["versions"][(GSTACK, "1.0")]["version_id"]


def test_the_rebuild_route_refuses_a_base_outside_this_deployment(world):
    w = world
    elsewhere = {image: f"{REGION}-docker.pkg.dev/other-project-1/swarm/{image}@{_digest(image)}"
                 for image in pb.RUNNER_IMAGES}
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": elsewhere}, headers=auth_header("root"))
    assert answer.status_code == 422
    assert answer.json()["code"] == "bundle_base_invalid"
    assert w["builds"].started == []
    # Nor one in another repository of this deployment's own registry.
    mirror = {image: f"{REGISTRY}/docker-hub-mirror/{image}@{_digest(image)}" for image in pb.RUNNER_IMAGES}
    answer = w["client"].post(f"{routes.BASE}/rebuild", json={"base": mirror}, headers=auth_header("root"))
    assert answer.status_code == 422
    assert answer.json()["code"] == "bundle_base_invalid"
    assert w["builds"].started == []


# --- a missing grant ----------------------------------------------------------------------


def test_a_missing_grant_gives_the_fixed_code_and_moves_nothing(world):
    w = world
    before = _current(w)
    w["builds"].refuse = "grant"
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
    assert w["builds"].started == []
