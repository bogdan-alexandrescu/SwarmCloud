"""Credential patterns shared by swarm-api and agent-worker. See `rules`."""

from .rules import (
    KEY_VALUE,
    MASK,
    PEM_BLOCK_MAX_CHARS,
    RULES,
    Redacted,
    Rule,
    mask_private_keys,
    open_key_start,
    redact,
)

__all__ = [
    "KEY_VALUE",
    "MASK",
    "PEM_BLOCK_MAX_CHARS",
    "RULES",
    "Redacted",
    "Rule",
    "mask_private_keys",
    "open_key_start",
    "redact",
]
