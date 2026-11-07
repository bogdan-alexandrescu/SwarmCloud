"""`Settings.allowed_domains` has no default domain (contract request 129).

A deployment trusts only the domains it was given: the dataclass default is
empty and `Settings.from_env` builds the field from `ALLOWED_DOMAINS` with no
fallback, so a service deployed without the variable admits no domain instead
of another deployment's.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from swarm_common.config import Settings

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("placeholders", ROOT / "tests" / "placeholders.py")
assert _spec is not None and _spec.loader is not None
placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(placeholders)


def test_settings_has_no_allowed_domain_by_default() -> None:
    assert Settings(project_id=placeholders.PROJECT).allowed_domains == ()


def test_from_env_without_allowed_domains_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_ID", placeholders.PROJECT)
    monkeypatch.delenv("ALLOWED_DOMAINS", raising=False)
    assert Settings.from_env().allowed_domains == ()


def test_from_env_with_blank_allowed_domains_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_ID", placeholders.PROJECT)
    monkeypatch.setenv("ALLOWED_DOMAINS", " , ")
    assert Settings.from_env().allowed_domains == ()


def test_from_env_reads_the_listed_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_ID", placeholders.PROJECT)
    monkeypatch.setenv(
        "ALLOWED_DOMAINS", f" {placeholders.ORG_DOMAIN} ,{placeholders.OUTSIDE_DOMAIN}"
    )
    assert Settings.from_env().allowed_domains == (
        placeholders.ORG_DOMAIN,
        placeholders.OUTSIDE_DOMAIN,
    )
