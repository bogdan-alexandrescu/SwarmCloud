"""`python -m agent_worker.graphmcp --snapshot <dir> [--workdir <checkout>]`.

The worker starts it with this fixed argv (§5.4: a caller names no command,
server, bucket or sha); lane KG5 writes it into `--mcp-config`. It refuses to
start on a snapshot it cannot read, so a missing or rewritten snapshot is a
failed server the agent's CLI reports, not a server answering from nothing.
"""

from __future__ import annotations

import argparse
import sys

from agent_worker.graphmcp.freshness import Freshness
from agent_worker.graphmcp.server import GraphServer
from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError, load_freshness_record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarm-graph", description=__doc__)
    parser.add_argument("--snapshot", required=True,
                        help="the staged snapshot directory (manifest.json, blobs/)")
    parser.add_argument("--workdir", default=None,
                        help="the step's checkout, for the paths dirtied in this step")
    args = parser.parse_args(argv)
    try:
        record = load_freshness_record(args.snapshot)
        snapshot = Snapshot(args.snapshot,
                            manifest_digest=(record or {}).get("manifest_digest"))
    except SnapshotError as exc:
        print(f"swarm-graph: {exc}", file=sys.stderr)
        return 2
    freshness = Freshness.establish(snapshot.commit_sha, workdir=args.workdir, record=record)
    GraphServer(snapshot, freshness).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
