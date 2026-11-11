"""Drive the console's hottest reads at a running swarm-api for a fixed time.

Used by `scripts/measure-api-workers.sh`; prints one JSON object on stdout:
requests per second, p50/p95/p99 latency in milliseconds, and the count of
answers that were not 200 (a run with any is reported, never averaged in).

CLOSED LOOP: `--concurrency` requests are in flight at all times, split over
`--processes` client processes so the client is not what saturates (one
asyncio process tops out well below what two server workers can answer). The
first `--warmup` seconds are driven but not counted, so the first reads that
build each worker's caches do not weigh on either side.

THE MIX is what one open console polls, weighted by how often it polls it
(apps/swarm-ui/src: Overview every 20 s, Home and the notification feed every
30 s, stats every 60 s, the shell's tenant on every page).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import sys
import time

import httpx

#: (path, weight). Weights are reads per minute of one console left open on
#: the Overview with the shell's badges live.
MIX: tuple[tuple[str, int], ...] = (
    ("/v1/tasks?view=summary", 3),  # Overview, every 20 s
    ("/v1/capacity", 3),            # Overview and the shell's capacity strip
    ("/v1/tenants/me", 2),          # the shell, on every page
    ("/v1/notifications", 2),       # the notification feed, every 30 s
    ("/v1/tasks?submitted_by=me", 2),  # Home, every 30 s
    ("/v1/onboarding", 2),          # Home, every 30 s
    ("/v1/stats", 1),               # Overview, every 60 s
)


def schedule() -> list[str]:
    return [path for path, weight in MIX for _ in range(weight)]


async def _client(base: str, token: str, connections: int, warmup: float, duration: float,
                  offset: int) -> tuple[list[float], int, int]:
    paths = schedule()
    headers = {"Authorization": f"Bearer {token}"}
    latencies: list[float] = []
    failed = 0
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    start = time.perf_counter()
    count_from = start + warmup
    stop = count_from + duration
    async with httpx.AsyncClient(base_url=base, headers=headers, limits=limits, timeout=30.0) as client:
        async def one(lane: int) -> None:
            nonlocal failed
            n = lane + offset
            while True:
                sent = time.perf_counter()
                if sent >= stop:
                    return
                response = await client.get(paths[n % len(paths)])
                done = time.perf_counter()
                n += connections
                if sent < count_from:
                    continue
                if response.status_code == 200:
                    latencies.append(done - sent)
                else:
                    failed += 1
        await asyncio.gather(*(one(lane) for lane in range(connections)))
    return latencies, failed, len(paths)


def _run_client(args: tuple[str, str, int, float, float, int], out: "multiprocessing.Queue") -> None:
    out.put(asyncio.run(_client(*args)))


def wait_ready(base: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            sys.exit(f"swarm-api did not answer /healthz at {base} within {seconds:.0f}s")
        time.sleep(0.2)


def percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    args = parser.parse_args()
    token = os.environ["SWARM_PERF_TOKEN"]
    wait_ready(args.base, args.ready_timeout)

    processes = max(1, min(args.processes, args.concurrency))
    share = [args.concurrency // processes + (1 if i < args.concurrency % processes else 0)
             for i in range(processes)]
    out: multiprocessing.Queue = multiprocessing.Queue()
    workers = [
        multiprocessing.Process(target=_run_client,
                                args=((args.base, token, n, args.warmup, args.duration, i * 7), out))
        for i, n in enumerate(share)
    ]
    for w in workers:
        w.start()
    results = [out.get() for _ in workers]
    for w in workers:
        w.join()

    latencies = sorted(lat for lats, _, _ in results for lat in lats)
    failed = sum(f for _, f, _ in results)
    print(json.dumps({
        "concurrency": args.concurrency,
        "duration_s": args.duration,
        "requests": len(latencies),
        "non_200": failed,
        "rps": round(len(latencies) / args.duration, 1),
        "p50_ms": round(percentile(latencies, 0.50) * 1000, 1),
        "p95_ms": round(percentile(latencies, 0.95) * 1000, 1),
        "p99_ms": round(percentile(latencies, 0.99) * 1000, 1),
        "mix": [{"path": p, "weight": w} for p, w in MIX],
    }))


if __name__ == "__main__":
    main()
