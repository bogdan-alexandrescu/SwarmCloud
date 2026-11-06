"""The new deployment's tasks and workflows, as the `swarm` CLI reaches them.

A run finishes at once on the fake clock. What it records follows the rules
the platform holds (lesson 43, decision 8): the GitHub credential kind
follows the SUBMITTER -- a person's task clones with their own App user token
(`app_user`), a service submission (the console's runs, started by swarm-api)
with the App installation token (`app_installation`). A check run on GKE
leaves its worker Job in the team's namespace, named as scheduler dispatch
names it; the live-browser one carries the `swarm-live` label and its display
sidecar.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from swarm_install.steps.verify import CHECK_RUNS, job_stem, tenant_namespace

from .world import World

REPOSITORY = "acme/app"
PERSON, SERVICE = "person", "service"
KIND = {PERSON: "app_user", SERVICE: "app_installation"}
#: The profile each check run's prompt names; any other dispatch is `claude-code`.
LIVE_PROFILE = "claude-code-browser"


@dataclass
class Task:
    id: str
    profile: str
    submitter: str
    tenant: str
    repository: str
    prompt: str
    pull_request: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)


class Deployment:
    def __init__(self, world: World) -> None:
        self.world = world
        self.tasks: dict[str, Task] = {}
        self.workflows: dict[str, list[tuple[str, str]]] = {}  # id -> (step_id, task_id)
        self.index_sha = secrets.token_hex(20)
        self.tenants: list[str] = []

    def submit(self, *, profile: str, submitter: str, tenant: str, repository: str, prompt: str,
               opens_pr: bool) -> Task:
        task = Task("task_" + secrets.token_hex(6), profile, submitter, tenant, repository, prompt)
        if opens_pr:
            task.pull_request = f"https://github.com/{repository}/pull/{len(self.tasks) + 1}"
        for check in CHECK_RUNS.values():
            if check.artifact and check.artifact in prompt:
                task.artifacts[check.artifact] = self._artifact(check.artifact, repository)
        if any(c.on_cluster and c.prompt_for(repository) == prompt for c in CHECK_RUNS.values()):
            self._worker_job(task)
        self.tasks[task.id] = task
        return task

    def _artifact(self, name: str, repository: str) -> str:
        if name.endswith(".json"):
            return json.dumps({"freshness": {"index_sha": self.index_sha},
                               "rows": [{"symbol": "main", "path": "src/main.py"}]})
        if name == "live-browser.txt":
            return f"GitHub - {repository}: the application\n"
        return "README.md,src,tests\n"

    def _worker_job(self, task: Task) -> None:
        live = task.profile == LIVE_PROFILE
        containers = [{"name": "agent"}] + ([{"name": "display"}] if live else [])
        job = {"apiVersion": "batch/v1", "kind": "Job",
               "metadata": {"name": job_stem(task.id) + "1", "namespace": tenant_namespace(task.tenant)},
               "spec": {"template": {"metadata": {"labels": {"swarm-live": "enabled"} if live else {}},
                                     "spec": {"containers": containers}}}}
        self.world.apply_in_cluster(json.dumps(job))  # type: ignore[attr-defined]

    def workflow(self, *, submitter: str, tenant: str, repository: str, spec: dict[str, Any]) -> str:
        workflow_id = "wf_" + secrets.token_hex(6)
        steps = spec.get("steps") or []
        rows = []
        for n, step in enumerate(steps):
            task = self.submit(profile=str(step.get("runner_profile") or "claude-code"), submitter=submitter,
                               tenant=tenant, repository=repository, prompt=str(step.get("prompt") or ""),
                               opens_pr=n == len(steps) - 1)
            rows.append((str(step.get("step_id")), task.id))
        self.workflows[workflow_id] = rows
        return workflow_id

    def result(self, task: Task) -> dict[str, Any]:
        summary: dict[str, Any] = {"forge_credential": {"kind": KIND[task.submitter],
                                                        "source": f"swarm-tenant-{task.tenant}-git"}}
        if task.pull_request:
            summary["git"] = {"pull_request": {"url": task.pull_request}}
        return summary


def _flags(words: list[str]) -> dict[str, str]:
    return dict(w.lstrip("-").split("=", 1) for w in words if w.startswith("--") and "=" in w)


def attach(world: World) -> Deployment:
    """`swarm --context <hostname> ...` against the deployment; the contexts
    themselves stay services.swarm_cli's."""
    deployment = Deployment(world)
    world.deployment = deployment  # type: ignore[attr-defined]
    swarm = next(fn for want, fn in world.command_handlers if want == "swarm")

    def on_deployment(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        flags = _flags(words)
        context = flags.get("context") or (words[words.index("--context") + 1] if "--context" in words else "")
        words = [w for i, w in enumerate(words) if w != "--context" and (i == 0 or words[i - 1] != "--context")
                 and not w.startswith("--context=")]
        contexts = world.contexts  # type: ignore[attr-defined]
        if not context:
            return swarm(argv, stdin, env, cwd)
        if context not in contexts:
            return 1, "", f"swarm: no context {context}"
        row = contexts[context]
        if words[:1] == ["login"]:
            if not row.get("has_secret"):
                return 1, "", "the Desktop client's secret is not in the credential store"
            row["signed_in"] = True
            return 0, "", ""
        if not row.get("signed_in"):
            return 1, "", "not signed in: swarm login"
        tenant = (env or {}).get("SWARM_TENANT") or (deployment.tenants[0] if deployment.tenants else "")
        if words[:1] == ["tenants"]:
            rows = [{"tenant_id": t} for t in deployment.tenants]
            return 0, json.dumps({"tenants": rows, "current": deployment.tenants[0] if rows else None}), ""
        if words[:3] == ["sc", "access", "repos"]:
            return 0, json.dumps({"repositories": [{"repository": REPOSITORY, "archived": False}]}), ""
        if words[:1] == ["dispatch"]:
            task = deployment.submit(profile=flags.get("profile", "claude-code"), submitter=PERSON, tenant=tenant,
                                     repository=flags["repo"], prompt=stdin or "",
                                     opens_pr="pull request" in (stdin or "") and "no pull request" not in (stdin or ""))
            return 0, json.dumps({"id": task.id}), ""
        if words[:1] == ["workflow"]:
            workflow_id = deployment.workflow(submitter=PERSON, tenant=tenant, repository=flags["repo"],
                                              spec=json.loads(stdin or "{}"))
            return 0, json.dumps({"workflow": {"workflow_id": workflow_id}}), ""
        if words[:1] == ["workflow-status"]:
            rows = deployment.workflows.get(words[1])
            if rows is None:
                return 1, "", f"no workflow {words[1]}"
            steps = [{"step_id": step, "task_id": task,
                      "produced": {"pull_request": deployment.tasks[task].pull_request}} for step, task in rows]
            return 0, json.dumps({"state": "SUCCEEDED", "steps": steps}), ""
        if words[:1] == ["status"]:
            task = deployment.tasks.get(words[1])
            return (0, f"{task.id} SUCCEEDED {task.profile}\n", "") if task else (1, "", f"no task {words[1]}")
        if words[:1] == ["result"]:
            task = deployment.tasks.get(words[1])
            return (0, json.dumps(deployment.result(task)), "") if task else (1, "", f"no task {words[1]}")
        if words[:1] == ["artifact"]:
            task = deployment.tasks.get(words[1])
            found = task.artifacts.get(words[2]) if task else None
            return (0, found, "") if found is not None else (1, "", f"no artifact {words[2]}")
        if words[:1] == ["graph-index"]:
            return 0, json.dumps({"index": {"commit_sha": deployment.index_sha}}), ""
        return swarm(argv, stdin, env, cwd)

    world.command_handlers = [(want, on_deployment if want == "swarm" else fn)
                              for want, fn in world.command_handlers]
    return deployment
