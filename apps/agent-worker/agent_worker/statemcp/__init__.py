"""`swarm-state`: the stdio MCP shim over the worker's state-reads spool
(docs/design/agent-autonomy.md §3.4; lane AU-4 STATE-READS, part of #1933).

    python -m agent_worker.statemcp [--spool <dir>]                  the MCP server
    python -m agent_worker.statemcp agent_view --agent-id task_...   one call, from a shell

WHAT IT IS. Nine read-only tools -- `agents_list`, `agent_view`,
`agent_events`, `agent_log_tail`, `agent_artifacts`, `workflow_view`,
`run_view_sc`, `heartbeats`, `logs_query` -- over the agent's OWN tenant's
SwarmCloud records, so a fix run can read the failure it is fixing. Each call
is one request file in the spool `$SWARM_STATE`
(`work/.swarm-state/requests/<id>.json`); the worker beside the agent
(`statemcp.relay.StateReads`) validates it, signs it with the attempt key and
asks swarm-api (`GET /v1/attempts/{attempt_id}/reads/{tool}`), and writes
`responses/<id>.json`, which this shim hands back.

WHAT IT HOLDS: NOTHING. No token, no key, no network, no tenant. The spool
accepts exactly `server.TOOLS`, the worker checks every request whatever
wrote it, and swarm-api scopes every read to the lease's tenant.

It is the `swarm-git` shim's loop (`gitmcp.server.GitServer`) with its own
name and tools: same transport, same spool protocol. Named in claude-code's
`--mcp-config` only when the worker exports `$SWARM_STATE`
(`runners.claude_code.write_mcp_config`).
"""
