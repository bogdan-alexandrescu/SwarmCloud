"""Stage 4, images: verified with the pinned release key, then copied by
digest (DAYONE-ENGINE-3; design §6.1; D21; lesson 13).

On fakes: `exec_` records every cosign and gcrane command and answers them.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from swarm_install import tfvars
from swarm_install.steps import bootstrap, images
from swarm_install.steps.bootstrap import Deps, ReleaseKeyMissing
from swarm_install.terraform import Completed, TerraformRefused

from .test_terraform_runner import FakeCtx


def digest() -> str:
    return "sha256:" + secrets.token_hex(32)


class FakeRelease:
    def __init__(self, data: dict) -> None:
        self.data = json.dumps(data).encode()

    def fetch(self, version: str, name: str, where: Path) -> bytes:
        assert name == "images.json"
        return self.data


class FakeGcp:
    """The project's registry: holds what `gcrane cp` copied."""

    def __init__(self) -> None:
        self.held: set[str] = set()

    def _call(self, method: str, url: str, *, missing_ok: bool = False, **kwargs):
        name = url.rsplit("/dockerImages/", 1)[-1]
        return {"name": name} if name in self.held else None


class FakeExec:
    def __init__(self, gcp: FakeGcp, *, verifies: bool = True) -> None:
        self.gcp = gcp
        self.verifies = verifies
        self.calls: list[list[str]] = []

    def __call__(self, argv, env, cwd) -> Completed:
        argv = list(argv)
        self.calls.append(argv)
        if argv[1] == "verify":
            return Completed(0 if self.verifies else 1, "", "" if self.verifies else "no matching signatures")
        if argv[1] == "cp":
            self.gcp.held.add(argv[3].rsplit("/", 1)[-1])
        return Completed(0, "")


def rig(tmp_path: Path, found: dict, *, verifies: bool = True, key=None):
    gcp = FakeGcp()
    exec_ = FakeExec(gcp, verifies=verifies)
    deps = Deps(gcp=lambda: gcp, exec_=exec_, release=FakeRelease(found))
    if key is None:
        key_path = tmp_path / "release_key.pub"
        key_path.write_text("-----BEGIN " + "PUBLIC KEY-----\n" + secrets.token_hex(16) + "\n")
        key = lambda: key_path  # noqa: E731
    step = images.CopyStep(deps, cosign=lambda: tmp_path / "cosign", gcrane=lambda: tmp_path / "gcrane", key=key)
    return step, exec_, FakeCtx()


def test_cosign_verified_before_copy(tmp_path: Path) -> None:
    found = {"swarm-api": digest(), "swarm-ui": digest()}
    step, exec_, ctx = rig(tmp_path, found)
    step.apply(ctx, step.plan(ctx))
    tools_run = [argv[1] for argv in exec_.calls]
    assert tools_run == ["verify", "cp", "verify", "cp"], "each image is verified, then copied"
    for verify, copy in zip(exec_.calls[::2], exec_.calls[1::2]):
        assert verify[-1] == copy[2], "the copy's source is exactly what was verified"
    assert step.check(ctx).status == "met"
    assert ctx.outputs["image_digests"] == found

    step, exec_, ctx = rig(tmp_path, {"swarm-api": digest()}, verifies=False)
    with pytest.raises(TerraformRefused, match="does not verify"):
        step.apply(ctx, step.plan(ctx))
    assert [argv[1] for argv in exec_.calls] == ["verify"], "an image that does not verify is never copied"


def test_copy_by_digest_only(tmp_path: Path) -> None:
    with pytest.raises(TerraformRefused, match="digest"):
        images.parse_images(json.dumps({"swarm-api": "latest"}).encode())
    with pytest.raises(TerraformRefused, match="digest"):
        images.parse_images(json.dumps({"swarm-api": "sha256:abc"}).encode())
    with pytest.raises(TerraformRefused, match="names no image"):
        images.parse_images(b"{}")

    found = {"swarm-api": digest()}
    step, exec_, ctx = rig(tmp_path, found)
    step.apply(ctx, step.plan(ctx))
    copy = next(argv for argv in exec_.calls if argv[1] == "cp")
    source, target = copy[2], copy[3]
    assert source == f"{tfvars.RELEASE_REGISTRY}/swarm-api@{found['swarm-api']}"
    assert target == f"europe-west1-docker.pkg.dev/{ctx.project}/{tfvars.REPOSITORY}/swarm-api@{found['swarm-api']}"
    assert ":latest" not in " ".join(copy)


def test_verified_with_pinned_release_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The key is the one this wheel carries, beside the package (RELHOME-1's output).
    assert bootstrap.RELEASE_KEY.name == "release_key.pub"
    assert bootstrap.RELEASE_KEY.parent.name == "swarm_install"
    assert images.CopyStep.__init__.__kwdefaults__["key"] is bootstrap.release_key

    pinned = tmp_path / "pinned.pub"
    pinned.write_text("-----BEGIN " + "PUBLIC KEY-----\n" + secrets.token_hex(16) + "\n")
    monkeypatch.setattr(bootstrap, "RELEASE_KEY", pinned)
    step, exec_, ctx = rig(tmp_path, {"swarm-api": digest()}, key=bootstrap.release_key)
    step.apply(ctx, step.plan(ctx))
    verify = next(argv for argv in exec_.calls if argv[1] == "verify")
    assert verify[verify.index("--key") + 1] == str(pinned)

    # Absent (BGD has not run scripts/release-home.sh yet): refused, nothing verified or copied.
    monkeypatch.setattr(bootstrap, "RELEASE_KEY", tmp_path / "absent.pub")
    step, exec_, ctx = rig(tmp_path, {"swarm-api": digest()}, key=bootstrap.release_key)
    with pytest.raises(ReleaseKeyMissing, match="release-home.sh"):
        step.apply(ctx, step.plan(ctx))
    assert exec_.calls == []
