"""The Jev client shared by the agent worker and swarm-api.

Jev is an advisory ranker and judge: it answers typed questions (`noul`,
`choice`, `score`) and never writes text
(docs/design/context-quality.md §5; docs/design/agent-quality/jev-contract.md).

* `client`   -- the wire shape, `validate_questions`, and `JudgeClient`: one
                call, validated, sanitised, admitted, retried, audited.
* `sanitize` -- §5.4: denied paths send names only; credential-shaped or
                registered-secret strings are DROPPED, never masked. The
                credential check is `swarm_redaction.credentials`, the
                worker's publish gate, in every process.

The worker's `agent_worker.judge.client` and `agent_worker.judge.sanitize`
re-export these objects (the same objects, not copies); the budget, the
settings and the call shapes stay the caller's.
"""

from swarm_jev.client import (
    Decision,
    JevBackend,
    JevLedger,
    JudgeClient,
    QuestionError,
    Transport,
    urllib_transport,
    validate_questions,
)
from swarm_jev.sanitize import (
    Sanitized,
    is_denied_path,
    is_flagged,
    sanitize_state,
)

__all__ = [
    "Decision",
    "JevBackend",
    "JevLedger",
    "JudgeClient",
    "QuestionError",
    "Sanitized",
    "Transport",
    "is_denied_path",
    "is_flagged",
    "sanitize_state",
    "urllib_transport",
    "validate_questions",
]
