"""Scheduler process settings.

The frozen `Settings` holds the platform-wide values. What is here is specific
to one drain loop: how many tasks to look at per pass, when to stop, and how
starvation aging is shaped.

The drain loop is BOUNDED in three independent ways, because a scheduler that
runs forever on a Pub/Sub push is a scheduler that gets killed mid-transaction:
a wall-clock budget, a maximum number of admissions, and a maximum number of
passes. Whichever trips first ends the run, and the next push (or the 1-minute
safety tick) picks up where it left off.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from swarm_common.config import Settings
from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES

from .judgment import check_internal_decision_api_url

log = logging.getLogger(__name__)

#: `<registry path>@sha256:<64 hex>`, and nothing before the `@` that could be a
#: tag. The same shape terraform/infra/variables.tf validates `image_refs`
#: against: the runtime ignores a tag when a digest is present, so a tag there
#: is only a label that can disagree with what runs.
_DIGEST_REF = re.compile(r"^[^@:\s]+@sha256:[0-9a-f]{64}$")


def check_worker_image_refs(refs: object) -> None:
    """Refuse a worker image map that is not digest-pinned, naming the entry.

    A scheduler configured with a tag must fail when it STARTS, not at its
    first dispatch -- by then admission has taken a lease, and the failure
    surfaces as a task stuck behind a pull of the wrong thing.
    """
    if not isinstance(refs, dict):
        raise ValueError(
            f"WORKER_IMAGE_REFS must be a JSON object of image name -> digest ref, "
            f"got {type(refs).__name__}"
        )
    for name, ref in refs.items():
        if not isinstance(name, str) or not isinstance(ref, str) or not _DIGEST_REF.match(ref):
            raise ValueError(
                f"WORKER_IMAGE_REFS[{name!r}] = {ref!r} is not pinned by digest; every "
                "value must be `<registry>/<image>@sha256:<64 hex>` with no tag"
            )
        if not ref.split("@", 1)[0].endswith(f"/{name}"):
            raise ValueError(
                f"WORKER_IMAGE_REFS[{name!r}] = {ref!r} is the digest of a different "
                "image; each key must be the image's own name"
            )


def parse_worker_image_refs(raw: str) -> dict[str, str]:
    """WORKER_IMAGE_REFS -> {image name: digest ref}. Empty means none."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        refs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WORKER_IMAGE_REFS is not valid JSON: {exc}") from exc
    check_worker_image_refs(refs)
    return dict(refs)


def check_worker_models(models: object) -> None:
    """Refuse a model map that is not {known runner profile: model name}.

    Like `check_worker_image_refs`, at START: a scheduler that could not read
    the map would create Jobs with no MODEL, and their agents would run the
    CLI's default model with nothing saying so. The model name's characters
    are the runner's to check (`cliagent`, where it becomes `--model`); what
    is refused here is a shape that cannot be a model map at all.
    """
    if not isinstance(models, dict):
        raise ValueError(
            f"WORKER_MODELS must be a JSON object of runner profile -> model name, "
            f"got {type(models).__name__}"
        )
    for profile, model in models.items():
        if not isinstance(profile, str) or profile not in RUNNER_PROFILES:
            raise ValueError(
                f"WORKER_MODELS names {profile!r}, which is not a runner profile in "
                f"the catalogue ({', '.join(sorted(RUNNER_PROFILES))})"
            )
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"WORKER_MODELS[{profile!r}] = {model!r} is not a model name"
            )


def check_worker_class_models(models: object) -> None:
    """Refuse a class map that is not exactly {MODEL_CLASSES name: an ID it admits}.

    #1435, docs/design/model-per-step.md §4.2. WORKER_CLASS_MODELS is
    terraform's `local.runner_models`, keyed by class; the scheduler renders a
    step's `MODEL` from it (`dispatch.step_model`). At START, like the image
    map, and against the frozen catalogue rather than a restatement of it:

      * an ID its class does not admit -- `haiku = "claude-opus-5-5"` -- would
        bill every haiku step as Opus with nothing saying so;
      * a missing class would leave that class with no pin, and its steps
        would fail at dispatch, after admission has taken a lease;
      * an extra key is a class the catalogue does not have, which no step can
        name: a typo for one that it does, so the real one is missing.

    EMPTY IS ALLOWED, and means the deployment pins no class (the local
    emulator loop, and a settings object a test builds). Then a step naming a
    class refuses at dispatch and a step naming none runs as before this map
    existed, on WORKER_MODELS (`dispatch.step_model`).
    """
    if not isinstance(models, dict):
        raise ValueError(
            f"WORKER_CLASS_MODELS must be a JSON object of model class -> model ID, "
            f"got {type(models).__name__}"
        )
    if not models:
        return
    missing = sorted(set(MODEL_CLASSES) - set(models))
    extra = sorted(str(k) for k in set(models) - set(MODEL_CLASSES))
    if missing or extra:
        raise ValueError(
            f"WORKER_CLASS_MODELS must name exactly the model classes "
            f"{tuple(MODEL_CLASSES)}; missing {missing}, not a class {extra}"
        )
    for name, model_id in models.items():
        if not MODEL_CLASSES[name].admits(model_id):
            raise ValueError(
                f"WORKER_CLASS_MODELS[{name!r}] = {model_id!r} is not a {name} model: "
                f"a pin of this class is claude-{MODEL_CLASSES[name].family}-<major>"
                f"[-<minor>][-<yyyymmdd>] with major at least "
                f"{MODEL_CLASSES[name].min_major}"
            )


def parse_worker_class_models(raw: str) -> dict[str, str]:
    """WORKER_CLASS_MODELS -> {model class: model ID}. Empty means none pinned.

    Written by terraform/infra/locals.tf as `jsonencode(local.runner_models)`
    (`model_class_env`), the ONE place a class's ID is stated (#1435 Q1).
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        models = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WORKER_CLASS_MODELS is not valid JSON: {exc}") from exc
    check_worker_class_models(models)
    return dict(models)


def check_spec_settings(settings: object) -> None:
    """The worker's step-spec settings, refused here rather than on every Job.

    The worker parses the same values (`agent_worker.specverify`); a bad one
    passed through would fail every worker this scheduler creates as
    CANNOT_START, which is the loudest possible place to find a typo and the
    most expensive.
    """
    import json

    raw = str(getattr(settings, "spec_verify_keys", "") or "")
    if raw:
        try:
            keys = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"SPEC_VERIFY_KEYS is not valid JSON: {exc}") from exc
        if not isinstance(keys, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
        ):
            raise ValueError("SPEC_VERIFY_KEYS must be a JSON object of version name -> PEM")
    mode = str(getattr(settings, "spec_signature_mode", "") or "").lower()
    if mode and mode not in ("enforce", "legacy"):
        raise ValueError(f"SPEC_SIGNATURE_MODE must be enforce or legacy, got {mode!r}")


def parse_worker_models(raw: str) -> dict[str, str]:
    """WORKER_MODELS -> {runner profile: model}. Empty means no profile pins one.

    Written by terraform/infra/locals.tf from `local.runner_models`, the ONE
    place a profile's model is stated (#226). The same value is `MODEL` on the
    Jobs Terraform creates; this is how the Jobs THIS scheduler creates, for
    tenants Terraform does not list, get it too.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        models = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WORKER_MODELS is not valid JSON: {exc}") from exc
    check_worker_models(models)
    return dict(models)


def parse_enforced_since(raw: str | None) -> datetime | None:
    """ON_STEP_FAILURE_ENFORCED_SINCE -> an aware datetime, or None when empty.

    An offset is REQUIRED. A naive time would be read in whatever zone the
    process runs in, and a cutoff that is hours off either cancels work it
    should have spared or spares work it should have cancelled. So a value
    without one refuses to start, as a malformed value does.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"ON_STEP_FAILURE_ENFORCED_SINCE must be an ISO-8601 timestamp with a "
            f"UTC offset, such as 2026-09-25T14:00:00Z; got {raw!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"ON_STEP_FAILURE_ENFORCED_SINCE={raw!r} has no UTC offset; write it as "
            "2026-09-25T14:00:00Z or with an explicit +HH:MM"
        )
    return parsed


#: A tenant id as register-tenant.sh and the tenancy module accept one
#: (`WS_GROUP_TENANT_RE`; a personal `u-` id matches it too): lowercase
#: letters, digits and inner hyphens, at most a Kubernetes label's 63.
_TENANT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def parse_state_via_api_tenants(raw: str | None) -> frozenset[str]:
    """SWARM_STATE_VIA_API_TENANTS -> the tenant ids switched to the state routes.

    docs/design/tenant-settings-integrity.md §4.6 (ISO-SCHED phase 2, #1457).
    Comma-separated; empty, the default, switches nobody.

    AN ENTRY THAT CANNOT BE A TENANT ID IS DROPPED AND LOGGED, IN ONE WARNING,
    AND NEVER STOPS THE SCHEDULER (owner decision, 2026-10-20). A typo in this
    list must not stop dispatch for every tenant, and dropping it is the safe
    direction: an unswitched tenant runs exactly as it did before the switch
    existed. `*` is such an entry. The design names it as "every tenant", and
    it is not built: switching every tenant at once is the owner's step 5, and
    a list says which tenants it was. A well-formed id no tenant has matches
    no attempt, so it is inert without being dropped.
    """
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    kept = frozenset(item for item in items if _TENANT_ID.match(item))
    dropped = sorted({item for item in items if item not in kept})
    if dropped:
        log.warning(
            "SWARM_STATE_VIA_API_TENANTS: ignoring %s, which cannot be a tenant id; "
            "no attempt of theirs is switched to the state routes",
            ", ".join(repr(item) for item in dropped),
        )
    return kept


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class SchedulerSettings:
    core: Settings

    #: READY tasks read per pass. Large enough that round-robin sees several
    #: tenants, small enough that one Firestore query stays cheap.
    candidate_batch_size: int = 200

    #: Fairness top-up. The global candidate query returns the highest-priority
    #: slice, so a tenant with more than `candidate_batch_size` high-priority
    #: tasks queued can fill it entirely and every other tenant disappears from
    #: the rotation -- round-robin cannot interleave what it never saw. When the
    #: slice comes back FULL (and only then, because a short slice is already
    #: the whole queue), the scheduler asks each unrepresented tenant for a few
    #: of its own READY tasks and adds them to the rotation.
    tenant_topup_candidates: int = 8
    #: Ceiling on how many tenants that top-up may query in one pass, so the
    #: cost of a pass stays bounded however many tenants exist.
    max_topup_tenants: int = 50

    #: Hard stops on the drain loop.
    max_leases_per_run: int = 200
    max_passes_per_run: int = 25
    max_run_seconds: float = 45.0

    #: Starvation aging. A task's effective priority rises by `aging_step` for
    #: every `aging_interval_seconds` it has waited, capped at `aging_max_bonus`.
    #: The cap is what stops aging from inverting the priority scheme entirely:
    #: an old low-priority task should overtake a fresh one, not outrank an
    #: urgent one forever.
    aging_interval_seconds: int = 60
    aging_step: int = 1
    aging_max_bonus: int = 50

    #: Dependency promotion sweep: PARKED(DEPENDENCY_INCOMPLETE) tasks examined
    #: per run. Bounded for the same reason as the candidate batch.
    dependency_sweep_size: int = 200

    #: How long a MERGE verdict's integrator waits PARKED after its last parent
    #: ended, for swarm-api to open its pull request without a worker (#748,
    #: `swarm_api.verdictpublish`). 0, the default, holds nothing: the hold
    #: only pays when swarm-api's `task_finished` push subscription exists,
    #: and terraform sets this in the same apply that creates it. A held step
    #: holds no capacity (invariant 1); if swarm-api never decides, it is
    #: promoted when the hold ends, as it was before.
    control_publish_hold_seconds: float = 0.0

    #: Prewarm: PARKED tasks whose cooldown expires within the lead window are
    #: promoted to READY early, so they are queued the instant capacity appears.
    #: READY costs nothing (invariant 1), so this is a latency win with no spend.
    enable_prewarm: bool = True

    #: `on_step_failure: fail_workflow` applies only to workflows whose document
    #: `created_at` is at or after this instant. From ON_STEP_FAILURE_ENFORCED_SINCE,
    #: an ISO-8601 timestamp with a UTC offset.
    #:
    #: WHY IT EXISTS. Until the scheduler read the field, every workflow was
    #: stored with `fail_workflow` (the API default), and docs/workflows.md told
    #: the submitter the setting was not honoured and behaved like `continue`.
    #: Applying the rule to such a workflow cancels steps nobody was told would
    #: be cancelled. A cancel cannot be undone, and a cancelled step's checkpoint
    #: becomes reclaimable. Workflows created before the cutoff keep the rule
    #: they were submitted under: only the dependents of a failed step are
    #: cancelled.
    #:
    #: WHY THE DEFAULT IS None. None means every workflow, retroactively, which
    #: is the owner's 2026-09-24 decision as it was written. Whether in-flight
    #: workflows should be exempt was not part of that decision and is open.
    #: This setting is how the other answer is given without a code change.
    #: `python -m scheduler.on_step_failure_audit` lists, read-only, what the
    #: next drain would cancel under either answer.
    on_step_failure_enforced_since: datetime | None = None

    #: GKE dispatch target (browser/GPU profiles only).
    gke_cluster: str = ""
    gke_location: str = ""

    #: Kubernetes service account the worker pod runs as, inside the tenant's own
    #: namespace. It must match the KSA the Workload Identity binding was issued
    #: for, or the pod gets no Google identity: terraform's tenancy module binds
    #: `<pool>[<namespace>/<ksa_name>]` with `ksa_name` defaulting to
    #: `swarm-agent-worker`. The namespace is what makes the binding per-tenant,
    #: so one shared spelling here is correct and is not a cross-tenant hole.
    worker_ksa_name: str = "swarm-agent-worker"

    #: Base URL of the quota broker, passed THROUGH to every worker as
    #: QUOTA_BROKER_URL. The scheduler never calls it; it is the only component
    #: that writes a worker's execution environment, so it is the only place
    #: this can be handed over.
    #:
    #: Without it the account pool is inert in production and nothing says so:
    #: `WorkerConfig.quota_broker_url` stays None, `Worker.__init__` builds no
    #: AccountBroker, and `_lease_account` returns None on its first branch for
    #: every task ever dispatched. The feature was fully implemented, fully
    #: tested -- every worker test injects `WorkerDeps.account_broker`, which is
    #: exactly what hid it -- and reached zero agents.
    #:
    #: Empty means "this deployment has no pool", which is the documented
    #: backwards-compatible default and not a guess at a URL.
    #:
    #: ADMISSION READS IT TOO, and only for that yes or no (credentials.py,
    #: #169). Set, a tenant with no key of its own may still be admitted for a
    #: profile that takes a subscription token, if an account in the
    #: `accounts` collection is owned by or lent to it. The scheduler reads
    #: that collection from Firestore; it still never calls the broker.
    quota_broker_url: str = ""
    #: OIDC audience the worker asks the metadata server for. Cloud Run checks
    #: `aud` against the service URL unless the service declares a custom
    #: audience -- and this one does (`custom_audiences` in terraform, which is
    #: how the broker's own `BROKER_AUDIENCE` check is satisfied), so the URL
    #: alone would mint a token the broker rejects. Empty falls back to the URL,
    #: which is correct for a deployment with no custom audience.
    quota_broker_audience: str = ""

    #: The console's origin, `https://<frontend_hostname>` (SWARM_CONSOLE_URL,
    #: rendered by terraform onto this service). Passed THROUGH to every worker
    #: by `worker_env`, which is the only way a worker learns it; the worker
    #: uses it for nothing but the console links in a pull request body.
    #: Empty means "no console link anywhere", never a guessed run.app URL --
    #: swarm-ui behind IAP does not open at its run.app address.
    console_url: str = ""
    #: Whether a pull request a worker opens carries the workflow and agent
    #: console links (SWARM_PR_CONSOLE_LINKS, terraform `pr_console_links`).
    #: Default False: off until the owner has seen it on a real PR (owner
    #: decision, 2026-10-01).
    pr_console_links: bool = False
    #: The internal decision API through the cluster's Tailscale egress Service
    #: (INTERNAL_DECISION_API_URL; JUDGE-NET's switch, terraform
    #: `internal_decision_api_enabled`). Set, a live-browser pod whose
    #: workspace chose an internal judgment backend gets JEV_API_URL under it
    #: and the `swarm-judge: internal` label (`scheduler.judgment`). Empty,
    #: no pod gets either, and an internal choice leaves Jev unavailable
    #: rather than sending page text to TypeSafe. Only "" or the decision
    #: API's tailnet origin (`judgment.INTERNAL_DECISION_ORIGIN`), because
    #: that is the one host the jev wrapper accepts and the pod's hostAliases
    #: entry resolves.
    internal_decision_api_url: str = ""

    # --- child tasks (docs/design/child-tasks.md) --------------------------
    #: `swarm-child-key` (SWARM_CHILD_KEY), from Secret Manager, held by this
    #: service and swarm-api only. The dispatcher mints each attempt's one-use
    #: registration nonce with it (§3.2 step 1). EMPTY MEANS NO CHILD PATH: no
    #: nonce is passed and the worker offers its agent no spool. Not in the
    #: repr, so a logged settings object cannot carry it.
    child_key: str = field(default="", repr=False)
    #: swarm-api's address and token audience, passed to a worker that has a
    #: child path so it can call the worker-only child routes. Platform
    #: settings, never a caller's (invariant 10). Omitted when empty.
    swarm_api_url: str = ""
    swarm_api_audience: str = ""
    #: The forge broker's switch (FORGE_BROKER, terraform `forge_broker`;
    #: #1041, docs/design/user-scoped-secrets.md §5). Off by default and in
    #: every environment until the owner turns it on. On, every worker is
    #: handed FORGE_BROKER=true, and a CHILD on a person's `git-u-` slot also
    #: gets the registration nonce, so it can prove itself to the broker. Off,
    #: dispatch is exactly what it was before the broker: no child is keyed
    #: for its slot, and workers read the slot with their own account.
    forge_broker: bool = False
    #: How long an awaiting parent waits for its children before the
    #: outstanding ones are cancelled and it is promoted (§7, F7). A day covers
    #: a full daily provider quota reset with margin.
    child_await_max_seconds: int = 86400

    project_id: str = ""
    region: str = "us-central1"
    artifact_registry_host: str = ""
    #: Tag for the agent runtime images, used ONLY when `worker_image_refs` is
    #: empty -- which a terraform-managed deployment never is. It remains for
    #: the local emulator loop, which has no promotion manifest to pin from.
    #: "latest" is not pushed by scripts/build-images.sh, so a deployed
    #: scheduler that somehow lost its digest map fails loudly on a 404 rather
    #: than quietly running whatever a tag points at.
    worker_image_tag: str = "latest"
    #: Runner image name -> `<registry>/<name>@sha256:<64 hex>`, from
    #: WORKER_IMAGE_REFS (terraform/infra/locals.tf writes it from the
    #: promotion manifest). When non-empty it is the ONLY source of a worker
    #: image: a tag is resolved when the image is pulled, so what an agent ran
    #: used to be whatever the tag pointed at on that node at that moment.
    #: Excluded from the hash: a dict is unhashable and the settings are frozen.
    worker_image_refs: dict[str, str] = field(default_factory=dict, hash=False)
    #: Runner profile -> the model its agent CLI runs, from WORKER_MODELS
    #: (terraform/infra/locals.tf derives it from `local.runner_models` through
    #: each profile's `default_model_class`, for one release, #1435). READ ONLY
    #: WHEN `worker_class_models` IS EMPTY: then it is the `MODEL` on every
    #: Cloud Run Job this scheduler CREATES and on every GKE pod of a pinned
    #: profile (`profile_model`), as it was before the class map (#226;
    #: contract request 53), and never in the per-execution environment. A
    #: task's own `model` is attribution on either path. Excluded from the
    #: hash, like `worker_image_refs`.
    worker_models: dict[str, str] = field(default_factory=dict, hash=False)
    #: Model class -> the ID a step of that class runs, from WORKER_CLASS_MODELS
    #: (terraform's `local.runner_models`; #1435). When set it is the ONLY
    #: source of a step's `MODEL`, and `worker_env` carries it on both backends
    #: (`dispatch.step_model`): the class is the step's, resolved by the frozen
    #: `step_model_class`, and the ID is this map's, never the task's own
    #: `model` (invariant 10). It also sets the default class's pin as the
    #: `MODEL` of every Cloud Run Job this scheduler creates. Empty, the
    #: scheduler runs on `worker_models` as before, and a step naming a class
    #: refuses at dispatch. Excluded from the hash, like `worker_image_refs`.
    worker_class_models: dict[str, str] = field(default_factory=dict, hash=False)
    #: THE LIVE-BROWSER ROLLOUT SWITCH (docs/design/live-browser.md, lane L5,
    #: part of #1030). On, a GKE pod of a `live_browser` profile
    #: (`claude-code-browser`) carries the `display` native sidecar, which runs
    #: the live gate (`python -m agent_worker.livegate`); off, it is the pod it
    #: always was. OFF BY DEFAULT because the gate is built by lanes L1-L4,
    #: and a native sidecar whose entrypoint does not exist yet crash-loops in
    #: every `claude-code-browser` pod. Turn it on (LIVE_BROWSER_SIDECAR=true)
    #: once the image that the scheduler dispatches carries the gate.
    live_browser_sidecar: bool = False
    #: THE BROWSER EGRESS SWITCH, the dispatcher's side (docs/browser-egress.md
    #: §10.3; lane EGRESS-DISPATCH, part of #1030). A live-browser pod is
    #: dispatched for residential egress -- `SWARM_BROWSER_EGRESS=proxy` on its
    #: `display` sidecar and the `swarm-egress: proxy` label -- only when
    #: `browser_egress_enabled`, its tenant is in `browser_egress_tenants` AND
    #: its profile is in `browser_egress_profiles` (scheduler.dispatch
    #: `browser_egress_on`). terraform/infra/egress.tf renders these from
    #: var.browser_egress, the SAME values it gives swarm-api, which mints the
    #: attempt's ticket by the same three tests: a pod routed here that
    #: swarm-api refused would fail closed with no browser, and one left
    #: direct that swarm-api would mint for would never ask. OFF BY DEFAULT,
    #: and the profile default is swarm-api's, so the two agree on a
    #: deployment that names only the tenants. Never from a task (invariant 10).
    browser_egress_enabled: bool = False
    browser_egress_tenants: tuple[str, ...] = ()
    browser_egress_profiles: tuple[str, ...] = ("claude-code-browser",)
    #: Hosts the browser reaches DIRECT, as SWARM_EGRESS_BYPASS: egress.tf's
    #: `egress_bypass_hosts`, the list swarm-egress refuses a CONNECT for, so
    #: the gate's `--proxy-bypass-list` and the forwarder's refusal agree.
    browser_egress_bypass: tuple[str, ...] = ()
    #: THE TYPESAFE FORWARDER SWITCH, the dispatcher's side (docs/design/
    #: workspace-secrets.md §3.5, SEC-MIGRATE-TYPESAFE; lane TS-MIGRATE-POD,
    #: part of #1347). A GKE live-browser pod whose workspace chose the
    #: `typesafe` judgment backend is labelled `swarm-typesafe: inject` -- the
    #: label both sides of the forwarder's NetworkPolicy select -- only when
    #: `typesafe_forwarder_enabled` and its tenant is in
    #: `typesafe_forwarder_tenants` (scheduler.dispatch `typesafe_forwarder_on`).
    #: terraform/infra/typesafe_fwd.tf renders these from var.typesafe_forwarder,
    #: the SAME `enabled` and `tenants` the forwarder re-reads as its kill
    #: switch, and "false" unless the forwarder is served: a pod labelled for a
    #: tenant the forwarder refuses would fail every judgment, and one left
    #: unlabelled could never reach it. OFF BY DEFAULT. Never from a task
    #: (invariant 10).
    typesafe_forwarder_enabled: bool = False
    typesafe_forwarder_tenants: tuple[str, ...] = ()
    #: THE SETTINGS-MAC SWITCH (docs/design/tenant-settings-integrity.md §4.5,
    #: interim A; lane ISO-SCHED, part of #1457). On, a `workspace_settings`
    #: `judgment_backend` -- top level or `code_intelligence.` -- counts only
    #: when its block's MAC verifies under `settings_mac_key`
    #: (`judgment.trusted_settings`); a forged or unsigned one reads as the
    #: default, internal-jev, for the pod (`judgment.read_backend`) and for
    #: the router (`dispatch.read_judge_backend`) alike. Every tenant's worker
    #: can write any tenant's settings document today, which is #1457.
    #: OFF BY DEFAULT, because on it reads every document swarm-api has not
    #: yet signed as unset: turn it on (SETTINGS_MAC_REQUIRED=true) only after
    #: lane ISO-SETTINGS-MAC's re-sign job has signed the documents that exist.
    settings_mac_required: bool = False
    #: `swarm-settings-key` (SWARM_SETTINGS_KEY), from Secret Manager, held by
    #: swarm-api, which signs with it, and this service, which verifies. Not in
    #: the repr, so a logged settings object cannot carry it.
    settings_mac_key: str = field(default="", repr=False)
    #: THE PER-TENANT STATE SWITCH (docs/design/tenant-settings-integrity.md
    #: §4.6; ISO-SCHED phase 2, part of #1457). Every attempt of a tenant in
    #: this set -- on both backends, a child included -- is dispatched with
    #: SWARM_STATE_VIA_API=true and SWARM_API_URL, and is keyed, so its worker
    #: reaches its state only through swarm-api's state routes
    #: (`dispatch.worker_env`). From SWARM_STATE_VIA_API_TENANTS (terraform
    #: `state_via_api_tenants`; `parse_state_via_api_tenants`). EMPTY BY
    #: DEFAULT: an unlisted tenant's worker uses direct Firestore and is
    #: dispatched byte-for-byte as before the switch existed. Dev first, then
    #: every tenant, and only then is the worker's Firestore role removed
    #: (owner decision Q7). Never from a task (invariant 10).
    state_via_api_tenants: frozenset[str] = frozenset()
    dispatch_topic: str = ""
    #: The step-spec verification settings a worker needs (contract request
    #: 34), passed through VERBATIM onto every Cloud Run Job this scheduler
    #: CREATES -- the same values terraform renders onto its own Jobs -- and
    #: NEVER into `worker_env`, which a task shapes. Empty means the scheduler
    #: was deployed without them and its Jobs' workers verify nothing: they
    #: refuse a signed task as CANNOT_START rather than run it unverified.
    spec_verify_keys: str = ""
    spec_signing_key: str = ""
    spec_signature_mode: str = ""
    spec_legacy_cutover: str = ""

    @classmethod
    def from_env(cls) -> "SchedulerSettings":
        core = Settings.from_env()
        registry = os.environ.get(
            "ARTIFACT_REGISTRY_HOST",
            f"{core.region}-docker.pkg.dev/{core.project_id}/{core.artifact_registry}",
        )
        return cls(
            core=core,
            candidate_batch_size=_int("CANDIDATE_BATCH_SIZE", 200),
            tenant_topup_candidates=_int("TENANT_TOPUP_CANDIDATES", 8),
            max_topup_tenants=_int("MAX_TOPUP_TENANTS", 50),
            max_leases_per_run=_int("MAX_LEASES_PER_RUN", 200),
            max_passes_per_run=_int("MAX_PASSES_PER_RUN", 25),
            max_run_seconds=_float("MAX_RUN_SECONDS", 45.0),
            aging_interval_seconds=_int("AGING_INTERVAL_SECONDS", 60),
            aging_step=_int("AGING_STEP", 1),
            aging_max_bonus=_int("AGING_MAX_BONUS", 50),
            dependency_sweep_size=_int("DEPENDENCY_SWEEP_SIZE", 200),
            control_publish_hold_seconds=_float("CONTROL_PUBLISH_HOLD_SECONDS", 0.0),
            enable_prewarm=_bool("ENABLE_QUOTA_PREWARM", core.enable_quota_prewarm),
            on_step_failure_enforced_since=parse_enforced_since(
                os.environ.get("ON_STEP_FAILURE_ENFORCED_SINCE")
            ),
            gke_cluster=os.environ.get("GKE_CLUSTER", ""),
            gke_location=os.environ.get("GKE_LOCATION", core.region),
            worker_ksa_name=os.environ.get("WORKER_KSA_NAME", "").strip()
            or "swarm-agent-worker",
            quota_broker_url=os.environ.get("QUOTA_BROKER_URL", "").strip(),
            quota_broker_audience=os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip(),
            console_url=os.environ.get("SWARM_CONSOLE_URL", "").strip(),
            pr_console_links=_bool("SWARM_PR_CONSOLE_LINKS", False),
            internal_decision_api_url=os.environ.get("INTERNAL_DECISION_API_URL", "").strip(),
            child_key=os.environ.get("SWARM_CHILD_KEY", "").strip(),
            swarm_api_url=os.environ.get("SWARM_API_URL", "").strip(),
            forge_broker=_bool("FORGE_BROKER", False),
            swarm_api_audience=os.environ.get("SWARM_API_AUDIENCE", "").strip(),
            child_await_max_seconds=_int("CHILD_AWAIT_MAX_SECONDS", 86400),
            project_id=core.project_id,
            region=core.region,
            artifact_registry_host=registry,
            worker_image_tag=(
                os.environ.get("WORKER_IMAGE_TAG")
                or os.environ.get("IMAGE_TAG")
                or "latest"
            ),
            worker_image_refs=parse_worker_image_refs(os.environ.get("WORKER_IMAGE_REFS", "")),
            worker_models=parse_worker_models(os.environ.get("WORKER_MODELS", "")),
            worker_class_models=parse_worker_class_models(
                os.environ.get("WORKER_CLASS_MODELS", "")
            ),
            live_browser_sidecar=_bool("LIVE_BROWSER_SIDECAR", False),
            browser_egress_enabled=_bool("BROWSER_EGRESS_ENABLED", False),
            browser_egress_tenants=_csv("BROWSER_EGRESS_TENANTS"),
            browser_egress_profiles=_csv("BROWSER_EGRESS_PROFILES", "claude-code-browser"),
            browser_egress_bypass=_csv("BROWSER_EGRESS_BYPASS"),
            typesafe_forwarder_enabled=_bool("TYPESAFE_FORWARDER_ENABLED", False),
            typesafe_forwarder_tenants=_csv("TYPESAFE_FORWARDER_TENANTS"),
            settings_mac_required=_bool("SETTINGS_MAC_REQUIRED", False),
            settings_mac_key=os.environ.get("SWARM_SETTINGS_KEY", "").strip(),
            state_via_api_tenants=parse_state_via_api_tenants(
                os.environ.get("SWARM_STATE_VIA_API_TENANTS")
            ),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", ""),
            spec_verify_keys=os.environ.get("SPEC_VERIFY_KEYS", "").strip(),
            spec_signing_key=os.environ.get("SPEC_SIGNING_KEY", "").strip(),
            spec_signature_mode=os.environ.get("SPEC_SIGNATURE_MODE", "").strip(),
            spec_legacy_cutover=os.environ.get("SPEC_LEGACY_CUTOVER", "").strip(),
        )

    def __post_init__(self) -> None:
        if self.candidate_batch_size <= 0:
            raise ValueError("candidate_batch_size must be positive")
        if self.tenant_topup_candidates < 0:
            raise ValueError("tenant_topup_candidates cannot be negative")
        if self.max_topup_tenants < 0:
            raise ValueError("max_topup_tenants cannot be negative")
        if self.max_leases_per_run <= 0 or self.max_passes_per_run <= 0:
            raise ValueError("the drain loop must be bounded by a positive number of passes")
        if self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive")
        if self.aging_interval_seconds <= 0:
            raise ValueError("aging_interval_seconds must be positive")
        if self.aging_max_bonus < 0:
            raise ValueError("aging_max_bonus cannot be negative")
        cutoff = self.on_step_failure_enforced_since
        if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
            raise ValueError(
                "on_step_failure_enforced_since must be timezone-aware; a naive "
                "cutoff would be read in whatever zone the process runs in"
            )
        # Checked here as well as when parsed, so a settings object built
        # directly -- as every test does -- cannot carry a tag either.
        check_worker_image_refs(self.worker_image_refs)
        check_worker_models(self.worker_models)
        check_worker_class_models(self.worker_class_models)
        check_spec_settings(self)
        check_internal_decision_api_url(self.internal_decision_api_url)
        # Logged, not refused: a switched tenant's worker refuses to start
        # without a nonce (fail closed, `dispatch.worker_env`), which harms only
        # the switched tenants; refusing here would stop every tenant's dispatch.
        # terraform refuses the combination at plan (`state_via_api_tenants`).
        if self.state_via_api_tenants and not (self.child_key and self.swarm_api_url):
            log.error(
                "SWARM_STATE_VIA_API_TENANTS switches %s, but SWARM_CHILD_KEY or "
                "SWARM_API_URL is empty: their workers get no nonce to open their "
                "attempts with and refuse to start",
                ", ".join(sorted(self.state_via_api_tenants)),
            )
        # At START, like the image map: on with no key would read every
        # workspace's choice as the default without saying why.
        if self.settings_mac_required and not self.settings_mac_key:
            raise ValueError(
                "SETTINGS_MAC_REQUIRED is on and SWARM_SETTINGS_KEY is empty: the "
                "scheduler cannot verify a workspace_settings MAC without the key"
            )
