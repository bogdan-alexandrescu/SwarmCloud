"""The console's onboarding, walked end to end, offline (lane DAYONE-E2E-CONSOLE;
part of #2688; docs/design/day-one-plan.md §5).

From the engine's hand-off to `console/finished`, in team mode and in
one-person mode (decision 7), over the REAL swarm-api app (`create_app`,
`TestClient`), calling every Admin › Setup route the console calls, in the
order `apps/swarm-ui/src/api.ts` lists them and the page reaches them:
the page's state, the GitHub App (manifest, GitHub's redirect, installation,
self-check), the first team (group check, create, invite and its Google Admin
members page), the pool account (its one-time link, the link page's redeem,
the existing sign-in), the console's verify runs, the smoke and its restart,
the team's git and add-on links, and finish.

Each seam is held where it is crossed:

* The engine's side is the engine's own code. The hand-off document is
  written by `swarm_install.record.InstallRecord.write` from an
  `InstallState`, and at the end the engine's reader (`read_console`, the
  hand-off step's `check`, verify's console keys) reads back what swarm-api
  wrote and must see `finished`.
* The Firestore fake refuses what google-cloud-firestore refuses
  (`firestore_rules`), and after every call every stored document under
  `swarm-install/` is checked against the one-writer table: the engine's
  document is byte-for-byte what the engine wrote, and every other document
  is one of swarm-api's rows in BOTH `swarm_install.record.WRITERS` and
  `swarm_api.install_record.SWARM_API_DOCUMENTS`, holding only its fields.
* Every answer is read through a port of api.ts's normalisers
  (`console_view`), and every field api.ts reads must be served by some
  answer of the walk.

A real product defect found here is not fixed here: it is written to the
lane's findings and its one assertion is `pytest.xfail(strict=True)`, named.
"""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

_ENGINE = str(Path(__file__).resolve().parents[4] / "apps" / "swarm-install")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from swarm_install import record as engine_record  # noqa: E402
from swarm_install.lease import LEASE_SECONDS, Lease  # noqa: E402
from swarm_install.state import STEP_DONE, InstallState, StepRecord  # noqa: E402
from swarm_install.steps import (  # noqa: E402
    cluster,
    foundations,
    github_switches,
    handoff,
    identity,
    preflight,
)
from swarm_install.steps import verify as engine_verify  # noqa: E402

from swarm_api import install_record as ir  # noqa: E402

from .. import test_setup_routes as base  # noqa: E402
from ..conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, auth_header, seed_tenant  # noqa: E402
from ..test_setup_routes import (  # noqa: E402,F401 - fixtures
    ALICE,
    CAROL,
    ENV,
    HOSTNAME,
    ORG,
    ROOT,
    TEAM_GROUP,
    PassingAppCheck,
    directory,
    fake_secrets,
    github,
    pool,
    runs,
    setup_app,
    setup_client,
    sizing,
    tokens,
)
from . import console_view as cv  # noqa: E402
from .firestore_rules import StrictFirestore, path_errors  # noqa: E402

ENGINE_DOC = engine_record.engine_document(ENV)
REPOSITORIES = (f"{ORG}/demo", f"{ORG}/api")
#: A teammate invited after the team is made.
DAVE = "dave@example.com"
#: Steps the engine has passed at hand-off, and its own hand-off step, by
#: their real ids and stages (each class's `id` and `stage`).
HANDED_OFF = {step.id: step.stage for step in (
    preflight.InputsStep, foundations.ProjectStep, identity.GroupsStep, cluster.WorkloadsStep,
    handoff.HandoffStep)}

# --------------------------------------------------------------------------
# fixtures: the strict Firestore, and an installer outside the team's group
# --------------------------------------------------------------------------


@pytest.fixture
def db() -> StrictFirestore:
    return StrictFirestore()


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """The installer starts OUTSIDE the team's group, so the walk meets the
    group check's refusal and its Google Admin page first (finding 27)."""
    return {ROOT: (ADMIN_GROUP, ENG_GROUP), ALICE: (ENG_GROUP, TEAM_GROUP), CAROL: (),
            base.WORKSPACE_ADMIN: ()}


# --------------------------------------------------------------------------
# the engine's side, through its own code
# --------------------------------------------------------------------------


class EngineDocuments:
    """`swarm_install.record.DocumentStore` over the walk's Firestore: the
    engine's own writer and reader, on the same documents swarm-api uses. A
    document's update time is its data as read (any write changes it)."""

    def __init__(self, db: StrictFirestore) -> None:
        self.db = db

    def get(self, path: str) -> tuple[dict[str, Any], Any] | None:
        snapshot = self.db.document(path).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return data, dict(data)

    def create(self, path: str, data: dict[str, Any]) -> None:
        if self.db.document(path).get().exists:
            raise engine_record.RecordConflict(path)
        self.db.document(path).set(dict(data))

    def update(self, path: str, data: dict[str, Any], *, update_time: Any) -> None:
        current = self.db.document(path).get()
        if not current.exists or current.to_dict() != update_time:
            raise engine_record.RecordConflict(path)
        self.db.document(path).update(dict(data))


class Engine:
    """The CLI at the hand-off (stage 13): its state, its lease, its record."""

    def __init__(self, db: StrictFirestore, outputs: dict[str, Any]) -> None:
        now = time.time()
        self.record = engine_record.InstallRecord(ENV, EngineDocuments(db))
        self.state = InstallState(
            project=PROJECT, environment=ENV, release="1.0.0", region="us-central1",
            hostname=HOSTNAME, outputs=outputs, hand_off_at=now,
            steps={step: StepRecord(state=STEP_DONE, at=now - 60 + stage, by=ROOT)
                   for step, stage in HANDED_OFF.items() if step != "handoff.console"})
        self.lease = Lease(holder=f"{ROOT} laptop", store=None, clock=time.time,
                           heartbeat=now, expires=now + LEASE_SECONDS)
        self.written: dict[str, Any] = {}

    def mirror(self, **verify: Any) -> None:
        """`Engine.mirror`: the state written to `swarm-install/<env>`."""
        self.record.write(self.state, HANDED_OFF, lease=self.lease.as_record(),
                          verify=verify or None)
        self.written = dict(self.record.documents.get(ENGINE_DOC)[0])

    # what the engine reads back

    def read_console(self, name: str) -> dict[str, Any] | None:
        found = self.record.read_console(name)
        if found is not None:
            self.state.console[name] = found
        return found

    @property
    def outputs(self) -> dict[str, Any]:
        return self.state.outputs

    @property
    def project(self) -> str:
        return PROJECT


def team_outputs() -> dict[str, Any]:
    return {"installer": ROOT, "sizing": sizing("team", 8), "github_org": ORG,
            "org_domain": "example.com", "hostname": HOSTNAME,
            "workspace_groups": {"team": TEAM_GROUP}}


def individual_outputs() -> dict[str, Any]:
    return {"installer": ROOT, "sizing": sizing("individual", 1), "github_org": ORG,
            "org_domain": "example.com", "hostname": HOSTNAME}


# --------------------------------------------------------------------------
# the console's side: each call, recorded, normalised, and the record audited
# --------------------------------------------------------------------------


def _template(path: str) -> str | None:
    """`path`'s row in the engine's `WRITERS`, or None."""
    rest = path.split("/")
    for template in engine_record.WRITERS:
        parts = template.format(env=ENV, id="{id}").split("/")
        if len(parts) == len(rest) and all(p == r or p == "{id}" for p, r in zip(parts, rest)):
            return template
    return None


class Console:
    """The browser: every route the page calls, as the person signed in."""

    def __init__(self, client: TestClient, db: StrictFirestore, engine: Engine) -> None:
        self.client = client
        self.db = db
        self.engine = engine
        self.answers: dict[str, list[Any]] = {}
        self.log: list[str] = []

    def call(self, route: str, path: str | None = None, body: Any = None, *,
             user: str = "root", headers: dict[str, str] | None = None,
             expect: int | tuple[int, ...] = 200) -> Any:
        method, template = route.split(" ", 1)
        target = path or template
        answer = self.client.request(method, target, json=body,
                                     headers={**auth_header(user), **(headers or {})})
        self.log.append(f"{method} {target} -> {answer.status_code}")
        wanted = expect if isinstance(expect, tuple) else (expect,)
        assert answer.status_code in wanted, (route, answer.status_code, answer.text)
        payload = answer.json()
        if answer.status_code < 300:
            self.answers.setdefault(route, []).append(payload)
        self.audit(route)
        return payload

    def page(self) -> dict[str, Any]:
        """`loadSetup()`: the page's state as `normSetupState` reads it."""
        return cv.norm_setup_state(self.call(cv.SETUP))

    def audit(self, route: str) -> None:
        """After every call: real Firestore would have taken every document,
        and every document under `swarm-install/` has its one writer."""
        errors = self.db.stored_errors()
        assert not errors, (route, errors)
        prefix = f"{engine_record.COLLECTION}/{ENV}"
        for path, data in self.db.docs.items():
            if not path.startswith(engine_record.COLLECTION + "/"):
                continue
            if path == ENGINE_DOC:
                assert data == self.engine.written, (route, "swarm-api changed the engine's "
                                                     "document")
                continue
            template = _template(path)
            assert template is not None, (route, f"{path}: no row in the one-writer table")
            writer, fields = engine_record.WRITERS[template]
            assert writer in (engine_record.SWARM_API, engine_record.SWARM_API_FOR_DOCTOR), \
                (route, path, writer)
            assert template in ir.SWARM_API_DOCUMENTS, (route, template)
            assert path.startswith(prefix + "/"), (route, path)
            ir.check_fields(path, ENV, data)
            if template.endswith("/accounts"):
                assert all(set(v) <= ir.ACCOUNT_FIELDS for v in data.values()), (route, data)
            else:
                assert set(data) <= fields, (route, path, sorted(set(data) - fields))


@pytest.fixture
def walk(setup_app, setup_client, db):
    """The console and the engine at the hand-off, for one mode."""

    def start(outputs: dict[str, Any]) -> Console:
        engine = Engine(db, outputs)
        engine.mirror()
        return Console(setup_client, db, engine)

    return start


def workspace_ready(db: StrictFirestore, tenant: str, *, person: str | None = None) -> None:
    """swarm-workspace-apply finishes the workspace (its one writer, §5.7);
    for a person's workspace it also leaves `tenants/u-<person>`, which
    `auth.PersonalTenants.ready` reads with the workspace record."""
    doc = db.docs[f"workspaces/{tenant}"]
    doc["state"] = "ready"
    doc["ready_at"] = doc["requested_at"]
    if person is not None:
        seed_tenant(db, tenant)
        db.docs[f"tenants/{tenant}"].update(kind="user", principal=person, display_name=person)


def github_app_step(console: Console, setup_app, github) -> None:
    """The GitHub App, as the page drives it (`startGitHubApp`, GitHub's
    redirect to the callback page and `convertGitHubApp`, the installation
    poll, `runGitHubAppSelfCheck`)."""
    start = cv.norm_manifest_start(console.call(cv.MANIFEST, body={}))
    assert start["action"].startswith(f"https://github.com/organizations/{ORG}/settings/apps/new")
    assert start["manifest"]["hook_attributes"]["url"]
    state = parse_qs(urlsplit(start["action"]).query)["state"][0]
    view = console.page()
    assert view["github_app"]["started"] is True and view["github_app"]["id"] is None
    # GitHub creates the App and redirects to the callback page with a code.
    github.conversion = {"id": 4242, "slug": "swarmcloud-test", "client_id": "Iv1.e2e",
                         "name": "SwarmCloud test", "owner": {"login": ORG},
                         "pem": base.FakeSecrets().private_key(),
                         "client_secret": secrets.token_hex(20),
                         "webhook_secret": secrets.token_hex(20)}
    created = cv.norm_conversion(console.call(cv.CONVERSION,
                                              body={"code": secrets.token_hex(8), "state": state}))
    assert created == {"id": "4242", "slug": "swarmcloud-test"}
    assert cv.norm_installation(console.call(cv.INSTALLATION))["installed"] is False
    # The person installs the App on the organisation, choosing repositories.
    github.installations = [{"id": 77, "account": {"login": ORG, "type": "Organization"},
                             "repository_selection": "selected"}]
    github.repositories = list(REPOSITORIES)
    installed = cv.norm_installation(console.call(cv.INSTALLATION))
    assert installed == {"installed": True, "org": ORG, "slug": "swarmcloud-test"}
    # Before infra pass 3 gives swarm-api the App's id the check waits for it;
    # the engine's pass 3 then lands (its trigger reads what swarm-api wrote).
    setup_app.state.forge_app = WaitingAppCheck()
    waiting = cv.norm_self_check(console.call(cv.SELF_CHECK))
    assert waiting["state"] == "waiting_for_infra_pass_3"
    assert console.page()["github_app"]["self_check"] == waiting
    app_doc = github_switches.app_document(console.engine)
    assert app_doc.get("secrets_written_at") and app_doc.get("id"), app_doc
    setup_app.state.forge_app = PassingAppCheck()
    passed = cv.norm_self_check(console.call(cv.SELF_CHECK))
    assert passed["ok"] is True and passed["state"] == "passed"
    view = console.page()
    app = view["github_app"]
    assert app["id"] == "4242" and app["slug"] == "swarmcloud-test"
    assert app["installed_on"] == ORG and app["secrets_written_at"]
    assert app["self_check"]["state"] == "passed"
    assert bool((github_switches.app_document(console.engine).get("self_check")
                 or {}).get("ok")), "pass 4's trigger cannot see the passed self-check"


class WaitingAppCheck:
    """`ForgeApp.check_app_key` before infra pass 3 rendered GITHUB_APP_ID."""

    def check_app_key(self) -> dict[str, Any]:
        return {"ok": False, "reason": "app_id_not_configured"}


def pool_account_step(console: Console, pool, *, owner: str, user: str, tenant: str) -> None:
    """A pool account: its one-time link (`mintSetupLink`), the link page's
    redeem (`SetupLink.tsx`) and the existing sign-in, with the tenant the
    redeem named."""
    body = {"kind": "pool-account", "label": "pool-1", "owner": owner}
    link = cv.norm_link(console.call(cv.LINKS, body=body, expect=201), body)
    assert link["url"] == f"https://{HOSTNAME}{link['path']}", link
    assert link["expires_at"] and link["owner"] == owner
    view = console.page()
    assert [a["label"] for a in view["accounts"]] == ["pool-1"]
    assert view["accounts"][0]["owner"] == owner and view["accounts"][0]["signed_in_at"] is None
    token = link["path"].rsplit("/", 1)[1]
    raw = console.call(cv.REDEEM, body={"token": token}, user=user)
    opened = cv.opened_from(raw)
    assert opened == {"kind": "pool-account", "label": "pool-1", "tenant": tenant}
    selected = {raw["sign_in"]["header"]: opened["tenant"]}
    begun = console.client.post(raw["sign_in"]["authorize"], headers={**auth_header(user),
                                                                      **selected},
                                json={"label": opened["label"], "lend_to": []})
    assert begun.status_code == 200, begun.text
    finished = console.client.post(raw["sign_in"]["exchange"],
                                   headers={**auth_header(user), **selected},
                                   json={"state": begun.json()["state"], "code": "a-code#x"})
    assert finished.status_code == 201, finished.text
    assert [a["owner_tenant"] for a in pool.finished] == [tenant]


def engine_sees_finished(console: Console, *, tenant: str, one_person: bool) -> None:
    """The engine's reader, on what swarm-api wrote: `finished`, the verify
    runs' ids under verify's own keys, and the hand-off step's own check."""
    engine = console.engine
    finished = engine.read_console(handoff.FINISHED)
    assert finished and finished["by"] == ROOT and finished["at"], finished
    runs = (engine.read_console(engine_record.VERIFY_RUNS) or {}).get("verify_runs") or {}
    assert set(engine_verify.CONSOLE_KEYS.values()) <= set(runs), runs
    engine.state.outputs["verify_runs"] = dict(runs)
    for step in handoff.build_triggered():
        engine.state.steps[step.id] = StepRecord(state=STEP_DONE, at=time.time(), by=ROOT)
    observed = handoff.HandoffStep(handoff.DEPS).check(engine)
    assert observed.status == handoff.MET, observed
    if one_person:
        personal = engine.read_console(engine_record.PERSONAL)
        assert personal and personal["workspace"] == tenant, personal
    # Verify ends: the engine writes its row and `ready_at`, and the page,
    # read once more, serves both through the engine's document.
    engine.state.ready_at = time.time()
    engine.mirror(check="verify.runs", done=6, total=6, started=time.time() - 600,
                  expected_end=time.time(), deadline=time.time() + 600, result="passed")
    view = console.page()
    assert view["engine"]["verify"]["result"] == "passed"
    assert view["engine"]["ready_at"] is not None
    assert view["finished"]["by"] == ROOT


# --------------------------------------------------------------------------
# the walks
# --------------------------------------------------------------------------


def team_walk(walk, setup_app, db, github, pool, directory, runs) -> Console:
    """The team walk, from the hand-off to the engine reading `finished`."""
    console = walk(team_outputs())

    # 1. The page at the hand-off: the engine's document, read and never written.
    view = console.page()
    assert view["env"] == ENV and view["mode"] == "team" and view["cli_running"] is True
    assert view["engine"]["release"] == "1.0.0" and view["engine"]["hostname"] == HOSTNAME
    assert view["engine"]["hand_off_at"] is not None
    assert view["engine"]["lease"]["holder"] == f"{ROOT} laptop"
    assert view["engine"]["lease"]["heartbeat"] is not None
    assert view["engine"]["workspace_groups"] == {"team": TEAM_GROUP}
    assert [i for i, s in enumerate(view["engine"]["stages"]) if s] == [0, 1, 8, 10]
    assert view["sizing"]["mode"] == "team" and view["sizing"]["members"] == 8
    assert view["steps"]["first_team"] == {"needed": True, "state": "to_do", "reason": None}
    assert view["github_app"] is None and view["teams"] == [] and view["finished"] is None
    assert view["github_org"] == ORG
    assert {a["name"] for a in view["addons"]} >= {"tailscale", "ci"}

    # 2. The GitHub App.
    github_app_step(console, setup_app, github)

    # 3. A pool account before a team: the API says the team comes first.
    console.call(cv.LINKS, body={"kind": "pool-account", "label": "pool-1", "owner": ALICE},
                 expect=409)

    # 4. The first team: the group check refuses a group the installer is not
    #    in, with its Google Admin page; once they are in, the team is made.
    refused = console.call(cv.TEAMS, body={"group": view["engine"]["workspace_groups"]["team"],
                                           "first_admin": ALICE, "invites": []}, expect=409)
    assert refused["detail"]["reason"] == "installer_not_member"
    assert refused["detail"]["add_members_url"].startswith("https://admin.google.com/")
    assert not db.paths(f"swarm-install/{ENV}/teams/")
    directory.join(ROOT, TEAM_GROUP)
    made = cv.norm_team_create(console.call(cv.TEAMS, body={
        "group": TEAM_GROUP, "first_admin": ALICE, "invites": [CAROL]}, expect=(200, 202)))
    team = made["team"]
    assert team and [i["email"] for i in made["invites"]] == [CAROL]
    assert made["invites"][0]["member"] is False
    assert made["invites"][0]["add_members_url"].startswith("https://admin.google.com/ac/groups/")
    workspace_ready(db, team)
    view = console.page()
    row = view["teams"][0]
    assert row["id"] == team and row["group"] == TEAM_GROUP and row["workspace"] == team
    assert row["first_admin"] == ALICE
    assert row["progress"]["workspace_state"] == "ready"
    assert row["grant"]["state"] == "granted"
    assert row["grant"]["repositories"] == sorted(REPOSITORIES)
    assert row["grant"]["account"] == ORG and row["grant"]["selection"] == "selected"
    assert view["steps"]["first_team"]["state"] == "done"
    assert view["steps"]["invites"]["state"] == "done"
    members = {m["email"]: m["member"] for m in view["teams"][0]["members"]}
    assert members == {ALICE: True, CAROL: False}

    # 5. Another invite: the Google Admin members page, then the membership
    #    polled until the Groups API says each person is in.
    invite = cv.norm_invite(console.call(cv.INVITES, f"/v1/admin/setup/teams/{team}/invites",
                                         body={"email": DAVE}))
    assert invite["email"] == DAVE and invite["member"] is False
    assert invite["add_members_url"].startswith("https://admin.google.com/ac/groups/")
    directory.join(CAROL, TEAM_GROUP)
    directory.join(DAVE, TEAM_GROUP)
    view = console.page()
    assert {m["email"]: m["member"] for m in view["teams"][0]["members"]} == {
        ALICE: True, CAROL: True, DAVE: True}
    assert view["teams"][0]["invites"] == [CAROL, DAVE]

    # 6. The pool account; the page sees it signed in and starts the smoke.
    pool_account_step(console, pool, owner=ALICE, user="alice", tenant=team)
    view = console.page()
    assert view["accounts"][0]["signed_in_at"] is not None
    assert view["teams"][0]["pool_accounts"] == ["pool-1"]
    assert view["teams"][0]["smoke"]["state"] == "running"
    assert runs.tasks[0]["tenant"] == team and runs.tasks[0]["group"] == TEAM_GROUP

    # 7. The console's verify runs, as the first team, on its granted repository.
    started = cv.norm_verify_runs(console.call(cv.VERIFY_RUNS))
    assert started["task"] and started["workflow"]
    assert started["repository"] in REPOSITORIES
    assert started["retries"] == {"task": 0, "workflow": 0}
    assert console.page()["verify_runs"] == started

    # 8. The smoke fails once and is started again on request; finish waits.
    runs.states[runs.tasks[0]["id"]] = "failed"
    assert console.page()["teams"][0]["smoke"]["state"] == "failed"
    waiting = console.call(cv.FINISH, expect=409)
    assert waiting["detail"]["missing"] == ["first_team_smoke"]
    again = cv.norm_restart_smoke(console.call(cv.SMOKE))
    assert again["workspace"] == team and again["smoke"]["state"] == "running"
    runs.states[runs.tasks[-1]["id"]] = "succeeded"
    assert console.page()["teams"][0]["smoke"]["state"] == "succeeded"

    # 9. The links the page offers after the first run: the team's git slot
    #    and an add-on's secret, each opened by the person it names.
    git_body = {"kind": "team-git", "label": team, "owner": ALICE}
    git = cv.norm_link(console.call(cv.LINKS, body=git_body, expect=201), git_body)
    opened = cv.opened_from(console.call(cv.REDEEM, body={
        "token": git["path"].rsplit("/", 1)[1], "value": secrets.token_hex(20)}, user="alice"))
    assert opened["kind"] == "team-git" and opened["secret"]
    addon_body = {"kind": "addon-secret", "label": "tailscale", "owner": ROOT}
    addon = cv.norm_link(console.call(cv.LINKS, body=addon_body, expect=201), addon_body)
    opened = cv.opened_from(console.call(cv.REDEEM, body={
        "token": addon["path"].rsplit("/", 1)[1], "value": secrets.token_hex(20)}))
    assert opened["kind"] == "addon-secret" and opened["secret"]

    # 10. Finish, and the engine reads it.
    done = cv.norm_finish(console.call(cv.FINISH))
    assert done["at"]
    assert console.page()["finished"]["by"] == ROOT
    engine_sees_finished(console, tenant=team, one_person=False)
    return console


def individual_walk(walk, setup_app, db, github, pool, runs) -> Console:
    """The one-person walk, from the hand-off to the engine reading `finished`."""
    console = walk(individual_outputs())
    tenant = base.personal_tenant(ROOT)

    # 1. The page: one person, no team step, the personal workspace first.
    view = console.page()
    assert view["mode"] == "individual" and view["sizing"]["members"] == 1
    assert view["engine"]["workspace_groups"] is None
    need = {"needed": False, "state": "not_needed", "reason": "not needed for one person"}
    assert view["steps"] == {"first_team": need, "invites": need}
    assert view["personal"]["owner"] == ROOT and view["personal"]["workspace"] == tenant
    assert view["personal"]["grant"]["state"] == "waiting_for_app"
    assert view["teams"] == []
    workspace_ready(db, tenant, person=ROOT)
    # Thirty seconds pass: `PersonalTenants` caches the installer's "not
    # ready yet" from the page reads before the job finished, for that long.
    setup_app.state.ctx.authenticator._personal._cache.clear()

    # 2. The GitHub App; the personal workspace is granted its repositories.
    github_app_step(console, setup_app, github)
    view = console.page()
    assert view["personal"]["grant"]["state"] == "granted"
    assert view["personal"]["grant"]["repositories"] == sorted(REPOSITORIES)
    assert view["personal"]["progress"]["workspace_state"] == "ready"

    # 3. No team and no invite for one person.
    console.call(cv.TEAMS, body={"group": TEAM_GROUP, "invites": []}, expect=409)
    console.call(cv.INVITES, "/v1/admin/setup/teams/anything/invites",
                 body={"email": CAROL}, expect=409)

    # 4. The installer's own pool account, in the personal workspace.
    pool_account_step(console, pool, owner=ROOT, user="root", tenant=tenant)
    view = console.page()
    assert view["personal"]["pool_accounts"] == ["pool-1"]
    assert view["accounts"][0]["signed_in_at"] is not None
    assert view["personal"]["smoke"]["state"] == "running"
    assert runs.tasks[0]["tenant"] == tenant and runs.tasks[0]["group"] == ROOT

    # 5. The verify runs, in the personal workspace.
    started = cv.norm_verify_runs(console.call(cv.VERIFY_RUNS))
    assert started["task"] and started["workflow"] and started["repository"] in REPOSITORIES
    assert runs.workflows[0]["tenant"] == tenant

    # 6. The smoke fails, starts again, passes.
    runs.states[runs.tasks[0]["id"]] = "failed"
    assert console.page()["personal"]["smoke"]["state"] == "failed"
    assert console.call(cv.FINISH, expect=409)["detail"]["missing"] == ["personal_smoke"]
    again = cv.norm_restart_smoke(console.call(cv.SMOKE))
    assert again["workspace"] == tenant and again["smoke"]["state"] == "running"
    runs.states[runs.tasks[-1]["id"]] = "succeeded"
    assert console.page()["personal"]["smoke"]["state"] == "succeeded"

    # 7. The personal workspace's git slot, then finish; the engine reads it.
    git_body = {"kind": "team-git", "label": tenant, "owner": ROOT}
    git = cv.norm_link(console.call(cv.LINKS, body=git_body, expect=201), git_body)
    opened = cv.opened_from(console.call(cv.REDEEM, body={
        "token": git["path"].rsplit("/", 1)[1], "value": secrets.token_hex(20)}))
    assert opened["kind"] == "team-git" and opened["secret"]
    assert cv.norm_finish(console.call(cv.FINISH))["at"]
    assert not db.paths(f"swarm-install/{ENV}/teams/")
    engine_sees_finished(console, tenant=tenant, one_person=True)
    return console


# --------------------------------------------------------------------------
# the tests
# --------------------------------------------------------------------------

#: What api.ts reads that a mode never serves BY DESIGN: a team has no
#: `personal` row and its steps carry no reason; one person has no team, and
#: the team and invite routes answer only their 409.
NOT_IN_MODE = {
    "team": {cv.SETUP: ["personal", *cv.PERSONAL_FIELDS, "steps.first_team.reason",
                        "steps.invites.reason"]},
    "individual": {cv.SETUP: [p for p in cv.FIELDS_READ[cv.SETUP] if p.startswith("teams[]")],
                   cv.TEAMS: list(cv.FIELDS_READ[cv.TEAMS]),
                   cv.INVITES: list(cv.FIELDS_READ[cv.INVITES])},
}

#: Fields api.ts reads that no route serves: real defects (findings E2E-2),
#: held by `test_the_smoke_row_serves_the_detail_and_link_the_page_shows`.
NEVER_SERVED = {
    cv.SETUP: ["teams[].smoke.detail", "teams[].smoke.url", "personal.smoke.detail",
               "personal.smoke.url"],
    cv.SMOKE: ["smoke.detail", "smoke.url"],
}


def _fields() -> dict[str, list[str]]:
    fields = {route: list(paths) for route, paths in cv.FIELDS_READ.items()}
    fields[cv.SETUP] += cv.PERSONAL_FIELDS
    return fields


def _without(gaps: dict[str, list[str]], *known: dict[str, list[str]]) -> dict[str, list[str]]:
    left = {route: [p for p in paths if not any(p in k.get(route, []) for k in known)]
            for route, paths in gaps.items()}
    return {route: paths for route, paths in left.items() if paths}


def test_a_team_install_walks_from_the_hand_off_to_finished(walk, setup_app, db, github,
                                                            pool, directory, runs) -> None:
    """The whole team walk; then every field api.ts reads was served by
    some answer of it, but what a team never has and the known defects."""
    console = team_walk(walk, setup_app, db, github, pool, directory, runs)
    gaps = cv.unserved(console.answers, _fields())
    assert _without(gaps, NOT_IN_MODE["team"], NEVER_SERVED) == {}


def test_a_one_person_install_walks_from_the_hand_off_to_finished(walk, setup_app, db,
                                                                  github, pool, runs) -> None:
    console = individual_walk(walk, setup_app, db, github, pool, runs)
    gaps = cv.unserved(console.answers, _fields())
    assert _without(gaps, NOT_IN_MODE["individual"], NEVER_SERVED) == {}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "E2E-2 (findings): api.ts reads a smoke's `detail` (or `reason`) and `url`, and a smoke "
    "whose task fails serves neither: routes/setup.py advance_smoke records state, task_id, "
    "repository and times, a `reason` only when the submission itself fails, so the "
    "failed row says 'its task's page says why' with no why and no link to that page"))
def test_the_smoke_row_serves_the_detail_and_link_the_page_shows(walk, setup_app, db, github,
                                                                 pool, directory, runs) -> None:
    console = team_walk(walk, setup_app, db, github, pool, directory, runs)
    gaps = _without(cv.unserved(console.answers, _fields()), NOT_IN_MODE["team"])
    assert {route: [p for p in paths if p in NEVER_SERVED.get(route, [])]
            for route, paths in gaps.items() if route in NEVER_SERVED} == {}


def test_the_engine_reads_every_app_field_pass_3_carries(walk, setup_app, github) -> None:
    """Infra pass 3 copies `github_switches.APP_FIELDS` from the console's
    `github_app` document into its tfvars (`tfvars.py`), which terraform/infra
    wants set together (github_app.tf, check
    `github_app_settings_are_complete`; the console names the App by
    GITHUB_APP_NAME)."""
    console = walk(team_outputs())
    github_app_step(console, setup_app, github)
    app = github_switches.app_document(console.engine)
    assert [k for k in github_switches.APP_FIELDS if not app.get(k)] == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "E2E-3 (findings): for one person, the installer's pool-account sign-in is refused "
    "403 tenant_not_member for up to 30 s after the personal workspace turns ready: "
    "auth.PersonalTenants caches the 'not ready' the page's own polling read"))
def test_one_persons_sign_in_works_as_soon_as_the_workspace_is_ready(walk, setup_app, db,
                                                                    github, pool) -> None:
    """The page is read (as it polls) before swarm-workspace-apply finishes
    the personal workspace; the moment it is ready, the installer opens their
    pool-account link and signs in, with no wait the page does not show."""
    console = walk(individual_outputs())
    tenant = base.personal_tenant(ROOT)
    assert console.page()["personal"]["workspace"] == tenant
    workspace_ready(db, tenant, person=ROOT)
    body = {"kind": "pool-account", "label": "pool-1", "owner": ROOT}
    link = cv.norm_link(console.call(cv.LINKS, body=body, expect=201), body)
    raw = console.call(cv.REDEEM, body={"token": link["path"].rsplit("/", 1)[1]})
    begun = console.client.post(raw["sign_in"]["authorize"],
                                headers={**auth_header("root"), raw["sign_in"]["header"]: tenant},
                                json={"label": "pool-1", "lend_to": []})
    assert begun.status_code == 200, begun.text


def test_the_walks_fake_refuses_what_real_firestore_refuses() -> None:
    """The rules the walk holds every path to, on the paths audit 3's A2 hit."""
    assert path_errors(f"swarm-install/{ENV}/console/teams/platform-team", collection=False)
    assert path_errors(f"swarm-install/{ENV}", collection=True)
    assert path_errors(f"swarm-install/{ENV}//teams", collection=False)
    assert path_errors(f"swarm-install/__{ENV}__", collection=False)
    assert not path_errors(f"swarm-install/{ENV}/teams/platform-team", collection=False)
    db = StrictFirestore()
    with pytest.raises(ValueError):
        db.collection(f"swarm-install/{ENV}/console").document("teams/platform-team")
    db.document(f"swarm-install/{ENV}/console/x").set({"rows": [[1, 2]], "owner": object()})
    errors = db.stored_errors()
    assert any("array directly inside an array" in e for e in errors)
    assert any("cannot encode" in e for e in errors)
