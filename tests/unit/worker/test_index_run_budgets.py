"""The worker's half of size-aware index budgets (lane INDEX-BUDGETS, part of #1693).

swarm-api now sizes an index run's timeout to the repository
(`swarm_api.repoindex.index_timeout_seconds`: 1,800 / 3,600 / 7,200 s for a
full run) and names, in the SIGNED prompt, what each language server cost
in the last full run (`LSP_COST_LINE`). What is held here:

  * `budgets()` hands each phase its share of whatever timeout the task has,
    so the LSP pass grows with the run: 540 s of 1,800, 1,260 of 3,600 and
    2,700 of 7,200;
  * the cost line is read from the signed prompt only, and a line that is
    malformed or appears twice is no history at all (the equal split);
  * the extractor is given the costs, one `--lsp-server-cost` a server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker import indexrun
from swarm_api import repoindex


@pytest.mark.parametrize(("timeout", "extract", "lsp", "write", "sweep"), [
    (1_800, 720, 540, 180, 90),
    (3_600, 1_440, 1_260, 360, 180),
    (7_200, 2_880, 2_700, 720, 360),
])
def test_the_phase_budgets_follow_the_timeout(timeout, extract, lsp, write, sweep):
    budget = indexrun.budgets(timeout)
    # MUTATION: pin the LSP pass to a fixed 540 s (or the extractor to a
    # fixed 720 s) and the 3,600 and 7,200 rows are red: the longer run
    # would go to the agent, not to the servers that ran out of time.
    assert (budget.extract, budget.lsp_total, budget.graph_write, budget.sweep) == \
        (extract, lsp, write, sweep)
    # The agent still has the larger part of every run.
    assert timeout - budget.extract - budget.graph_write - budget.sweep >= 810


def test_ai_studios_run_gives_the_pass_more_than_pyright_needed():
    # #1693: pyright needed ~680-890 s on ai-studio (3,795 source files) and
    # had 270. The pass as a whole now has 1,260 s, and the driver grants it
    # in proportion to each server's measured cost.
    timeout = repoindex.index_timeout_seconds("full", 3_795)
    assert indexrun.budgets(timeout).lsp_total >= 890


def _input(*lines: str) -> dict:
    return {"prompt": "Index the repository.\n\n" + "\n".join(lines) + "\n"}


def test_the_costs_are_read_from_the_signed_prompt():
    line = f"{indexrun.LSP_COST_LINE}pyright=890 tsserver=120"
    # MUTATION: return None from lsp_costs() and the extractor never learns
    # what each server cost: the equal split, every run.
    assert indexrun.lsp_costs(_input(line)) == {"pyright": 890, "tsserver": 120}
    # The prefix is swarm-api's own.
    assert indexrun.LSP_COST_LINE == repoindex.LSP_COST_LINE


@pytest.mark.parametrize("task_input", [
    None,
    {"prompt": None},
    _input(),
    _input(f"{indexrun.LSP_COST_LINE}"),
    _input(f"{indexrun.LSP_COST_LINE}pyright=890", f"{indexrun.LSP_COST_LINE}pyright=10"),
    _input(f"{indexrun.LSP_COST_LINE}pyright=0"),
    _input(f"{indexrun.LSP_COST_LINE}pyright=-3"),
    _input(f"{indexrun.LSP_COST_LINE}pyright=1e9"),
    _input(f"{indexrun.LSP_COST_LINE}--bin-dir=/tmp"),
    _input(f"{indexrun.LSP_COST_LINE}pyright=10 pyright=20"),
])
def test_a_cost_line_that_is_not_well_formed_is_no_history(task_input):
    # MUTATION: parse each pair on its own and keep the good ones, and the
    # two-line and duplicate cases read a cost the signer never wrote once.
    assert indexrun.lsp_costs(task_input) is None


def test_the_extractor_is_given_one_cost_a_server(tmp_path: Path):
    argv = indexrun.extractor_argv("swarm-repo-index", checkout=tmp_path / "c", work=tmp_path,
                                   lsp_total=1_260, lsp_costs={"tsserver": 120, "pyright": 890})
    # MUTATION: drop the costs from extractor_argv() and the driver has no history.
    assert argv[-4:] == ["--lsp-server-cost", "pyright=890", "--lsp-server-cost", "tsserver=120"]
    bare = indexrun.extractor_argv("swarm-repo-index", checkout=tmp_path / "c", work=tmp_path,
                                   lsp_total=540)
    assert "--lsp-server-cost" not in bare
