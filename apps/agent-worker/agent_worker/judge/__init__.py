"""The worker's Jev client (lane CTXQ-J, docs/design/context-quality.md §5).

Jev is an advisory ranker and judge: it answers typed questions (`noul`,
`choice`, `score`) and never writes text. Nothing here can fail, park or
delay an attempt; a Jev problem leaves the caller with its local answer.

* `settings`  -- whether the workspace opted in to Jev for code, and which
                 backend answers (Internal Jev by default, Internal Laya, TypeSafe).
* `sanitize`  -- §5.4: denied paths send names only; credential-shaped or
                 registered-secret strings are DROPPED, never masked.
* `budget`    -- 8 calls / 120k input tokens per task, the monthly cap, the
                 §5.5 breaker and the audit, in one ledger file both callers share.
* `client`    -- one call: validated, sanitised, admitted, retried, audited.
* `shapes`    -- §5.2's J0-J5 call shapes and their readers.
* `mcp`, `__main__` -- `jev_decide`, the agent's own tool (owner override of §5.1).
* `register`  -- what the runner lane (CTXQ-JW) calls to set an attempt up.
"""
