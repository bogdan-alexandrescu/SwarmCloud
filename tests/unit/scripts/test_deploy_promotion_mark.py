"""deploy.sh accepts only `promoted-<channel>-` as a promotion mark (#1886).

THE LEGACY FORM IS RETIRED. The mark used to be `<channel>-promoted-<tag>`;
the rename let the registry's cleanup keep `:dev` and `:prod` forever without
keeping every mark forever (cleanup policies match tags by PREFIX, and
`dev-promoted-*` starts with `dev`). scripts/registry-remark.sh gave every
digest carrying the old form the new one too, so deploy.sh's "did a release
promote this digest?" check (`promoted_earlier`) stopped reading the old form.
A digest that carries only `dev-promoted-*` now reads as never promoted: once
the cleanup keeps only `promoted-*`, a check that still trusted the old mark
would vouch for an image the registry no longer protects.

`legacy_promotion_mark_prefix` stays in lib/common.sh: registry-remark.sh
still finds old marks by it. Nothing in deploy.sh reads it, which is what the
last test holds, error message included.

The function is lifted out of the SHIPPED deploy.sh and run against a fake
`gcloud` on PATH, so the test cannot pass against a copy of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .gate_script_functions import function_source

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
DEPLOY = SCRIPTS / "lib" / "deploy.sh"

PROJECT = "swarm-test-project"
REGION = "us-central1"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images/workspace-apply"
PINNED = "sha256:" + "c" * 64

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the scripts need bash and jq",
)

FAKE_TAGS_LIST = r'''#!{python}
import os, sys
args = sys.argv[1:]
assert args[:4] == ["artifacts", "docker", "tags", "list"], args
with open(os.environ["FAKE_CALLS"], "a") as fh:
    fh.write(" ".join(args) + "\n")
for row in open(os.environ["FAKE_TAGS"]).read().splitlines():
    tag, image, version = row.split("|")
    print(f"projects/p/locations/l/repositories/r/packages/x/tags/{tag}\t{image}\tprojects/p/versions/{version}")
'''


def _promoted_earlier(tmp_path: Path, tags: list[str], channel: str = "dev") -> int:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(FAKE_TAGS_LIST.replace("{python}", sys.executable))
    (bindir / "gcloud").chmod(0o755)
    rows = tmp_path / "tags.txt"
    rows.write_text("".join(f"{tag}|workspace-apply|{PINNED}\n" for tag in tags))
    calls = tmp_path / "calls.txt"
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": PROJECT,
        "REGION": REGION,
        "ENVIRONMENT": channel,
        "NO_COLOR": "1",
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_TAGS": str(rows),
        "FAKE_CALLS": str(calls),
    }
    for name in ("IMAGE_REPO", "IMAGE_HOST", "ARTIFACT_REGISTRY", "SWARM_ASSUME_YES"):
        env.pop(name, None)
    body = (
        f'source "{SCRIPTS}/lib/common.sh"\n'
        f'WORK="{work}"; CHANNEL="{channel}"\n'
        + function_source(DEPLOY, "promoted_earlier")
        + f'rc=0; promoted_earlier "{IMAGE}" "{PINNED}" || rc=$?; printf "rc=%s" "$rc"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", body, "deploy-promotion-mark-test"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.stdout.startswith("rc="), proc.stderr[-2000:]
    # Control: the registry was asked. A check that never looked would answer too.
    assert calls.exists() and "tags list" in calls.read_text()
    return int(proc.stdout.removeprefix("rc="))


@pytest.mark.parametrize("channel", ["dev", "prod"])
def test_the_new_mark_reads_as_promoted(tmp_path, channel):
    # Control for the next test: the same fixture with the new form answers yes,
    # so a "no" there is the prefix's doing, not the fake registry's.
    assert _promoted_earlier(tmp_path, [f"promoted-{channel}-0123abcd"], channel) == 0


@pytest.mark.parametrize("channel", ["dev", "prod"])
def test_a_digest_carrying_only_the_legacy_mark_is_not_promoted(tmp_path, channel):
    # MUTATION: restoring `|| index(t[k], o) == 1` in deploy.sh's awk returns 0 here.
    assert _promoted_earlier(tmp_path, [f"{channel}-promoted-0123abcd"], channel) == 1


def test_deploy_sh_never_reads_the_legacy_prefix():
    # The awk and the "no release promoted" error both named the old form;
    # the error must not tell the operator that `:dev-promoted-*` would do.
    text = DEPLOY.read_text()
    # MUTATION: leaving `legacy_promotion_mark_prefix` in the awk or in the
    # job error message fails here.
    assert "legacy_promotion_mark_prefix" not in text
    assert "-promoted-" not in text.replace("promotion_mark_prefix", "")
