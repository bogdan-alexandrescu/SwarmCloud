"""Stages 5 and 7: infra pass 1 (every key switch off), then infra pass 2
(switches computed from key versions) and bootstrap pass 2, all as
`swarm-tf-deployer` (design §4, §5; plan R7; lessons 22, 23, 35, 39).

| Step | Stage | Pass | Manifest entries |
|---|---|---|---|
| `infra.pass-1` | 5 | `infra-1` | the services, jobs, Scheduler, Pub/Sub, Firestore, network, cluster and the secret containers |
| `infra.pass-2` | 7 | `infra-2` | `tenant_job_vpc_egress`, the key switches, `enable_console_app_setup`, `require_alert_channels` |
| `bootstrap.pass-2` | 7 | `bootstrap-2` | the IAP programmatic clients and the log view grant |
| `infra.pass-identity` | 8 | `infra-2` again | none: the admin groups stage 8 recorded, and a version for any platform key pass 2 made (the graph ticket key) |
| `infra.pass-close` | 14 | `infra-close` | `other.engine-cluster-reach-closed`: the installer's address leaves the control plane's authorised networks, after stage 14's verify steps |
| `infra.voice-it` | 0 | none | `other.voice-it-choice`: the optional question whether to turn Voice it on |
| `infra.voice-speech` | 0 | none | `other.voice-speech-choice`, `other.voice-speech-offered`: for a yes to Voice it only, the optional question whether to add its own speech engine |
| `infra.voice-speech-zone` | 1 | none | `other.voice-speech-zone`: for a yes, the L4 zone, one NVIDIA_L4_GPUS of quota and the pinned image |

The passes declare no manifest entries as `satisfies`: the manifest's
`depends_on` among the resources one apply makes runs both ways (a container
depends on its switch, the switch on the container's version), so declaring
them would make the engine's dependency gate refuse the stage. Each pass's
check is that its generated inputs are the ones it applied.

Pass 1 runs with every key-gated switch false, so no revision names a secret
that has no version (lesson 39). Stage 6 (`secrets.py`) gives the platform
keys their versions; pass 2 turns each switch on exactly when its key has an
ENABLED version. Pass 2 also records the API backend's IAP audience, which the
hand-off needs to call swarm-api as the deployer. Bootstrap pass 2 follows it
in the same stage (registration order: it is listed after pass 2 here).

**Stage 8 applies what it recorded.** `identity.admin-group` records
`admin_groups` only once swarm-api has read the group, which is after pass 2;
`infra.pass-identity` re-applies pass 2 with them, so a later run's stage 8
checks see what stage 8 recorded. It runs after every identity step: the
registry is discovered by module name and `infra` follows `identity`, and it
declares no manifest entry (each stage 8 entry is an identity step's), so the
engine keeps that order. It first gives a version to any
platform key whose container pass 2 made (the graph service's ticket key is
made in pass 2, once the cluster's workload pool exists), key before switch.

**The closing pass** (decision 2). Every infra pass before it, infra-3 and
infra-4 included, carries the installer's `/32` in
`gke_master_authorized_cidrs`: the control plane admits no public Google range
(`gcp_public_cidrs_access_enabled = false`), and the cluster stages (10, 11)
and stage 14's `verify.reconciler-jobs` run kubectl from the laptop.
`infra.pass-close` is the last pass: in stage 14, its manifest entry depends on
`other.verify-end-to-end` and `other.firestore-migrations`, so the engine
orders it after every verify step and before `handoff.ready` (stage 15). It
applies infra-4's inputs without the address and is met while those are the
inputs it applied, so a run that stopped before it has the leftover entry
removed by the next run.

**Pass 1 adopts the reserved address** (finding 6). Stage 2's
`edge.global-ip` reserves `swarm-ui-ip` by API so the A record can be set
early; the frontend module declares the same address. Before pass 1 plans,
the engine imports it into `module.frontend[0].google_compute_global_address.this`
when the project holds it and the state does not (as bootstrap imports its
keys), so the apply never fails on "already exists". No `import {}` block is
added to terraform/infra: the live deployment's plan does not change.

**Reopening after the close** (finding 16). Once `infra-close` has applied,
a re-run's cluster stages, `swarm team add`, `swarm upgrade` and an add-on
still run kubectl from the laptop. `ClusterReach.reopen`, which `kube_for`'s
`Kube` calls before its first kubectl call, refreshes `installer_cidr` from
this machine's address and applies infra-4's inputs (the close's inputs with
the address) as the deployer, and records `cluster_reach_reopened`. The
closing pass is unmet while that is recorded, so `swarm install`'s stage 14
closes it again; the verbs close it themselves when they finish
(`ClusterReach.close`), and a verb that died in between leaves the record for
the next run's close.

**Voice it** (terraform/infra/voice.tf; BGD, 2026-11-05). `infra.voice-it`
asks in stage 0 whether to turn Voice it on, default no, and records
`outputs["voice_it"]` as `{"enabled": bool}`. A no leaves `voice_it` "off" on
every pass. A yes walks voice.tf's three steps in the order it needs them
(`tfvars.voice_it`): the registry pass "off"; infra pass 1 "account" (bootstrap
pass 1, stage 3, made swarm-voice); infra pass 2 on "on", with the release's
swarm-voice digest in `image_refs`, once pass 1 has made the cluster whose
workload pool the binding names; stage 10's `cluster.workloads` then applies
`swarm_voice_manifest`. voice.tf's middle step, a ChatGPT plan account in the
Voice it pool, is the console's (Capacity › Accounts): the installer asks for
no account and reads no secret, and swarm-voice serves once one is added.

**Voice it's own speech engine** (docs/design/voice-it-own-speech.md §15,
lane L11). It is nested under Voice it: `infra.voice-speech` asks only after a
yes to Voice it (`other.voice-speech-offered` orders it after the Voice it
question); after a no it asks nothing and is met, and so is
`infra.voice-speech-zone`. Under a yes it asks in stage 0 whether to
add the engine: one stopped L4 machine, with the design's cost line and, when
the project already exists, the zone it would live in; the answer defaults to
no and is asked once. Stage 0 cannot read the accelerators of a project stage 1
has not made yet, so for a yes `infra.voice-speech-zone` runs once Compute
Engine is on: the first of the region's zones offering `nvidia-l4`
(`acceleratorTypes`; one zone, one machine, no fallback zone), an
NVIDIA_L4_GPUS quota of at least 1 (the Quotas page opens, and the step waits
until it is granted), and the newest Deep Learning VM image of an Ubuntu 22.04
CUDA family, pinned by name. A region with no L4 zone turns the choice off and
says why. The engine never starts, stops or makes the machine itself: the
first infra pass with `voice_it` "on" (pass 2) makes it stopped, and swarm-api
starts it for a session.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping
from typing import Any

from .. import record as install_record
from .. import tfvars
from ..api_client import deployer_email
from ..exitcodes import MET, UNMET
from ..terraform import Terraform
from ..waits import wait_until
from . import Observation, Plan, bootstrap, open_page
from .bootstrap import DEPS, BootstrapPass2Step, Deps, PassStep, applied_key
from .edge import ADDRESS
from .secrets import PlatformKeysStep

#: Where infra's frontend module keeps the address stage 2 reserved
#: (terraform/modules/frontend/main.tf `google_compute_global_address.this`).
FRONTEND_ADDRESS = "module.frontend[0].google_compute_global_address.this"

#: Recorded while an infra pass has put the installer's address back after the
#: close; the closing pass is unmet until it removes it again.
REOPENED = "cluster_reach_reopened"
#: The pass whose inputs reopen the control plane: infra-close's, with the address.
REOPEN_PASS = "infra-4"


def address_imports(gcp: Any, project: str, in_state: list[str]) -> list[tuple[str, str]]:
    """(address, id) of stage 2's global address when the project holds it and
    infra's state does not: pass 1 imports it instead of creating it again."""
    if FRONTEND_ADDRESS in in_state or gcp.global_address(project, ADDRESS) is None:
        return []
    return [(FRONTEND_ADDRESS, f"projects/{project}/global/addresses/{ADDRESS}")]


class InfraPass1Step(PassStep):
    pass_name = "infra-1"
    _explain = (
        "Infra pass 1 creates the platform with every key-gated switch off: a Cloud Run revision that names a "
        "secret with no version never starts (lesson 39). It runs as swarm-tf-deployer, never as you, so a "
        "missing actAs shows here and not in the first release (lesson 23)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-1", stage=5, satisfies=())

    def apply(self, ctx: Any, plan: Plan) -> None:
        gcp = self.deps.gcp()
        deployer = deployer_email(ctx.project)

        def adopt_address(tf: Terraform) -> None:
            for address, resource_id in address_imports(gcp, ctx.project, tf.state_list(impersonate=deployer)):
                ctx.events.emit("step_apply", step=self.id, message=f"import {address} (stage 2 reserved it)")
                tf.run("import", "-input=false", address, resource_id, impersonate=deployer)

        bootstrap.apply_pass(ctx, self.deps, self.pass_name, before_plan=adopt_address)
        # Firestore exists now: from here every save mirrors to the install
        # record and the hand-off reads the console (N1). A resumed run skips
        # this apply; `swarm install` attaches it then (verbs/install.py).
        ctx.attach_record(install_record.open_record(ctx.project, ctx.environment))


class InfraPass2Step(PassStep):
    pass_name = "infra-2"
    _explain = (
        "Infra pass 2 turns on each switch whose key now has a version (key before switch), the console's "
        "App setup grants, Direct VPC egress for scheduler-made Jobs (lesson 35) and the alert channel with "
        "require_alert_channels (#1817)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-2", stage=7, satisfies=())


class IdentityPassStep(PassStep):
    """Infra pass 2 again, in stage 8, after the identity steps."""

    pass_name = "infra-2"
    _explain = (
        "Stage 8 registers the admin group only once swarm-api has read it, after infra pass 2 applied. This "
        "applies pass 2 again with the group, so the console admits its members, and gives a version to any "
        "platform key pass 2 made first (key before switch)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-identity", stage=8, satisfies=())
        self.keys = PlatformKeysStep(deps)

    def check(self, ctx: Any) -> Observation:
        keys = self.keys.check(ctx)
        if keys.status != MET:
            return Observation(UNMET, keys.detail)
        return super().check(ctx)

    def plan(self, ctx: Any) -> Plan:
        keys = self.keys.plan(ctx)
        base = super().plan(ctx)
        return Plan("Terraform infra-2 with the admin groups stage 8 recorded", (*keys.actions, *base.actions))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.keys.apply(ctx, self.keys.plan(ctx))
        super().apply(ctx, plan)


class ClosingPassStep(PassStep):
    """Decision 2: the installer's address leaves the control plane's authorised networks."""

    pass_name = "infra-close"
    _explain = (
        f"The install reached the cluster from your address, listed as {tfvars.CLUSTER_REACH_NAME!r} in the "
        "control plane's authorised networks. With the cluster stages and the verify reads done, this pass "
        "removes it and changes nothing else."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-close", stage=14, satisfies=("other.engine-cluster-reach-closed",))

    def _pending(self, ctx: Any) -> str | None:
        if ctx.outputs.get(REOPENED):
            return f"the control plane admits {ctx.outputs[REOPENED]} again (reopened for kubectl after the close)"
        return super()._pending(ctx)

    def apply(self, ctx: Any, plan: Plan) -> None:
        from .preflight import remember

        bootstrap.apply_pass(ctx, self.deps, self.pass_name)
        if ctx.outputs.get(REOPENED):
            remember(ctx, outputs={REOPENED: None})


def reach_closed(outputs: Any) -> bool:
    """The closing pass has removed the installer's address and nothing has put it back."""
    return bool(outputs.get(applied_key("infra-close"))) and not outputs.get(REOPENED)


def latest_applied(outputs: Any, root: str) -> str | None:
    """The last pass of `root` the install has applied, in the install's order
    (`registry` aside: it targets the registry alone), or None."""
    applied = [p for p in tfvars.PASSES if tfvars.ROOT_OF[p] == root and p != "registry"
               and outputs.get(applied_key(p))]
    return applied[-1] if applied else None


def latest_pass(outputs: Any, *, reopening: bool = False) -> str:
    """The infra pass a re-apply after the install uses: the latest one the
    install reached, so no switch a later pass turned on goes off again (N12;
    lesson 39). `infra-close`, or `REOPEN_PASS` while the control plane admits
    this machine again (`reopening`: the caller is about to put the address
    back); infra-4 or infra-3 before the close; never earlier than infra-2, the
    pass that carries the alert channel. `create_alerts` follows
    `metric_descriptors_ready` in every pass's inputs."""
    found = latest_applied(outputs, "infra")
    if found == "infra-close" and (reopening or outputs.get(REOPENED)):
        return REOPEN_PASS
    if found is None or not tfvars.after(found, "infra-2"):
        return "infra-2"
    return found


def _refresh_address(ctx: Any) -> None:
    """`installer_cidr` from this machine's address now (preflight.installer-address)."""
    from .preflight import ASK, GCP, InstallerAddressStep

    step = InstallerAddressStep(GCP, ASK)
    if step.check(ctx).status != MET:
        step.apply(ctx, step.plan(ctx))


class ClusterReach:
    """Decision 2 after the close: put this machine's /32 back for kubectl, then take it out again."""

    def __init__(self, deps: Deps = DEPS, *, address: Callable[[Any], None] = _refresh_address) -> None:
        self.deps = deps
        self.address = address

    def reopen(self, ctx: Any) -> bool:
        """Apply infra-4's inputs with the current address when the close removed it; True when it did."""
        from .preflight import remember

        if not reach_closed(ctx.outputs):
            return False
        if ctx.dry_run:
            raise RuntimeError("the closing pass removed this machine's address from the control plane's "
                               "authorised networks; a dry run never applies the infra pass that reopens it")
        self.address(ctx)
        ctx.events.emit("step_apply", step="infra.reopen",
                        message=f"the control plane admits {ctx.outputs.get('installer_cidr')} for kubectl "
                                "until the closing pass removes it again")
        bootstrap.apply_pass(ctx, self.deps, REOPEN_PASS)
        remember(ctx, outputs={REOPENED: str(ctx.outputs.get("installer_cidr") or "")})
        return True

    def close(self, ctx: Any) -> bool:
        """The closing pass again when a reopen left the address; True when it applied."""
        step = ClosingPassStep(self.deps)
        if not ctx.outputs.get(REOPENED):
            return False
        step.apply(ctx, step.plan(ctx))
        return True

    def close_after(self, ctx: Any, verb: str) -> str | None:
        """`close` at the end of a verb; a failure is the line the verb prints, and
        the record stays for `swarm install`'s closing pass."""
        try:
            self.close(ctx)
        except Exception as exc:  # noqa: BLE001 - the verb's own outcome stands; this line says what is left
            return (f"{verb}: the closing pass did not apply ({type(exc).__name__}: {str(exc)[:300]}); the control "
                    f"plane still admits {ctx.outputs.get(REOPENED)}: swarm install removes it")
        return None


REACH = ClusterReach()


class VerbContext:
    """What `ClusterReach` needs of an engine context, for a verb that runs
    outside the engine loop (`swarm team add`, an add-on): the state, saved
    through `save` (a store's re-reading `update`, which returns the state it
wrote), and the bucket
    a pass locks and writes its inputs to."""

    dry_run = False

    def __init__(self, *, project: str, environment: str, holder: str, state: Any, events: Any, bucket: Any,
                 save: Callable[[Callable[[Any], None]], Any]) -> None:
        self.project = project
        self.environment = environment
        self.holder = holder
        self.state = state
        self.events = events
        self.bucket = bucket
        self._save = save

    @property
    def outputs(self) -> dict[str, Any]:
        return self.state.outputs

    @property
    def engine(self) -> VerbContext:
        return self

    @property
    def store(self) -> VerbContext:
        return self

    def save(self, change: Callable[[Any], None]) -> None:
        self.state = self._save(change)


# -- Voice it (terraform/infra/voice.tf) ------------------------------------------

#: The install state's key: `{"enabled": bool}` from stage 0 (tfvars.voice_it
#: renders infra's `voice_it` from it, pass by pass).
VOICE_IT = tfvars.VOICE_IT
VOICE_IT_QUESTION = ("Voice it: talk to the deployment by voice, served by swarm-voice on the cluster. "
                     "Turn it on? Optional, y or n")
#: Off unless the person says yes: Terraform's own default is "off".
VOICE_IT_DEFAULT = "n"
#: The one line after a yes: what the installer does not ask for, and where it goes.
VOICE_IT_POOL = "Voice it's ChatGPT plan accounts are added later in the console, on Capacity › Accounts."


def _voice_it(ctx: Any) -> bool | None:
    """Stage 0's Voice it answer, or None while it is not answered."""
    found = ctx.outputs.get(VOICE_IT)
    if isinstance(found, Mapping) and isinstance(found.get("enabled"), bool):
        return bool(found["enabled"])
    return None


def _ask_yes_no(ask: Callable[..., str], ctx: Any, step_id: str, question: str, default: str) -> bool:
    """y or n, asked again on anything else, up to SPEECH_TRIES answers."""
    asked = question
    for _ in range(SPEECH_TRIES):
        answer = ask(ctx, step_id, asked, default=default).strip().lower()
        if answer in YES or answer in NO:
            return answer in YES
        asked = f"{answer!r} is neither y nor n. {question}"
    raise ValueError(f"no y or n after {SPEECH_TRIES} answers to: {question}; nothing was recorded")


# -- Voice it's own speech engine (design voice-it-own-speech.md §15) -----------

#: The install state's key: `{"enabled": bool}` from stage 0, and for a yes the
#: `zone` and `image` stage 1 adds (tfvars.voice_speech renders them).
VOICE_SPEECH = "voice_speech"
#: The machine's one GPU (a g2-standard-4 carries one) and its regional quota.
L4 = "nvidia-l4"
L4_QUOTA = "NVIDIA_L4_GPUS"
L4_QUOTA_METRIC = "compute.googleapis.com/nvidia_l4_gpus"
#: Deep Learning VM images: Ubuntu 22.04 with a CUDA NVIDIA driver and the
#: container toolkit (design §5.1), e.g. `common-cu128-ubuntu-2204-nvidia-570`.
IMAGE_PROJECT = "deeplearning-platform-release"
IMAGE_FAMILY = re.compile(r"^common-cu[0-9]+-ubuntu-2204(-[a-z0-9-]+)?$")
#: The design's cost line (§15): about $0.71 an hour while running (§1 pick 1)
#: and the 150 GB pd-balanced boot disk, kept between sessions.
SPEECH_COST = "about $0.71 an hour while used, $15 a month for its disk"
SPEECH_QUESTION = ("Voice it's own speech engine (one stopped L4 machine in {where}; " + SPEECH_COST
                   + "). Add it? Optional, y or n")
#: No by default, and never asked while Voice it is off (§15; BGD, 2026-11-05).
SPEECH_DEFAULT = "n"
YES = ("y", "yes")
NO = ("", "n", "no")
#: An answer that is neither yes nor no is asked again, up to this many answers in all.
SPEECH_TRIES = 3


def _compute() -> str:
    from .preflight import COMPUTE

    return COMPUTE


def l4_zones(gcp: Any, project: str, region: str) -> list[str]:
    """The zones of `region` offering the L4, by name (`acceleratorTypes.aggregatedList`).

    Only a zone infra's `voice_speech.zone` accepts counts: one letter after the
    region, so an AI zone such as `<region>-ai1a` is never picked."""
    zones: set[str] = set()
    params: dict[str, Any] = {"filter": f'name = "{L4}"'}
    while True:
        page = gcp._call("GET", f"{_compute()}/projects/{project}/aggregated/acceleratorTypes", params=params) or {}
        for scoped in (page.get("items") or {}).values():
            for found in (scoped or {}).get("acceleratorTypes", []) or []:
                zone = str(found.get("zone", "")).rsplit("/", 1)[-1]
                if found.get("name") == L4 and zone.startswith(f"{region}-") and tfvars.SPEECH_ZONE.match(zone):
                    zones.add(zone)
        if not page.get("nextPageToken"):
            return sorted(zones)
        params["pageToken"] = page["nextPageToken"]


def l4_quota(gcp: Any, project: str, region: str) -> int | None:
    """The region's NVIDIA_L4_GPUS limit (`regions.get`), or None when it is not reported."""
    from .preflight import _compute_limit

    return _compute_limit(gcp._call("GET", f"{_compute()}/projects/{project}/regions/{region}") or {}, L4_QUOTA)


def speech_image(gcp: Any, project: str) -> str:
    """The newest READY, undeprecated Deep Learning VM image of an Ubuntu 22.04
    CUDA family, by name: a /family/ path moves under the stopped machine and
    would replace it at the next plan (variables.tf, `voice_speech.image`)."""
    images: list[dict[str, Any]] = []
    params: dict[str, Any] = {"filter": 'family eq "common-cu.*-ubuntu-2204.*"'}
    while True:
        page = gcp._call("GET", f"{_compute()}/projects/{IMAGE_PROJECT}/global/images", params=params,
                         quota_project=project) or {}
        images.extend(page.get("items", []) or [])
        if not page.get("nextPageToken"):
            break
        params["pageToken"] = page["nextPageToken"]
    usable = [image for image in images
              if IMAGE_FAMILY.match(str(image.get("family", ""))) and image.get("status") == "READY"
              and (image.get("deprecated") or {}).get("state", "ACTIVE") == "ACTIVE"
              and tfvars.SPEECH_IMAGE.match(f"projects/{IMAGE_PROJECT}/global/images/{image.get('name', '')}")]
    if not usable:
        raise LookupError(f"{IMAGE_PROJECT} lists no READY Ubuntu 22.04 CUDA Deep Learning VM image (family "
                          f"{IMAGE_FAMILY.pattern}); the speech machine has no image to pin")
    newest = max(usable, key=lambda image: dt.datetime.fromisoformat(str(image["creationTimestamp"])))
    return f"projects/{IMAGE_PROJECT}/global/images/{newest['name']}"


def _chosen(ctx: Any) -> Mapping[str, Any] | None:
    found = ctx.outputs.get(VOICE_SPEECH)
    return found if isinstance(found, Mapping) and isinstance(found.get("enabled"), bool) else None


class _SpeechStep:
    kind = "engine"

    def __init__(self, deps: Deps = DEPS) -> None:
        self.deps = deps

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    _explain = ""


class VoiceItChoiceStep(_SpeechStep):
    """Stage 0's optional choice: turn Voice it on (default no)."""

    id = "infra.voice-it"
    stage = 0
    read_only = True
    satisfies = ("other.voice-it-choice",)
    _explain = (
        "Voice it lets a person talk to the deployment; swarm-voice serves it on the cluster, and each session "
        "leases a ChatGPT plan account from the Voice it pool. It is optional and off unless you say yes. "
        + VOICE_IT_POOL
    )

    def __init__(self, deps: Deps = DEPS, *, ask: Callable[..., str] | None = None) -> None:
        super().__init__(deps)
        self._ask = ask

    @property
    def ask(self) -> Callable[..., str]:
        if self._ask is not None:
            return self._ask
        from .preflight import ASK

        return ASK

    def check(self, ctx: Any) -> Observation:
        voice = _voice_it(ctx)
        if voice is None:
            return Observation(UNMET, "whether to turn Voice it on is not answered yet")
        return Observation(MET, "Voice it: " + ("yes" if voice else "no"))

    def plan(self, ctx: Any) -> Plan:
        return Plan("ask whether to turn Voice it on (optional; default no)")

    def apply(self, ctx: Any, plan: Plan) -> None:
        from .preflight import remember

        enabled = _ask_yes_no(self.ask, ctx, self.id, VOICE_IT_QUESTION, VOICE_IT_DEFAULT)
        remember(ctx, outputs={VOICE_IT: {"enabled": enabled}})
        if enabled:
            ctx.events.emit("step_apply", step=self.id, message=VOICE_IT_POOL)


class VoiceSpeechChoiceStep(_SpeechStep):
    """Stage 0's optional choice: add Voice it's own speech engine (default no)."""

    id = "infra.voice-speech"
    stage = 0
    read_only = True
    satisfies = ("other.voice-speech-choice", "other.voice-speech-offered")
    _explain = (
        "Asked only when Voice it is on. Voice it can hear and speak through its own engine on one L4 machine the deployment runs itself, kept "
        "stopped between sessions. It is optional and costs " + SPEECH_COST + "; without it Voice it uses "
        "GPT-Live or text."
    )

    def __init__(self, deps: Deps = DEPS, *, ask: Callable[..., str] | None = None) -> None:
        super().__init__(deps)
        self._ask = ask

    @property
    def ask(self) -> Callable[..., str]:
        if self._ask is not None:
            return self._ask
        from .preflight import ASK

        return ASK

    def check(self, ctx: Any) -> Observation:
        voice = _voice_it(ctx)
        if voice is None:
            return Observation(UNMET, "Voice it is not chosen yet (infra.voice-it asks first)")
        if not voice:
            return Observation(MET, "Voice it's own speech engine is not offered: Voice it is off")
        chosen = _chosen(ctx)
        if chosen is None:
            return Observation(UNMET, "whether to add Voice it's own speech engine is not answered yet")
        return Observation(MET, "Voice it's own speech engine: " + ("yes" if chosen["enabled"] else "no"))

    def plan(self, ctx: Any) -> Plan:
        return Plan("ask whether to add Voice it's own speech engine (optional; default no)")

    def _zone_now(self, ctx: Any, region: str) -> str | None:
        """The zone the machine would live in, when the project already exists
        with Compute Engine on; else None (stage 1 reads it)."""
        from .preflight import ApiError

        gcp = self.deps.gcp()
        try:
            if gcp.project(ctx.project) is None or "compute.googleapis.com" not in gcp.enabled_services(ctx.project):
                return None
            zones = l4_zones(gcp, ctx.project, region)
        except ApiError:
            return None
        return zones[0] if zones else None

    def apply(self, ctx: Any, plan: Plan) -> None:
        from .preflight import remember

        voice = _voice_it(ctx)
        if voice is None:
            raise LookupError("Voice it is not chosen yet; infra.voice-it asks it before its own speech engine")
        if not voice:
            return
        region = str(ctx.state.region)
        zone = self._zone_now(ctx, region)
        where = zone or f"the first zone of {region} that offers the L4, read once stage 1 has enabled Compute Engine"
        enabled = _ask_yes_no(self.ask, ctx, self.id, SPEECH_QUESTION.format(where=where), SPEECH_DEFAULT)
        remember(ctx, outputs={VOICE_SPEECH: {"enabled": enabled}})


class VoiceSpeechZoneStep(_SpeechStep):
    """For a yes: the L4 zone, one L4 of quota and the pinned image, once the project exists."""

    id = "infra.voice-speech-zone"
    stage = 1
    read_only = False
    satisfies = ("other.voice-speech-zone",)
    _explain = (
        "The speech machine lives in one zone, the first of the region's zones offering the NVIDIA L4, with "
        "a Deep Learning VM image pinned by name so a stopped machine is never replaced under a session. A "
        "project's accelerators and quotas can only be read once stage 1 has made it and enabled Compute Engine."
    )

    def check(self, ctx: Any) -> Observation:
        if _voice_it(ctx) is False:
            return Observation(MET, "Voice it is off, so its own speech engine needs no zone or image")
        chosen = _chosen(ctx)
        if chosen is None:
            return Observation(UNMET, "Voice it's own speech engine is not chosen yet (infra.voice-speech asks)")
        if not chosen["enabled"]:
            return Observation(MET, "Voice it's own speech engine is off: no zone or image is needed")
        try:
            speech = tfvars.voice_speech(ctx.state)
        except (LookupError, ValueError) as exc:
            return Observation(UNMET, str(exc))
        if speech is None or not speech["enabled"]:
            return Observation(UNMET, "Voice it's own speech engine has no zone and image recorded")
        return Observation(MET, f"the speech machine's zone is {speech['zone']}, its image {speech['image']}")

    def plan(self, ctx: Any) -> Plan:
        region = str(ctx.state.region)
        return Plan("pick the speech machine's zone and pin its image", (
            f"the first zone of {region} offering {L4} (acceleratorTypes)",
            f"{L4_QUOTA} in {region} at least 1, requested on the Quotas page if not",
            f"the newest Ubuntu 22.04 CUDA image of {IMAGE_PROJECT}, by name",
        ))

    def apply(self, ctx: Any, plan: Plan) -> None:
        from .preflight import QUOTA_WAIT, ApiError, quotas_page, remember

        gcp = self.deps.gcp()
        region = str(ctx.state.region)
        zones = l4_zones(gcp, ctx.project, region)
        if not zones:
            why = f"no zone of {region} offers the {L4}, so Voice it's own speech engine is off (one zone, one machine)"
            ctx.events.emit("step_apply", step=self.id, message=why)
            remember(ctx, outputs={VOICE_SPEECH: {"enabled": False, "unavailable": why}})
            return
        quota = l4_quota(gcp, ctx.project, region)
        if quota is not None and quota < 1:
            open_page(quotas_page(ctx.project, metric=L4_QUOTA_METRIC, region=region), why=(
                f"Voice it's own speech engine needs one NVIDIA L4 in {region}, and this project's {L4_QUOTA} quota "
                f"there is {quota}: request an increase to at least 1 on this page. This installer goes on by "
                "itself once it is granted."))

            def granted() -> str:
                try:
                    found = l4_quota(gcp, ctx.project, region)
                except ApiError:  # an unreadable poll has not seen it yet; the next one reads again
                    return UNMET
                return MET if found is None or found >= 1 else UNMET

            wait_until(f"{self.id}:l4-quota", QUOTA_WAIT, granted, clock=ctx.clock, events=ctx.events,
                       on_poll=getattr(ctx.engine, "_heartbeat", None))
        image = speech_image(gcp, ctx.project)
        ctx.events.emit("step_apply", step=self.id, message=f"the speech machine: {zones[0]}, image {image}")
        remember(ctx, outputs={VOICE_SPEECH: {"enabled": True, "zone": zones[0], "image": image}})


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [InfraPass1Step(deps), InfraPass2Step(deps), BootstrapPass2Step(deps), IdentityPassStep(deps),
            ClosingPassStep(deps), VoiceItChoiceStep(deps), VoiceSpeechChoiceStep(deps), VoiceSpeechZoneStep(deps)]


STEPS = build_steps()
