"""The `server` group: server values the console restates and its tests hold.

Limits, flag names, codes and refusal sentences defined outside the console --
in swarm-api, the agent worker, the installer, the plugin-intake job and the
scheduler's Terraform -- that the console repeats. The console's tests compare
their copies with this file instead of opening those sources.

Read from live objects, except one: Terraform has no Python object, so the
Cloud Scheduler jobs are parsed from `terraform/modules/scheduler/jobs.tf` by
`scheduler_jobs` (tested in tests/unit/scripts/test_api_constants_server.py).

  * `workspace_secrets`: `SLOT_HOST`'s pattern, `MAX_SLOT_HOSTS`,
    `HOST_SCOPED_SLOTS`, and the three refusals `slot_hosts` raises, captured
    by calling it with a refused host list;
  * `repository_url_prefixes`: `validation.REPOSITORY_URL_PREFIXES`;
  * `merge_max_head_updates`: `agent_worker.merge.MERGE_MAX_HEAD_UPDATES`;
  * `doctor`: the keys of a sample `Finding.to_json()` (sorted), the
    MET/UNMET/UNKNOWN statuses, and `tfvars.SCHEDULED_DOCTOR_SHIPPED`;
  * `team_add_flags`: the long options of `swarm team add`, from the parser
    `swarm_install.cli.build_parser` builds;
  * `plugin_refusals`: the `REFUSALS` tables of swarm-api's plugin store and
    of the plugin-intake job;
  * `schema_fields`: `WorkflowStepCreate` and `StepPluginCreate` field names;
  * `events_route_pages`: whether the task events route takes `page_token`,
    from swarm-api's OpenAPI document;
  * `read_scope_checks_collision_only`: whether `SubmissionService.scope_for`
    consults nothing but `Store.assert_tenant_scope` (so a disabled tenant can
    still read), found by calling it on a probe that records what it touches;
  * `scheduler_jobs`: each `google_cloud_scheduler_job` in file order.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

INPUTS = (
    "scripts/api_constants/server.py",
    "scripts/gen_api_types.py",
    "apps/swarm-api/swarm_api/",
    "apps/common/swarm_common/",
    "apps/agent-worker/agent_worker/merge.py",
    "apps/swarm-install/swarm_install/",
    "apps/plugin-intake/plugin_intake/",
    "terraform/modules/scheduler/jobs.tf",
)

JOBS_TF = REPO / "terraform/modules/scheduler/jobs.tf"
PLUGIN_INTAKE = REPO / "apps/plugin-intake"
EVENTS_PATH = "/v1/tasks/{task_id}/events"

_JOB = re.compile(r'^resource "google_cloud_scheduler_job" "([A-Za-z0-9_]+)" \{$', re.M)
_NAME_PREFIX = "${var.name_prefix}-"


def _hcl_string(text: str, start: int) -> str:
    """The HCL string literal whose opening quote is at `start`, unquoted.

    Quotes inside `${...}` belong to the interpolation, not the string."""
    if text[start] != '"':
        raise ValueError(f"no string at offset {start}")
    out: list[str] = []
    depth = 0
    i = start + 1
    while i < len(text):
        ch = text[i]
        if depth == 0 and ch == '"':
            return "".join(out)
        if depth == 0 and ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if text.startswith("${", i):
            depth += 1
            out.append("${")
            i += 2
            continue
        if depth and ch == "}":
            depth -= 1
        elif depth and ch == '"':
            # A string inside the interpolation: copy it whole.
            end = text.index('"', i + 1)
            out.append(text[i:end + 1])
            i = end + 1
            continue
        out.append(ch)
        i += 1
    raise ValueError(f"unterminated string at offset {start}")


def _attribute(block: str, name: str) -> str | None:
    """The first string attribute `name = "..."` in `block`, or None."""
    m = re.search(rf'^\s*{name}\s*=\s*"', block, re.M)
    return None if m is None else _hcl_string(block, m.end() - 1)


def scheduler_jobs(text: str) -> list[dict[str, Any]]:
    """Every `google_cloud_scheduler_job` in `text`, in file order: its resource
    name, its job name after `${var.name_prefix}-` with `${each.key}` read as
    `<tenant>`, and its HTTP target's `uri` as written (None for a Pub/Sub job).
    A block ends at the first line that is a lone `}`."""
    jobs = []
    for m in _JOB.finditer(text):
        end = text.find("\n}", m.end())
        block = text[m.end():len(text) if end == -1 else end]
        name = _attribute(block, "name")
        if name is None or not name.startswith(_NAME_PREFIX):
            raise ValueError(f"scheduler job {m.group(1)} has no name starting {_NAME_PREFIX!r}")
        jobs.append({
            "resource": m.group(1),
            "job": name[len(_NAME_PREFIX):].replace("${each.key}", "<tenant>"),
            "uri": _attribute(block, "uri"),
        })
    return jobs


def intake_refusals() -> dict[str, str]:
    """The plugin-intake job's `REFUSALS`. The job is not a member of the root
    uv project; it goes on `sys.path` as its own tests put it."""
    path = str(PLUGIN_INTAKE)
    if path not in sys.path:
        sys.path.insert(0, path)
    from plugin_intake.refusals import REFUSALS

    return dict(REFUSALS)


def _workspace_secrets() -> dict[str, Any]:
    from swarm_api import workspace_secrets as ws
    from swarm_api.errors import ValidationFailed

    def refusal(slot: str, hosts: list[str]) -> str:
        try:
            ws.slot_hosts(slot, hosts, enabled=True)
        except ValidationFailed as exc:
            return exc.message
        raise RuntimeError(f"slot_hosts accepted {hosts!r} for {slot}; the probe is wrong")

    slot = ws.HOST_SCOPED_SLOTS[0]
    return {
        "slot_host_pattern": ws.SLOT_HOST.pattern,
        "max_slot_hosts": ws.MAX_SLOT_HOSTS,
        "host_scoped_slots": list(ws.HOST_SCOPED_SLOTS),
        "host_refused": refusal(slot, ["https://example.com"]),
        "hosts_too_many": refusal(slot, [f"h{i}.example.com" for i in range(ws.MAX_SLOT_HOSTS + 1)]),
        "hosts_needed": {s: refusal(s, []) for s in ws.HOST_SCOPED_SLOTS},
    }


def _team_add_flags() -> list[str]:
    import argparse

    from swarm_install import verbs
    from swarm_install.cli import build_parser

    def sub(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
        for action in parser._actions:  # noqa: SLF001 -- argparse exposes no other way in
            if isinstance(action, argparse._SubParsersAction) and name in action.choices:  # noqa: SLF001
                return action.choices[name]
        raise RuntimeError(f"the swarm parser has no {name!r} subcommand")

    add = sub(sub(build_parser(verbs.discover()), "team"), "add")
    return sorted(o for a in add._actions for o in a.option_strings  # noqa: SLF001
                  if o.startswith("--") and o != "--help")


def _read_scope_checks_collision_only() -> bool:
    from swarm_api.service import SubmissionService

    class Consulted(Exception):
        pass

    checked: list[str] = []

    class Probe:
        """Answers the collision check and records anything else asked of it."""

        def __init__(self, **known: Any) -> None:
            self.__dict__.update(known)

        def __getattr__(self, name: str) -> Any:
            raise Consulted(name)

    store = Probe(assert_tenant_scope=lambda tenant_id, principal: checked.append(tenant_id))
    ctx = Probe(tenant_id="probe", tenant_principal=None, email="probe@example.com")
    try:
        SubmissionService.scope_for(Probe(_store=store), ctx)
    except Consulted:
        return False
    if checked != ["probe"]:
        raise RuntimeError("scope_for no longer calls Store.assert_tenant_scope; re-read it")
    return True


def _events_route_pages() -> bool:
    import importlib.util

    module = sys.modules.get("gen_api_types")
    if module is None:
        spec = importlib.util.spec_from_file_location("gen_api_types", REPO / "scripts/gen_api_types.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["gen_api_types"] = module
        spec.loader.exec_module(module)
    route = module.openapi_document()["paths"].get(EVENTS_PATH, {}).get("get")
    if route is None:
        raise RuntimeError(f"the OpenAPI document has no GET {EVENTS_PATH}")
    return any(p["name"] == "page_token" and p["in"] == "query" for p in route.get("parameters", []))


def build() -> dict[str, Any]:
    from agent_worker.merge import MERGE_MAX_HEAD_UPDATES
    from swarm_install import exitcodes
    from swarm_install.doctor import Finding
    from swarm_install.tfvars import SCHEDULED_DOCTOR_SHIPPED

    from swarm_api.pluginstore import REFUSALS as API_REFUSALS
    from swarm_api.schemas import StepPluginCreate, WorkflowStepCreate
    from swarm_api.validation import REPOSITORY_URL_PREFIXES

    sample = Finding(id="api.example", status=exitcodes.MET, detail="seen")
    return {
        "workspace_secrets": _workspace_secrets(),
        "repository_url_prefixes": list(REPOSITORY_URL_PREFIXES),
        "merge_max_head_updates": MERGE_MAX_HEAD_UPDATES,
        "doctor": {
            "finding_keys": sorted(sample.to_json()),
            "statuses": [exitcodes.MET, exitcodes.UNMET, exitcodes.UNKNOWN],
            "scheduled_doctor_shipped": SCHEDULED_DOCTOR_SHIPPED,
        },
        "team_add_flags": _team_add_flags(),
        "plugin_refusals": {"api": dict(API_REFUSALS), "intake": intake_refusals()},
        "schema_fields": {
            "WorkflowStepCreate": list(WorkflowStepCreate.model_fields),
            "StepPluginCreate": list(StepPluginCreate.model_fields),
        },
        "events_route_pages": _events_route_pages(),
        "read_scope_checks_collision_only": _read_scope_checks_collision_only(),
        "scheduler_jobs": scheduler_jobs(JOBS_TF.read_text()),
    }
