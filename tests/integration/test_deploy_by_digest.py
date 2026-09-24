"""Deploy by digest: the manifest becomes `image_refs`, and the deploy proves it ran.

TWO SCRIPTS, ONE PROPERTY -- nothing this pipeline deploys is a mutable tag.

`scripts/lib/image-refs.sh` turns a promotion manifest (or what a channel tag
points at now) into the `image_refs` input terraform/infra takes. It must refuse
a manifest that names a tag, and it must tell "the registry has nothing on this
channel" (a fresh project, exit 3) apart from "the registry could not be read"
(exit 1) -- an unreadable registry is not an empty one, and reading it as empty
is how a plan against a fresh-project path gets run on a live one.

`scripts/lib/deploy.sh --verify-only` is the release's post-apply check, and it
is the one that answers the question the 2026-09-20 audit found nothing could:
"is the code I just built the code that is running". A service serving an older
digest reported HEALTHY with `latestReady == latestCreated` and every check
passed (docs/audits/2026-09-20/tag-vs-digest.md). So the checks below each make
ONE fact false against an otherwise healthy deployment:

  * a service whose newest revision serves a digest other than the manifest's;
  * a terraform-managed Cloud Run job still at a tag;
  * a scheduler whose WORKER_IMAGE_REFS names a stale runner digest -- the map
    the dispatcher builds every GKE Job from;
  * a service whose newest revision never became ready.

`gcloud` is faked on PATH and answers from a scenario; nothing reaches a real
project, and a call the fake does not model fails loudly rather than passing.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE_REFS = REPO / "scripts" / "lib" / "image-refs.sh"
DEPLOY = REPO / "scripts" / "lib" / "deploy.sh"

PROJECT = "swarm-test-project"
REGION = "us-central1"
REPO_PATH = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images"

NAMES = {
    "swarm-api": "1",
    "swarm-scheduler": "2",
    "swarm-quota-broker": "3",
    "swarm-reconciler": "4",
    "swarm-ui": "5",
    "swarm-verify": "6",
    "agent-runtime-base": "a",
    "agent-runtime-browser": "b",
}


def digest(char: str) -> str:
    return "sha256:" + char * 64


def ref(name: str, char: str | None = None) -> str:
    return f"{REPO_PATH}/{name}@{digest(char or NAMES[name])}"


def manifest(**overrides: str) -> dict:
    images = []
    for name, char in NAMES.items():
        entry_ref = overrides.get(name, ref(name))
        images.append({
            "name": name,
            "image": f"{REPO_PATH}/{name}",
            "tag": "abc123def456",
            "channel": "dev",
            "digest": entry_ref.rpartition("@")[2],
            "ref": entry_ref,
        })
    return {"tag": "abc123def456", "channel": "dev", "environment": "dev", "images": images}


GCLOUD_SHIM = r'''#!/usr/bin/env python3
"""A gcloud that answers from FAKE_GCLOUD_SCENARIO. Unknown calls FAIL."""
import json, os, sys

scenario = json.loads(open(os.environ["FAKE_GCLOUD_SCENARIO"]).read())
with open(os.environ["FAKE_GCLOUD_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")

args = [a for a in sys.argv[1:]]
words = [a for a in args if not a.startswith("-")]


def flag(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


if words[:4] == ["artifacts", "docker", "tags", "list"]:
    if scenario.get("tags_error"):
        sys.stderr.write(scenario["tags_error"] + "\n")
        sys.exit(1)
    for tag, image, version in scenario.get("tags", []):
        sys.stdout.write("%s\t%s\t%s\n" % (tag, image, version))
    sys.exit(0)

if words[:3] == ["run", "services", "describe"]:
    name = words[3]
    service = scenario.get("services", {}).get(name)
    if service is None:
        sys.stderr.write("ERROR: (gcloud.run.services.describe) Cannot find service [%s]: NOT_FOUND\n" % name)
        sys.exit(1)
    if flag("--format") != "json":
        sys.stderr.write("fake gcloud: services describe is modelled for --format=json only\n")
        sys.exit(2)
    sys.stdout.write(json.dumps(service))
    sys.exit(0)

if words[:3] == ["run", "jobs", "list"]:
    if flag("--format") != "json":
        sys.stderr.write("fake gcloud: jobs list is modelled for --format=json only\n")
        sys.exit(2)
    sys.stdout.write(json.dumps(scenario.get("jobs", [])))
    sys.exit(0)

sys.stderr.write("fake gcloud: no model for: %s\n" % " ".join(sys.argv[1:]))
sys.exit(2)
'''


def service(name: str, image: str, *, ready: bool = True, env: list | None = None) -> dict:
    created = f"{name}-00007-new"
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"containers": [{"image": image, "env": env or []}]}}},
        "status": {
            "url": f"https://{name}-abc.a.run.app",
            "latestCreatedRevisionName": created,
            "latestReadyRevisionName": created if ready else f"{name}-00006-old",
        },
    }


def job(name: str, image: str, managed_by: str = "swarm-terraform") -> dict:
    return {
        "metadata": {"name": name, "labels": {"managed-by": managed_by}},
        "spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}},
    }


def worker_refs_env(base: str | None = None, browser: str | None = None) -> list:
    refs = {
        "agent-runtime-base": base or ref("agent-runtime-base"),
        "agent-runtime-browser": browser or ref("agent-runtime-browser"),
    }
    return [{"name": "DISPATCH_TOPIC", "value": "swarm-scheduler-wake"},
            {"name": "WORKER_IMAGE_REFS", "value": json.dumps(refs)}]


def healthy_deployment() -> dict:
    return {
        "services": {
            "swarm-api": service("swarm-api", ref("swarm-api")),
            "swarm-scheduler": service("swarm-scheduler", ref("swarm-scheduler"), env=worker_refs_env()),
            "swarm-quota-broker": service("swarm-quota-broker", ref("swarm-quota-broker")),
            "swarm-reconciler": service("swarm-reconciler", ref("swarm-reconciler")),
            "swarm-ui": service("swarm-ui", ref("swarm-ui")),
        },
        "jobs": [
            job("swarm-job-eng-mock", ref("agent-runtime-base")),
            job("swarm-job-eng-claude-code", ref("agent-runtime-base")),
            job("swarm-verify", ref("swarm-verify")),
            # The dispatcher's own jobs are not terraform's to pin; the check
            # must not claim them.
            job("swarm-job-u-alice-mock", f"{REPO_PATH}/agent-runtime-base:old", managed_by="swarm-scheduler"),
        ],
    }


def environment(tmp_path: Path, scenario: dict) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(GCLOUD_SHIM)
    gcloud.chmod(0o755)

    scenario_path = tmp_path / "gcloud-scenario.json"
    scenario_path.write_text(json.dumps(scenario))

    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION={REGION}\nENVIRONMENT=dev\n")
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_GCLOUD_SCENARIO"] = str(scenario_path)
    env["FAKE_GCLOUD_LOG"] = str(tmp_path / "gcloud.log")
    env["SWARM_ENV_FILE"] = str(env_file)
    env["TMPDIR"] = str(tmp_path / "tmp")
    env["NO_COLOR"] = "1"
    for key in ("PROJECT_ID", "REGION", "ENVIRONMENT", "IMAGE_REPO", "IMAGE_HOST", "ARTIFACT_REGISTRY"):
        env.pop(key, None)
    return env


def run(script: Path, tmp_path: Path, scenario: dict, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO,
        env=environment(tmp_path, scenario),
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


def write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "deployed-images-dev.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# image-refs.sh
# ---------------------------------------------------------------------------


def test_a_promotion_manifest_becomes_image_refs_verbatim(tmp_path):
    source = write_manifest(tmp_path, manifest())
    out = tmp_path / "refs.tfvars.json"
    code, text = run(IMAGE_REFS, tmp_path, {}, "--manifest", str(source), "--out", str(out))
    assert code == 0, text
    assert json.loads(out.read_text()) == {"image_refs": {n: ref(n) for n in NAMES}}


def test_a_manifest_that_names_a_tag_is_refused(tmp_path):
    source = write_manifest(tmp_path, manifest(**{"swarm-api": f"{REPO_PATH}/swarm-api:abc123def456"}))
    out = tmp_path / "refs.tfvars.json"
    code, text = run(IMAGE_REFS, tmp_path, {}, "--manifest", str(source), "--out", str(out))
    assert code != 0, text
    assert "swarm-api" in text, text
    assert not out.exists(), "a refused manifest must not leave an image_refs file behind"


def test_a_manifest_that_pins_nothing_is_refused(tmp_path):
    data = manifest()
    data["images"] = []
    source = write_manifest(tmp_path, data)
    code, text = run(IMAGE_REFS, tmp_path, {}, "--manifest", str(source), "--out", str(tmp_path / "o.json"))
    assert code != 0, text


def test_a_channel_is_resolved_to_the_digests_it_points_at(tmp_path):
    tags = [["dev", f"{REPO_PATH}/{n}", digest(c)] for n, c in NAMES.items()]
    # Other tags on the same images are not this channel and must be ignored.
    tags += [["prod", f"{REPO_PATH}/swarm-api", digest("9")],
             ["abc123def456", f"{REPO_PATH}/swarm-api", digest("8")]]
    out = tmp_path / "refs.tfvars.json"
    written = tmp_path / "manifest.json"
    code, text = run(IMAGE_REFS, tmp_path, {"tags": tags},
                     "--channel", "dev", "--write-manifest", str(written), "--out", str(out))
    assert code == 0, text
    assert json.loads(out.read_text()) == {"image_refs": {n: ref(n) for n in NAMES}}
    recorded = {i["name"]: i["ref"] for i in json.loads(written.read_text())["images"]}
    assert recorded == {n: ref(n) for n in NAMES}


def test_an_empty_channel_is_a_fresh_project_not_an_error(tmp_path):
    code, text = run(IMAGE_REFS, tmp_path, {"tags": [["prod", f"{REPO_PATH}/swarm-api", digest("9")]]},
                     "--channel", "dev", "--out", str(tmp_path / "o.json"))
    assert code == 3, text


def test_an_unreadable_registry_is_not_an_empty_one(tmp_path):
    code, text = run(IMAGE_REFS, tmp_path,
                     {"tags_error": "ERROR: (gcloud.artifacts.docker.tags.list) PERMISSION_DENIED: "
                                    "Permission 'artifactregistry.tags.list' denied"},
                     "--channel", "dev", "--out", str(tmp_path / "o.json"))
    assert code not in (0, 3), text
    assert "PERMISSION_DENIED" in text, text


# ---------------------------------------------------------------------------
# deploy.sh --verify-only
# ---------------------------------------------------------------------------


def verify(tmp_path: Path, deployment: dict) -> tuple[int, str]:
    source = write_manifest(tmp_path, manifest())
    return run(DEPLOY, tmp_path, deployment,
               "--verify-only", "--no-health", "--wait", "1", "--manifest", str(source))


def test_a_deployment_running_exactly_the_manifest_is_verified(tmp_path):
    code, text = verify(tmp_path, healthy_deployment())
    assert code == 0, text
    log = (tmp_path / "gcloud.log").read_text()
    assert "services update" not in log, "a verification must never change what is deployed"


def test_a_service_serving_an_older_digest_is_caught(tmp_path):
    deployment = healthy_deployment()
    stale = ref("swarm-ui", "0")
    deployment["services"]["swarm-ui"] = service("swarm-ui", stale)
    code, text = verify(tmp_path, deployment)
    assert code != 0, text
    assert "swarm-ui" in text and digest("0") in text, text


def test_a_terraform_job_still_at_a_tag_is_caught(tmp_path):
    deployment = healthy_deployment()
    deployment["jobs"][0] = job("swarm-job-eng-mock", f"{REPO_PATH}/agent-runtime-base:abc123def456")
    code, text = verify(tmp_path, deployment)
    assert code != 0, text
    assert "swarm-job-eng-mock" in text, text


def test_a_stale_worker_image_map_on_the_scheduler_is_caught(tmp_path):
    deployment = healthy_deployment()
    deployment["services"]["swarm-scheduler"] = service(
        "swarm-scheduler", ref("swarm-scheduler"),
        env=worker_refs_env(browser=ref("agent-runtime-browser", "0")),
    )
    code, text = verify(tmp_path, deployment)
    assert code != 0, text
    assert "WORKER_IMAGE_REFS" in text and "agent-runtime-browser" in text, text


def test_a_scheduler_with_no_worker_image_map_is_caught(tmp_path):
    deployment = healthy_deployment()
    deployment["services"]["swarm-scheduler"] = service("swarm-scheduler", ref("swarm-scheduler"))
    code, text = verify(tmp_path, deployment)
    assert code != 0, text
    assert "WORKER_IMAGE_REFS" in text, text


def test_a_revision_that_never_became_ready_is_caught(tmp_path):
    deployment = copy.deepcopy(healthy_deployment())
    deployment["services"]["swarm-api"] = service("swarm-api", ref("swarm-api"), ready=False)
    code, text = verify(tmp_path, deployment)
    assert code != 0, text
    assert "swarm-api" in text and "not ready" in text, text
