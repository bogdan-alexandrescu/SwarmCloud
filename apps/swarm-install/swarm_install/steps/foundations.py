"""Stage 1, project foundations: the project and its billing, the budget, the
APIs, the default compute account's Editor grant, and the state bucket
(design §4, §5.5; plan §9.1).

| Step | Manifest entries | Lesson |
|---|---|---|
| `foundations.project` | `other.gcp-project`, `other.billing-link` | decision 6 (2026-10-30): one typed approval showing the cost estimate and the budget, then `projects.create` and `projects.updateBillingInfo`; a refusal opens the parent's IAM page or the project's billing page and polls until the grant or the link is seen |
| `foundations.apis` | every core `api.*` entry with a `service_enabled` probe, and `api.containerscanning-NOT-enabled` | 4: one list, read from the manifest; containerscanning is disabled through Service Usage |
| `foundations.budget` | `monitoring.budget-alert` | the amount chosen at the approval; asked here when there was no approval (the project already existed and was linked); both questions default to the sizing's `budget_usd`; a refusal opens the account's Budgets page and polls for the budget |
| `foundations.default-compute-editor` | `iam-binding.default-compute-sa-editor-removed` | 6 (#1020); 3: a write-rate limit is retried, never reported as a denial |
| `foundations.state-bucket` | `bucket.state`, `other.install-state` | 7: created with bootstrap.sh's settings, then the local state is uploaded |

The state bucket's Terraform declaration gains its `import` block in lane
DAYONE-BOOT-1, so creating it here never collides with bootstrap pass 1.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..exitcodes import MET, UNMET
from ..manifest import Manifest
from ..manifest import load as load_manifest
from ..state import STATE_OBJECT, state_bucket
from ..waits import Clock, WaitGaveUp, WaitSpec, wait_until
from . import Observation, Plan, open_page
from .preflight import ASK, BILLING, CRM, GCP, USAGE, ApiError, Ask, Gcp, project_number, remember

#: API enablement is polled (plan §9.1: 10 min).
API_ENABLEMENT = WaitSpec(
    reason="the APIs finishing enabling",
    expected="1 to 3 min",
    expected_s=3 * 60,
    bound_s=10 * 60,
    look_at="gcloud services list --enabled: an API stuck enabling usually means billing is not linked",
    interval_s=10,
)

#: Never enabled: Google's per-image scanning cost money for results nothing
#: read; trivy is the gate (owner decision 2026-10-21).
NEVER_ENABLED = "containerscanning.googleapis.com"

#: A project just created can take a minute to appear in `projects:search`,
#: which the check reads; it is polled, never assumed.
PROJECT_READY = WaitSpec(
    reason="the new project appearing with its billing linked",
    expected="under 1 min",
    expected_s=60,
    bound_s=5 * 60,
    look_at="gcloud projects describe <project> and gcloud billing projects describe <project>",
    interval_s=5,
)

#: `projects.create` returns a long-running operation; it is polled this often,
#: for at most this long (Google states no bound; it is seconds in practice).
OPERATION_INTERVAL_S = 2.0
OPERATION_BOUND_S = 5 * 60.0

#: The budget's default, in USD a month, when stage 0 recorded no sizing (the
#: sizing's `budget_usd` is the default otherwise, `default_budget`): five times
#: the fixed floor below, so the 50 % alert fires only once agents run in
#: earnest. The person may type another amount at the approval, or at
#: foundations.budget when there was no approval (an optional choice; Enter
#: keeps the default).
DEFAULT_BUDGET_USD = 1000

#: When a write is refused, the engine opens the page where the person (or
#: someone with the right) does it by hand, and polls until it sees the change
#: (the Day One goal: never "press Enter when done"). A grant or a click takes
#: minutes; past the bound the step fails naming the right, and a re-run
#: carries on from there.
CREATOR_GRANT = WaitSpec(
    reason="roles/resourcemanager.projectCreator on the parent",
    expected="1 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="the parent's IAM page: your account needs roles/resourcemanager.projectCreator there",
    interval_s=10,
)
BILLING_BY_HAND = WaitSpec(
    reason="the project's billing linked on its billing page",
    expected="1 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="the project's billing page: link the billing account, or get roles/billing.user on it",
    interval_s=10,
)
BUDGET_BY_HAND = WaitSpec(
    reason="the budget created on the billing account's Budgets page",
    expected="1 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="Billing > Budgets & alerts: a budget of the name the step gave",
    interval_s=10,
)

#: An API enabled minutes ago answers 403 SERVICE_DISABLED ("If you enabled
#: this API recently, wait a few minutes") until the enablement reaches every
#: Google system, which is later than `services.list` shows it (audit 4a F4).
#: The call is retried, never taken as a refusal; past the bound the run
#: pauses and `swarm install` resumes it.
API_PROPAGATION = WaitSpec(
    reason="an API enabled minutes ago answering",
    expected="1 to 3 min",
    expected_s=3 * 60,
    bound_s=5 * 60,
    look_at="gcloud services list --enabled: the API is listed, and Google says to wait a few minutes after "
            "enabling it",
    interval_s=15,
)

#: An account that never opened the Cloud console has not accepted the Google
#: Cloud Terms of Service, and `projects.create` answers 400 FAILED_PRECONDITION
#: "Callers must accept Terms of Service" (audit 4a F3). The console's first
#: page asks for it; the engine opens it and tries the create again until
#: Google takes it. Past the bound the run pauses.
TERMS_ACCEPTED = WaitSpec(
    reason="the Google Cloud Terms of Service accepted in the Cloud console",
    expected="1 to 5 min",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="https://console.cloud.google.com/, signed in as the installing account: accept the Terms of Service",
    interval_s=10,
)

CONSOLE = "https://console.cloud.google.com"
TERMS_PAGE = f"{CONSOLE}/"
CREATE_PERMISSION = "resourcemanager.projects.create"

#: Google's project id rule: 6-30 characters, lowercase letters, digits and
#: hyphens, starting with a letter and not ending with a hyphen. The verb's
#: `--project` and the question asked when an id is taken both use it.
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def service_disabled(exc: ApiError) -> bool:
    """Google's answer while an API is not enabled, or not enabled everywhere
    yet: a 403 whose reason is SERVICE_DISABLED or whose message says the API
    "has not been used in project ... before or it is disabled". `_call` takes
    the reason from the error's `status`, which for this answer is
    PERMISSION_DENIED, so the message is what tells it from an IAM refusal."""
    if exc.status != 403:
        return False
    text = str(exc)
    return (exc.reason == "SERVICE_DISABLED" or "SERVICE_DISABLED" in text
            or "has not been used in project" in text or "or it is disabled" in text)


def terms_not_accepted(exc: ApiError) -> bool:
    """`projects.create`'s answer to an account that never accepted the Google
    Cloud Terms of Service: 400 FAILED_PRECONDITION naming them."""
    return exc.status == 400 and "terms of service" in str(exc).lower()


def after_enabling(ctx: Any, step_id: str, call: Callable[[], Any]) -> Any:
    """`call()`, tried again every API_PROPAGATION interval while Google says
    its API is disabled (`service_disabled`); any other error is raised at
    once. Past the bound `WaitGaveUp` pauses the run."""
    done: dict[str, Any] = {}

    def attempt() -> str:
        try:
            done["value"] = call()
        except ApiError as exc:
            if not service_disabled(exc):
                raise
            return "waiting"
        return MET

    wait_until(f"{step_id}:api-propagation", API_PROPAGATION, attempt, clock=ctx.clock, events=ctx.events)
    return done["value"]


def iam_page(parent: str) -> str:
    """The IAM page of an organization or folder ("organizations/N", "folders/N")."""
    kind, _, number = parent.partition("/")
    return f"{CONSOLE}/iam-admin/iam?{'folderId' if kind == 'folders' else 'organizationId'}={number}"


def billing_page(project: str) -> str:
    """The project's billing page, where its billing account is linked."""
    return f"{CONSOLE}/billing/linkedaccount?project={project}"


def budgets_page(account: str, project: str) -> str:
    """The billing account's Budgets & alerts page."""
    return f"{CONSOLE}/billing/{account}/budgets?project={project}"


def default_budget(ctx: Any) -> int:
    """The budget question's default: the sizing's `budget_usd` when stage 0
    recorded a sizing (lane SIZING-ASK's proposal, which the person already
    accepted or changed), else DEFAULT_BUDGET_USD."""
    found = ctx.outputs.get("sizing")
    budget = found.get("budget_usd") if isinstance(found, Mapping) else None
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        return budget
    return DEFAULT_BUDGET_USD

#: The monthly fixed floor at Google's list price, before any agent runs: what
#: the deployment costs while nobody uses it. Agent runs bill on top, by the
#: minute they run (docs/cost-control.md). The design states no figure, so each
#: line names its own basis.
MONTHLY_FLOOR_USD = (
    ("swarm-api's two always-on instances, 1 vCPU / 1 GiB each (terraform/infra/main.tf, swarm-api's "
     "min_instances)", 105),
    ("the GKE cluster's management fee, $0.10 an hour (docs/BUILD_PROMPT_V2.md, section 2.1)", 73),
    ("the HTTPS load balancer's forwarding rule, $0.025 an hour", 18),
)


def monthly_estimate() -> int:
    return sum(usd for _, usd in MONTHLY_FLOOR_USD)


class ProjectApi:
    """The Resource Manager, Cloud Billing and Service Usage writes stage 1
    makes that `preflight.Gcp` has no method for: they go through its one REST
    session, so errors are the same `ApiError`."""

    def __init__(self, gcp: Callable[[], Gcp]) -> None:
        self._gcp = gcp

    def create_project(self, project: str, parent: str, labels: Mapping[str, str]) -> dict[str, Any]:
        """Resource Manager v3 `projects.create`; returns its operation."""
        return self._gcp()._call("POST", f"{CRM}/v3/projects", body={
            "projectId": project, "displayName": project, "parent": parent, "labels": dict(labels),
        })

    def operation(self, name: str) -> dict[str, Any]:
        return self._gcp()._call("GET", f"{CRM}/v3/{name}")

    def can_create_projects(self, parent: str) -> bool:
        """Resource Manager v3 `testIamPermissions` on the parent: whether the
        signed-in account holds resourcemanager.projects.create there."""
        found = self._gcp()._call("POST", f"{CRM}/v3/{parent}:testIamPermissions",
                                  body={"permissions": [CREATE_PERMISSION]}) or {}
        return CREATE_PERMISSION in (found.get("permissions") or [])

    def link_billing(self, project: str, account: str) -> dict[str, Any]:
        """Cloud Billing `projects.updateBillingInfo`."""
        return self._gcp()._call("PUT", f"{BILLING}/projects/{project}/billingInfo",
                                 body={"billingAccountName": f"billingAccounts/{account}"})

    def disable_service(self, project: str, service: str) -> dict[str, Any]:
        """Service Usage `services.disable`; nothing depends on the services disabled here."""
        return self._gcp()._call("POST", f"{USAGE}/v1/projects/{project}/services/{service}:disable",
                                 body={"disableDependentServices": False})


def parse_budget(answer: str, default: int = DEFAULT_BUDGET_USD) -> int:
    """The budget typed at the approval or at foundations.budget: whole US
    dollars, Enter keeps the default."""
    text = answer.strip().removeprefix("$").replace(",", "")
    if not text:
        return default
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"the budget must be a whole number of US dollars above 0, not {answer.strip()!r}; "
                         "nothing was changed")
    return int(text)


#: lesson 3: back-to-back IAM writes are rate-limited; these are the waits
#: between tries (seconds), then the error is raised as what it is.
IAM_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0)


def core_apis(manifest: Manifest) -> dict[str, str]:
    """entry id -> service name, for every core `api.*` entry whose probe is
    `service_enabled`: the one API list (lesson 4)."""
    return {
        entry.id: str(entry.probe.model_extra.get("name"))
        for entry in manifest.entries
        if entry.id.startswith("api.") and entry.scope == "core" and entry.probe is not None
        and entry.probe.type == "service_enabled"
    }


def retry_rate_limited(call: Callable[[], Any], clock: Clock, *,
                       delays: Sequence[float] = IAM_RETRY_DELAYS, on_retry: Callable[[str], None] | None = None) -> Any:
    """Run an IAM write, retrying while Google says it is rate-limited or the
    policy moved under it (409/412 on the etag). A denial is raised at once."""
    for delay in (*delays, None):
        try:
            return call()
        except ApiError as exc:
            retryable = exc.rate_limited or exc.status in (409, 412)
            if not retryable or delay is None:
                raise
            if on_retry is not None:
                on_retry(f"{exc.reason or exc.status}: retrying in {delay:.0f} s (a write-rate limit, not a denial)")
            clock.sleep(delay)
    raise AssertionError("unreachable")


class _Base:
    stage = 1
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask) -> None:
        self._gcp = gcp
        self._ask = ask

    @property
    def gcp(self) -> Gcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    #: Whether the check needs the project to exist. On an empty project (a dry
    #: run, or a first run before foundations.project) the check is `unmet`,
    #: never `unknown`: nothing failed to run, there is simply nothing yet.
    needs_project = True

    def check(self, ctx: Any) -> Observation:
        if self.needs_project and self.gcp.project(ctx.project) is None:
            return Observation(UNMET, f"project {ctx.project} does not exist yet (foundations.project)")
        return self._check(ctx)

    def _check(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")


class ProjectStep(_Base):
    id = "foundations.project"
    kind = "wait"
    wait = PROJECT_READY
    needs_project = False
    satisfies = ("other.gcp-project", "other.billing-link")
    _explain = (
        "Everything lives in one dedicated project in an organization (an Internal OAuth brand needs one), "
        "and no API can be enabled on a project with no billing. After one typed approval that shows the "
        "monthly cost estimate and the budget, the engine creates the project and links its billing "
        "(decision 6, 2026-10-30). A project that already exists and is linked is left alone."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, api: ProjectApi | None = None) -> None:
        super().__init__(gcp, ask)
        self.api = api if api is not None else ProjectApi(gcp)

    def check(self, ctx: Any) -> Observation:
        found = self.gcp.project(ctx.project)
        if found is None:
            return Observation(UNMET, f"project {ctx.project} does not exist")
        if found.get("state") not in (None, "ACTIVE"):
            return Observation(UNMET, f"project {ctx.project} is {found.get('state')}")
        if not str(found.get("parent", "")).startswith(("organizations/", "folders/")):
            return Observation(UNMET, f"project {ctx.project} is not in an organization")
        info = self.gcp.billing_info(ctx.project)
        if not info.get("billingEnabled"):
            return Observation(UNMET, f"project {ctx.project} has no billing account linked")
        number = str(found.get("name", "")).removeprefix("projects/")
        account = str(info.get("billingAccountName", "")).removeprefix("billingAccounts/")
        return Observation(MET, "", {"project_number": number, "billing_account": account})

    def _work(self, ctx: Any) -> tuple[bool, bool]:
        """(create the project, link its billing). A project that exists but is
        not ACTIVE or not in an organization is never changed: it is refused."""
        found = self.gcp.project(ctx.project)
        if found is None:
            return True, True
        if found.get("state") not in (None, "ACTIVE"):
            raise RuntimeError(f"project {ctx.project} is {found.get('state')}; restore it or choose another "
                               "project id (the installer never changes a project in that state)")
        if not str(found.get("parent", "")).startswith(("organizations/", "folders/")):
            raise RuntimeError(f"project {ctx.project} is not in an organization, so it cannot hold an Internal "
                               "OAuth brand; move it into one, or choose another project id")
        return False, not self.gcp.billing_info(ctx.project).get("billingEnabled")

    def _summary(self, ctx: Any, create: bool, link: bool, budget: int) -> list[str]:
        parent = str(ctx.outputs.get("parent") or "(not recorded yet: preflight.rights records it)")
        account = str(ctx.outputs.get("billing_account") or "(not recorded yet: preflight.inputs records it)")
        lines = [f"project        {ctx.project}" + ("" if create else " (exists)")]
        if create:
            lines.append(f"parent         {parent}")
        if link:
            lines.append(f"billing        billingAccounts/{account}")
        lines.append(f"budget         {budget} USD a month, alerts at 50/90/100 %")
        lines.append(f"cost estimate  about {monthly_estimate()} USD a month before any agent runs, at list price:")
        lines.extend(f"                 {usd} USD  {what}" for what, usd in MONTHLY_FLOOR_USD)
        lines.append("               agent runs bill on top, by the minute they run")
        return lines

    def plan(self, ctx: Any) -> Plan:
        create, link = self._work(ctx)
        budget = int(ctx.outputs.get("budget_amount") or default_budget(ctx))
        doing = [*(["create the project"] if create else []), *(["link its billing"] if link else [])]
        return Plan(
            f"{' and '.join(doing)}, after one typed approval (the project id)",
            tuple(self._summary(ctx, create, link, budget)),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        create, link = self._work(ctx)
        parent = str(ctx.outputs.get("parent") or "")
        account = str(ctx.outputs.get("billing_account") or "").removeprefix("billingAccounts/")
        if create and not parent.startswith(("organizations/", "folders/")):
            raise LookupError("no organization or folder is recorded for the project (preflight.rights records it)")
        if link and not account:
            raise LookupError("no billing account is recorded (preflight.inputs records it)")

        default = default_budget(ctx)
        budget = parse_budget(self._ask(
            ctx, self.id, f"Monthly budget for {ctx.project}, in US dollars (Enter keeps it)",
            default=str(default),
        ), default)
        summary = "\n".join(f"  {line}" for line in self._summary(ctx, create, link, budget))
        doing = " and ".join([*(["create"] if create else []), *(["link billing for"] if link else [])])
        typed = self._ask(
            ctx, self.id,
            f"The installer will {doing} this project:\n{summary}\nType the project id ({ctx.project}) to approve",
        ).strip()
        if typed != ctx.project:
            raise PermissionError(f"not confirmed (typed {typed!r}, not {ctx.project!r}); nothing was created "
                                  "or linked")
        remember(ctx, outputs={"budget_amount": budget})

        created = self._create(ctx, parent) if create else False
        if create and not created:
            # The id was taken and the person named a project that exists:
            # it is used as found, refused if it cannot hold the install.
            _, link = self._work(ctx)
        if link:
            self._link(ctx, account, created=created)

    def _create(self, ctx: Any, parent: str) -> bool:
        """Create the project. False when its id was taken and the person
        moved the install onto a project that exists already (`_another`)."""
        labels = {"managed-by": "swarm-install", "environment": ctx.environment}
        try:
            operation = self._start(ctx, parent, labels)
        except PermissionError as refused:
            self._until_creator(ctx, parent, refused)
            operation = self._start(ctx, parent, labels)
        if operation is None:
            return False
        waited = 0.0
        while not operation.get("done"):
            if waited >= OPERATION_BOUND_S:
                raise TimeoutError(f"creating project {ctx.project} did not finish in {OPERATION_BOUND_S:.0f} s "
                                   f"({operation.get('name')}); run swarm install again to carry on")
            ctx.clock.sleep(OPERATION_INTERVAL_S)
            waited += OPERATION_INTERVAL_S
            operation = self.api.operation(str(operation["name"]))
        if operation.get("error"):
            error = operation["error"]
            raise RuntimeError(f"creating project {ctx.project} failed: {error.get('message') or error}")
        return True

    def _start(self, ctx: Any, parent: str, labels: Mapping[str, str]) -> dict[str, Any] | None:
        """`projects.create`'s operation; None when the id was taken and the
        person named a project that exists instead. Only `projects.create`
        can say an id is taken: Resource Manager answers a read of another
        owner's project and of an id nobody holds with the same 403."""
        while True:
            try:
                return self._create_once(ctx, parent, labels)
            except ApiError as exc:
                if exc.status == 409:
                    if self._another(ctx):
                        return None
                    continue
                if exc.denied:
                    raise PermissionError(f"you cannot create projects in {parent} (resourcemanager.projects.create; "
                                          f"roles/resourcemanager.projectCreator on {parent}): {exc}") from None
                raise

    def _create_once(self, ctx: Any, parent: str, labels: Mapping[str, str]) -> dict[str, Any]:
        """One `projects.create`. An account that never accepted the Google
        Cloud Terms of Service is sent to the Cloud console, which asks for
        them, and the create is tried again until Google takes it (audit 4a
        F3); any other error is raised as it is."""
        try:
            return self.api.create_project(ctx.project, parent, labels)
        except ApiError as exc:
            if not terms_not_accepted(exc):
                raise
        who = str(ctx.outputs.get("installer") or "the account you signed in with")
        open_page(TERMS_PAGE, why=(
            f"Google refuses to create projects for {who} until it accepts the Google Cloud Terms of Service. "
            "Open the Cloud console signed in as that account and accept them there; the installer creates "
            f"{ctx.project} by itself once Google takes them"))
        done: dict[str, Any] = {}

        def accepted() -> str:
            try:
                done["operation"] = self.api.create_project(ctx.project, parent, labels)
            except ApiError as exc:
                if terms_not_accepted(exc):
                    return UNMET
                raise
            return MET

        wait_until(f"{self.id}:terms-of-service", TERMS_ACCEPTED, accepted, clock=ctx.clock, events=ctx.events)
        return done["operation"]

    def _another(self, ctx: Any) -> bool:
        """The project id is taken (a 409 on create): ask for another in this
        run, and move the install, with every answer stage 0 recorded, onto
        it (audit 4a F1). True when the id typed is a project that exists and
        the caller can see: it is used rather than created."""
        taken = ctx.project
        said = (f"Project id {taken} is taken: project ids are global across Google Cloud, and one deleted in "
                "the last 30 days stays taken.")
        while True:
            typed = self._ask(
                ctx, self.id,
                f"{said} Type another project id to create (6-30 characters: lowercase letters, digits and "
                "hyphens, starting with a letter); every answer given so far is kept",
            ).strip()
            if not typed:
                raise RuntimeError(f"project id {taken} is taken and no other id was typed; nothing was created. "
                                   "Run swarm install again and type another id when it asks")
            if not PROJECT_ID.match(typed):
                said = f"{typed!r} is not a Google Cloud project id."
                continue
            if typed == taken:
                said = f"Project id {taken} is the one that is taken."
                continue
            try:
                self._move(ctx, typed)
            except FileExistsError as exc:
                said = (f"{exc}: resume that install with swarm install --project {typed}, or choose another "
                        "id.")
                continue
            return self.gcp.project(typed) is not None

    def _move(self, ctx: Any, project: str) -> None:
        """The install is now `project`'s: its local state moves to that id
        (`StateStore.rename`), the steps after this one see it, and the
        billing-budget calls name it as their quota project."""
        ctx.state = ctx.engine.store.rename(project)
        ctx.project = project
        use = getattr(self._gcp, "use_quota_project", None)
        if callable(use):
            use(project)
        else:
            self.gcp.quota_project = project

    def _until_creator(self, ctx: Any, parent: str, refused: PermissionError) -> None:
        """Open the parent's IAM page and poll testIamPermissions until the
        signed-in account may create projects there; past the bound, `refused`."""
        who = str(ctx.outputs.get("installer") or "your account")
        open_page(iam_page(parent), why=(
            f"You cannot create projects in {parent}. On its IAM page, grant {who} the Project Creator role "
            f"(roles/resourcemanager.projectCreator), or ask an organization administrator to; the installer "
            "carries on by itself once it sees the grant"))

        def granted() -> str:
            try:
                return MET if self.api.can_create_projects(parent) else UNMET
            except ApiError:
                return UNMET

        try:
            wait_until(f"{self.id}:project-creator", CREATOR_GRANT, granted, clock=ctx.clock, events=ctx.events)
        except WaitGaveUp:
            raise refused from None

    def _link(self, ctx: Any, account: str, *, created: bool) -> None:
        try:
            after_enabling(ctx, self.id, lambda: self.api.link_billing(ctx.project, account))
            return
        except ApiError as exc:
            if not exc.denied:
                left = (f"project {ctx.project} was created and is left in place; " if created else "")
                raise PermissionError(f"linking billing account {account} failed: {exc}. {left}run swarm "
                                      "install again to link it") from None
        # Refused: the person (or a billing administrator) links it on the
        # project's billing page, and the engine polls the project's billing.
        open_page(billing_page(ctx.project), why=(
            f"You cannot link billing account billingAccounts/{account} to {ctx.project} "
            f"(billing.resourceAssociations.create). Link it on the project's billing page, or ask a billing "
            f"administrator to grant you roles/billing.user on billingAccounts/{account}; the installer carries "
            "on by itself once the project shows billing enabled"))

        def linked() -> str:
            try:
                return MET if self.gcp.billing_info(ctx.project).get("billingEnabled") else UNMET
            except ApiError:
                return UNMET

        try:
            wait_until(f"{self.id}:billing", BILLING_BY_HAND, linked, clock=ctx.clock, events=ctx.events)
        except WaitGaveUp:
            left = (f"project {ctx.project} was created and is left in place; " if created else "")
            raise PermissionError(
                f"you cannot link billing account {account} (billing.resourceAssociations.create; "
                f"roles/billing.user on billingAccounts/{account}), and the project's billing page did not link "
                f"it within {BILLING_BY_HAND.bound_s / 60:.0f} min. {left}run swarm install again to link it"
            ) from None


class ApisStep(_Base):
    id = "foundations.apis"
    kind = "wait"
    wait = API_ENABLEMENT
    _explain = (
        "Three API lists disagreed on the live deployment (lesson 4). The install enables exactly the "
        "manifest's core api.* entries, one list, and never containerscanning (trivy is the gate)."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, manifest: Manifest,
                 api: ProjectApi | None = None) -> None:
        super().__init__(gcp, ask)
        self.api = api if api is not None else ProjectApi(gcp)
        self.apis = core_apis(manifest)
        self.satisfies = (*self.apis, "api.containerscanning-NOT-enabled")

    def missing(self, ctx: Any) -> tuple[list[str], bool]:
        enabled = self.gcp.enabled_services(ctx.project)
        return sorted(set(self.apis.values()) - enabled), NEVER_ENABLED in enabled

    def _check(self, ctx: Any) -> Observation:
        missing, scanning = self.missing(ctx)
        if scanning:
            return Observation(UNMET, f"{NEVER_ENABLED} is enabled (foundations.apis disables it: trivy is the "
                                      "gate)")
        if missing:
            return Observation("waiting", f"{len(missing)} APIs not enabled yet: {', '.join(missing)}")
        return Observation(MET, f"{len(self.apis)} APIs enabled")

    def plan(self, ctx: Any) -> Plan:
        missing, scanning = self.missing(ctx)
        disable = (f"disable {NEVER_ENABLED} (Service Usage services.disable)",) if scanning else ()
        return Plan(f"enable {len(missing)} APIs, the manifest's one list", (*missing, *disable))

    def apply(self, ctx: Any, plan: Plan) -> None:
        missing, scanning = self.missing(ctx)
        if scanning:
            self.api.disable_service(ctx.project, NEVER_ENABLED)
        if missing:
            self.gcp.enable_services(ctx.project, missing)


class BudgetStep(_Base):
    id = "foundations.budget"
    satisfies = ("monitoring.budget-alert",)
    _explain = (
        "The platform's fixed floor (swarm-api's minimum instances, NAT, the cluster) costs money from the "
        "first apply; a budget with 50, 90 and 100 percent alerts is set before anything bills. Its amount "
        "is the one chosen at foundations.project's approval; when there was no approval (the project "
        "already existed and was linked) it is asked here, Enter keeping the sizing's proposed budget (owner "
        "pick, 2026-10-30)."
    )

    def _amount(self, ctx: Any) -> int:
        return int(ctx.outputs.get("budget_amount") or default_budget(ctx))

    def _name(self, ctx: Any) -> str:
        return f"swarm-{ctx.project}"

    def _account(self, ctx: Any) -> str:
        account = str(ctx.outputs.get("billing_account") or "")
        if not account:
            raise LookupError("no billing account is recorded (foundations.project records it)")
        return account

    def _check(self, ctx: Any) -> Observation:
        names = {b.get("displayName") for b in self.gcp.budgets(self._account(ctx))}
        if self._name(ctx) in names:
            return Observation(MET, f"budget {self._name(ctx)} exists")
        return Observation(UNMET, f"no budget named {self._name(ctx)}")

    def plan(self, ctx: Any) -> Plan:
        amount = (f"{self._amount(ctx)} USD a month" if ctx.outputs.get("budget_amount")
                  else f"an amount asked before it is created (Enter keeps {default_budget(ctx)} USD a month)")
        return Plan(f"create budget {self._name(ctx)} of {amount} on the project, alerts at 50/90/100 %",
                    (f"billing account {self._account(ctx)}",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        if not ctx.outputs.get("budget_amount"):
            # No approval ran (the project already existed and was linked), so
            # nobody has chosen the amount yet: ask it here (owner pick, 2026-10-30).
            default = default_budget(ctx)
            amount = parse_budget(self._ask(
                ctx, self.id, f"Monthly budget for {ctx.project}, in US dollars (Enter keeps it)",
                default=str(default),
            ), default)
            remember(ctx, outputs={"budget_amount": amount})
        number = ctx.outputs.get("project_number") or project_number(self.gcp, ctx.project)
        body = {
            "displayName": self._name(ctx),
            "budgetFilter": {"projects": [f"projects/{number}"]},
            "amount": {"specifiedAmount": {"currencyCode": "USD", "units": str(self._amount(ctx))}},
            "thresholdRules": [{"thresholdPercent": p} for p in (0.5, 0.9, 1.0)],
        }
        try:
            # Stage 1 enabled the Budgets API minutes ago: its SERVICE_DISABLED
            # answer is waited out, never taken as a refusal (audit 4a F4).
            after_enabling(ctx, self.id, lambda: self.gcp.create_budget(self._account(ctx), body))
            return
        except ApiError as exc:
            if not exc.denied:
                raise
        # Refused: the budget is made by hand on the account's Budgets page,
        # and the engine polls the account's budgets for its name.
        account = self._account(ctx)
        open_page(budgets_page(account, ctx.project), why=(
            f"You cannot create budgets on billing account {account} (billing.budgets.create). On its Budgets "
            f"& alerts page, Create budget: named {self._name(ctx)}, scoped to project {ctx.project}, "
            f"{self._amount(ctx)} USD a month, alerts at 50/90/100 % (or ask a billing administrator to); the "
            "installer carries on by itself once it sees it"))

        def made() -> str:
            try:
                return MET if self._check(ctx).status == MET else UNMET
            except ApiError:
                return UNMET

        try:
            wait_until(f"{self.id}:by-hand", BUDGET_BY_HAND, made, clock=ctx.clock, events=ctx.events)
        except WaitGaveUp:
            raise PermissionError(
                f"you cannot create budgets on billing account {account} (billing.budgets.create), and no "
                f"budget named {self._name(ctx)} appeared on its Budgets page within "
                f"{BUDGET_BY_HAND.bound_s / 60:.0f} min; create it there, scoped to {ctx.project}, alerts at "
                "50/90/100 %, and run swarm install again"
            ) from None


def _editor_members(policy: Mapping[str, Any], member: str) -> bool:
    return any(
        b.get("role") == "roles/editor" and member in (b.get("members") or [])
        for b in policy.get("bindings", []) or []
    )


class DefaultComputeEditorStep(_Base):
    id = "foundations.default-compute-editor"
    satisfies = ("iam-binding.default-compute-sa-editor-removed",)
    CONFIRM = "remove-editor"
    _explain = (
        "Older organizations grant Editor to the default compute service account, so anything running as it "
        "can act as every swarm account (#1020, lesson 6). No SwarmCloud service runs as it; the grant is "
        "removed on day one."
    )

    def _member(self, ctx: Any) -> str:
        number = ctx.outputs.get("project_number") or project_number(self.gcp, ctx.project)
        return f"serviceAccount:{number}-compute@developer.gserviceaccount.com"

    def _check(self, ctx: Any) -> Observation:
        member = self._member(ctx)
        if _editor_members(self.gcp.iam_policy(ctx.project), member):
            return Observation(UNMET, f"{member} holds roles/editor")
        return Observation(MET, f"{member} holds no roles/editor")

    def plan(self, ctx: Any) -> Plan:
        return Plan("remove roles/editor from the default compute service account (typed confirmation)",
                    (f"remove {self._member(ctx)} from roles/editor on {ctx.project}",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        member = self._member(ctx)
        typed = self._ask(ctx, self.id, f"Type {self.CONFIRM} to remove roles/editor from {member}").strip()
        if typed != self.CONFIRM:
            raise PermissionError(f"not confirmed (typed {typed!r}, not {self.CONFIRM!r}); nothing was changed")

        def write() -> None:
            policy = dict(self.gcp.iam_policy(ctx.project))  # re-read each try: the etag moves
            bindings = []
            for binding in policy.get("bindings", []) or []:
                if binding.get("role") == "roles/editor" and member in (binding.get("members") or []):
                    binding = {**binding, "members": [m for m in binding["members"] if m != member]}
                    if not binding["members"]:
                        continue
                bindings.append(binding)
            policy["bindings"] = bindings
            self.gcp.set_iam_policy(ctx.project, policy)

        retry_rate_limited(
            write, ctx.clock,
            on_retry=lambda why: ctx.events.emit("wait_countdown", step=self.id, reason=why, state="waiting",
                                                 expected="seconds", remaining_s=0),
        )


#: bootstrap.sh's settings for the state bucket (scripts/bootstrap.sh:105-113).
STATE_BUCKET_LABELS = {"managed-by": "swarm-bootstrap", "component": "tfstate"}


class StateBucketStep(_Base):
    id = "foundations.state-bucket"
    satisfies = ("bucket.state", "other.install-state")
    _explain = (
        "Bootstrap state on one laptop planned '9 to import, 91 to add' from a fresh worktree (lesson 7). "
        "The bucket is created once, with versioning, uniform access and public access prevented, and the "
        "install state moves into it at once; Terraform state never lives anywhere else."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask) -> None:
        super().__init__(gcp, ask)

    def _problems(self, found: Mapping[str, Any] | None) -> list[str]:
        if found is None:
            return ["the bucket does not exist"]
        problems = []
        if not (found.get("versioning") or {}).get("enabled"):
            problems.append("versioning is off")
        iam = found.get("iamConfiguration") or {}
        if not (iam.get("uniformBucketLevelAccess") or {}).get("enabled"):
            problems.append("uniform bucket-level access is off")
        if iam.get("publicAccessPrevention") != "enforced":
            problems.append("public access prevention is not enforced")
        return problems

    def _check(self, ctx: Any) -> Observation:
        name = state_bucket(ctx.project)
        problems = self._problems(self.gcp.bucket(name))
        if not problems and ctx.engine.store.bucket is None:
            problems.append("the install state is still local only")
        if not problems and ctx.engine.store.bucket.read(STATE_OBJECT) is None:
            problems.append(f"gs://{name}/{STATE_OBJECT} does not exist")
        if problems:
            return Observation(UNMET, f"gs://{name}: " + "; ".join(problems))
        return Observation(MET, f"gs://{name} holds the install state", {"state_bucket": name})

    def plan(self, ctx: Any) -> Plan:
        name = state_bucket(ctx.project)
        actions = []
        if self.gcp.bucket(name) is None:
            actions.append(f"create gs://{name} in {ctx.state.region}: versioning, uniform access, "
                           "public access prevention enforced")
        actions.append(f"upload the local install state to gs://{name}/{STATE_OBJECT}")
        return Plan("create the state bucket, then move the install state into it", tuple(actions))

    def apply(self, ctx: Any, plan: Plan) -> None:
        name = state_bucket(ctx.project)
        found = self.gcp.bucket(name)
        if found is None:
            if not ctx.state.region:
                raise LookupError("no region is confirmed (preflight.region)")
            self.gcp.create_bucket(ctx.project, {
                "name": name,
                "location": ctx.state.region,
                "versioning": {"enabled": True},
                "iamConfiguration": {
                    "uniformBucketLevelAccess": {"enabled": True},
                    "publicAccessPrevention": "enforced",
                },
                "labels": {**STATE_BUCKET_LABELS, "environment": ctx.environment},
            })
        elif self._problems(found):
            raise RuntimeError(f"gs://{name} exists but {'; '.join(self._problems(found))}; fix it by hand "
                               "(a state bucket is never changed under its writer)")
        ctx.attach_bucket(self.gcp.object_store(name))


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK, manifest: Manifest | None = None,
                api: ProjectApi | None = None) -> list[Any]:
    """Stage 1, in order; the engine also orders a stage by the manifest's depends_on."""
    return [
        ProjectStep(gcp, ask, api),
        ApisStep(gcp, ask, manifest if manifest is not None else load_manifest(), api),
        BudgetStep(gcp, ask),
        DefaultComputeEditorStep(gcp, ask),
        StateBucketStep(gcp, ask),
    ]


STEPS = build_steps()
