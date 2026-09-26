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
        "submitted_by": "bob@saga.xyz",
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
                     "/v1/attempts"):
        assert expected in routes, f"{expected} is not in the sweep"


UNREACHED_BY_THE_SWEEP = {
    # Path parameters this sweep cannot invent a real value for. Each is
    # covered by its own test rather than left unmentioned: a route quietly
    # outside the sweep is the gap this file is about.
    "/v1/admin/limits/{provider}",
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
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
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
