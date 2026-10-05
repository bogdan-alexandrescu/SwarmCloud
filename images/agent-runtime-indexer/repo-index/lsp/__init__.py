"""The headless LSP pass of the repository index (docs/repo-index.md §3.5, RI10).

client.py   one server as a child process over stdio: framing, requests
            with timeouts, the budget, the memory watchdog
servers.py  the four servers §3.5 names and how each is started
driver.py   the pass itself: definition, call hierarchy, references, and
            the ok / timed_out / failing / unsupported fallback

repo_index_extract.py calls run_pass() after the tree-sitter pass and adds
the edges it returns with evidence `lsp`.
"""

from .client import (BudgetExceeded, LspClient, LspError, RequestTimeout, ServerError,
                     ServerExited, session_rss_bytes)
from .driver import (LSP_DECLARED, LSP_INFERRED, NO_SERVER_REASON, STATUS_FAILING, STATUS_OK,
                     STATUS_TIMED_OUT, STATUS_UNSUPPORTED, LanguageResult, LspEdge, LspOptions,
                     PassResult, Site, default_memory_limit_mib, run_pass, server_budget_seconds,
                     simple_name, total_budget_seconds)
from .servers import DEFAULT_BIN_DIR, SERVERS, ServerSpec

__all__ = [
    "BudgetExceeded", "DEFAULT_BIN_DIR", "LSP_DECLARED", "LSP_INFERRED", "LanguageResult",
    "LspClient", "LspEdge", "LspError", "LspOptions", "NO_SERVER_REASON", "PassResult",
    "RequestTimeout", "SERVERS", "STATUS_FAILING", "STATUS_OK", "STATUS_TIMED_OUT",
    "STATUS_UNSUPPORTED", "ServerError", "ServerExited", "ServerSpec", "Site",
    "default_memory_limit_mib", "run_pass", "server_budget_seconds", "session_rss_bytes",
    "simple_name", "total_budget_seconds",
]
