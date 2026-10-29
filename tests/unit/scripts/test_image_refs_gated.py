"""A laptop plan refuses to plan a gated service away for want of a digest (#1878).

scripts/plan.sh pins the digests terraform last applied
(`scripts/lib/image-refs.sh --applied`, the state's `image_refs` output). That
output recorded the platform and runner images only, so on 2026-10-21 a laptop
plan of dev had no swarm-github-hooks digest, `local.github_hooks_serving` read
false, and the plan destroyed the GitHub push receiver and its public invoker.

The images a resource or rendered manifest is switched on by -- the GATED
images -- are listed once, in terraform/infra/images.json, which terraform and
image-refs.sh both read. These tests hold:

  * a refs set missing a gated image whose resources or output are in state is
    refused, exit 4, naming the image and what it deploys, and nothing is
    written for terraform to plan with;
  * a complete set, or a missing image nothing in state needs, passes;
  * plan.sh stops on that refusal and never runs `terraform plan`;
  * every `lookup(var.image_refs, "<name>", ...)` in terraform/infra names an
    image in that list, and every address and output the list names exists.

terraform is a fake on PATH; nothing is planned and no credentials are used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "terraform" / "infra"
IMAGES_JSON = INFRA / "images.json"
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("curl") is None,
    reason="image-refs.sh and plan.sh need jq and curl",
)

# Answers from files: FAKE_IMAGE_REFS (the image_refs output), FAKE_OUTPUTS
# (every output, `output -json`), FAKE_STATE_LIST (`state list`). Every call is
# logged, one per line.
FAKE_TERRAFORM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_TF_LOG}"
case "$*" in
  *"output -json image_refs"*) cat "${FAKE_IMAGE_REFS}" ;;
  *"output -json"*)            cat "${FAKE_OUTPUTS}" ;;
  *"state list"*)              cat "${FAKE_STATE_LIST}" ;;
  *) : ;;
esac
exit 0
"""

PLATFORM_AND_RUNNERS = [
    "swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-ui",
    "swarm-verify", "agent-runtime-base", "agent-runtime-browser", "agent-runtime-indexer",
]

# The receiver has no allUsers binding since DAYONE-TF-2: its invoker check is
# off on the service itself (Q3), so the service is its one gated address.
RECEIVER = [
    "google_cloud_run_v2_service.github_hooks[0]",
]


def ref(name: str) -> str:
    return f"{REGISTRY}/{name}@sha256:{hashlib.sha256(name.encode()).hexdigest()}"


def refs(names: list[str]) -> dict[str, str]:
    return {n: ref(n) for n in names}


class Sandbox:
    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        self.repo.mkdir()
        shutil.copytree(REPO / "scripts", self.repo / "scripts")
        infra = self.repo / "terraform" / "infra"
        (infra / ".terraform").mkdir(parents=True)
        shutil.copy(IMAGES_JSON, infra / "images.json")
        tfvars = self.repo / "terraform" / "environments" / "dev"
        tfvars.mkdir(parents=True)
        (tfvars / "dev.tfvars").write_text("")

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "terraform-fake"
        fake.write_text(FAKE_TERRAFORM)
        fake.chmod(0o755)
        self.log = tmp / "terraform-calls"
        self.log.write_text("")
        self.image_refs = tmp / "image-refs.json"
        self.outputs = tmp / "outputs.json"
        self.state_list = tmp / "state-list"
        self.out = tmp / "out" / "refs.tfvars.json"

        env_file = tmp / "env"
        env_file.write_text(
            "PROJECT_ID=swarm-test-project\nREGION=us-central1\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n"
        )
        env_file.chmod(0o600)
        self.env = dict(os.environ)
        self.env.pop("SWARM_ASSUME_YES", None)
        self.env.update(
            PATH=f"{bin_dir}{os.pathsep}{self.env['PATH']}",
            SWARM_ENV_FILE=str(env_file),
            SWARM_TERRAFORM=str(fake),
            FAKE_TF_LOG=str(self.log),
            FAKE_IMAGE_REFS=str(self.image_refs),
            FAKE_OUTPUTS=str(self.outputs),
            FAKE_STATE_LIST=str(self.state_list),
            NO_COLOR="1",
        )

    def state(self, applied: dict[str, str], addresses: list[str], outputs: dict | None = None) -> None:
        self.image_refs.write_text(json.dumps(applied))
        self.state_list.write_text("".join(f"{a}\n" for a in addresses))
        everything = {"image_refs": {"value": applied}}
        for name, value in (outputs or {}).items():
            everything[name] = {"value": value}
        self.outputs.write_text(json.dumps(everything))

    def image_refs_applied(self) -> subprocess.CompletedProcess:
        cmd = [str(self.repo / "scripts" / "lib" / "image-refs.sh"), "--applied", "--out", str(self.out)]
        return subprocess.run(cmd, cwd=self.repo, env=self.env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)

    def plan(self) -> subprocess.CompletedProcess:
        cmd = [str(self.repo / "scripts" / "plan.sh")]
        return subprocess.run(cmd, cwd=self.repo, env=self.env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_refs_missing_the_receivers_image_are_refused_naming_it(box: Sandbox) -> None:
    box.state(refs(PLATFORM_AND_RUNNERS), ["module.artifact_registry.google_artifact_registry_repository.this", *RECEIVER])
    result = box.image_refs_applied()
    # MUTATION: drop the refuse_unpinned_gated_images call in image-refs.sh and
    # this exits 0 with the receiver unpinned: the #1878 laptop plan.
    assert result.returncode == 4, result.stderr
    # MUTATION: print the refusal without the image's name and this fails.
    assert "has no swarm-github-hooks digest" in result.stderr
    # MUTATION: stop listing what the image deploys and this fails.
    for address in RECEIVER:
        assert address in result.stderr
    assert "next release" in result.stderr
    # MUTATION: move the check after the write and a stale plan input is left.
    assert not box.out.exists()


def test_a_complete_refs_set_passes(box: Sandbox) -> None:
    applied = refs([*PLATFORM_AND_RUNNERS, "swarm-github-hooks"])
    box.state(applied, RECEIVER)
    result = box.image_refs_applied()
    # MUTATION: make the check refuse whenever state holds the receiver (not
    # only when the digest is missing) and this fails.
    assert result.returncode == 0, result.stderr
    assert json.loads(box.out.read_text())["image_refs"] == applied


def test_a_missing_image_nothing_in_state_needs_passes(box: Sandbox) -> None:
    # Receiver off (prod): no digest is fine, nothing would be planned away. A
    # resource whose name only STARTS with the receiver's is not the receiver.
    box.state(refs(PLATFORM_AND_RUNNERS), ["google_cloud_run_v2_service.github_hooks_other[0]"])
    result = box.image_refs_applied()
    # MUTATION: match resource addresses by prefix alone and this is refused.
    assert result.returncode == 0, result.stderr


def test_a_rendered_manifest_with_no_digest_is_refused(box: Sandbox) -> None:
    box.state(refs(PLATFORM_AND_RUNNERS), [], outputs={"swarm_egress_manifest": "apiVersion: v1\n"})
    result = box.image_refs_applied()
    # MUTATION: check only resources, not outputs, and this exits 0 while the
    # egress manifest would plan to null.
    assert result.returncode == 4, result.stderr
    assert "has no swarm-egress digest" in result.stderr
    assert "output swarm_egress_manifest" in result.stderr


def test_a_null_rendered_manifest_needs_no_digest(box: Sandbox) -> None:
    box.state(refs(PLATFORM_AND_RUNNERS), [], outputs={"swarm_egress_manifest": None})
    result = box.image_refs_applied()
    # MUTATION: treat a null output as present and this is refused.
    assert result.returncode == 0, result.stderr


def test_plan_sh_stops_on_the_refusal_and_never_plans(box: Sandbox) -> None:
    box.state(refs(PLATFORM_AND_RUNNERS), RECEIVER)
    result = box.plan()
    # MUTATION: let plan.sh fall through on exit 4 and it plans the receiver away.
    assert result.returncode != 0
    assert "refusing to plan: a gated image has no digest" in result.stderr
    assert "has no swarm-github-hooks digest" in result.stderr
    calls = box.log.read_text().splitlines()
    assert not any(re.search(r"(^|\s)plan(\s|$)", c) for c in calls), calls


def test_every_image_refs_lookup_in_infra_is_in_the_one_list() -> None:
    gated = json.loads(IMAGES_JSON.read_text())["gated"]
    looked_up: set[str] = set()
    for tf in sorted(INFRA.glob("*.tf")):
        code = "\n".join(line for line in tf.read_text().splitlines() if not line.lstrip().startswith("#"))
        looked_up |= set(re.findall(r'lookup\(\s*var\.image_refs\s*,\s*"([^"]+)"', code))
    # MUTATION: remove an image (swarm-github-hooks) from images.json's gated
    # map and this fails, naming it.
    assert looked_up, "found no lookup(var.image_refs, ...) in terraform/infra; the scan is broken"
    assert sorted(looked_up - set(gated)) == [], "a gated image is missing from terraform/infra/images.json"
    assert sorted(set(gated) - looked_up) == [], "images.json names a gated image nothing looks up"


def test_every_address_and_output_the_list_names_exists() -> None:
    gated = json.loads(IMAGES_JSON.read_text())["gated"]
    source = "\n".join(tf.read_text() for tf in sorted(INFRA.glob("*.tf")))
    seen = 0
    for image, needs in gated.items():
        assert needs["resources"] or needs["outputs"], f"{image} names nothing it switches on"
        for address in needs["resources"]:
            kind, name = address.split(".")
            # MUTATION: misspell an address in images.json and this fails; the
            # shell check would otherwise never find it in state.
            assert re.search(rf'resource\s+"{kind}"\s+"{name}"', source), f"{image}: no {address}"
            seen += 1
        for output in needs["outputs"]:
            assert re.search(rf'output\s+"{output}"', source), f"{image}: no output {output}"
            seen += 1
    assert seen >= len(gated)
