"""Comments that contradicted the code next to them, held corrected (#177).

Each case names a sentence the 2026-09-25 audit verified as false and checks
that it is gone and that the replacement states the fact the code enforces. A
comment is read with its line breaks and comment markers folded away, so a
sentence that wraps is still one sentence.

The reconciler's `pass_retention_hours` ("A pass runs every minute ... 10,080
documents a week") is not here: the tick became */1, which made it true, and
tests/unit/worker/test_recovery_after_a_dead_worker.py holds it to the
deployed schedule.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _folded(text: str) -> str:
    """Fold line breaks and leading `#`, `#:` and `//` markers into spaces."""
    return re.sub(r"\s*\n\s*(?:#:?|//)?\s*", " ", text)


def _source(rel: str) -> str:
    return _folded((REPO / rel).read_text())


def _docstring(rel: str, name: str) -> str:
    """The docstring of function `name` in `rel`, or of the module for ""."""
    tree = ast.parse((REPO / rel).read_text())
    if not name:
        return _folded(ast.get_docstring(tree) or "")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return _folded(ast.get_docstring(node) or "")
    raise AssertionError(f"{rel} has no function {name!r}")


def _tf_description(rel: str, variable: str) -> str:
    match = re.search(
        rf'variable\s+"{variable}"\s*\{{\s*description\s*=\s*<<-EOT\n(.*?)\n\s*EOT',
        (REPO / rel).read_text(),
        re.S,
    )
    assert match, f"{rel} has no heredoc description for {variable!r}"
    return _folded(match.group(1))


def test_broker_credentials_states_the_per_token_write_count():
    text = _docstring("apps/quota-broker/quota_broker/credentials.py", "")
    assert "~1,700 times per token" not in text
    assert "about 60 times per token" in text


def test_broker_secret_name_says_its_rename_orphaned_accounts():
    text = _docstring("apps/quota-broker/quota_broker/accounts.py", "secret_name")
    assert "the only moment it was free" not in text
    assert "orphaned three" in text
    assert "secret_ref" in text


def test_broker_sweep_cites_the_incident_figures_instead_of_restating_them():
    text = _source("apps/quota-broker/quota_broker/main.py")
    assert "1,741 and 1,698" not in text
    assert "quota_broker.credentials" in text


def test_restrict_egress_describes_the_rule_it_turns_on():
    text = _tf_description("terraform/modules/network/variables.tf", "restrict_egress")
    assert "restricted VIP" not in text
    assert "egress_allowed_ports" in text and "UDP 53" in text and "any destination" in text


def test_safety_tick_comment_says_the_deadline_does_nothing_for_pubsub():
    text = _source("terraform/modules/scheduler/jobs.tf")
    assert "Shorter than the schedule interval" not in text
    assert "ignores attempt_deadline for a pubsub_target" in text


def test_ui_component_test_header_says_where_non_watch_mode_comes_from():
    text = _source("scripts/ui-component-test.sh")
    assert "The test run itself passes `--run`" not in text
    assert "`vitest run`" in text


def test_plan_guard_success_line_claims_only_what_was_checked():
    text = (REPO / "scripts/lib/plan-guard.sh").read_text()
    assert "every deletion is ours, and every creation carries" not in text
    assert "unlabelable types are exempt" in text


def test_reconciler_record_pass_docstring_says_it_raises():
    text = _docstring("apps/reconciler/reconciler/store.py", "record_pass")
    assert "Returns None rather than raising" not in text
    assert "Raises on a failed write" in text


def test_reconciler_once_exit_code_is_report_errors():
    text = _docstring("apps/reconciler/reconciler/__main__.py", "")
    assert "when any repair failed" not in text
    assert "`report.errors` is non-empty" in text


@pytest.mark.parametrize("stale", ["unrecognised host", "no credential at all"])
def test_forge_probe_repository_none_means_an_unparseable_url(stale):
    text = _docstring("apps/agent-worker/agent_worker/forge.py", "probe_repository")
    assert stale not in text
    assert "does not parse as a forge repository URL" in text
