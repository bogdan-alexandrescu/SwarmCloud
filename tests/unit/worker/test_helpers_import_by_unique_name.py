"""The worker's test helpers live in a module whose name is unique under tests/.

Every directory under tests/unit has its own conftest.py, and under pytest's
default `prepend` import mode each one is imported as the top-level module
`conftest`, so a bare import of that name means whichever directory's
conftest was imported first (owner decision 2026-10-06, observer P24). The
worker's helpers live in `worker_seeds.py` instead, and that only works while
nothing else under tests/ bears the name. The sweep that no test module
imports `conftest` by its bare name is the `no-bare-conftest-import` rule of
scripts/source_text_rules/tests_tree.py, run once over the tree by
scripts/check_source_text.py (issue 73).
"""

from __future__ import annotations

from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]


def test_the_helper_module_name_is_unique_under_tests() -> None:
    found = sorted(TESTS.rglob("worker_seeds.py"))
    assert found == [TESTS / "unit" / "worker" / "worker_seeds.py"]
