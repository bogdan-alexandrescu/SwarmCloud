"""The acceptance suite's claude-code fixture: one deliberate one-line bug.

`add` subtracts. test_calc.py fails because of it. The suite asks an agent to
fix it and asserts that the patch it produced removes exactly the line below
and adds one in its place (scripts/acceptance/groups/claude-code.sh).

DO NOT FIX THIS FILE. The bug is the fixture: a correct `add` here would make
every claude-code acceptance check fail, because there would be nothing to fix.
Nothing imports this module; it lives outside tests/unit and is never collected.
"""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
