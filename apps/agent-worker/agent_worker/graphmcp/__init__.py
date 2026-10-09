"""`swarm-graph`: the read-only MCP server over a repository's graph snapshot
(docs/design/knowledge-graph.md §4.2, §5, §7.6, §7.7; lane KG4).

    python -m agent_worker.graphmcp --snapshot <dir> [--workdir <checkout>]

WHAT IT IS. A stdio MCP server with six tools -- `search`, `context`,
`impact`, `tests_for`, `neighbours`, `territory` -- that a claude-code step
asks mid-task instead of grepping one level at a time. It answers from a
LOCAL snapshot of the KG2 graph (`snapshot.py`) and nothing else: it makes no
network call, holds no credential and takes no repository argument, so
nothing an agent asks it can reach another tenant's data or anything beyond
the one snapshot it was started on (§5.3, invariant 9). It writes nothing.

WHAT IT IS NOT, YET. Nothing starts it today. Staging the snapshot beside the
checkout and naming the server in `--mcp-config` is lane KG5, behind the
owner's contract request A' (§7.4). The snapshot directory this package
reads is the one KG5 stages:

    <dir>/manifest.json               the commit's manifest, as stored
    <dir>/blobs/<sha256>.jsonl.gz     every blob it names, as stored
    <dir>/freshness.json              optional: what the stager recorded (§5.2)

WHY ITS OWN JSON-RPC LOOP. The worker package depends on no MCP SDK, and
§3.D's "no new third-party runtime" holds for the base image every profile
runs. The stdio transport is newline-delimited JSON-RPC 2.0 and the server
needs four methods of it; `server.py` is those four.

THE BUDGETS (§7.7), each held by a test:
  * every tool schema together, plus the server's instructions, under 2k
    tokens (GitNexus's is about 17.9k): the agent pays it on every turn;
  * every answer at most 4 KiB, with a `next_cursor` to page the rest;
  * answer p95 under 300 ms on this repository's own snapshot.

A STALE INDEX SAYS SO, EVERY TIME (§5.2, §7.6). Every answer, an error
included, begins with the freshness block, and every row on a path changed
since the index or dirtied in this step carries `stale`. Zero resolved
callers is `UNKNOWN`, never "safe".
"""
