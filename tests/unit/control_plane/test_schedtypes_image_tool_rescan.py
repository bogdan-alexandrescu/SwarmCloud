"""The `image-tool-rescan` schedule type (#442, owner decision 2026-10-09).

WHAT IS HELD HERE
-----------------
* THE CATALOGUE lists the type with its parameters (`tools`, default tofu and
  tflint, an unknown tool name refused) and its caps, which are no larger
  than `dependency-cve-refresh`'s, the type it is modelled on.
* THE TASK is one `claude-code` `direct-pr` task per firing, cloned at the
  default branch. Its prompt sends the agent to read the promote gate's
  filter and trivy version FROM MAIN and quote them, forbids every waiver
  (owner decision 2026-10-01), and allows ONE pull request.
* A TOOL ALREADY SWITCHED ON is skipped: the executor reads the Dockerfile on
  the default branch and asks only about the tools still held. Nothing held,
  an open pull request of this schedule, or no Dockerfile submits nothing.
* A DRY RUN creates nothing.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import schedulefire, schedules, scheduletypes
from swarm_api.errors import ValidationFailed
from swarm_api.forgewrite import GitHubWriter
from swarm_api.schedtypes import image_tool_rescan as rescan

from .conftest import seed_tenant
from .test_schedtypes_issue_sweep import (
    SCHEDULE_ID,
    firing_for,
    make_context,
    make_schedule,
    register,
    tasks,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha() -> str:
    # Built at runtime: a 64-hex literal is the shape a credential scan flags.
    return secrets.token_hex(32)


def dockerfile(*, tofu: str | None = "0", tflint: str | None = "0", installs: tuple[str, ...] = ("tofu", "tflint")) -> str:
    """The lines of images/agent-runtime-base/Dockerfile the executor reads.

    `None` for a tool drops its hold argument, as the PR that switches it on
    for good does ("a hold with nothing behind it is dead code")."""
    lines = ["FROM debian:bookworm-slim"]
    if tofu is not None:
        lines.append(f"ARG INSTALL_TOFU={tofu}")
    if tflint is not None:
        lines.append(f"ARG INSTALL_TFLINT={tflint}")
    lines += [
        "ARG TOFU_VERSION=1.13.1",
        f"ARG TOFU_SHA256={_sha()}",
        "ARG TFLINT_VERSION=0.64.0",
        f"ARG TFLINT_SHA256={_sha()}",
    ]
    lines += [f" && install -o root -g root -m 0755 /tmp/toolbox/{tool} /usr/local/bin/{tool} ; \\"
              for tool in installs]
    return "\n".join(lines) + "\n"


class RescanGitHub:
    """api.github.com's contents and pulls routes, read-only, for `GitHubWriter`."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {rescan.DOCKERFILE: dockerfile()}
        self.pulls: list[dict[str, Any]] = []
        self.refuse: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    def send(self, method: str, url: str, headers: dict, body: Any, timeout: float) -> tuple[int, bytes]:
        parsed = urlparse(url)
        self.calls.append((method, parsed.path))
        if method != "GET":
            return 405, b""
        rest = re.match(r"^/repos/[^/]+/[^/]+/(.*)$", parsed.path).group(1)
        if rest in self.refuse:
            return self.refuse[rest], b"{}"
        if rest.startswith("contents/"):
            path = rest[len("contents/"):]
            if path not in self.files:
                return 404, b"{}"
            content = base64.b64encode(self.files[path].encode()).decode()
            # GitHub wraps the base64 at 60 characters.
            wrapped = "\n".join(content[i:i + 60] for i in range(0, len(content), 60))
            return 200, json.dumps({"type": "file", "encoding": "base64", "content": wrapped}).encode()
        if rest == "pulls":
            return 200, json.dumps(self.pulls).encode()
        return 404, b"{}"


@pytest.fixture
def github() -> RescanGitHub:
    return RescanGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, github, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    context = make_context(db, tokens, group_map, objects)
    context.forge_writer = GitHubWriter(send=github.send)
    return context


def schedule(db, *repo_ids: str, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="image-tool-rescan", repo_ids=repo_ids, cron=rescan.DEFAULT_CRON, **kwargs)


def rescan_tasks(db) -> list[dict[str, Any]]:
    return [t for t in tasks(db) if t["metadata"].get("image_tool_rescan")]


def strategy(task) -> str:
    return (task["metadata"].get("dispatch") or {}).get("strategy") or "collect"


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_the_catalogue_lists_the_type_with_its_params_and_caps() -> None:
    entry = scheduletypes.get("image-tool-rescan")
    # MUTATION: drop the entry from TYPES and `get` answers None.
    assert entry is not None
    # MUTATION: delete schedtypes/image_tool_rescan.py and the type is unavailable.
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is rescan
    caps = scheduletypes.budget_caps(entry)
    ceiling = scheduletypes.budget_caps(scheduletypes.get("dependency-cve-refresh"))
    # MUTATION: raise any figure of BUDGET_CAPS above dependency-cve-refresh's and this fails.
    assert caps is rescan.BUDGET_CAPS
    assert caps.per_run_usd <= ceiling.per_run_usd and caps.per_day_usd <= ceiling.per_day_usd
    assert caps.max_concurrent <= ceiling.max_concurrent
    row = next(r for r in scheduletypes.catalogue() if r["name"] == "image-tool-rescan")
    # MUTATION: give the params model a different field name and the schema loses `tools`.
    assert row["params_schema"]["properties"]["tools"]["default"] == ["tofu", "tflint"]
    assert row["budget_caps"] == caps.as_dict() and row["available"] is True
    # It opens a pull request: the merge is a person's, never below approve.
    # MUTATION: give the entry `pushes=_never` or a floor of `merge: auto` and this fails.
    assert entry.pushes(entry.params_model()) is True
    assert entry.floor_gate.merge in ("approve", "off") and entry.profile == rescan.PROFILE == "claude-code"


def test_the_params_default_to_tofu_and_tflint() -> None:
    entry = scheduletypes.get("image-tool-rescan")
    gate = schedules.resolve_gate(entry, None)
    # MUTATION: change the default_factory to ["tofu"] and this fails.
    assert schedules.resolve_params(entry, {}, gate=gate)["tools"] == ["tofu", "tflint"]
    assert schedules.resolve_params(entry, {"tools": ["tflint"]}, gate=gate)["tools"] == ["tflint"]


@pytest.mark.parametrize("tools", [["terraform"], ["tofu", "trivy"], [], ["tofu", "tofu"]])
def test_an_unknown_tool_an_empty_list_or_a_repeat_is_refused(tools) -> None:
    entry = scheduletypes.get("image-tool-rescan")
    gate = schedules.resolve_gate(entry, None)
    # MUTATION: type `tools` as list[str] (or drop min_length / the unique check) and this passes through.
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_params(entry, {"tools": tools}, gate=gate)
    assert caught.value.code == "invalid_params"


# ---------------------------------------------------------------------------
# The Dockerfile: which tools are still held
# ---------------------------------------------------------------------------


def test_a_tools_state_is_read_from_its_hold_argument() -> None:
    # MUTATION: read every hold as "held" and the "on" cases fail.
    assert rescan.tool_state(dockerfile(), "tofu") == "held"
    assert rescan.tool_state(dockerfile(tofu="1"), "tofu") == "on"
    assert rescan.tool_state(dockerfile(tofu="1"), "tflint") == "held"
    # The hold deleted and the install left: installed unconditionally.
    assert rescan.tool_state(dockerfile(tofu=None), "tofu") == "on"
    # Neither a hold nor an install: a layout this type does not know.
    # MUTATION: treat a missing hold as "on" without looking for the install and this fails.
    assert rescan.tool_state(dockerfile(tofu=None, installs=("tflint",)), "tofu") == "absent"
    assert rescan.pinned_version(dockerfile(), "tflint") == "0.64.0"


def test_the_dockerfile_on_main_has_the_layout_the_executor_reads() -> None:
    text = (REPO_ROOT / rescan.DOCKERFILE).read_text()
    for tool in rescan.TOOLS:
        # MUTATION: rename INSTALL_TOFU in the reader's TOOLS table and tofu reads "absent".
        assert rescan.tool_state(text, tool) in ("held", "on"), tool
        assert rescan.pinned_version(text, tool), tool


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def test_the_prompt_names_the_gates_filter_as_read_from_main_no_waiver_and_one_pr() -> None:
    text = rescan.prompt(["tofu", "tflint"], {"tofu": "1.13.1", "tflint": "0.64.0"},
                         schedule_id=SCHEDULE_ID, repository="example-org/widgets")
    # The gate's filter and version, read from main and quoted, never retyped.
    # MUTATION: drop the gate's files from GATE_SOURCES and these fail.
    for source in ("scripts/lib/image-scan.sh", "image_scan_args", ".github/workflows/release.yml",
                   "TRIVY_VERSION", ".github/actions/release-promote/action.yml"):
        assert source in text, source
    assert "quote" in text.lower() and "never retype" in text.lower()
    # MUTATION: drop the no-waiver paragraph and these fail.
    assert "no waiver" in text.lower()
    for forbidden in (".trivyignore", "--ignorefile", "severity"):
        assert forbidden in text, forbidden
    # MUTATION: say "a pull request per tool" and this fails.
    assert "ONE pull request" in text
    # The binary must be executable, or trivy's Go analyzer scans it empty.
    assert "chmod +x" in text
    # Checksum verified against the vendor's file before the scan.
    assert "tofu_<version>_SHA256SUMS" in text and "checksums.txt" in text
    # The switch is the hold argument, with the pin and checksum the mirror uses.
    for name in ("INSTALL_TOFU=1", "INSTALL_TFLINT=1", "TOFU_VERSION", "TFLINT_SHA256",
                 "images/agent-runtime-base/tool-mirror.sha256"):
        assert name in text, name
    # A run that cannot scan changes nothing and says why.
    assert "cannot scan" in text.lower() and rescan.REPORT_FILE in text
    assert rescan.marker(SCHEDULE_ID) in text


def test_the_gate_sources_the_prompt_names_hold_the_gates_filter_on_main() -> None:
    # The prompt sends the agent to these lines; they must exist to be quoted.
    flags = (REPO_ROOT / "scripts/lib/image-scan.sh").read_text()
    # MUTATION: point GATE_SOURCES at a file that does not define image_scan_args and this fails.
    assert "image_scan_args()" in flags and "--ignore-unfixed" in flags
    assert re.search(r"^\s*TRIVY_VERSION:", (REPO_ROOT / ".github/workflows/release.yml").read_text(), re.M)


def test_the_prompt_asks_only_about_the_tools_it_is_given() -> None:
    text = rescan.prompt(["tflint"], {"tflint": "0.64.0"}, schedule_id=SCHEDULE_ID,
                         repository="example-org/widgets")
    # MUTATION: list every TOOLS entry whatever is passed and this fails.
    assert "INSTALL_TFLINT=1" in text and "INSTALL_TOFU" not in text


# ---------------------------------------------------------------------------
# A firing
# ---------------------------------------------------------------------------


def test_a_firing_submits_one_direct_pr_task_for_the_held_tools(db, ctx) -> None:
    repo_id = register(db)
    firing = firing_for(ctx, schedule(db, repo_id))

    work = rescan.create(firing)

    (task,) = rescan_tasks(db)
    assert work == [{"kind": "task", "id": task["id"], "repo_id": repo_id}]
    assert task["runner_profile"] == "claude-code" and strategy(task) == "direct-pr"
    assert task["repository_ref"] == "main" and task["metadata"]["schedule"] == firing.mark
    # MUTATION: submit with an empty tool list and this fails.
    assert task["metadata"]["tools"] == ["tofu", "tflint"]
    assert "INSTALL_TOFU=1" in task["input"]["prompt"] and "1.13.1" in task["input"]["prompt"]


def test_a_tool_already_switched_on_is_skipped(db, ctx, github) -> None:
    github.files[rescan.DOCKERFILE] = dockerfile(tofu="1")
    repo_id = register(db)

    rescan.create(firing_for(ctx, schedule(db, repo_id)))

    (task,) = rescan_tasks(db)
    # MUTATION: skip the tool_state check in plan() and tofu is asked about again.
    assert task["metadata"]["tools"] == ["tflint"]
    assert "INSTALL_TOFU" not in task["input"]["prompt"]


def test_every_tool_switched_on_submits_nothing(db, ctx, github) -> None:
    github.files[rescan.DOCKERFILE] = dockerfile(tofu=None, tflint="1")
    repo_id = register(db)

    # MUTATION: submit when no tool is held and this fails.
    assert rescan.create(firing_for(ctx, schedule(db, repo_id))) == []
    assert rescan_tasks(db) == []
    plan = rescan.plan(firing_for(ctx, schedule(db, repo_id)))
    assert plan.outcomes[repo_id]["outcome"] == "nothing_held"
    assert plan.outcomes[repo_id]["tools"] == {"tofu": "on", "tflint": "on"}


def test_a_tool_not_named_in_params_is_not_asked_about(db, ctx) -> None:
    repo_id = register(db)
    rescan.create(firing_for(ctx, schedule(db, repo_id, params={"tools": ["tofu"]})))
    (task,) = rescan_tasks(db)
    assert task["metadata"]["tools"] == ["tofu"]


def test_an_open_pull_request_of_this_schedule_submits_nothing(db, ctx, github) -> None:
    github.pulls = [{"number": 77, "body": "switch tofu on\n" + rescan.marker(SCHEDULE_ID)},
                    {"number": 78, "body": rescan.marker("sch_000000000999")}]
    repo_id = register(db)

    # MUTATION: skip the open-pull check and a second pull request is opened.
    assert rescan.create(firing_for(ctx, schedule(db, repo_id))) == []
    plan = rescan.plan(firing_for(ctx, schedule(db, repo_id)))
    assert plan.outcomes[repo_id]["outcome"] == "pr_open" and plan.outcomes[repo_id]["pull_request"] == 77


def test_one_task_per_firing_across_repositories(db, ctx, github) -> None:
    first = register(db, repo="nodocker")
    second = register(db, repo="widgets")
    third = register(db, repo="gadgets")

    def send(method, url, headers, body, timeout):
        # The first repository has no image Dockerfile.
        if "/nodocker/contents/" in url:
            return 404, b"{}"
        return github.send(method, url, headers, body, timeout)

    ctx.forge_writer = GitHubWriter(send=send)
    plan = rescan.plan(firing_for(ctx, schedule(db, first, second, third)))

    assert plan.outcomes[first]["outcome"] == "no_dockerfile"
    # MUTATION: keep reading after one repository is chosen and the third is read too.
    assert plan.outcomes[third]["outcome"] == "deferred"
    assert plan.chosen is not None and plan.chosen[0] == second
    assert not any("/gadgets/" in path for _, path in github.calls)


def test_no_room_submits_nothing(db, ctx) -> None:
    repo_id = register(db)
    # MUTATION: ignore firing.room and a task is submitted past the cap.
    assert rescan.create(firing_for(ctx, schedule(db, repo_id), room=0)) == []
    assert rescan_tasks(db) == []


def test_an_unreadable_dockerfile_submits_nothing_and_says_why(db, ctx, github) -> None:
    github.refuse = {f"contents/{rescan.DOCKERFILE}": 403}
    repo_id = register(db)
    plan = rescan.plan(firing_for(ctx, schedule(db, repo_id)))
    # MUTATION: treat a refused read as "every tool held" and a task is submitted blind.
    assert plan.outcomes[repo_id]["outcome"].startswith("failed: ")
    assert rescan.create(firing_for(ctx, schedule(db, repo_id))) == []


def test_another_tenants_registration_is_not_read(db, ctx, github) -> None:
    theirs = register(db, tenant_id="research")
    assert rescan.create(firing_for(ctx, schedule(db, theirs))) == []
    assert not any("/contents/" in path for _, path in github.calls)


def test_a_dry_run_describes_the_task_and_creates_nothing(db, ctx, github) -> None:
    github.files[rescan.DOCKERFILE] = dockerfile(tflint="1")
    repo_id = register(db)

    answer = rescan.dry_run(firing_for(ctx, schedule(db, repo_id)))

    assert rescan_tasks(db) == [] and all(method == "GET" for method, _ in github.calls)
    assert answer["would_submit"]["repo_id"] == repo_id
    # MUTATION: report the params' tools rather than the held ones and this fails.
    assert answer["would_submit"]["tools"] == ["tofu"]
    assert answer["profile"] == "claude-code" and answer["strategy"] == "direct-pr"
    assert answer["cadence"] == rescan.DEFAULT_CRON
