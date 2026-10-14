"""The `swarm-search` shim: the tool schema and the spool's names.

    $SWARM_SEARCH = work/.swarm-search/
      requests/<request_id>.json   {"tool": "search", "arguments": {"query", "count"?}}
      responses/<request_id>.json  the worker's: {"tool", "result": {...}} or {"refused": {...}}

`agent_worker.searchtools` imports every constant below, so the worker and the
shim cannot disagree about the tool or its arguments. The loop and the spool
protocol are `gitmcp.server`'s, reused.
"""

from __future__ import annotations

from typing import Any

from ..gitmcp.server import GitServer

SERVER_NAME = "swarm-search"
SPOOL_DIR_NAME = ".swarm-search"
SPOOL_ENV = "SWARM_SEARCH"
TOOL_NAME = "search"
#: swarm-api's limits (`swarm_api.search.MAX_QUERY_CHARS`, `MAX_COUNT`),
#: restated for the fast answer; tests/unit/worker/test_search_tool_wiring.py
#: holds them equal.
MAX_QUERY_CHARS = 400
MAX_COUNT = 10
DEFAULT_COUNT = 5

INSTRUCTIONS = ("Web search through the platform's search API. Search here, then open "
                "the results you need in the live browser.")

TOOLS: list[dict[str, Any]] = [
    {"name": TOOL_NAME,
     "description": "Search the web. Returns up to count results as title, url and "
                    "snippet; open the ones you need in the live browser.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "maxLength": MAX_QUERY_CHARS},
         "count": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT,
                   "description": f"results wanted (default {DEFAULT_COUNT})"}}}},
]
ARGUMENTS: dict[str, frozenset[str]] = {
    tool["name"]: frozenset(tool["inputSchema"]["properties"]) for tool in TOOLS
}
REQUIRED: dict[str, frozenset[str]] = {
    tool["name"]: frozenset(tool["inputSchema"].get("required", ())) for tool in TOOLS
}


class SearchServer(GitServer):
    name = SERVER_NAME
    instructions = INSTRUCTIONS
    tools = TOOLS
    arguments = ARGUMENTS
    required = REQUIRED
