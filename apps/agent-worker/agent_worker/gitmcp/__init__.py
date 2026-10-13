"""`swarm-git`: the stdio MCP shim over the worker's git-tools spool
(docs/runner-git-tools.md §1.1, §1.3; lane G3).

    python -m agent_worker.gitmcp [--spool <dir>]          the MCP server
    python -m agent_worker.gitmcp <tool> [--ref ...] ...   one call, for a runner without MCP

WHAT IT IS. Five tools -- `git_fetch`, `git_rebase_onto`, `git_push_branch`,
`read_private`, `pr_checks` -- that a runner calls mid-task for the git work
its container cannot do: the container holds no forge token, by design (§0).
Each call becomes one request file in the spool `$SWARM_GIT`
(`work/.swarm-git/requests/<id>.json`); the worker beside the agent
(`agent_worker.gittools`) validates it, does its half -- the scans, the
bundle, the signed call -- and writes `responses/<id>.json`, which this shim
hands back.

WHAT IT HOLDS: NOTHING. No token, no key, no network, no repository argument
of its own. The agent can bypass it and write the spool directly, and that is
fine: the spool accepts exactly the five tools with exactly the arguments in
`server.TOOLS`, and the worker checks every request whatever wrote it. The
shim is an interface, not a gate.

WHY ITS OWN JSON-RPC LOOP: the same reason as `agent_worker.graphmcp` -- the
worker package depends on no MCP SDK. The tool schemas and the instructions
together stay under the 2k-token budget graphmcp holds (§7.7 of the
knowledge-graph design), because the agent pays for them on every turn.

Nothing starts it yet: naming it in claude-code's `--mcp-config` and codex's
MCP config is lane G6.
"""
