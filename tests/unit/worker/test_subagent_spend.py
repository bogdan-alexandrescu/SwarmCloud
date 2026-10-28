"""Per-model spend and sub-agent spend are recorded; a scan refusal names its rule.

SUBAGENT-SPEND (part of #2524, item 8). The token analysis of 2026-10-20..27
found about 11% of cost with no token count behind it: the CLI's `usage` is the
parent agent's alone, while `total_cost_usd` also pays for every sub-agent it
started. The usage summary kept only the totals and a list of model names, so
which model, and which share of the bill, went to sub-agents could not be read.

Pinned here, all offline:

* a CLI result with two models and sub-agent usage yields one row per model
  (input, output, cache read, cache write, cost) and a `subagent_cost_usd`
  equal to the cost the parent's own tokens do not explain;
* a result whose one model is all the parent's spend shows a sub-agent share
  of zero, and one with nothing to read it from shows none at all;
* `_add_spend` sums the rows model by model across runners, and for a runner
  that resumed a session reads the session's rows and sub-agent share once;
* a `publish_failed:` refusal from the pre-publish credential scan names the
  scan rule and the `path:line` it matched, and never the matched text.

MUTATIONS: drop the `MODEL_USAGE_KEY` rows from `_usage_summary` -- the
two-model test fails. Credit the parent the whole home row's cost -- the
sub-agent figure falls to the other model's cost alone and the test fails.
Sum resumed rows instead of replacing them -- the session test sees the first
pass twice. Drop the `credential_locator` line from `_publish_failed` -- the
refusal no longer names `path:line`.
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import lifecycle

#: The parent ran Opus; it started Haiku sub-agents, and one Opus sub-agent
#: whose tokens sit in the Opus row beside the parent's.
PARENT_USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 2_000,
    "cache_creation_input_tokens": 10_000,
    "cache_read_input_tokens": 100_000,
}
#: The Opus row: the parent's counts doubled, so half its weighted tokens --
#: and so half its cost -- are the Opus sub-agent's.
OPUS_ROW = {
    "inputTokens": 2_000,
    "outputTokens": 4_000,
    "cacheCreationInputTokens": 20_000,
    "cacheReadInputTokens": 200_000,
    "costUSD": 2.0,
}
HAIKU_ROW = {
    "inputTokens": 500,
    "outputTokens": 300,
    "cacheCreationInputTokens": 0,
    "cacheReadInputTokens": 4_000,
    "costUSD": 0.25,
}
CLI_RESULT = {
    "type": "result",
    "total_cost_usd": 2.25,
    "num_turns": 30,
    "usage": dict(PARENT_USAGE),
    "modelUsage": {"claude-opus-5-5": OPUS_ROW, "claude-haiku-4-5-20251001": HAIKU_ROW},
}


def _rows(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["model"]: row for row in summary["model_usage"]}


def test_two_models_and_sub_agent_usage_yield_per_model_rows_and_a_sub_agent_field():
    summary = lifecycle._usage_summary(CLI_RESULT)

    rows = _rows(summary)
    assert [row["model"] for row in summary["model_usage"]] == [
        "claude-haiku-4-5-20251001", "claude-opus-5-5"]
    assert rows["claude-opus-5-5"] == {
        "model": "claude-opus-5-5",
        "input_tokens": 2_000,
        "output_tokens": 4_000,
        "cache_read_input_tokens": 200_000,
        "cache_creation_input_tokens": 20_000,
        "cost_usd": 2.0,
    }
    assert rows["claude-haiku-4-5-20251001"]["cost_usd"] == 0.25
    assert rows["claude-haiku-4-5-20251001"]["cache_read_input_tokens"] == 4_000
    # Half the Opus row (1.00) plus all of Haiku (0.25): the parent's tokens
    # explain 1.00 of 2.25.
    assert summary["subagent_cost_usd"] == pytest.approx(1.25)
    # The totals are what they were.
    assert summary["total_cost_usd"] == 2.25
    assert summary["input_tokens"] == 1_000
    assert summary["models"] == ["claude-haiku-4-5-20251001", "claude-opus-5-5"]


def test_the_rows_survive_the_production_envelope():
    """The CLI's JSON arrives under `structured_output` (runners/cliagent.py)."""
    summary = lifecycle._usage_summary({"exit_code": 0, "structured_output": CLI_RESULT})
    assert len(summary["model_usage"]) == 2
    assert summary["subagent_cost_usd"] == pytest.approx(1.25)


def test_a_parent_that_started_no_sub_agent_shows_a_share_of_zero():
    alone = {
        "total_cost_usd": 1.0,
        "usage": dict(PARENT_USAGE),
        "modelUsage": {"claude-opus-5-5": {
            "inputTokens": 1_000, "outputTokens": 2_000,
            "cacheCreationInputTokens": 10_000, "cacheReadInputTokens": 100_000,
            "costUSD": 1.0,
        }},
    }
    assert lifecycle._usage_summary(alone)["subagent_cost_usd"] == 0.0


def test_no_share_is_guessed_when_no_row_can_hold_the_parent():
    """A row missing a count cannot be split: "not known", not a figure."""
    partial = {
        "total_cost_usd": 1.0,
        "usage": dict(PARENT_USAGE),
        "modelUsage": {"claude-opus-5-5": {"outputTokens": 2_000, "costUSD": 1.0}},
    }
    summary = lifecycle._usage_summary(partial)
    assert summary["model_usage"] == [
        {"model": "claude-opus-5-5", "output_tokens": 2_000, "cost_usd": 1.0}]
    assert "subagent_cost_usd" not in summary


def test_add_spend_sums_the_rows_model_by_model_across_runners():
    first = lifecycle._usage_summary(CLI_RESULT)
    second = lifecycle._usage_summary({
        **CLI_RESULT,
        "modelUsage": {"claude-opus-5-5": OPUS_ROW},
        "total_cost_usd": 2.0,
    })
    total = lifecycle._add_spend(lifecycle._add_spend({}, first), second)

    rows = _rows(total)
    assert rows["claude-opus-5-5"]["cost_usd"] == pytest.approx(4.0)
    assert rows["claude-opus-5-5"]["input_tokens"] == 4_000
    assert rows["claude-haiku-4-5-20251001"]["cost_usd"] == pytest.approx(0.25)
    assert total["subagent_cost_usd"] == pytest.approx(1.25 + 1.0)


def test_a_resumed_session_counts_its_rows_and_sub_agent_share_once():
    """`modelUsage` is a session running total (#667): the resumed runner's
    rows replace the session's earlier ones, and its sub-agent share is read
    against the parent's tokens over both invocations."""
    first = lifecycle._usage_summary({
        "total_cost_usd": 1.0,
        "usage": dict(PARENT_USAGE),
        "modelUsage": {"claude-opus-5-5": {
            "inputTokens": 1_000, "outputTokens": 2_000,
            "cacheCreationInputTokens": 10_000, "cacheReadInputTokens": 100_000,
            "costUSD": 1.0,
        }},
    })
    # The session began with this attempt, so nothing was spent before it.
    base: dict[str, Any] = {}
    total = lifecycle._add_spend(base, first)
    # Resumed: the parent did the same again, and started Haiku sub-agents.
    resumed = lifecycle._usage_summary({
        "total_cost_usd": 2.25,
        "usage": dict(PARENT_USAGE),
        "modelUsage": {"claude-opus-5-5": {
            "inputTokens": 2_000, "outputTokens": 4_000,
            "cacheCreationInputTokens": 20_000, "cacheReadInputTokens": 200_000,
            "costUSD": 2.0,
        }, "claude-haiku-4-5-20251001": HAIKU_ROW},
    })
    total = lifecycle._add_spend(total, resumed, session_base=base)

    assert total["total_cost_usd"] == pytest.approx(2.25)
    rows = _rows(total)
    assert rows["claude-opus-5-5"]["cost_usd"] == pytest.approx(2.0), "the first pass counted twice"
    assert rows["claude-opus-5-5"]["input_tokens"] == 2_000
    # The whole Opus row is the parent's over the session; only Haiku is not.
    assert total["subagent_cost_usd"] == pytest.approx(0.25)


# -- a publish_failed refusal names the scan rule and path:line ---------------


class _QuietLog:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _git(repo: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _publish_failed(cause: str) -> str | None:
    """`Worker._publish_failed` for a deliverable-branch step whose push was
    refused with `cause`, the way `_publish_git` records a `GitError`."""
    worker = SimpleNamespace(
        _deliverable_branch=lambda: "the step's branch is its deliverable",
        _verdict=None,
        cfg=SimpleNamespace(task_id="task_x"),
    )
    summary = {"git": {"published": False, "publish_failed": True, "publish_reason": cause}}
    return lifecycle.Worker._publish_failed(worker, summary)


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git is not installed")
def test_a_scan_refusal_names_its_rule_and_path_line_and_not_the_text(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "logs").mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (repo / "README.md").write_text("readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    value = secrets.token_hex(20)
    (repo / "app").mkdir()
    (repo / "app" / "settings.py").write_text(
        "DEBUG = False\n" + "api_key = " + repr(value) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "settings")

    leak = lifecycle.final_tree_leak(
        repo=repo, base=base, leaks=lifecycle._credential_in, private_dir=tmp_path,
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_QuietLog(),
    )
    assert leak is not None
    # The carrier route raises this, and `_publish_git` records it.
    message = _publish_failed(f"refusing to push: {leak}")

    assert message is not None and message.startswith("publish_failed: ")
    assert "rule key_value_assignment at app/settings.py:2" in message, message
    assert value not in message
    assert value[:12] not in message


def test_a_refusal_that_is_not_the_scans_is_unchanged():
    message = _publish_failed("push failed with exit 1")
    assert message is not None
    assert "credential scan" not in message
    assert "not pushed: push failed with exit 1." in message
