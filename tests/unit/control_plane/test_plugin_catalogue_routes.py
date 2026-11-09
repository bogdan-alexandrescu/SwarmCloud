"""The managed plugin catalogue's routes (docs/design/managed-plugins.md, lane 5 of section 17).

    GET    {base}                                  built-ins beside the installs
    POST   {base}/installs                         GitHub or a marketplace entry, fetched into quarantine
    POST   {base}/uploads                          an intake and its signed upload URL
    POST   {base}/intakes/{id}/start               the upload arrived: run the intake
    GET    {base}/intakes/{id}                     an intake's state (finishes it from its report)
    GET    {base}/installs/{id}/versions|diff      versions, and current -> a candidate
    POST   {base}/installs/{id}/update|apply       a candidate by the same intake, applied after its diff
    DELETE {base}/installs/{id}?confirm=           remove it: later submissions naming it are refused
    POST   /v1/internal/plugin-intake/report       the intake's callback (Pub/Sub push, OIDC)

The owner's answers: caps 100 MiB packed, 500 MiB unpacked, 20,000 files;
npm, pip and bare git refused; swarm-api fetches into quarantine and the
network-less job unpacks and pins; private plugins by a workspace's owner or
admin; an update applies only after one admin read its diff; a removed plugin
refuses submissions naming it.

Offline: FakeFirestore, and in-memory stand-ins for the plugins bucket, the
intake job, GitHub and the callback's ID-token verifier on `app.state`, so
the shipped routes run with no credentials and no network.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Iterable, Iterator, Mapping

import pytest

from swarm_api import pluginstore as ps
from swarm_api.routes import plugin_catalogue as pc
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .test_settings_mac import configure, new_key

CAROL = "carol@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
PLATFORM = pc.PLATFORM_BASE
BUCKET = "plugins-bucket-test"
CALLER = "plugin-intake-push@example.com"
AUDIENCE = "https://api.example.com/v1/internal/plugin-intake/report"

REPO = "example-org/lint-kit"
MARKET = "example-org/marketplace"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_M = "c" * 40


def _workspace(tenant_id: str) -> str:
    return pc.WORKSPACE_BASE.format(tenant_id=tenant_id)


# --- stand-ins ---------------------------------------------------------------


class FakeBucket:
    """The plugins bucket: quarantine, staging and the scope prefixes, as dicts."""

    def __init__(self) -> None:
        self.quarantine: dict[str, bytes] = {}
        self.reports: dict[str, bytes] = {}
        self.staged: dict[str, bytes] = {}
        self.pinned: dict[str, bytes] = {}
        self.cleaned: list[str] = []
        self.signed: list[tuple[str, str, int, int]] = []

    def put_quarantine(self, intake_id: str, chunks: Iterable[bytes], limit: int) -> int:
        data, size = [], 0
        for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise pc.ArchiveTooLarge()
            data.append(chunk)
        self.quarantine[intake_id] = b"".join(data)
        return size

    def quarantine_size(self, intake_id: str) -> int | None:
        data = self.quarantine.get(intake_id)
        return None if data is None else len(data)

    def upload_url(self, intake_id: str, content_type: str, limit: int, seconds: int) -> str:
        self.signed.append((intake_id, content_type, limit, seconds))
        return f"https://storage.example.com/signed/{intake_id}"

    def read_report(self, intake_id: str, limit: int) -> bytes | None:
        return self.reports.get(intake_id)

    def staged_digest(self, intake_id: str) -> tuple[str, int, int] | None:
        data = self.staged.get(intake_id)
        return None if data is None else (hashlib.sha256(data).hexdigest(), len(data), 7)

    def pin(self, intake_id: str, object_name: str, generation: int, sha256: str) -> bool:
        if object_name in self.pinned:
            return hashlib.sha256(self.pinned[object_name]).hexdigest() == sha256
        self.pinned[object_name] = self.staged[intake_id]
        return True

    def clean(self, intake_id: str) -> None:
        self.cleaned.append(intake_id)
        for held in (self.quarantine, self.reports, self.staged):
            held.pop(intake_id, None)


class FakeJobs:
    """The intake job: records each execution, and that its quarantine object
    was already there when it started (quarantine, then intake)."""

    def __init__(self, bucket: FakeBucket) -> None:
        self.bucket = bucket
        self.started: list[tuple[str, dict[str, str], bool]] = []
        self.fail = False

    def start(self, intake_id: str, env: Mapping[str, str]) -> None:
        if self.fail:
            raise RuntimeError("intake job not started: HTTP 500")
        self.started.append((intake_id, dict(env), intake_id in self.bucket.quarantine))


class FakeGitHub:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str, str], bytes] = {}
        self.tarballs: dict[tuple[str, str], bytes] = {}
        self.tokens: list[str | None] = []
        self.oversize = False

    def resolve(self, repository: str, ref: str, token: str | None) -> str | None:
        self.tokens.append(token)
        return self.refs.get((repository, ref))

    def read_file(self, repository: str, commit: str, path: str, token: str | None, limit: int) -> bytes | None:
        return self.files.get((repository, commit, path))

    def tarball(self, repository: str, commit: str, token: str | None) -> Iterator[bytes]:
        if self.oversize:
            chunk = bytes(ps.MIB)
            for _ in range(101):
                yield chunk
            return
        yield self.tarballs.get((repository, commit), b"packed:" + commit.encode())


class FakeVerifier:
    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self.claims = claims

    def verify(self, token: str) -> dict[str, Any]:
        if token not in self.claims:
            raise ValueError("token refused")
        return self.claims[token]


class FakeInstallations:
    """A workspace's GitHub App installation: one contents-only token per
    call. `run_token` is recorded too, so a read through it shows up."""

    def __init__(self) -> None:
        self.minted: list[tuple[str, str, str]] = []
        self.run_minted: list[tuple[str, str, str, bool]] = []

    def contents_token(self, tenant_id: str, owner: str, repo: str) -> str:
        self.minted.append((tenant_id, owner, repo))
        return "contents-" + secrets.token_hex(8)

    def run_token(self, tenant_id: str, owner: str, repo: str, *, write: bool) -> str:
        self.run_minted.append((tenant_id, owner, repo, write))
        return "run-" + secrets.token_hex(8)


@pytest.fixture
def world(client, api_context, db, monkeypatch):
    configure(api_context, settings_key=new_key())
    seed_tenant(db, "eng")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    bucket = FakeBucket()
    jobs = FakeJobs(bucket)
    github = FakeGitHub()
    github.refs[(REPO, "v1.0.0")] = COMMIT_A
    github.refs[(REPO, "v2.0.0")] = COMMIT_B
    github.refs[(MARKET, "main")] = COMMIT_M
    push = "push-" + secrets.token_hex(8)
    stranger = "stranger-" + secrets.token_hex(8)
    client.app.state.plugin_bucket = bucket
    client.app.state.plugin_intake_jobs = jobs
    client.app.state.plugin_github = github
    client.app.state.plugin_intake_verifier = FakeVerifier({
        push: {"email": CALLER, "email_verified": True},
        stranger: {"email": "someone@example.com", "email_verified": True},
    })
    monkeypatch.setenv(pc.BUCKET_ENV, BUCKET)
    monkeypatch.setenv(pc.AUDIENCE_ENV, AUDIENCE)
    monkeypatch.setenv(pc.CALLERS_ENV, CALLER)
    return {"client": client, "ctx": api_context, "db": db, "bucket": bucket, "jobs": jobs,
            "github": github, "push": push, "stranger": stranger}


# --- helpers -----------------------------------------------------------------


def _components(**over: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    base: dict[str, list[dict[str, Any]]] = {"skills": [{"name": "lint"}], "commands": [], "agents": [],
                                             "hooks": [], "mcp_servers": []}
    base.update(over)
    return base


def run_intake(bucket: FakeBucket, intake: Mapping[str, Any], *, name: str = "lint-kit",
               version: str = "1.0.0", components: dict[str, Any] | None = None,
               hosts: list[str] | None = None, runnable: bytes | None = None, **over: Any) -> dict[str, Any]:
    """What the network-less job writes: the staged tar, then its report."""
    runnable = runnable if runnable is not None else f"runnable:{name}:{version}".encode()
    report = {
        "schema": ps.REPORT_SCHEMA, "intake_id": intake["intake_id"], "scope": intake["scope"],
        "tenant_id": intake["tenant_id"], "runnable_object": f"staging/{intake['intake_id']}/runnable.tar",
        "limits": dict(ps.LIMITS), "outcome": "pinned", "refusal": None,
        "source_sha256": hashlib.sha256(b"source").hexdigest(), "source_bytes": 6,
        "runnable_sha256": hashlib.sha256(runnable).hexdigest(), "runnable_bytes": len(runnable),
        "runnable_format": "canonical-tar", "files": 3, "unpacked_bytes": 2048,
        "manifest": {"name": name, "version": version, "description": "Lints.", "user_config": []},
        "manifest_source": "plugin", "components": components or _components(), "overlay": None,
        "requires": {"suggested": {"hosts": hosts or [], "runtimes": []}, "declared": None},
    }
    report.update(over)
    bucket.staged[intake["intake_id"]] = runnable
    bucket.reports[intake["intake_id"]] = json.dumps(report).encode()
    return report


def refuse_intake(bucket: FakeBucket, intake: Mapping[str, Any], code: str) -> None:
    report = {
        "schema": ps.REPORT_SCHEMA, "intake_id": intake["intake_id"], "scope": intake["scope"],
        "tenant_id": intake["tenant_id"], "runnable_object": f"staging/{intake['intake_id']}/runnable.tar",
        "limits": dict(ps.LIMITS), "outcome": "refused",
        "refusal": {"code": code, "sentence": "upstream words", "entry_index": 4},
        "source_sha256": None, "source_bytes": None,
    }
    bucket.reports[intake["intake_id"]] = json.dumps(report).encode()


def notify(w: dict[str, Any], intake_id: str, *, token: str | None = None, bucket: str = BUCKET,
           event: str = "OBJECT_FINALIZE", name: str | None = None):
    headers = {} if token == "" else {"Authorization": f"Bearer {token or w['push']}"}
    envelope = {"message": {"attributes": {
        "objectId": name or f"staging/{intake_id}/report.json", "bucketId": bucket, "eventType": event}},
        "subscription": "plugin-intake-reports"}
    return w["client"].post(pc.CALLBACK_ROUTE, json=envelope, headers=headers)


def github_install(w: dict[str, Any], *, base: str = PLATFORM, user: str = "root",
                   ref: str = "v1.0.0", overlay: dict[str, Any] | None = None, **source: Any):
    body: dict[str, Any] = {"source": {"kind": "github", "repository": REPO, "ref": ref, **source}}
    if overlay is not None:
        body["overlay"] = overlay
    return w["client"].post(base + "/installs", json=body, headers=auth_header(user))


def installed(w: dict[str, Any], *, base: str = PLATFORM, user: str = "root", **report: Any) -> dict[str, Any]:
    """A plugin installed end to end: fetch, intake, callback."""
    started = github_install(w, base=base, user=user)
    assert started.status_code == 202, started.text
    intake = started.json()
    run_intake(w["bucket"], intake, **report)
    done = notify(w, intake["intake_id"])
    assert done.status_code == 200, done.text
    assert done.json()["state"] == ps.INTAKE_PINNED, done.text
    status = w["client"].get(f"{base}/intakes/{intake['intake_id']}", headers=auth_header(user)).json()
    return {"intake": status, "install_id": status["install_id"], "version_id": status["version_id"]}


def audit(db: Any, *actions: str) -> list[dict[str, Any]]:
    rows = [d for p, d in db.docs.items() if p.startswith("admin_audit/") and d.get("action") in ps.AUDIT_ACTIONS]
    return [r for r in rows if not actions or r["action"] in actions]


# --- the catalogue -------------------------------------------------------------


def test_the_caps_are_the_owners():
    # MUTATION: any other cap and the intake job's reports are refused as
    # made under other limits.
    assert ps.LIMITS == {"max_packed_bytes": 100 * 1024 * 1024, "max_unpacked_bytes": 500 * 1024 * 1024,
                         "max_files": 20_000, "max_expansion_ratio": 100}


def test_the_platform_catalogue_lists_the_built_ins_beside_the_installs(world):
    w = world
    done = installed(w)
    body = w["client"].get(PLATFORM, headers=auth_header("root")).json()
    assert {e["name"] for e in body["built_in"]} == {"gstack", "security-audit"}
    assert all(e["built_in"] and not e["removable"] and not e["updatable"] for e in body["built_in"])
    [entry] = body["installs"]
    assert (entry["install_id"], entry["name"], entry["version"], entry["built_in"]) == (
        done["install_id"], "lint-kit", "1.0.0", False)
    assert body["limits"] == ps.LIMITS


def test_a_built_in_cannot_be_removed_or_updated(world):
    w = world
    removed = w["client"].delete(f"{PLATFORM}/installs/builtin:gstack?confirm=gstack", headers=auth_header("root"))
    assert removed.status_code == 422, removed.text
    assert removed.json()["code"] == "plugin_built_in"


def test_with_no_settings_key_nothing_is_installed(world):
    w = world
    configure(w["ctx"], settings_key="")
    response = github_install(w)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "settings_key_missing"
    assert w["jobs"].started == [] and w["bucket"].quarantine == {}


def test_a_forged_install_document_is_not_in_the_catalogue(world):
    w = world
    done = installed(w)
    # A direct Firestore write without the MAC: absent, not trusted.
    w["db"].docs["plugin_installs/p-0123456789ab"] = {
        "install_id": "p-0123456789ab", "scope": "platform", "tenant_id": None, "name": "evil",
        "state": "active", "current_version": None}
    names = [e["install_id"] for e in w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"]]
    assert names == [done["install_id"]]


# --- who may --------------------------------------------------------------------

PLATFORM_CALLS = [
    ("GET", PLATFORM, None),
    ("POST", PLATFORM + "/installs", {"source": {"kind": "github", "repository": REPO, "ref": "v1.0.0"}}),
    ("POST", PLATFORM + "/uploads", {"filename": "kit.zip", "size": 10}),
    ("GET", PLATFORM + "/intakes/" + "0" * 32, None),
    ("POST", PLATFORM + "/intakes/" + "0" * 32 + "/start", None),
    ("GET", PLATFORM + "/installs/p-0123456789ab/versions", None),
    ("POST", PLATFORM + "/installs/p-0123456789ab/update", {}),
    ("GET", PLATFORM + "/installs/p-0123456789ab/diff?version_id=v-0123456789ab", None),
    ("POST", PLATFORM + "/installs/p-0123456789ab/apply", {"version_id": "v-0123456789ab", "diff_sha256": "0"}),
    ("DELETE", PLATFORM + "/installs/p-0123456789ab?confirm=x", None),
]


@pytest.mark.parametrize(("method", "path", "body"), PLATFORM_CALLS)
def test_every_platform_route_is_a_platform_admins(world, method, path, body):
    w = world
    response = w["client"].request(method, path, json=body, headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert w["jobs"].started == [] and w["bucket"].signed == []


def test_a_workspace_member_reads_but_does_not_install(world):
    w = world
    read = w["client"].get(_workspace("eng"), headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["can_install"] is False
    refused = github_install(w, base=_workspace("eng"), user="alice")
    assert refused.status_code == 403, refused.text
    upload = w["client"].post(_workspace("eng") + "/uploads", json={"filename": "kit.zip", "size": 10},
                              headers=auth_header("alice"))
    assert upload.status_code == 403, upload.text
    assert w["jobs"].started == [] and w["bucket"].signed == []


def test_a_platform_admin_who_is_not_a_member_does_not_install_a_workspaces_private_plugin(world):
    # Q3: the workspace's owner or admin. root is a platform admin outside
    # research: it may read research's catalogue, not install into it.
    w = world
    seed_tenant(w["db"], "research")
    read = w["client"].get(_workspace("research"), headers=auth_header("root"))
    assert read.status_code == 200, read.text
    assert read.json()["can_install"] is False
    refused = github_install(w, base=_workspace("research"), user="root")
    assert refused.status_code == 403, refused.text
    assert w["jobs"].started == []


def test_a_group_workspaces_admin_installs_its_private_plugin(world):
    # root is in eng, and a member platform admin is eng's admin (roles.py).
    w = world
    assert w["client"].get(_workspace("eng"), headers=auth_header("root")).json()["can_install"] is True
    done = installed(w, base=_workspace("eng"), user="root")
    assert done["install_id"].startswith("w-")
    eng = w["client"].get(_workspace("eng"), headers=auth_header("alice")).json()
    assert [i["install_id"] for i in eng["installs"]] == [done["install_id"]]


def test_another_workspaces_catalogue_is_not_readable(world):
    w = world
    response = w["client"].get(_workspace("eng"), headers=auth_header("bob"))
    assert response.status_code in (403, 404), response.text


def test_the_owner_installs_a_private_plugin_with_a_token_from_its_own_installation(world):
    w = world
    installations = FakeInstallations()
    w["ctx"].forge_installations = installations
    base = _workspace(CAROL_TENANT)
    assert w["client"].get(base, headers=auth_header("carol")).json()["can_install"] is True
    done = installed(w, base=base, user="carol")
    # Contents-only, for the one repository, from carol's workspace
    # installation; never a run token (which also asks pull requests).
    assert installations.minted == [(CAROL_TENANT, "example-org", "lint-kit")]
    assert installations.run_minted == []
    assert w["github"].tokens[-1] is not None
    [(intake_id, env, _)] = w["jobs"].started
    assert env["SWARM_INTAKE_SCOPE"] == "workspace" and env["SWARM_INTAKE_TENANT"] == CAROL_TENANT
    assert f"tenants/{CAROL_TENANT}/" + hashlib.sha256(b"runnable:lint-kit:1.0.0").hexdigest() + ".tar" \
        in w["bucket"].pinned
    listed = w["client"].get(base, headers=auth_header("carol")).json()
    assert [i["install_id"] for i in listed["installs"]] == [done["install_id"]]
    # Not in the platform's own installs; shown to a platform admin read-only.
    platform = w["client"].get(PLATFORM, headers=auth_header("root")).json()
    assert platform["installs"] == []
    assert [i["install_id"] for i in platform["workspace_installs"]] == [done["install_id"]]


def test_the_platform_catalogue_reads_public_repositories_without_a_token(world):
    w = world
    installations = FakeInstallations()
    w["ctx"].forge_installations = installations
    installed(w)
    assert installations.minted == [] and installations.run_minted == [] and w["github"].tokens == [None]


def test_a_platform_admin_may_remove_a_workspaces_plugin_but_a_member_may_not(world):
    w = world
    base = _workspace(CAROL_TENANT)
    done = installed(w, base=base, user="carol")
    path = f"{base}/installs/{done['install_id']}?confirm=lint-kit"
    # Not alice's workspace: not even found.
    assert w["client"].delete(path, headers=auth_header("alice")).status_code == 404
    # eng's member, on eng's own plugin: refused.
    eng = installed(w, base=_workspace("eng"), user="root")
    member = w["client"].delete(f"{_workspace('eng')}/installs/{eng['install_id']}?confirm=lint-kit",
                                headers=auth_header("alice"))
    assert member.status_code == 403, member.text
    removed = w["client"].delete(path, headers=auth_header("root"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["state"] == ps.STATE_REMOVED
    [row] = audit(w["db"], ps.AUDIT_REMOVED)
    assert (row["by"], row["target_tenant_id"]) == ("root@example.com", CAROL_TENANT)


def test_one_workspaces_install_is_not_found_through_another_scope(world):
    w = world
    done = installed(w, base=_workspace(CAROL_TENANT), user="carol")
    response = w["client"].get(f"{PLATFORM}/installs/{done['install_id']}/versions", headers=auth_header("root"))
    assert response.status_code == 404, response.text


# --- refused sources --------------------------------------------------------------


@pytest.mark.parametrize("kind", ["npm", "pip", "git", "url", "upload"])
def test_a_source_that_is_not_github_or_a_marketplace_is_refused(world, kind):
    w = world
    response = w["client"].post(PLATFORM + "/installs", json={"source": {
        "kind": kind, "repository": REPO, "ref": "v1.0.0"}}, headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "source_kind_refused"
    assert w["bucket"].quarantine == {} and w["jobs"].started == []


@pytest.mark.parametrize(("source", "code"), [
    ({"repository": "https://github.com/example-org/lint-kit.git"}, "repository_invalid"),
    ({"repository": "example-org"}, "repository_invalid"),
    ({"ref": "../main"}, "ref_invalid"),
    ({"ref": "main;rm"}, "ref_invalid"),
    ({"subdir": "../outside"}, "subdir_invalid"),
    ({"ref": "no-such-tag"}, "ref_not_found"),
])
def test_an_invalid_github_source_is_refused_before_any_fetch(world, source, code):
    w = world
    response = w["client"].post(PLATFORM + "/installs", json={"source": {
        "kind": "github", "repository": REPO, "ref": "v1.0.0", **source}}, headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert w["bucket"].quarantine == {} and w["jobs"].started == []


def _marketplace(w: dict[str, Any], entry_source: Any) -> None:
    w["github"].files[(MARKET, COMMIT_M, pc.MARKETPLACE_FILE)] = json.dumps({
        "name": "example", "owner": {"name": "Example"},
        "plugins": [{"name": "lint-kit", "source": entry_source}]}).encode()


def _marketplace_install(w: dict[str, Any]):
    return w["client"].post(PLATFORM + "/installs", json={"source": {
        "kind": "marketplace", "repository": MARKET, "ref": "main", "plugin": "lint-kit"}},
        headers=auth_header("root"))


@pytest.mark.parametrize(("entry_source", "code"), [
    ({"source": "npm", "package": "lint-kit"}, "marketplace_source_npm"),
    ({"source": "pip", "package": "lint-kit"}, "marketplace_source_pip"),
    ({"source": "url", "url": "https://git.example.com/lint-kit.git"}, "marketplace_source_git"),
    ({"source": "git", "url": "https://git.example.com/lint-kit.git"}, "marketplace_source_git"),
    ("https://git.example.com/lint-kit.git", "marketplace_source_git"),
    ("git@git.example.com:example-org/lint-kit", "marketplace_source_git"),
    ({"source": "ftp"}, "marketplace_source_unknown"),
    ("../outside", "marketplace_source_unknown"),
])
def test_a_marketplace_entry_that_installs_from_npm_pip_or_bare_git_is_refused(world, entry_source, code):
    w = world
    _marketplace(w, entry_source)
    response = _marketplace_install(w)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert w["bucket"].quarantine == {} and w["jobs"].started == []


def test_a_marketplace_without_the_entry_or_the_file_is_refused(world):
    w = world
    assert _marketplace_install(w).json()["code"] == "marketplace_missing"
    w["github"].files[(MARKET, COMMIT_M, pc.MARKETPLACE_FILE)] = json.dumps({"plugins": []}).encode()
    assert _marketplace_install(w).json()["code"] == "marketplace_plugin_missing"


def test_a_marketplace_path_entry_is_fetched_at_the_marketplaces_commit(world):
    w = world
    _marketplace(w, "./plugins/lint-kit")
    response = _marketplace_install(w)
    assert response.status_code == 202, response.text
    source = response.json()["source"]
    assert (source["kind"], source["repository"], source["commit"], source["subdir"]) == (
        "marketplace", MARKET, COMMIT_M, "plugins/lint-kit")
    [(_, env, _)] = w["jobs"].started
    assert env["SWARM_INTAKE_SUBDIR"] == "plugins/lint-kit"


def test_a_marketplace_github_entry_is_resolved_to_its_own_commit(world):
    w = world
    _marketplace(w, {"source": "github", "repo": REPO, "ref": "v2.0.0"})
    response = _marketplace_install(w)
    assert response.status_code == 202, response.text
    source = response.json()["source"]
    assert (source["repository"], source["commit"], source["marketplace_commit"]) == (REPO, COMMIT_B, COMMIT_M)
    assert w["bucket"].quarantine[response.json()["intake_id"]] == b"packed:" + COMMIT_B.encode()


# --- the caps ---------------------------------------------------------------------


def test_a_fetch_over_100_mib_is_refused_and_nothing_is_stored(world):
    w = world
    w["github"].oversize = True
    response = github_install(w)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "archive_too_large"
    assert w["bucket"].quarantine == {} and w["jobs"].started == []
    assert not [p for p in w["db"].docs if p.startswith("plugin_intake/")]


def test_an_upload_declared_over_100_mib_gets_no_url(world):
    w = world
    response = w["client"].post(PLATFORM + "/uploads", json={"filename": "kit.tar.gz",
                                                            "size": ps.MAX_PACKED_BYTES + 1},
                                headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "archive_too_large"
    assert w["bucket"].signed == []


def test_an_uploaded_object_over_100_mib_is_refused_at_start(world):
    w = world
    opened = w["client"].post(PLATFORM + "/uploads", json={"filename": "kit.zip", "size": 10},
                              headers=auth_header("root")).json()
    intake_id = opened["intake"]["intake_id"]
    w["bucket"].quarantine[intake_id] = bytes(ps.MAX_PACKED_BYTES + 1)
    response = w["client"].post(f"{PLATFORM}/intakes/{intake_id}/start", headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "archive_too_large"
    assert w["jobs"].started == [] and intake_id in w["bucket"].cleaned


@pytest.mark.parametrize(("over", "code"), [
    ({"files": ps.MAX_FILES + 1}, "report_limits_differ"),
    ({"unpacked_bytes": ps.MAX_UNPACKED_BYTES + 1}, "report_limits_differ"),
    ({"limits": {**ps.LIMITS, "max_files": 50_000}}, "report_limits_differ"),
    ({"scope": "workspace", "tenant_id": "eng"}, "report_mismatch"),
    ({"intake_id": "f" * 32}, "report_mismatch"),
    ({"extra": True}, "report_invalid"),
])
def test_a_report_past_the_caps_or_bound_elsewhere_is_refused(world, over, code):
    w = world
    intake = github_install(w).json()
    run_intake(w["bucket"], intake, **over)
    response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json()["state"] == ps.INTAKE_REFUSED
    status = w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()
    assert status["refusal"]["code"] == code
    assert w["bucket"].pinned == {}
    assert not [p for p in w["db"].docs if p.startswith("plugin_installs/")]


def test_a_staged_copy_that_is_not_the_reported_one_is_refused(world):
    w = world
    intake = github_install(w).json()
    run_intake(w["bucket"], intake)
    w["bucket"].staged[intake["intake_id"]] = b"something else"
    assert notify(w, intake["intake_id"]).json()["state"] == ps.INTAKE_REFUSED
    status = w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()
    assert status["refusal"]["code"] == "intake_digest_mismatch"
    assert w["bucket"].pinned == {}


# --- quarantine, then the intake job, then its authenticated callback --------------


def test_a_github_install_quarantines_the_resolved_commit_then_starts_the_job(world):
    w = world
    response = github_install(w, overlay={"plugin": {"name": "lint-kit"}})
    assert response.status_code == 202, response.text
    intake = response.json()
    assert intake["state"] == ps.INTAKE_RUNNING and intake["source"]["commit"] == COMMIT_A
    assert w["bucket"].quarantine[intake["intake_id"]] == b"packed:" + COMMIT_A.encode()
    [(intake_id, env, quarantined)] = w["jobs"].started
    assert intake_id == intake["intake_id"] and quarantined
    # Only the intake's own facts: no command, image or resource from a caller.
    assert env == {"SWARM_INTAKE_ID": intake_id, "SWARM_INTAKE_SCOPE": "platform",
                   "SWARM_INTAKE_OVERLAY": json.dumps({"plugin": {"name": "lint-kit"}},
                                                      sort_keys=True, separators=(",", ":"))}
    # Nothing is installed until the job's report is in.
    assert w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"] == []


def test_the_callback_pins_the_report_and_writes_the_install(world):
    w = world
    intake = github_install(w).json()
    report = run_intake(w["bucket"], intake)
    response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"intake_id": intake["intake_id"], "state": ps.INTAKE_PINNED}
    assert w["bucket"].pinned == {f"platform/{report['runnable_sha256']}.tar": b"runnable:lint-kit:1.0.0"}
    assert intake["intake_id"] in w["bucket"].cleaned
    [entry] = w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"]
    assert entry["runnable_sha256"] == report["runnable_sha256"]
    # A second notification of the same report changes nothing.
    again = notify(w, intake["intake_id"])
    assert again.status_code == 200 and again.json()["ignored"] is True
    assert len(w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"]) == 1


def test_the_callback_refuses_a_request_without_the_push_accounts_token(world):
    w = world
    intake = github_install(w).json()
    run_intake(w["bucket"], intake)
    assert notify(w, intake["intake_id"], token="").status_code == 401
    assert notify(w, intake["intake_id"], token="forged-" + secrets.token_hex(4)).status_code == 401
    assert notify(w, intake["intake_id"], token=w["stranger"]).status_code == 403
    # An admin's own session is not the callback's caller either.
    admin = w["client"].post(pc.CALLBACK_ROUTE, json={}, headers=auth_header("root"))
    assert admin.status_code == 401
    assert w["bucket"].pinned == {}
    assert w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"] == []


def test_the_callback_admits_nobody_when_unconfigured(world, monkeypatch):
    w = world
    monkeypatch.delenv(pc.CALLERS_ENV)
    intake = github_install(w).json()
    run_intake(w["bucket"], intake)
    assert notify(w, intake["intake_id"]).status_code == 503
    assert w["bucket"].pinned == {}


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"bucket": "another-bucket"}, "not_an_intake_report"),
    ({"event": "OBJECT_DELETE"}, "not_an_intake_report"),
    ({"name": "staging/xyz/report.json"}, "not_an_intake_report"),
])
def test_the_callback_ignores_a_notification_that_is_not_an_intake_report(world, kwargs, reason):
    w = world
    intake = github_install(w).json()
    run_intake(w["bucket"], intake)
    response = notify(w, intake["intake_id"], **kwargs)
    assert response.status_code == 200, response.text
    assert response.json() == {"ignored": True, "reason": reason}
    assert w["bucket"].pinned == {}


def test_a_refused_report_ends_the_intake_with_the_code_and_swarm_apis_sentence(world):
    w = world
    intake = github_install(w).json()
    refuse_intake(w["bucket"], intake, "too_many_files")
    assert notify(w, intake["intake_id"]).json()["state"] == ps.INTAKE_REFUSED
    status = w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()
    # The job's code, never the job's sentence.
    assert status["refusal"] == {"code": "too_many_files", "sentence": None, "entry_index": 4}
    [row] = audit(w["db"], ps.AUDIT_INTAKE_REFUSED)
    assert row["detail"]["code"] == "too_many_files"


def test_a_job_that_does_not_start_refuses_the_intake(world):
    w = world
    w["jobs"].fail = True
    response = github_install(w)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "intake_start_failed"
    assert "HTTP" not in response.text
    [row] = audit(w["db"], ps.AUDIT_INTAKE_REFUSED)
    assert row["detail"]["code"] == "intake_start_failed"


def test_a_name_already_installed_is_refused(world):
    w = world
    installed(w)
    intake = github_install(w).json()
    run_intake(w["bucket"], intake, runnable=b"other bytes")
    assert notify(w, intake["intake_id"]).json()["state"] == ps.INTAKE_REFUSED
    status = w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()
    assert status["refusal"]["code"] == "plugin_name_taken"


# --- uploads ------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["kit.exe", "kit", "../kit.zip", ".zip", "dir/kit.tgz"])
def test_an_upload_that_is_not_one_archive_is_refused(world, filename):
    w = world
    response = w["client"].post(PLATFORM + "/uploads", json={"filename": filename, "size": 10},
                                headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "upload_name_invalid"


def test_an_upload_goes_through_a_signed_url_then_the_same_intake(world):
    w = world
    opened = w["client"].post(PLATFORM + "/uploads", json={"filename": "kit.tar.gz", "size": 10},
                              headers=auth_header("root"))
    assert opened.status_code == 201, opened.text
    body = opened.json()
    intake_id = body["intake"]["intake_id"]
    assert body["intake"]["state"] == ps.INTAKE_AWAITING_UPLOAD
    assert body["upload"]["method"] == "PUT" and body["upload"]["expires_in"] == 600
    assert body["upload"]["headers"] == {"Content-Type": "application/gzip",
                                         "x-goog-content-length-range": f"0,{ps.MAX_PACKED_BYTES}"}
    assert w["bucket"].signed == [(intake_id, "application/gzip", ps.MAX_PACKED_BYTES, 600)]

    early = w["client"].post(f"{PLATFORM}/intakes/{intake_id}/start", headers=auth_header("root"))
    assert early.status_code == 409 and early.json()["code"] == "upload_missing"
    assert w["jobs"].started == []

    w["bucket"].quarantine[intake_id] = b"uploaded archive"
    started = w["client"].post(f"{PLATFORM}/intakes/{intake_id}/start", headers=auth_header("root"))
    assert started.status_code == 202, started.text
    assert started.json()["state"] == ps.INTAKE_RUNNING
    assert [s[0] for s in w["jobs"].started] == [intake_id]

    # Reading the intake finishes it from its report, with no callback.
    run_intake(w["bucket"], started.json())
    status = w["client"].get(f"{PLATFORM}/intakes/{intake_id}", headers=auth_header("root")).json()
    assert status["state"] == ps.INTAKE_PINNED and status["install_id"].startswith("p-")


# --- update, shown as a diff before it applies ----------------------------------------


def _candidate(w: dict[str, Any], install_id: str, **report: Any) -> str:
    started = w["client"].post(f"{PLATFORM}/installs/{install_id}/update", json={"ref": "v2.0.0"},
                               headers=auth_header("root"))
    assert started.status_code == 202, started.text
    intake = started.json()
    assert intake["source"]["commit"] == COMMIT_B and intake["install_id"] == install_id
    run_intake(w["bucket"], intake, version="2.0.0", runnable=b"runnable v2", **report)
    assert notify(w, intake["intake_id"]).json()["state"] == ps.INTAKE_PINNED
    status = w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()
    return status["version_id"]


def test_an_update_is_a_candidate_until_an_admin_applies_its_diff(world):
    w = world
    done = installed(w)
    install_id = done["install_id"]
    hook = {"event": "PreToolUse", "matcher": "Bash", "command": "./check.sh"}
    candidate = _candidate(w, install_id, components=_components(hooks=[hook]), hosts=["registry.example.com"])

    versions = w["client"].get(f"{PLATFORM}/installs/{install_id}/versions", headers=auth_header("root")).json()
    assert versions["install"]["current_version"] == done["version_id"]
    assert {v["version_id"]: v["current"] for v in versions["versions"]} == {
        done["version_id"]: True, candidate: False}
    [row] = audit(w["db"], ps.AUDIT_UPDATE_CHECKED)
    assert row["detail"]["version_id"] == candidate

    shown = w["client"].get(f"{PLATFORM}/installs/{install_id}/diff?version_id={candidate}",
                            headers=auth_header("root")).json()
    diff = shown["diff"]
    assert diff["manifest"]["version"] == {"from": "1.0.0", "to": "2.0.0"}
    assert diff["hooks"]["added"] == [hook]
    assert diff["hosts"]["added"] == ["registry.example.com"]
    assert diff["marked"] == ["hooks", "hosts"] and diff["needs_confirmation"] is True
    assert diff["source"]["compare"].endswith(f"{COMMIT_A}...{COMMIT_B}")

    apply = f"{PLATFORM}/installs/{install_id}/apply"
    stale = w["client"].post(apply, json={"version_id": candidate, "diff_sha256": "0" * 64,
                                          "confirm_name": "lint-kit"}, headers=auth_header("root"))
    assert stale.status_code == 409 and stale.json()["code"] == "diff_changed"
    unnamed = w["client"].post(apply, json={"version_id": candidate, "diff_sha256": shown["diff_sha256"]},
                               headers=auth_header("root"))
    assert unnamed.status_code == 422 and unnamed.json()["code"] == "confirmation_required"
    wrong = w["client"].post(apply, json={"version_id": candidate, "diff_sha256": shown["diff_sha256"],
                                          "confirm_name": "lint"}, headers=auth_header("root"))
    assert wrong.status_code == 422 and wrong.json()["code"] == "confirmation_mismatch"
    current = w["client"].get(f"{PLATFORM}/installs/{install_id}/versions", headers=auth_header("root")).json()
    assert current["install"]["current_version"] == done["version_id"]
    assert audit(w["db"], ps.AUDIT_UPDATED) == []

    applied = w["client"].post(apply, json={"version_id": candidate, "diff_sha256": shown["diff_sha256"],
                                            "confirm_name": "lint-kit"}, headers=auth_header("root"))
    assert applied.status_code == 200, applied.text
    assert applied.json()["install"]["current_version"] == candidate
    assert applied.json()["install"]["version"] == "2.0.0"
    [row] = audit(w["db"], ps.AUDIT_UPDATED)
    assert (row["detail"]["from_version"], row["detail"]["to_version"]) == (done["version_id"], candidate)
    assert row["detail"]["to_sha256"] == hashlib.sha256(b"runnable v2").hexdigest()


def test_an_unmarked_update_applies_on_its_digest_alone(world):
    w = world
    done = installed(w)
    candidate = _candidate(w, done["install_id"])
    shown = w["client"].get(f"{PLATFORM}/installs/{done['install_id']}/diff?version_id={candidate}",
                            headers=auth_header("root")).json()
    assert shown["diff"]["needs_confirmation"] is False
    applied = w["client"].post(f"{PLATFORM}/installs/{done['install_id']}/apply",
                               json={"version_id": candidate, "diff_sha256": shown["diff_sha256"]},
                               headers=auth_header("root"))
    assert applied.status_code == 200, applied.text
    again = w["client"].post(f"{PLATFORM}/installs/{done['install_id']}/apply",
                             json={"version_id": candidate, "diff_sha256": shown["diff_sha256"]},
                             headers=auth_header("root"))
    assert again.status_code == 409


def test_a_workspace_member_cannot_update_or_apply(world):
    w = world
    base = _workspace("eng")
    for path, body in ((f"{base}/installs/w-0123456789ab/update", {}),
                       (f"{base}/installs/w-0123456789ab/apply", {"version_id": "v-0123456789ab",
                                                                   "diff_sha256": "0"})):
        assert w["client"].post(path, json=body, headers=auth_header("alice")).status_code == 403


# --- removal --------------------------------------------------------------------------


def test_removal_needs_the_name_typed_then_refuses_later_submissions(world):
    w = world
    done = installed(w)
    install_id = done["install_id"]
    path = f"{PLATFORM}/installs/{install_id}"
    unnamed = w["client"].delete(path, headers=auth_header("root"))
    assert unnamed.status_code == 422 and unnamed.json()["code"] == "confirmation_required"
    wrong = w["client"].delete(path + "?confirm=lint", headers=auth_header("root"))
    assert wrong.status_code == 422 and wrong.json()["code"] == "confirmation_mismatch"
    store = pc._store(w["ctx"])
    ps.refuse_removed(store, "eng", [install_id])  # still active: passes

    removed = w["client"].delete(path + "?confirm=lint-kit", headers=auth_header("root"))
    assert removed.status_code == 200, removed.text
    assert w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"] == []
    eng = w["client"].get(_workspace("eng"), headers=auth_header("alice")).json()
    assert eng["platform_installs"] == []

    with pytest.raises(ps.PluginRefused) as refused:
        ps.refuse_removed(store, "eng", ["gstack", install_id])
    assert refused.value.code == "plugin_removed" and refused.value.status_code == 422
    assert refused.value.detail == {"install_id": install_id, "field": "plugins"}

    # The versions stay, so a step already signed with one names a real record.
    versions = w["client"].get(f"{path}/versions", headers=auth_header("root")).json()
    assert [v["version_id"] for v in versions["versions"]] == [done["version_id"]]
    # And it cannot be updated or removed again.
    assert w["client"].post(f"{path}/update", json={}, headers=auth_header("root")).status_code == 409
    assert w["client"].delete(path + "?confirm=lint-kit", headers=auth_header("root")).status_code == 409
    [row] = audit(w["db"], ps.AUDIT_REMOVED)
    assert row["detail"] == {"scope": "platform", "install_id": install_id,
                             "current_version": done["version_id"]}


def test_a_removed_workspace_install_is_refused_only_to_its_own_workspace(world):
    w = world
    done = installed(w, base=_workspace(CAROL_TENANT), user="carol")
    w["client"].delete(f"{_workspace(CAROL_TENANT)}/installs/{done['install_id']}?confirm=lint-kit",
                       headers=auth_header("carol"))
    store = pc._store(w["ctx"])
    with pytest.raises(ps.PluginRefused):
        ps.refuse_removed(store, CAROL_TENANT, [done["install_id"]])
    # Another workspace learns nothing about it from this check.
    ps.refuse_removed(store, "eng", [done["install_id"]])


# --- audit ----------------------------------------------------------------------------


def test_every_change_is_audited_with_ids_and_digests_never_the_archives_text(world):
    w = world
    done = installed(w, name="lint-kit")
    rows = audit(w["db"])
    assert [r["action"] for r in rows] == [ps.AUDIT_INTAKE_STARTED, ps.AUDIT_INSTALLED]
    started, installed_row = rows
    assert started["by"] == "root@example.com" and started["detail"]["commit"] == COMMIT_A
    assert started["detail"]["source_kind"] == "github"
    assert installed_row["detail"]["install_id"] == done["install_id"]
    assert installed_row["detail"]["version_id"] == done["version_id"]
    # The manifest's name is the archive's text: never in a row's detail.
    assert "lint-kit" not in json.dumps([r["detail"] for r in rows])
