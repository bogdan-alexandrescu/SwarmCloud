"""No route serves credential material. Asserted by sweeping every route.

WHY A SWEEP AND NOT A LIST. Credential leaks are not usually written into the
route that handles credentials -- that one is designed. They arrive through a
route that echoes something back: a task's own input, a `last_error` carrying
an upstream message that named a secret, a metadata blob, a workflow step's
input. `swarm_api/redaction.py` says it plainly: the worker's scrubbing pass
covers REGISTERED LITERAL VALUES and nothing else, so "a pass may have run
over this" is not a basis on which to hand bytes to a browser.

So the check is over every GET route the app mounts, not over the ones that
look risky. A route added tomorrow is swept the day it is added, without
anybody editing this file.

THREE SENTINELS, three different claims:

  * REGISTERED_KEY is stored through `POST /v1/tenants/me/credentials`, which
    is write-only by design. It must not come back to ANYONE, including the
    tenant that stored it.
  * OTHER_TENANTS_SECRET is credential-shaped text inside another tenant's
    task. It must not reach this caller through any route, including the
    admin ones that cross tenants.
  * OWN_TASK_SECRET is credential-shaped text the caller put in their own
    task. It never reaches a DIFFERENT tenant, and it is redacted out of the
    log stream, which is the surface that is served as raw bytes. Since the
    owner's decision of 2026-09-26 it does not come back in the task's input
    or metadata to its OWN tenant either -- both are served masked -- and
    since the PR #229 review not through `last_error` or `result_summary`
    either: both are masked by the input's masker, with their counts.

TWO CARVE-OUTS, NAMED. Owner decision Q2 (docs/design/workspace-secrets.md
§10): `POST /v1/attempts/{attempt_id}/secrets:release` hands an attempt its
granted workspace secrets, signed by its worker's attempt key and fenced by its
generation. Owner decision Q9 of #1433 (docs/design/registered-mcp-servers.md
§4.6): `POST /v1/attempts/{attempt_id}/mcp:attach` hands an attempt its
registered MCP servers' credentials, signed under its own purpose, fenced and
once, through the same reader. Both are outside the GET sweep because they
are POSTs, and that is a reason to name them rather than to rely on the sweep
missing them. So the last
tests below find every mounted route that can read a workspace secret's value
(it depends on `attempts_secrets.secret_reader`, the one place a reader is
made) and every module that reads any Secret Manager payload, and require each
set to be exactly what is named here. A second credential-serving route fails
them until someone names it, with the owner's decision to point to.

Offline: FakeFirestore, an in-memory credential store and an in-memory object
reader. No credentials, no emulator, no network.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.codec import tenant_to_api
from swarm_api.redaction import redact

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)

#: Shaped like the real thing on purpose -- `redaction.py` matches families by
#: prefix, so a sentinel of `xxxx` would prove nothing about the rules that
#: matter.
REGISTERED_KEY = "sk-ant-api03-REGISTEREDoOnLyWrItE-0123456789abcdef"
#: Deliberately a DIFFERENT credential family from REGISTERED_KEY. Both tests
#: below sweep every route; if the two sentinels shared the `sk-ant-api03-`
#: prefix, research's own task -- which research is entitled to read back --
#: would trip the "nothing anywhere may look like an Anthropic key" assertion,
#: and the only way to keep that assertion would be to weaken it.
OTHER_TENANTS_SECRET = "xoxb-SYNTHETIC-RESEARCHtEnAnTsEcReT-NOT-REAL"
OWN_TASK_SECRET = "ghp_OWNTASKSECRET0123456789abcdefghijkl"

#: Every router `create_app` mounts. Named rather than discovered, for the same
#: reason test_runtimes_screen.py names them: discovering from the app object
#: would make this agree with whatever main.py happens to do, and a router
#: added and never mounted would pass silently.
ROUTER_MODULES = (
    "platform", "tasks", "attempts", "workflows", "tenants", "admin", "accounts", "health",
    # The checkpoint CONTENT routes (listing, one file, the whole archive).
    # A router missing from this tuple is outside every sweep in this file.
    "checkpoints",
    # GET /v1/outcomes (#185): counts and task ids across a span, never task
    # content. Swept WITH a tz (QUERY_FOR below), so the sweep reads a real
    # 200 rather than the 422 a missing tz earns.
    "outcomes",
    # GET /v1/leases (#179): the caller's own tenant's heartbeats, ids and
    # timestamps only.
    "leases",
    # GET /v1/provider-keys/{provider} (KEYS-API): whether a TypeSafe key is
    # set, when and by whom, never the key. Swept for `typesafe`.
    "provider_keys",
)

#: A query string for a route that refuses to answer without one. Stripped
#: again before a swept path is compared with its template.
QUERY_FOR = {"/v1/outcomes": "?tz=UTC&span=7d"}


@pytest.fixture
def leaky(client, db):
    """Two tenants, both carrying credential-shaped text somewhere."""
    seed_tenant(db, "eng", credentials=("anthropic",))
    seed_tenant(db, "research", credentials=("anthropic",))
    for name in ("global", "resource:standard", "runner:mock", "backend:cloudrun"):
        seed_pool(db, name, hard_limit=20, active=1)

    mine = seed_task(db, task_id="task_mine", tenant_id="eng", state="RUNNING")
    mine.update(
        {
            "input": {"prompt": f"use {OWN_TASK_SECRET} to push the branch"},
            "metadata": {"note": OWN_TASK_SECRET},
            "last_error": f"clone failed for {OWN_TASK_SECRET}",
            "result_summary": {"stdout_tail": OWN_TASK_SECRET},
        }
    )

    theirs = seed_task(db, task_id="task_theirs", tenant_id="research", state="RUNNING")
    theirs.update(
        {
            "input": {"prompt": f"the key is {OTHER_TENANTS_SECRET}"},
            "metadata": {"note": OTHER_TENANTS_SECRET},
            "last_error": f"provider rejected {OTHER_TENANTS_SECRET}",
            "result_summary": {"stdout_tail": OTHER_TENANTS_SECRET},
        }
    )

    # An attempt's `error` is upstream text, and `GET /v1/attempts` serves
    # attempts across every task of a tenant -- so the cross-task route is the
    # one place research's error could reach eng without naming research's task.
    db.docs["attempts/att_theirs"] = {
        "attempt_id": "att_theirs", "task_id": "task_theirs", "tenant_id": "research",
        "generation": 1, "lease_id": "lease_theirs", "backend": "CLOUD_RUN_JOB",
        "created_at": NOW, "exit_code": 1,
        "error": f"provider rejected {OTHER_TENANTS_SECRET}",
    }

    db.docs["workflows/wf_theirs"] = {
        "workflow_id": "wf_theirs", "tenant_id": "research",
        "created_at": NOW, "updated_at": NOW, "state": "RUNNING",
        "submitted_by": "bob@example.com",
        "steps": [{"step_id": "s1", "runner_profile": "mock",
                   "input": {"prompt": OTHER_TENANTS_SECRET}, "depends_on": [],
                   "resource_class": "standard", "input_from": {},
                   "timeout_seconds": 600, "task_id": "task_theirs"}],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }

    # Stored the way a caller really stores one, through the shipped route.
    stored = client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "anthropic", "api_key": REGISTERED_KEY},
    )
    assert stored.status_code == 201, stored.text
    assert REGISTERED_KEY not in stored.text, (
        "the credential route echoed the key it was given back in its own response"
    )
    # The same key as eng's TypeSafe tenant key, through its own write-only
    # route (KEYS-API), so the sweep proves that route's status read too.
    stored = client.put("/v1/provider-keys/typesafe/tenant", headers=auth_header("root"),
                        json={"api_key": REGISTERED_KEY})
    assert stored.status_code == 200, stored.text
    assert REGISTERED_KEY not in stored.text
    return client


def _get_routes() -> list[str]:
    """Every GET path the mounted routers declare, with ids filled in."""
    paths: list[str] = []
    for name in ROUTER_MODULES:
        module = importlib.import_module(f"swarm_api.routes.{name}")
        for route in module.router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set()) or set()
            if not path or "GET" not in methods:
                continue
            filled = (
                path.replace("{task_id}", "task_theirs")
                .replace("{workflow_id}", "wf_theirs")
                .replace("{account_id}", "acct_1")
                .replace("{checkpoint_id}", "ckpt-00001")
                .replace("{path:path}", "notes.md")
            )
            if filled.startswith("/v1/provider-keys/"):
                filled = filled.replace("{provider}", "typesafe")
            if re.search(r"\{[^}]*\}", filled):
                # An unfilled parameter would request a resource that does not
                # exist, which proves nothing. Anything left here is a route
                # this sweep does not reach, and the test below says so.
                continue
            paths.append(filled + QUERY_FOR.get(filled, ""))
    return sorted(set(paths))


def test_the_sweep_actually_reaches_the_routes(leaky):
    """A sweep over an empty list passes vacuously and looks thorough."""
    routes = _get_routes()
    assert len(routes) >= 15, f"only {len(routes)} GET routes were collected: {routes}"
    for expected in ("/v1/capacity", "/v1/tasks", "/v1/tasks/task_theirs",
                     "/v1/admin/leases", "/v1/tenants/me", "/v1/stats",
                     "/v1/attempts", "/v1/leases"):
        assert expected in routes, f"{expected} is not in the sweep"


UNREACHED_BY_THE_SWEEP = {
    # Path parameters this sweep cannot invent a real value for. Each is
    # covered by its own test rather than left unmentioned: a route quietly
    # outside the sweep is the gap this file is about.
    "/v1/admin/limits/{provider}",
    # Serves `{"tenant_id", "findings_epic"}`, an issue number and nothing
    # else (#638); read back in test_findings_epic_setting.py.
    "/v1/admin/tenants/{tenant_id}/findings-epic",
}


def test_every_get_route_is_either_swept_or_named(leaky):
    """A route outside the sweep must be a decision, not an omission."""
    declared: set[str] = set()
    for name in ROUTER_MODULES:
        module = importlib.import_module(f"swarm_api.routes.{name}")
        for route in module.router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set()) or set()
            if path and "GET" in methods:
                declared.add(path)

    swept_templates = {
        p.split("?")[0].replace("task_theirs", "{task_id}")
         .replace("wf_theirs", "{workflow_id}")
         .replace("acct_1", "{account_id}")
         .replace("ckpt-00001", "{checkpoint_id}")
         .replace("notes.md", "{path:path}")
         .replace("/v1/provider-keys/typesafe", "/v1/provider-keys/{provider}")
        for p in _get_routes()
    }
    unreached = sorted(declared - swept_templates - UNREACHED_BY_THE_SWEEP)
    assert not unreached, (
        f"these GET routes are served and never swept for credential material: "
        f"{unreached}. Give the sweep a value for their parameters, or name "
        "them in UNREACHED_BY_THE_SWEEP with the reason."
    )


@pytest.mark.parametrize("user", ["root", "alice", "bob", "carol"])
def test_no_get_route_ever_serves_a_registered_credential(leaky, user: str):
    """Write-only means write-only, including for the tenant that wrote it.

    Whatever the status code: a 500 whose body carries the key is a leak, and
    filtering to 200s would step over exactly the responses most likely to
    echo an internal value.
    """
    for path in _get_routes():
        response = leaky.get(path, headers=auth_header(user))
        assert REGISTERED_KEY not in response.text, (
            f"{path} served the registered credential to {user} "
            f"(HTTP {response.status_code})"
        )
        # The prefix alone is enough to be worth refusing: `sk-ant-api03-` in a
        # response body is either the key or something shaped closely enough
        # that nobody should have to tell them apart by eye.
        assert "sk-ant-api03-" not in response.text, (
            f"{path} served something credential-shaped to {user} "
            f"(HTTP {response.status_code})"
        )


@pytest.mark.parametrize("user", ["alice", "carol"])
def test_no_route_leaks_another_tenants_task_content(leaky, user: str):
    """Tenant isolation, stated as a credential property.

    `task_to_api` serves `input`, `metadata`, `last_error` and
    `result_summary` verbatim, which is right for the tenant that wrote them
    and is a disclosure for anybody else.
    """
    for path in _get_routes():
        response = leaky.get(path, headers=auth_header(user))
        assert OTHER_TENANTS_SECRET not in response.text, (
            f"{path} served research's task content to {user} "
            f"(HTTP {response.status_code})"
        )


def test_an_admin_does_not_get_another_tenants_task_content_either(leaky):
    """Being an admin widens which COUNTS you see, not whose prompts.

    `/v1/admin/leases` denormalises `last_error` onto every row across every
    tenant, which is the one admin surface that carries task-authored text --
    and the route's own docstring says it is a stable code plus an attempt id
    for exactly this reason.
    """
    for path in _get_routes():
        response = leaky.get(path, headers=auth_header("root"))
        assert OTHER_TENANTS_SECRET not in response.text, (
            f"{path} served research's task content to an admin "
            f"(HTTP {response.status_code})"
        )


def test_the_task_owners_own_text_does_come_back_to_them(leaky):
    """The control, without which every assertion above could pass vacuously.

    If the API served nothing at all, or the sentinel never reached a document,
    the sweeps would be green and would mean nothing. Alice's own task text
    comes back to her, with the sentinel MASKED in it: its identifying prefix
    and the text around it are served, and each field counts one mask.

    HER PROMPT AND METADATA COME BACK MASKED (owner decision, 2026-09-26, on
    #184: "the API never serves a credential-shaped string back, even to the
    submitter"), and so, since the PR #229 review, do `last_error` and
    `result_summary`; the sweep over every route's whole body is
    `test_task_masked_everywhere.py`.
    """
    response = leaky.get("/v1/tasks/task_mine", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["last_error"] == "clone failed for ghp_********", (
        "the fixture's sentinel never reached a served payload, so the leak "
        "sweeps above were comparing against nothing"
    )
    assert task["last_error_redaction_count"] == 1
    assert task["result_summary"] == {"stdout_tail": "ghp_********"}
    assert task["result_summary_redaction_count"] == 1
    assert OWN_TASK_SECRET not in response.text, "the owner's own sentinel came back raw"
    assert task["input_redaction_count"] == 1
    assert task["metadata_redaction_count"] == 1


def test_a_tenants_credential_list_is_provider_names_and_never_key_material():
    """`tenant_to_api` is where a key would be one attribute away from public."""
    from swarm_common.models import Tenant

    tenant = Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        credentials=["anthropic", "openai"],
    )
    payload = tenant_to_api(tenant)
    assert payload["credentials"] == ["anthropic", "openai"]
    assert not any(isinstance(v, str) and "sk-" in v for v in payload.values())


@pytest.mark.parametrize(
    "text",
    (
        f"ANTHROPIC_API_KEY={REGISTERED_KEY}",
        f"Authorization: Bearer {REGISTERED_KEY}",
        f"the agent printed {OWN_TASK_SECRET} to stdout",
    ),
)
def test_read_time_redaction_covers_the_bytes_this_api_hands_over_raw(text: str):
    """The log stream is the one surface served as bytes rather than as fields.

    The worker's scrub covers registered literals only and short-circuits
    entirely for a profile that registered none, so the API redacts again at
    read time regardless of what happened at write time.
    """
    result = redact(text)
    cleaned = result.text
    assert REGISTERED_KEY not in cleaned
    assert OWN_TASK_SECRET not in cleaned
    assert "********" in cleaned
    assert result.count >= 1, "a masked credential must also be COUNTED: a UI that cannot say 'this log had four credentials in it' is hiding an incident"


def test_the_redaction_helper_leaves_ordinary_text_alone():
    """A redactor that masks everything hides the incident it was read for."""
    ordinary = "attempt att_1 exited 137 after 412s; peak RSS 6.1 GiB"
    assert redact(ordinary).text == ordinary
    assert redact(ordinary).count == 0


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for k, v in value.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list):
        out = []
        for i, v in enumerate(value):
            out.extend(_flatten(v, f"{prefix}[{i}]"))
        return out
    return [(prefix, value)]


def test_no_response_carries_a_key_named_like_a_secret(leaky):
    """A field CALLED `api_key` is a leak even when this fixture left it empty.

    The value-based sweeps above only fail once somebody has stored a real
    credential. This fails the moment a serialiser gains the field.
    """
    forbidden = re.compile(r"(api_key|secret|password|private_key|token)$", re.IGNORECASE)
    allowed = {
        # Pagination, not credentials. Named so the pattern stays tight rather
        # than being loosened to let these through.
        "next_page_token",
        "page_token",
    }
    problems: list[str] = []
    for path in _get_routes():
        response = leaky.get(path, headers=auth_header("root"))
        if response.status_code != 200:
            continue
        try:
            body = response.json()
        except ValueError:
            continue
        for key_path, _ in _flatten(body):
            leaf = key_path.split(".")[-1].split("[")[0]
            if leaf in allowed:
                continue
            if forbidden.search(leaf):
                problems.append(f"{path} -> {key_path}")
    assert not problems, (
        f"these responses carry a field named like credential material: "
        f"{sorted(problems)}"
    )


# --------------------------------------------------------------------------
# The two carve-outs (owner decisions Q2 of #1347 and Q9 of #1433)
# --------------------------------------------------------------------------

#: Every route that may serve credential material, by (method, path). Each
#: only by an owner's decision: docs/design/workspace-secrets.md §10 Q2 for
#: the release, docs/design/registered-mcp-servers.md §7 Q9 for the MCP attach.
CREDENTIAL_SERVING_ROUTES = {
    ("POST", "/v1/attempts/{attempt_id}/secrets:release"),
    ("POST", "/v1/attempts/{attempt_id}/mcp:attach"),
}

#: Every module that reads a Secret Manager PAYLOAD, and what it does with it.
#: The release serves one, and so does the QA target issue below, to its
#: fenced attempt only; the others use the value inside swarm-api.
SECRET_PAYLOAD_READERS = {
    # The release: the carve-out above, and the only reader that answers with it.
    "routes/attempts_secrets.py",
    # The platform's search key, sent to the provider and scrubbed from every answer.
    "search.py",
    # The forge App's private key, used to mint installation tokens.
    "forgeapp.py",
    # A tenant's forge token, sent to GitHub for issue previews and writes.
    "forge.py",
    # A QA target's test credential (docs/design/visual-qa-any-app.md §2.1,
    # §2.2): read to merge a new one in or drop a deleted target's, and served
    # only to the worker of a proven, fenced attempt whose run names the target,
    # at qatargets.ISSUE_ROUTE, which main.UNCAPTURED_ROUTES keeps out of capture.
    "qatargets.py",
    # The Desktop OAuth client's secret, read once at start and served in the
    # unauthenticated discovery document ONLY when DISCOVERY_SERVE_CLIENT_SECRET
    # is true: BGD's Q1 decision (docs/design/day-one-plan.md §6.3; Google
    # classifies a Desktop client's secret as non-confidential). A separate
    # app, swarm-discovery, never mounted on swarm-api's routes.
    "discovery.py",
    # gittools.py read the `-git-wf` slot until #2029 (WF-APP-TOKEN) removed
    # it: a workflow-touching push now uses an App installation token minted
    # per push, never a stored secret. MUTATION: keep the slot reader and
    # gittools.py is a reader again, outside this set.
}


def _api_routes(routes: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every APIRoute the app serves, with its full path: through included
    routers too, which newer FastAPI keeps as wrappers around the original."""
    from fastapi.routing import APIRoute

    out: list[tuple[str, Any]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append((prefix + route.path, route))
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            out.extend(_api_routes(inner.routes, prefix + (getattr(context, "prefix", "") or "")))
    return out


def _depends_on(dependant: Any, target: Any) -> bool:
    for sub in dependant.dependencies:
        if sub.call is target or _depends_on(sub, target):
            return True
    return False


def test_only_the_named_routes_can_read_a_workspace_secrets_value():
    from swarm_api.main import create_app
    from swarm_api.routes import attempts_secrets

    from .conftest import api_settings

    from swarm_api.deps import build_context
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.groups import StaticGroups
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker
    from .fakes import FakeFirestore

    ctx = build_context(settings=api_settings(), db=FakeFirestore(),
                        verifier=StaticTokenVerifier({}), groups=StaticGroups({}),
                        credentials=InMemoryCredentials(), waker=NullWaker(),
                        metrics=ApiMetrics())
    app = create_app(ctx)
    routes = _api_routes(app.routes)
    serving = {
        (method, path)
        for path, route in routes if _depends_on(route.dependant, attempts_secrets.secret_reader)
        for method in route.methods
    }
    assert len(routes) > 100, f"the walk reached only {len(routes)} routes"
    assert serving == CREDENTIAL_SERVING_ROUTES, (
        f"routes that can read a workspace secret's value: {sorted(serving)}. Only the "
        "release (owner decision Q2 of #1347) and the MCP attach (Q9 of #1433) may; "
        "another needs its own decision, named here."
    )


def test_only_the_named_modules_read_a_secret_payload():
    """A route that builds its own reader would be outside the walk above, so
    the payload read itself is found in the source."""
    from pathlib import Path

    import swarm_api

    root = Path(swarm_api.__file__).parent
    readers = {
        str(path.relative_to(root)) for path in root.rglob("*.py")
        if "access_secret_version" in path.read_text(encoding="utf-8")
    }
    assert readers == SECRET_PAYLOAD_READERS, (
        f"modules reading a Secret Manager payload: {sorted(readers)}; name each in "
        "SECRET_PAYLOAD_READERS with what it does with the value, and never serve one "
        "outside CREDENTIAL_SERVING_ROUTES"
    )
    makers = {
        str(path.relative_to(root)) for path in root.rglob("*.py")
        if "SecretManagerReader(" in path.read_text(encoding="utf-8")
    }
    assert makers == {"routes/attempts_secrets.py"}


# --------------------------------------------------------------------------
# What a run's forge credential WAS, never what it is
# --------------------------------------------------------------------------
#
# DAYONE-FORGE-CRED-RECORD (part of #2688). The worker records, in an
# attempt's result summary, the KIND of GitHub credential the run cloned and
# pushed with and the secret NAME it came from (`result_summary.
# forge_credential`); stage 14 of `swarm install` reads it through `swarm
# result <task> --json` to check lesson 43 (an App installation token, never a
# PAT). It is a kind and a name, not credential material: served where the
# summary already is, the task read, unchanged by the masking, and never to
# another tenant's caller.
FORGE_RECORD = {"kind": "app_installation", "source": "swarm-tenant-eng-git"}


def _seed_forge_record(db, record) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_cred", tenant_id="eng", state="SUCCEEDED")
    doc["result_summary"] = {"summary": "done", "forge_credential": record}


@pytest.mark.parametrize("record", [
    FORGE_RECORD,
    {"kind": "pat", "source": "swarm-tenant-eng-git"},
    {"kind": "mixed", "kinds": ["app_installation", "pat"], "source": "swarm-tenant-eng-git"},
    {"kind": "none", "source": None},
])
def test_the_task_read_serves_the_recorded_kind_unmasked(client, db, record):
    _seed_forge_record(db, record)
    response = client.get("/v1/tasks/task_cred", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["result_summary"]["forge_credential"] == record
    assert task["result_summary_redaction_count"] == 0


def test_another_tenants_caller_is_served_no_forge_record(client, db):
    _seed_forge_record(db, FORGE_RECORD)
    response = client.get("/v1/tasks/task_cred", headers=auth_header("bob"))
    assert response.status_code in (403, 404)
    assert "forge_credential" not in response.text
