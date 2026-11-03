"""Which build of the worker is running: its commit, its image and its code.

WHY (part of #2688). On 2026-11-03 a visual-QA run's capture steps ran on GKE
Autopilot from 17:43, after two releases had deployed a change to the live
gate, and nothing the run kept could say whether the gate that refused its
requests had that change: the worker logged no image or commit, and the
capture manifest's `commit` is the app's checkout, not the worker's. So the
worker says at start-up, and the gate's refusal record (and through it the
capture manifest's `worker`) carries:

* `commit`: the build's commit, from `SWARM_GIT_SHA` or `GIT_SHA` when the
  image sets one (build-images.sh passes `GIT_SHA` to every build; an image
  that declares it as an ENV makes it readable here), else null;
* `image`: the container image, from `SWARM_WORKER_IMAGE` when the dispatch
  sets it, else null;
* `code`: `sha256:<hex>` over the worker package's own Python source, ALWAYS
  present, because it needs nothing from the image or the dispatch. It is
  the same for the installed wheel and for a checkout of the commit it was
  built from: `python -m agent_worker.buildinfo apps/agent-worker/agent_worker`
  in a checkout prints the value to compare.

Nothing here can raise into its caller, and nothing it reads is a secret: a
value that is not the shape it should be is dropped, never echoed.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

COMMIT_ENVS = ("SWARM_GIT_SHA", "GIT_SHA")
IMAGE_ENV = "SWARM_WORKER_IMAGE"
PACKAGE_ROOT = Path(__file__).resolve().parent

_COMMIT = re.compile(r"[0-9A-Za-z._-]{1,64}")
_IMAGE = re.compile(r"[a-z0-9.-]{1,253}(?::[0-9]{1,5})?/[A-Za-z0-9._/-]{1,255}"
                    r"(?::[A-Za-z0-9._-]{1,128})?(?:@sha256:[0-9a-f]{64})?")


def code_fingerprint(root: Path) -> str | None:
    """`sha256:<hex>` over every `*.py` under `root` (no `__pycache__`), each
    as its POSIX path relative to `root`, its length and its bytes, in path
    order. None when `root` holds none or cannot be read."""
    try:
        files = sorted((p for p in root.rglob("*.py") if "__pycache__" not in p.parts and p.is_file()),
                       key=lambda p: p.relative_to(root).as_posix())
        if not files:
            return None
        digest = hashlib.sha256()
        for path in files:
            body = path.read_bytes()
            digest.update(f"{path.relative_to(root).as_posix()}\0{len(body)}\0".encode())
            digest.update(body)
    except (OSError, ValueError):
        return None
    return f"sha256:{digest.hexdigest()}"


def describe_env(env: Mapping[str, str], root: Path) -> dict[str, Any]:
    """`{commit, image, code}` for this environment and package root."""
    commit = next((env[k].strip() for k in COMMIT_ENVS if env.get(k, "").strip()), None)
    image = env.get(IMAGE_ENV, "").strip() or None
    return {"commit": commit if commit and _COMMIT.fullmatch(commit) else None,
            "image": image if image and _IMAGE.fullmatch(image) else None,
            "code": code_fingerprint(root)}


@functools.lru_cache(maxsize=1)
def _cached() -> tuple[tuple[str, Any], ...]:
    return tuple(describe_env(os.environ, PACKAGE_ROOT).items())


def describe() -> dict[str, Any]:
    """This process's build, read once. Never raises."""
    try:
        return dict(_cached())
    except Exception:  # noqa: BLE001 -- the build line never stops a start-up
        return {"commit": None, "image": None, "code": None}


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else PACKAGE_ROOT
    fingerprint = code_fingerprint(root)
    if fingerprint is None:
        print(f"no Python source under {root}", file=sys.stderr)
        return 1
    print(fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
