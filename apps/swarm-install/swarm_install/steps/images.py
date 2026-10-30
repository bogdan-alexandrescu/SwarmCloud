"""Stage 4, images: the registry, then every release image copied into it by
digest after its signature is verified (design §4, §6.1; D21; lessons 9, 13).

| Step | Manifest entries | Lesson |
|---|---|---|
| `images.registry` | `registry.swarm` | 9: a targeted infra apply of the registry alone; whether it exists is read from Terraform state, never from the registry |
| `images.copy` | `image.copy-to-company-registry`, `other.release-signing-public-key` | 13: every image by digest, never a tag; D21: `cosign verify` with the key pinned in this wheel, before the copy |

The release's `images.json` (`{image: "sha256:<digest>"}`) is read through the
release's signed `SHA256SUMS`, so a tampered bucket cannot name other digests.
The images come from the release home's registry
(`us-docker.pkg.dev/swarmcloud/swarmcloud`) and land in the project's own
(`<region>-docker.pkg.dev/<project>/swarm`): after the install nothing in the
deployment reads the release home (D21).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import tfvars, tools
from ..adopt import InstallGcp
from ..api_client import deployer_email
from ..exitcodes import MET, UNMET
from ..state import state_bucket
from ..terraform import Terraform, TerraformRefused
from . import Observation, Plan
from .bootstrap import DEPS, Deps, PassStep, Release, release_key, workdir
from .preflight import remember

ARTIFACT_REGISTRY = "https://artifactregistry.googleapis.com/v1"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REGISTRY_TARGET = "module.artifact_registry"


def docker_image(gcp: InstallGcp, project: str, region: str, image: str, digest: str) -> dict[str, Any] | None:
    name = quote(f"{image}@{digest}", safe="@:")
    return gcp._call(
        "GET", f"{ARTIFACT_REGISTRY}/projects/{project}/locations/{region}/repositories/"
        f"{tfvars.REPOSITORY}/dockerImages/{name}", missing_ok=True,
    )


def parse_images(data: bytes) -> dict[str, str]:
    """`images.json`, refused unless every image is pinned by a sha256 digest (lesson 13)."""
    found = json.loads(data)
    if not isinstance(found, Mapping) or not found:
        raise TerraformRefused("the release's images.json names no image")
    bad = sorted(name for name, digest in found.items() if not DIGEST.match(str(digest)))
    if bad:
        raise TerraformRefused(f"the release's images.json pins {', '.join(bad)} by something other than a sha256 "
                               "digest; every image is copied and deployed by digest only (lesson 13)")
    return {str(k): str(v) for k, v in found.items()}


class RegistryStep(PassStep):
    pass_name = "registry"
    targets = (REGISTRY_TARGET,)
    _explain = (
        "Images need somewhere to land before infra's full apply, which refuses to run without their digests. "
        "A targeted apply makes the registry alone; whether it exists is read from Terraform state, never by "
        "listing the registry (lesson 9)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="images.registry", stage=4, satisfies=("registry.swarm",))

    def check(self, ctx: Any) -> Observation:
        tf = Terraform(root="infra", root_dir=self.deps.module_root(ctx, "infra"),
                       state_bucket=state_bucket(ctx.project), environment=ctx.environment, exec_=self.deps.exec_)
        deployer = deployer_email(ctx.project)
        tf.init(impersonate=deployer)
        listed = tf.state_list(impersonate=deployer)
        if any(address.startswith(REGISTRY_TARGET) for address in listed):
            return Observation(MET, "the registry is in infra's state")
        return Observation(UNMET, "infra's state holds no registry yet")


class CopyStep:
    id = "images.copy"
    stage = 4
    kind = "engine"
    satisfies = ("image.copy-to-company-registry", "other.release-signing-public-key")

    def __init__(self, deps: Deps, *, cosign: Callable[[], Path] = lambda: tools.ensure("cosign"),
                 gcrane: Callable[[], Path] = lambda: tools.ensure("gcrane"),
                 key: Callable[[], Path] = release_key) -> None:
        self.deps = deps
        self._cosign = cosign
        self._gcrane = gcrane
        self._key = key

    def explain(self) -> str:
        return ("Every image the release names is verified with the release key this installer carries, then "
                "copied by digest into the project's own registry (D21, lesson 13). After the install nothing "
                "reads the release home.")

    def _release(self) -> Release:
        if self.deps.release is None:
            self.deps.release = Release(read_public=self.deps.gcp().read_public, exec_=self.deps.exec_,
                                        cosign=self._cosign, key=self._key)
        return self.deps.release

    def images(self, ctx: Any) -> dict[str, str]:
        if not ctx.state.release:
            raise LookupError("no release is recorded (preflight.release)")
        version = str(ctx.state.release)
        return parse_images(self._release().fetch(version, "images.json", workdir(ctx.project, version)))

    def _missing(self, ctx: Any, images: Mapping[str, str]) -> list[str]:
        gcp = self.deps.gcp()
        return [name for name, digest in sorted(images.items())
                if docker_image(gcp, ctx.project, str(ctx.state.region), name, digest) is None]

    def check(self, ctx: Any) -> Observation:
        images = self.images(ctx)
        missing = self._missing(ctx, images)
        if missing:
            return Observation(UNMET, f"not in the project's registry yet: {', '.join(missing)}")
        return Observation(MET, f"{len(images)} images in the project's registry, by digest",
                           {"image_digests": images})

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        images = self.images(ctx)
        actions = tuple(f"cosign verify {tfvars.RELEASE_REGISTRY}/{name}@{digest}, then copy it"
                        for name, digest in sorted(images.items()) if name in self._missing(ctx, images))
        return Plan(f"verify and copy {len(actions)} images by digest", actions)

    def apply(self, ctx: Any, plan: Plan) -> None:
        images = self.images(ctx)
        key = self._key()
        cosign, gcrane = self._cosign(), self._gcrane()
        region = str(ctx.state.region)
        for name in self._missing(ctx, images):
            digest = images[name]
            source = f"{tfvars.RELEASE_REGISTRY}/{name}@{digest}"
            target = f"{region}-docker.pkg.dev/{ctx.project}/{tfvars.REPOSITORY}/{name}@{digest}"
            verified = self.deps.exec_([str(cosign), "verify", "--key", str(key), "--insecure-ignore-tlog=true",
                                        source], {}, key.parent)
            if verified.returncode != 0:
                raise TerraformRefused(f"{source} does not verify with the pinned release key; not copied: "
                                       f"{verified.stderr.strip()[-500:]}")
            copied = self.deps.exec_([str(gcrane), "cp", source, target], {}, key.parent)
            if copied.returncode != 0:
                raise RuntimeError(f"gcrane cp {source} failed: {copied.stderr.strip()[-500:]}")
            ctx.events.emit("step_apply", step=self.id, message=f"{name}@{digest}")
        remember(ctx, outputs={"image_digests": images})


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [RegistryStep(deps), CopyStep(deps)]


STEPS = build_steps()
