"""One Jev call, made safely: validated, sanitised, budgeted, retried, audited.

The client lives in the shared package `swarm_jev` (apps/jev-client), which
swarm-api installs too (docs/design/agent-quality/jev-contract.md section 13,
lane 1; the owner's answer 10, 3 November 2026). This module hands out the
package's OWN objects, not copies, so every existing import keeps working and
the worker and swarm-api can never drift; tests/unit/jev_client holds them
identical. The worker's `judge.settings.Backend` and `judge.budget.Ledger` are
the package's `JevBackend` and `JevLedger`.

Each name is bound by assignment rather than `from ... import` so that
docs/jev-data-flow.md's cites of this file still resolve to a definition.
"""

from __future__ import annotations

from swarm_jev import client as _shared

# The worker's sanitize, imported first: importing it adds the worker's
# publish-gate predicate to the shared scan before any call is made.
from agent_worker.judge import sanitize

CALL_TIMEOUT_SECONDS = _shared.CALL_TIMEOUT_SECONDS
RETRIES = _shared.RETRIES
BACKOFF_SECONDS = _shared.BACKOFF_SECONDS
MAX_STATE_CHARS = _shared.MAX_STATE_CHARS
MAX_CHOICE_OPTIONS = _shared.MAX_CHOICE_OPTIONS
MAX_QUESTIONS = _shared.MAX_QUESTIONS
MAX_INSTRUCTIONS_CHARS = _shared.MAX_INSTRUCTIONS_CHARS
QUESTION_TYPES = _shared.QUESTION_TYPES
MEASURED_TOKENS_PER_CALL = _shared.MEASURED_TOKENS_PER_CALL
Transport = _shared.Transport
JevBackend = _shared.JevBackend
JevLedger = _shared.JevLedger
QuestionError = _shared.QuestionError
Decision = _shared.Decision
urllib_transport = _shared.urllib_transport
validate_questions = _shared.validate_questions
JudgeClient = _shared.JudgeClient
_NAME = _shared._NAME
_DIRECT = _shared._DIRECT
_opener = _shared._opener
_stderr_log = _shared._stderr_log
_question_strings = _shared._question_strings
_probability = _shared._probability
_numbers_only = _shared._numbers_only
_valid_answer = _shared._valid_answer
_count = _shared._count
_usage = _shared._usage

__all__ = [
    "BACKOFF_SECONDS", "CALL_TIMEOUT_SECONDS", "Decision", "JevBackend", "JevLedger",
    "JudgeClient", "MAX_CHOICE_OPTIONS", "MAX_INSTRUCTIONS_CHARS", "MAX_QUESTIONS",
    "MAX_STATE_CHARS", "MEASURED_TOKENS_PER_CALL", "QUESTION_TYPES", "QuestionError",
    "RETRIES", "Transport", "sanitize", "urllib_transport", "validate_questions",
]
