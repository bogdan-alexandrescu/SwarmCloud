"""The headless LSP pass of the repository index (docs/repo-index.md §3.5, RI10).

client.py   one server as a child process over stdio: framing, requests
            with timeouts, the budget, the memory watchdog
servers.py  the four servers §3.5 names, how each is started, and the paths
            the pass never opens
driver.py   the pass itself: definition, call hierarchy, references, and
            the ok / partial / timed_out / failing / unsupported fallback

repo_index_extract.py calls run_pass() after the tree-sitter pass and adds
the edges it returns with evidence `lsp`.
"""

from .client import (BudgetExceeded, LspClient, LspError, RequestTimeout, ServerError,
                     ServerExited, session_rss_bytes)
from .driver import (LSP_DECLARED, LSP_INFERRED, NO_SERVER_REASON, RSS_STEP_MIB, STATUS_FAILING,
                     STATUS_OK, STATUS_PARTIAL, STATUS_TIMED_OUT, STATUS_UNSUPPORTED,
                     Answered, LanguageResult, LspEdge, LspOptions, PassResult, Site,
                     answers_kept, default_memory_limit_mib, grant_seconds, measured_cost, planned_needs,
                     rss_mib, run_order, run_pass, server_budget_seconds, simple_name,
                     total_budget_seconds, workspace_seconds)
from .servers import (DEFAULT_BIN_DIR, DISABLED_SERVERS, LSP_EXCLUDED_PATHS, SERVERS, ServerSpec,
                      lsp_excluded)

__all__ = [
    "Answered", "BudgetExceeded", "DEFAULT_BIN_DIR", "DISABLED_SERVERS", "LSP_DECLARED", "LSP_INFERRED", "LanguageResult",
    "LSP_EXCLUDED_PATHS", "LspClient", "LspEdge", "LspError", "LspOptions", "NO_SERVER_REASON", "PassResult",
    "RSS_STEP_MIB", "RequestTimeout", "SERVERS", "STATUS_FAILING", "STATUS_OK", "STATUS_PARTIAL",
    "STATUS_TIMED_OUT", "STATUS_UNSUPPORTED", "ServerError", "ServerExited", "ServerSpec", "Site",
    "answers_kept", "default_memory_limit_mib", "grant_seconds", "lsp_excluded", "measured_cost", "planned_needs", "rss_mib",
    "run_order", "run_pass", "server_budget_seconds", "session_rss_bytes",
    "simple_name", "total_budget_seconds", "workspace_seconds",
]
