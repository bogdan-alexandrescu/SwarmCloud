"""swarm-api's uvicorn worker count, and each per-instance cap divided by it.

THE OWNER'S CHOICE (2026-11-10): dev runs two uvicorn workers an instance
(measured offline on 2 CPUs: 82 -> 172 requests per second at 8 in flight,
p95 206 -> 96 ms), once the per-process caps hold per instance.
images/swarm-api/Dockerfile passes SWARM_API_WORKERS to `uvicorn --workers`,
and Terraform renders it (terraform/infra `api_workers`, default 1).

Each worker calls create_app() itself, so every in-process semaphore and
token bucket exists once per worker. A cap sized for an instance -- the 2 GiB
a git call or a multipart answer is bounded against, or a rate a principal is
held to -- is therefore that figure divided by the worker count, never below
1. One worker keeps today's values. Requests reach the workers through one
shared listening socket, so a principal's requests are spread over them; the
instance's total is today's, and one worker's share is the lower bound one
caller sees.

NOT DIVIDED: `forge_limits.QUOTAS`. It counts nothing itself -- it records
GitHub's own `X-RateLimit-*`, a counter GitHub keeps for every worker and
every instance, so a second worker reads the same remaining figure and
defers at the same threshold. That module says why next to the ledger.
"""

from __future__ import annotations

import os
from typing import Mapping

#: The image's setting (images/swarm-api/Dockerfile) and Terraform's
#: rendering of `api_workers` (terraform/infra/locals.tf).
WORKERS_ENV = "SWARM_API_WORKERS"


def worker_count(environ: Mapping[str, str] | None = None) -> int:
    """The uvicorn workers this instance runs: SWARM_API_WORKERS, at least 1.

    Unset, empty or not a whole number is 1, the image's own default (`:-1`),
    so a cap is never divided by a count the instance does not run."""
    raw = (os.environ if environ is None else environ).get("SWARM_API_WORKERS", "")
    try:
        count = int(str(raw).strip())
    except ValueError:
        return 1
    return max(1, count)


def per_worker(per_instance: int, environ: Mapping[str, str] | None = None) -> int:
    """`per_instance` divided by the worker count, rounded down, at least 1."""
    return max(1, int(per_instance) // worker_count(environ))


def per_worker_rate(per_instance: float, environ: Mapping[str, str] | None = None) -> float:
    """A rate (per second) for one worker: the instance's divided by the count."""
    return float(per_instance) / worker_count(environ)
