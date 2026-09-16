"""Quota-broker process settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from swarm_common.config import Settings

from .aimd import AimdConfig


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class BrokerSettings:
    core: Settings

    #: Default ceiling for a (provider, tenant) pair the broker has never seen.
    #: An admin raises it per pair; AIMD may only ever go BELOW it.
    default_hard_max: int = 50

    aimd: AimdConfig = AimdConfig()

    @property
    def project_id(self) -> str:
        return self.core.project_id

    @classmethod
    def from_env(cls) -> "BrokerSettings":
        return cls(
            core=Settings.from_env(),
            default_hard_max=_int("QUOTA_DEFAULT_HARD_MAX", 50),
            aimd=AimdConfig(
                additive_increase=_int("AIMD_ADDITIVE_INCREASE", 1),
                multiplicative_decrease=_float("AIMD_MULTIPLICATIVE_DECREASE", 0.5),
                success_threshold=_int("AIMD_SUCCESS_THRESHOLD", 20),
                min_target=_int("AIMD_MIN_TARGET", 1),
                default_cooldown_seconds=_int("AIMD_DEFAULT_COOLDOWN_SECONDS", 30),
                max_cooldown_seconds=_int("AIMD_MAX_COOLDOWN_SECONDS", 900),
                exhaustion_threshold=_int("AIMD_EXHAUSTION_THRESHOLD", 5),
            ),
        )
