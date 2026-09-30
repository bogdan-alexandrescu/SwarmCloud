"""Fails on purpose until calc.add is fixed -- see calc.py. Never collected by
the repository's own test run (pyproject's testpaths is tests/unit)."""

from calc import add, multiply


def test_add():
    assert add(2, 3) == 5


def test_multiply():
    assert multiply(2, 3) == 6
