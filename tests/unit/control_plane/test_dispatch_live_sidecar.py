"""A live-browser pod carries the display sidecar (docs/design/live-browser.md
§3.1, §8.2, §8.3; lane L5, part of #1030).

`claude-code-browser` is the one profile with `live_browser=True` (contract
request 71). Its GKE pod gets a Kubernetes NATIVE sidecar -- an init container
with `restartPolicy: Always` -- named `display`, which runs Xvfb, the
platform's Chromium and the live gate under a uid of its own. These hold:

  * the sidecar is native, runs the same image under a different, non-root
    uid, with the gate's entrypoint, every capability dropped and a read-only
    root;
  * requests == limits on EVERY container (invariant 7), and the containers
    sum to the task's resource class, so admission's unit arithmetic is
    untouched -- for `browser`, and for a smaller class a step may shrink to;
  * /dev/shm moves to the sidecar, where Chromium runs;
  * the sidecar's own volumes (its /tmp, where the X socket lives, and the
    browser profile) are never mounted into the worker container, and the
    only shared volumes are downloads, uploads and the gate's control socket;
  * the pod is labelled `swarm-live: enabled`, which the live egress policy
    (§8.3, lane L6) selects on;
  * the worker gets no DISPLAY, and the sidecar no task identifiers;
  * every other profile's manifest is unchanged, byte for byte, and so is
    `claude-code-browser`'s while the rollout switch is off;
  * the image carries the second uid, Xvfb and the §5.6 Chromium policy.

MUTATION: drop `"restartPolicy": "Always"` from the display container in
dispatch.py and `test_the_sidecar_is_native_and_runs_as_its_own_uid` fails;
give the worker the class's full CPU back and the sum test fails; leave the
dshm mount on the worker and `test_dev_shm_is_on_the_sidecar_only` fails;
add the label to every pod and the unchanged-profiles test fails.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend

from scheduler import dispatch
from scheduler.dispatch import WORKER_UID, GkeJobDispatcher, GkeTarget
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
PROFILE = "claude-code-browser"
DOCKERFILE = REPO / "images/agent-runtime-browser/Dockerfile"
POLICY = REPO / "images/agent-runtime-browser/chromium-policy.json"
DOC = REPO / "docs/execution-backends.md"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _job(tenant: Tenant, profile_name: str = PROFILE, *, live: bool = True,
         resource_class: str | None = None) -> dict[str, Any]:
    settings = scheduler_settings(live_browser_sidecar=live)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = make_task(profile_name)
    if resource_class is not None:
        task = dataclasses.replace(task, resource_class=resource_class)
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile_name], tenant=tenant)


def _spec(job: dict[str, Any]) -> dict[str, Any]:
    return job["spec"]["template"]["spec"]


def _worker(spec: dict[str, Any]) -> dict[str, Any]:
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    return worker


def _display(spec: dict[str, Any]) -> dict[str, Any]:
    (display,) = [c for c in spec.get("initContainers", []) if c["name"] == "display"]
    return display


def _mounts(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in container.get("volumeMounts", [])}


def _volumes(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in spec["volumes"]}


_UNITS = {"Gi": 1, "Mi": 1 / 1024}


def _gib(quantity: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Gi|Mi)", quantity)
    assert m, f"unexpected quantity {quantity!r}"
    return float(m.group(1)) * _UNITS[m.group(2)]


def _cpu(quantity: str) -> float:
    q = str(quantity)
    return int(q[:-1]) / 1000 if q.endswith("m") else float(q)


def _gke_profiles() -> list[str]:
    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if resolve_backend(p) is Backend.GKE_AUTOPILOT)


# --- the catalogue this lane reads -------------------------------------------


def test_claude_code_browser_is_the_live_profile():
    live = sorted(n for n, p in RUNNER_PROFILES.items() if getattr(p, "live_browser", False))
    assert live == [PROFILE]


# --- the sidecar ---------------------------------------------------------------


def test_the_sidecar_is_native_and_runs_as_its_own_uid(tenant):
    spec = _spec(_job(tenant))
    display = _display(spec)
    worker = _worker(spec)

    assert display["restartPolicy"] == "Always", "a native sidecar, or the Job never completes"
    assert display["image"] == worker["image"], "the same agent-runtime-browser image"
    assert display.get("command"), "the sidecar runs the gate, not the worker lifecycle"
    assert "command" not in worker and "args" not in worker, "the worker keeps its ENTRYPOINT"

    sc = display["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] != WORKER_UID, "the uid boundary is the point of the sidecar"
    assert sc["runAsUser"] >= 1000
    assert sc["runAsGroup"] == sc["runAsUser"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["privileged"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    # The worker's own context is the one every pod has always had.
    assert worker["securityContext"]["runAsUser"] == WORKER_UID


def test_the_sidecar_waits_to_be_ready_before_the_worker_starts(tenant):
    """§5.4.2: the gate must be listening before the worker registers on ctl.sock."""
    display = _display(_spec(_job(tenant)))
    probe = display["startupProbe"]
    command = probe["exec"]["command"]
    assert command[-1].endswith("/ctl.sock")
    assert "-S" in command


def test_every_container_has_requests_equal_to_limits(tenant):
    spec = _spec(_job(tenant))
    for c in spec["containers"] + spec["initContainers"]:
        r = c["resources"]
        assert r["requests"] == r["limits"], f"{c['name']}: invariant 7, no bursting"
        assert set(r["limits"]) == {"cpu", "memory", "ephemeral-storage"}, c["name"]


@pytest.mark.parametrize("resource_class", [None, "standard"])
def test_the_containers_sum_to_the_resource_class(tenant, resource_class):
    rc = RESOURCE_CLASSES[resource_class or RUNNER_PROFILES[PROFILE].resource_class]
    spec = _spec(_job(tenant, resource_class=resource_class))
    pod_without_sidecar = _worker(_spec(_job(tenant, live=False, resource_class=resource_class)))
    containers = spec["containers"] + spec["initContainers"]
    assert len(containers) == 2

    cpu = sum(_cpu(c["resources"]["limits"]["cpu"]) for c in containers)
    mem = sum(_gib(c["resources"]["limits"]["memory"]) for c in containers)
    eph = sum(_gib(c["resources"]["limits"]["ephemeral-storage"]) for c in containers)
    assert cpu == rc.cpu
    assert mem == rc.memory_gib
    assert eph == _gib(pod_without_sidecar["resources"]["limits"]["ephemeral-storage"])
    for c in containers:
        assert _cpu(c["resources"]["limits"]["cpu"]) > 0
        assert _gib(c["resources"]["limits"]["memory"]) > 0


def test_the_browser_split_follows_the_measurement(tenant):
    """Owner decision 2026-10-20 (BROWSER-SPLIT, part of #1689): worker
    2 vCPU / 3 GiB, display 2 vCPU / 5 GiB, of the class's 4 / 8.

    It was display 3 / worker 1 since contract request 79, which ran the
    agent's `npm ci` and dev server on one core. The display's measured peak
    (docs/browser-startup-speed.md §11) is 1.90 cores, so 2 still covers it,
    about 1.05x. The memory split did not move."""
    spec = _spec(_job(tenant))
    worker, display = _worker(spec), _display(spec)
    assert (_cpu(worker["resources"]["limits"]["cpu"]),
            _gib(worker["resources"]["limits"]["memory"])) == (2, 3)
    assert (_cpu(display["resources"]["limits"]["cpu"]),
            _gib(display["resources"]["limits"]["memory"])) == (2, 5)


def test_a_live_browser_pod_renders_two_cores_each_with_requests_equal_to_limits(tenant):
    """The rendered quantities themselves, as the API server receives them:
    cpu "2" on the worker and "2" on the display, both directions equal."""
    spec = _spec(_job(tenant))
    for container in (_worker(spec), _display(spec)):
        r = container["resources"]
        assert r["limits"]["cpu"] == "2", container["name"]
        assert r["requests"]["cpu"] == "2", container["name"]
        assert r["requests"] == r["limits"], f"{container['name']}: invariant 7"
    assert _worker(spec)["resources"]["limits"]["memory"] == "3Gi"
    assert _display(spec)["resources"]["limits"]["memory"] == "5Gi"


def test_the_non_live_browser_pod_is_byte_for_byte_what_the_old_split_rendered(
        tenant, monkeypatch):
    """The re-split reaches only the live pod: with the switch off,
    `claude-code-browser` renders the same bytes under the old 3 / 1 split as
    under the new one, and its single worker keeps the whole class."""
    new = json.dumps(_job(tenant, live=False), sort_keys=True)
    monkeypatch.setitem(dispatch.LIVE_DISPLAY_SHARE, "browser",
                        dispatch.LiveDisplayShare(cpu=3, memory_gib=5))
    old = json.dumps(_job(tenant, live=False), sort_keys=True)
    assert new == old
    worker = _worker(_spec(json.loads(new)))
    assert worker["resources"]["requests"] == worker["resources"]["limits"]
    assert worker["resources"]["limits"]["cpu"] == "4"
    assert worker["resources"]["limits"]["memory"] == "8Gi"


def test_dev_shm_is_on_the_sidecar_only(tenant):
    spec = _spec(_job(tenant))
    assert _mounts(_display(spec))["dshm"]["mountPath"] == "/dev/shm"
    assert "dshm" not in _mounts(_worker(spec)), "Chromium runs in the sidecar, not the worker"
    dshm = _volumes(spec)["dshm"]["emptyDir"]
    assert dshm["medium"] == "Memory"
    assert _gib(dshm["sizeLimit"]) == 2


def test_the_sidecars_private_volumes_never_reach_the_worker(tenant):
    spec = _spec(_job(tenant))
    display, worker = _mounts(_display(spec)), _mounts(_worker(spec))
    volumes = _volumes(spec)

    shared = {"live-downloads", "live-uploads", "live-gate"}
    private = set(display) - shared
    # The sidecar mounts its own /tmp (the X socket, the gate's restart
    # marker), the browser profile and /dev/shm, and nothing of the worker's.
    assert private == {"display-tmp", "browser-profile", "dshm"}
    assert display["display-tmp"]["mountPath"] == "/tmp"
    assert not private & set(worker), "the worker container must not reach the display"
    for name in ("workspace", "tmp", "home", "spec-verify-keys"):
        assert name not in display, f"the sidecar must not mount the worker's {name}"

    assert shared <= set(worker)
    for name in shared:
        assert display[name]["mountPath"] == worker[name]["mountPath"], name
    # uploads: worker writes, sidecar reads.
    assert display["live-uploads"].get("readOnly") is True
    assert not worker["live-uploads"].get("readOnly")

    # Every live volume is memory-backed: inside the pod's memory, and never
    # checkpointed (none is under the workspace).
    for name in shared | {"display-tmp", "browser-profile"}:
        assert volumes[name]["emptyDir"]["medium"] == "Memory", name
        assert "sizeLimit" in volumes[name]["emptyDir"], name
        assert not display[name]["mountPath"].startswith("/workspace"), name


#: The display sidecar's measured peak, whole container, in GiB
#: (docs/browser-startup-speed.md §11, 2026-10-15: 1.48 GiB over four pods
#: and eight samples). It already includes what Chromium had in /dev/shm.
DISPLAY_MEASURED_PEAK_GIB = 1.48


def test_the_memory_volumes_fit_inside_the_container_that_fills_them(tenant):
    """Every tmpfs the sidecar keeps full PLUS its measured peak fits its limit.

    Until contract request 79 this held the tmpfs under HALF a 10 GiB sidecar;
    no split of an 8 GiB pod can do that with 4.25 GiB of sizeLimits. So the
    volumes that stay resident (/dev/shm, its /tmp, the browser profile) are
    added to the measured peak, which is conservative because that peak already
    counts the shm Chromium used. Downloads are left out: the worker MOVES each
    one into work/ (with_live_display), so the volume does not stay full.
    A download that fills it while Chromium is at a heavier peak than any
    sampled is an OOM; the worker's checkpoint bounds what that costs
    (invariant 8)."""
    spec = _spec(_job(tenant))
    volumes = _volumes(spec)
    display = _display(spec)
    resident = ("dshm", "display-tmp", "browser-profile")
    tmpfs = sum(_gib(volumes[n]["emptyDir"]["sizeLimit"]) for n in resident)
    assert tmpfs + DISPLAY_MEASURED_PEAK_GIB <= _gib(display["resources"]["limits"]["memory"]), (
        "the sidecar's resident tmpfs and Chromium's measured peak must both fit"
    )


def test_the_pod_carries_the_live_label(tenant):
    job = _job(tenant)
    assert job["spec"]["template"]["metadata"]["labels"]["swarm-live"] == "enabled"


def test_the_worker_gets_no_display_and_the_paths_it_needs(tenant):
    spec = _spec(_job(tenant))
    worker_env = {e["name"]: e["value"] for e in _worker(spec)["env"]}
    assert "DISPLAY" not in worker_env, "a headed launch in the worker must fail loudly"
    mounts = _mounts(_worker(spec))
    assert worker_env["SWARM_GATE_CTL"] == mounts["live-gate"]["mountPath"] + "/ctl.sock"
    assert worker_env["SWARM_BROWSER_DOWNLOADS"] == mounts["live-downloads"]["mountPath"]
    assert worker_env["SWARM_BROWSER_UPLOADS"] == mounts["live-uploads"]["mountPath"]
    assert worker_env["SWARM_CDP_ENDPOINT"] == "http://127.0.0.1:9222"


def test_the_sidecar_carries_no_task_identity(tenant):
    """The ticket arrives over ctl.sock; the gate needs to know nothing else."""
    display = _display(_spec(_job(tenant)))
    env = {e["name"]: e.get("value") for e in display.get("env", [])}
    assert all("valueFrom" not in e for e in display.get("env", []))
    allowed = {"HOME", "DISPLAY", "SWARM_GATE_CTL", "SWARM_BROWSER_PROFILE_DIR",
               "SWARM_BROWSER_DOWNLOADS", "SWARM_BROWSER_UPLOADS", "SWARM_CDP_LISTEN"}
    assert set(env) <= allowed, sorted(set(env) - allowed)
    assert env["SWARM_CDP_LISTEN"] == "127.0.0.1:9222", "the CDP proxy is localhost-only"
    assert env["HOME"] == "/tmp"


def test_the_disk_volumes_still_fit_the_pods_ephemeral_storage(tenant):
    spec = _spec(_job(tenant))
    disk = sum(_gib(v["emptyDir"]["sizeLimit"]) for v in spec["volumes"]
               if "emptyDir" in v and v["emptyDir"].get("medium") != "Memory")
    total = sum(_gib(c["resources"]["limits"]["ephemeral-storage"])
                for c in spec["containers"] + spec["initContainers"])
    assert disk <= total <= 10


# --- nothing else changes --------------------------------------------------------


@pytest.mark.parametrize("profile_name", [p for p in _gke_profiles() if p != PROFILE])
def test_every_other_profiles_manifest_is_unchanged(tenant, profile_name):
    assert _job(tenant, profile_name, live=True) == _job(tenant, profile_name, live=False)
    spec = _spec(_job(tenant, profile_name, live=True))
    assert "initContainers" not in spec
    assert "swarm-live" not in _job(tenant, profile_name)["spec"]["template"]["metadata"]["labels"]


def test_with_the_switch_off_the_live_profile_is_the_pod_it_was(tenant):
    """The rollout switch: off, `claude-code-browser` keeps one container with
    the class's whole budget and /dev/shm, exactly as before this lane."""
    job = _job(tenant, live=False)
    spec = _spec(job)
    assert "initContainers" not in spec
    assert "swarm-live" not in job["spec"]["template"]["metadata"]["labels"]
    worker = _worker(spec)
    assert _cpu(worker["resources"]["limits"]["cpu"]) == RESOURCE_CLASSES["browser"].cpu
    assert "dshm" in _mounts(worker)
    names = {e["name"] for e in worker["env"]}
    assert not names & {"SWARM_GATE_CTL", "SWARM_CDP_ENDPOINT"}


def test_the_manifest_shares_no_dicts_between_renders(tenant):
    first = _job(tenant)
    snapshot = copy.deepcopy(_job(tenant))
    _display(_spec(first))["securityContext"]["runAsUser"] = 0
    _worker(_spec(first))["securityContext"]["runAsUser"] = 0
    assert _job(tenant) == snapshot


def test_the_switch_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.delenv("LIVE_BROWSER_SIDECAR", raising=False)
    assert SchedulerSettings.from_env().live_browser_sidecar is False
    monkeypatch.setenv("LIVE_BROWSER_SIDECAR", "true")
    assert SchedulerSettings.from_env().live_browser_sidecar is True


# --- the image -------------------------------------------------------------------


def test_the_chromium_policy_is_section_5_6():
    policy = json.loads(POLICY.read_text())
    blocked = set(policy["URLBlocklist"])
    assert {"file://*", "chrome://*", "devtools://*", "view-source:*"} <= blocked
    assert {"svc.cluster.local", "swarm-system"} <= blocked
    assert policy["URLAllowlist"] == ["chrome://newtab"]
    assert policy["DeveloperToolsAvailability"] == 2
    assert policy["PasswordManagerEnabled"] is False
    assert policy["AutofillAddressEnabled"] is False
    assert policy["AutofillCreditCardEnabled"] is False
    assert policy["FullscreenAllowed"] is False
    assert policy["SSLErrorOverrideAllowed"] is False
    assert policy["BrowserSignin"] == 0
    assert policy["SyncDisabled"] is True


def test_the_image_carries_the_display_uid_xvfb_and_the_policy(tenant):
    text = DOCKERFILE.read_text()
    uid = _display(_spec(_job(tenant)))["securityContext"]["runAsUser"]
    assert re.search(rf"useradd[^\n]*--uid {uid}\b[^\n]*swarm-display", text), (
        "the image must create the uid the pod runs the sidecar as"
    )
    assert re.search(r"\bxvfb\b", text)
    for target in ("/etc/opt/chrome/policies/managed/", "/etc/chromium/policies/managed/"):
        assert target in text, f"the policy must be installed where Chromium reads it: {target}"
    assert "chromium-policy.json" in text


def test_the_backend_doc_describes_the_sidecar():
    text = DOC.read_text()
    for needle in ("native sidecar", "swarm-live", "LIVE_BROWSER_SIDECAR", "/dev/shm"):
        assert needle in text, needle
