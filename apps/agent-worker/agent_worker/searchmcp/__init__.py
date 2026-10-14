"""`swarm-search`: the stdio MCP shim over the worker's search spool
(docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

    python -m agent_worker.searchmcp [--spool <dir>]                 the MCP server
    python -m agent_worker.searchmcp search --query "..." [--count N]  one call, from a shell

WHAT IT IS. One tool, `search`, that a live-browser agent calls to search the
web through the platform's search API, so it opens result pages in the live
browser instead of typing a query into a search engine's page (where Google
answered 429 /sorry on 3 of 3 measured searches, docs/browser-egress.md §0).
Each call is one request file in the spool `$SWARM_SEARCH`
(`work/.swarm-search/requests/<id>.json`); the worker beside the agent
(`agent_worker.searchtools`) validates it, signs it with the attempt key and
calls swarm-api, and writes `responses/<id>.json`, which this shim hands back.

WHAT IT HOLDS: NOTHING. No key, no network. The provider's key is swarm-api's
alone (a platform secret); the attempt key is the worker's. The spool accepts
exactly `server.TOOLS`, and the worker checks every request whatever wrote it.

It is the `swarm-git` shim's loop (`gitmcp.server.GitServer`) with its own
name and tool: same transport, same spool protocol.

Named in claude-code's `--mcp-config` only when the worker offers the tool:
SEARCH_TOOL_ENABLED on in swarm-api, a live-browser profile, and a registered
attempt key (`children.offers_search_tool`).
"""
