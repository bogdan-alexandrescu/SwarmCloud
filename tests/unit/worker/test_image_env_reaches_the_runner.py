"""What the image sets for the runner must reach the runner.

WHY THIS FILE EXISTS. The browser runner is started as the worker LIFECYCLE's
child, with an environment the lifecycle BUILDS rather than inherits
(`lifecycle._build_child_env`, then `workspace.child_env`). That is deliberate:
the worker's own environment carries control-plane identifiers and the service
account it authenticates as, and "an environment variable is a command in
disguise" (see `test_runners.py::test_the_child_environment_is_built_not_inherited`).

The cost of building it is that a variable the IMAGE sets for the runner's
benefit is dropped unless someone names it. `agent-runtime-browser` sets

    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

because that is where its Chromium is installed, read-only, outside every
writable path. Without it Playwright looks under `$HOME/.cache/ms-playwright`,
and HOME in the runner child is the attempt's own work dir -- which holds no
browser. So every browser attempt would fail at launch.

Nobody saw it because nothing ran the browser runner under the lifecycle.
Cloud Run always started the lifecycle, but the browser profile is pinned to
GKE, and the scheduler's GKE Job replaced the lifecycle with the bare runner --
which inherited the container's environment whole and so found its browsers.
PR #31 makes the GKE Job start the lifecycle, and its reviewer predicted the
first GKE browser proof would fail on exactly this variable. NOT OBSERVED: no
browser task has yet run under the lifecycle on GKE; the failure is derived
from reading the code, and these tests hold the derivation.

The last two tests are the ones that stop the next variable going the same
way. The browser image's environment has three layers: the upstream
`python:3.11-slim-bookworm` the base is built FROM, the base's runtime stage,
and the browser Dockerfile. The two Dockerfiles are parsed; the upstream layer
is written in no file here, so its ENV is RECORDED below from the registry and
pinned to the digest it was read from. Every variable from the three is
required to be either carried to the runner verbatim or listed below with the
reason it is not, and a bump of the upstream digest fails until the recording
is re-read.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod

from conftest import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

REPO = Path(__file__).resolve().parents[3]
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
BROWSER_DOCKERFILE = REPO / "images" / "agent-runtime-browser" / "Dockerfile"

TENANT_KEY = "sk-ant-api03-tenant-own-key"

#: The digest `images/agent-runtime-base/Dockerfile` pins its `PYTHON_IMAGE`
#: to, as of the reading below. `test_the_recorded_upstream_env_is_for_the_pinned_base`
#: fails the moment the Dockerfile pins anything else.
UPSTREAM_PYTHON_DIGEST = "sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84"

#: The ENV the UPSTREAM image sets -- `python:3.11-slim-bookworm` at the digest
#: above, which the base's runtime stage is built FROM. The browser image
#: inherits it through the base exactly as it inherits the base's own ENV, but
#: it is written in no file in this repository, so it cannot be parsed like the
#: other two layers. It is RECORDED here instead, and pinned to the digest it
#: was read from, so a bump of the base cannot leave it describing an image
#: nobody builds any more.
#:
#: Read 2026-09-24 from Docker Hub, for linux/amd64 -- the only platform
#: `scripts/build-images.sh` and the browser's cloudbuild.yaml build: manifest
#: sha256:b1add8a6f2aca6bcfcf0b9c9b522352f7ce0d62a3d556a2f2f32511aa0cca250,
#: config sha256:9356cb064a7cbecce9a3ccba46e7fd5459d3a3e939db884ef7fbf3f757cc5ec8.
#: linux/arm64 under the same index carries the identical list. To re-read
#: after a bump (read-only, anonymous, no credentials):
#:
#:     T=$(curl -fsS 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull' | jq -r .token)
#:     R=https://registry-1.docker.io/v2/library/python
#:     M=$(curl -fsS -H "Authorization: Bearer $T" \
#:           -H 'Accept: application/vnd.oci.image.index.v1+json' "$R/manifests/<digest>" \
#:         | jq -r '.manifests[] | select(.platform.os=="linux" and .platform.architecture=="amd64") | .digest')
#:     C=$(curl -fsS -H "Authorization: Bearer $T" \
#:           -H 'Accept: application/vnd.oci.image.manifest.v1+json' "$R/manifests/$M" | jq -r .config.digest)
#:     curl -fsSL -H "Authorization: Bearer $T" "$R/blobs/$C" | jq -r '.config.Env[]'
#:
#: PATH and LANG are overridden by the base's runtime stage; the layering in
#: `_browser_image_env` applies that, the same way Docker does.
UPSTREAM_PYTHON_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "GPG_KEY": "A035C8C19219BA821ECEA86B64E628F8D684696D",
    "PYTHON_VERSION": "3.11.16",
    "PYTHON_SHA256": "91bcdebfdde239a003ae93738a7fce0f9230fee5c4bc2b86f6e6e8c6f98aabe8",
}

# NONE OF THESE IS A BUILD-TIME SETTING. Every ENV here is set in a RUNTIME
# stage, or by the upstream image under it, so it is in the environment of
# every process the container starts, not only of the build's own RUN steps.
# The runner is the exception, because the lifecycle builds its environment.
# So each reason says why the RUNNER does not need the variable, not what the
# build gained by setting it.
#
# "The runner" is the process the lifecycle starts and whatever inherits ITS
# environment -- for `browser`, Playwright's node driver and Chromium. The
# claude-code, codex and generic runners start the agent CLI or the catalogue
# command with an environment they BUILD AGAIN (`runners/cliagent.py`,
# `generic._build_env`), so nothing carried at this hop reaches an agent's own
# processes unless that runner forwards it as well.
#
# OPEN OWNER DECISION, raised on PR #37 and NOT taken here. An agent's own
# `pip install` -- a claude-code or codex tool call -- runs in the environment
# `cliagent.py` builds, which has no PIP_NO_CACHE_DIR. pip therefore caches
# under $HOME/.cache/pip; HOME is the attempt's work dir; and every checkpoint
# archives the work dir whole (`checkpoint._write_archive`), so that cache is
# uploaded with each checkpoint, restored with it, and counts toward the 2 GiB
# checkpoint cap. The image's PIP_NO_CACHE_DIR=1 reaches no agent today, on
# either backend. Carrying it at THIS hop would not change that: if it is
# wanted, it belongs in `cliagent.py`'s environment, the way
# `generic._build_env` already sets npm's two settings for its own commands.
# Those two, NPM_CONFIG_UPDATE_NOTIFIER and NPM_CONFIG_FUND, are the same
# question at smaller stakes: `cliagent.py` does not set them either.

#: Set by the image and deliberately NOT given to the runner. A variable the
#: image gains that is neither carried nor listed here fails
#: `test_every_env_the_browser_image_sets_is_carried_or_refused_for_a_reason`
#: until someone decides which it is.
NOT_FOR_THE_RUNNER: dict[str, str] = {
    "DEBIAN_FRONTEND": (
        "read only by apt and debconf when a package is installed or configured. "
        "The runner is uid 10001 and the image has no sudo, so nothing it can "
        "run installs a package"
    ),
    "PIP_NO_CACHE_DIR": (
        "no runner process runs pip: the browser runner's subprocesses are "
        "Playwright's driver and Chromium, and the other runners give their "
        "commands a rebuilt environment this hop does not reach. Nor is the "
        "build relying on it: its two `pip install` steps pass --no-cache-dir "
        "themselves. Whether AGENTS should have it is the open decision above"
    ),
    "NPM_CONFIG_PREFIX": (
        "where the build's `npm install -g` put the agent CLIs; they are found "
        "through PATH, which IS carried. No runner process runs npm, and an npm "
        "without it falls back to node's own prefix, /usr/local -- root-owned, "
        "exactly as /usr/local/share/npm-global is, so a global install by uid "
        "10001 fails either way"
    ),
    "NPM_CONFIG_UPDATE_NOTIFIER": (
        "no runner process runs npm. The generic runner, whose catalogue does, "
        "sets it for its own commands (`generic._build_env`); the claude-code "
        "and codex runners build their CLI's environment without it, which is "
        "part of the open decision above, not of this hop"
    ),
    "NPM_CONFIG_FUND": (
        "no runner process runs npm. The generic runner sets it for its own "
        "commands (`generic._build_env`) and its `npm ci` passes --no-fund as "
        "well; the claude-code and codex runners build their CLI's environment "
        "without it, as for NPM_CONFIG_UPDATE_NOTIFIER"
    ),
    "WORKSPACE_ROOT": (
        "the LIFECYCLE's setting: the directory it creates every attempt's "
        "workspace under. The runner is told its own workspace as "
        "SWARM_WORKSPACE and has no business with the parent"
    ),
    "PLAYWRIGHT_SKIP_BROWSER_GC": (
        "read only on Playwright's browser-INSTALL path (`registry.install` in "
        "the 1.63.0 driver's coreBundle.js, which `playwright install` runs); "
        "launching a browser never reaches it. The image build installs, the "
        "runner never does, and /opt/playwright is read-only to uid 10001"
    ),
    "GPG_KEY": (
        "the upstream python image's own build input: the key its Dockerfile "
        "verified the CPython source tarball with. It describes how the image's "
        "CPython was built, and nothing at runtime verifies anything with it"
    ),
    "PYTHON_VERSION": (
        "the upstream python image's own build input: the CPython version its "
        "Dockerfile downloaded. The interpreter reports its own version, and "
        "nothing in agent_worker reads this variable"
    ),
    "PYTHON_SHA256": (
        "the upstream python image's own build input: the checksum of the "
        "CPython source tarball its Dockerfile downloaded"
    ),
}

#: Set by the image and given to the runner with a DIFFERENT value, on purpose.
REPLACED_FOR_THE_RUNNER: dict[str, str] = {
    "HOME": (
        "`workspace.child_env` points HOME at the attempt's own work dir, so "
        "whatever the agent writes to its home is checkpointed with the attempt "
        "and destroyed with it rather than shared across attempts"
    ),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _browser_worker(db, worker_factory, tmp_path):
    """A worker for the `browser` profile, one step before it starts its runner.

    The browser profile declares the anthropic credential, so the tenant has to
    have registered it and Secret Manager has to answer -- the same fixture shape
    `test_lifecycle.py::test_worker_env_does_not_leak_into_the_runner` uses for
    claude-code.
    """
    seed_attempt(db, runner_profile="browser")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": TENANT_KEY})
    worker, _, _ = worker_factory(runner_profile="browser", secret_client=secrets)
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    return worker


def _instructions(text: str) -> list[str]:
    """A Dockerfile's instructions, one string each.

    Continuation lines are joined, comment lines dropped (Docker drops them
    inside a continued instruction too) and heredoc bodies skipped, so a line
    of an inline script that happens to start with `ENV` is not read as one.
    """
    out: list[str] = []
    pending = ""
    heredoc_end: str | None = None
    for raw in text.splitlines():
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        line = pending + stripped
        pending = ""
        out.append(line)
        heredoc = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if heredoc:
            heredoc_end = heredoc.group(1)
    assert not pending, "Dockerfile ends inside a continued instruction"
    return out


def _stage_env(dockerfile: Path, stage: str) -> dict[str, str]:
    """Every ENV the named build stage sets, in order, later wins."""
    env: dict[str, str] = {}
    current: str | None = None
    for line in _instructions(dockerfile.read_text()):
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            named = re.search(r"\s[Aa][Ss]\s+(\S+)\s*$", " " + rest)
            current = named.group(1) if named else None
            continue
        if keyword != "ENV" or current != stage:
            continue
        tokens = shlex.split(rest)
        if tokens and "=" not in tokens[0]:
            pairs = [(tokens[0], " ".join(tokens[1:]))]  # legacy `ENV KEY value`
        else:
            pairs = [tuple(token.split("=", 1)) for token in tokens]
        for key, value in pairs:
            if "$" in value:
                pytest.fail(
                    f"{dockerfile.relative_to(REPO)} sets {key}={value!r}, which "
                    "references another variable. This parser does not expand "
                    "them; extend it rather than guess the value."
                )
            env[key] = value
    return env


def _pinned_upstream() -> tuple[str, str]:
    """The upstream image the base's runtime stage is built FROM: (ref, digest).

    Read from the Dockerfile rather than assumed, and the runtime stage is
    required to be `FROM ${PYTHON_IMAGE}` -- otherwise the recorded upstream ENV
    would be attached to an image that is not the base's parent at all.
    """
    text = BASE_DOCKERFILE.read_text()
    arg = re.search(r"^ARG PYTHON_IMAGE=(\S+)@(sha256:[0-9a-f]{64})\s*$", text, re.M)
    assert arg, (
        f"{BASE_DOCKERFILE.relative_to(REPO)} no longer pins PYTHON_IMAGE by "
        "digest; the upstream ENV below cannot be tied to an image"
    )
    runtime_from = re.search(r"^FROM\s+(\S+)\s+[Aa][Ss]\s+runtime\s*$", text, re.M)
    assert runtime_from and runtime_from.group(1) == "${PYTHON_IMAGE}", (
        "the base's runtime stage is no longer built FROM ${PYTHON_IMAGE}; "
        f"it is FROM {runtime_from.group(1) if runtime_from else '<not found>'}"
    )
    return arg.group(1), arg.group(2)


def _browser_image_env() -> dict[str, str]:
    """What a process in `agent-runtime-browser` starts with.

    Three layers, later wins, as Docker applies them: the upstream python
    image's ENV (recorded above, pinned by digest), the base's `runtime`
    stage, then the browser Dockerfile's own. The browser image is built FROM
    the base (its `BASE_IMAGE` default and its cloudbuild both say so). The
    base's `builder` stage is not included: nothing it sets survives into the
    runtime image.

    NOT INCLUDED, because it is not the image: the Job definition's `env:`.
    That is the worker's configuration, and this test does not classify it.
    """
    parent = re.search(
        r"^ARG BASE_IMAGE=agent-runtime-base\b", BROWSER_DOCKERFILE.read_text(), re.M
    )
    assert parent, (
        "the browser image no longer builds FROM agent-runtime-base; this test "
        "reads the wrong parent's ENV"
    )
    env = dict(UPSTREAM_PYTHON_ENV)
    env.update(_stage_env(BASE_DOCKERFILE, "runtime"))
    env.update(_stage_env(BROWSER_DOCKERFILE, "runtime"))
    return env


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------


def test_playwright_browsers_path_set_on_the_worker_reaches_the_runner(
    db, worker_factory, tmp_path, monkeypatch
):
    """The browser image installs Chromium under PLAYWRIGHT_BROWSERS_PATH; the
    runner has to be told where, or Playwright looks in $HOME -- the attempt's
    empty work dir -- and fails at launch."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright")
    worker = _browser_worker(db, worker_factory, tmp_path)

    env = worker._build_child_env()

    assert env.get("PLAYWRIGHT_BROWSERS_PATH") == "/opt/playwright", (
        "the runner child was built without PLAYWRIGHT_BROWSERS_PATH; the "
        "browser runner cannot find the Chromium the image installed"
    )
    # And HOME is still the workspace -- which is exactly why the default
    # browser location cannot be relied on.
    assert env["HOME"] == str(worker.ws.work)


def test_a_secret_on_the_worker_still_does_not_reach_the_runner(
    db, worker_factory, tmp_path, monkeypatch
):
    """Carrying the browser path must not open the door to anything else.

    PLAYWRIGHT_SERVICE_ACCESS_TOKEN is a real Playwright credential (the hosted
    browser service reads it), and is here to prove the passthrough matches
    exact names: a `PLAYWRIGHT_*` prefix would hand it over. ANTHROPIC_API_KEY
    in the worker's environment must not win over the tenant's own key either:
    the runner gets the one Secret Manager returned for THIS tenant.
    """
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright")
    planted = {
        "PLAYWRIGHT_SERVICE_ACCESS_TOKEN": "pw-service-token-must-not-leak",
        "GOOGLE_APPLICATION_CREDENTIALS": "/var/secrets/worker-sa.json",
        "OPENAI_API_KEY": "sk-proj-worker-env-must-not-leak",
        "GIT_TOKEN": "ghp_worker_env_must_not_leak",
        "GITHUB_TOKEN": "ghs_worker_env_must_not_leak",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-must-not-leak",
        "ANTHROPIC_API_KEY": "sk-ant-api03-worker-env-must-not-win",
    }
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    worker = _browser_worker(db, worker_factory, tmp_path)

    env = worker._build_child_env()

    assert env.get("PLAYWRIGHT_BROWSERS_PATH") == "/opt/playwright"
    for name in planted:
        if name == "ANTHROPIC_API_KEY":
            continue
        assert name not in env, f"{name} from the worker's environment reached the runner"
    assert env["ANTHROPIC_API_KEY"] == TENANT_KEY
    # Not under ANY name: a value copied into a differently named variable
    # leaks just as well.
    rendered = json.dumps(env)
    for name, value in planted.items():
        assert value not in rendered, f"the worker's {name} value reached the runner"


# ---------------------------------------------------------------------------
# the class of defect
# ---------------------------------------------------------------------------


def test_every_env_the_browser_image_sets_is_carried_or_refused_for_a_reason(
    db, worker_factory, tmp_path, monkeypatch
):
    """Take the image's environment -- all three layers -- start the worker
    from exactly that, and follow each variable to the runner.

    Carried means the runner sees the image's value unchanged. Anything else
    must be in NOT_FOR_THE_RUNNER or REPLACED_FOR_THE_RUNNER, with the reason.
    A new ENV in either Dockerfile, or in the upstream base once its new digest
    is re-read, therefore fails here until someone decides, in writing, whether
    the runner needs it -- which is the decision nobody made for
    PLAYWRIGHT_BROWSERS_PATH.
    """
    image_env = _browser_image_env()
    # Guards against passing vacuously on a parser that found nothing, and on
    # a layering that dropped the upstream layer.
    assert "PLAYWRIGHT_BROWSERS_PATH" in image_env, sorted(image_env)
    assert "PATH" in image_env and "HOME" in image_env, sorted(image_env)
    assert "PYTHON_VERSION" in image_env, sorted(image_env)

    for name, value in image_env.items():
        monkeypatch.setenv(name, value)
    worker = _browser_worker(db, worker_factory, tmp_path)

    env = worker._build_child_env()

    problems: list[str] = []
    for name, value in sorted(image_env.items()):
        if name in NOT_FOR_THE_RUNNER:
            if name in env:
                problems.append(
                    f"{name} reaches the runner, but is listed as not for it: "
                    f"{NOT_FOR_THE_RUNNER[name]}"
                )
        elif name in REPLACED_FOR_THE_RUNNER:
            if env.get(name) == value:
                problems.append(
                    f"{name} reaches the runner with the image's value {value!r}, "
                    f"but is listed as replaced: {REPLACED_FOR_THE_RUNNER[name]}"
                )
        elif env.get(name) != value:
            problems.append(
                f"the image sets {name}={value!r} and the runner sees "
                f"{env.get(name)!r}. Either carry it in "
                f"lifecycle._build_child_env or record in NOT_FOR_THE_RUNNER why "
                f"the runner must not have it."
            )
    assert not problems, "\n".join(problems)

    # And the lists describe the images as they are, not as they were.
    stale = sorted(
        (set(NOT_FOR_THE_RUNNER) | set(REPLACED_FOR_THE_RUNNER)) - set(image_env)
    )
    assert not stale, f"listed but no longer set by any layer of the image: {stale}"


def test_the_recorded_upstream_env_is_for_the_pinned_base():
    """UPSTREAM_PYTHON_ENV is a reading of ONE image. If the base pins another,
    the reading describes nothing that is built, and the test above would go on
    passing against it -- which is how a mirrored value goes stale silently.
    """
    ref, digest = _pinned_upstream()
    assert digest == UPSTREAM_PYTHON_DIGEST, (
        f"{BASE_DOCKERFILE.relative_to(REPO)} now builds FROM {ref}@{digest}, "
        f"but UPSTREAM_PYTHON_ENV was read from {UPSTREAM_PYTHON_DIGEST}. "
        "Re-read that image's ENV with the commands next to UPSTREAM_PYTHON_ENV, "
        "update both, and classify anything new."
    )
