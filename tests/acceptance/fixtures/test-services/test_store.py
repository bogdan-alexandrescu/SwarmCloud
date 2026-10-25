"""Writes a row to the declared Postgres and reads it back.

The claude-code acceptance group asks an agent to break the assertion below,
and expects the pre-publish gate to report `graph-pytest: failed` with a fix
turn when .swarm/test-services.toml declares the service, and `skipped`,
unreachable, on the same tree without it. The connection is made in the
fixture, so a missing service is an error at setup, never a failure.
Never collected by the repository's own test run (pyproject's testpaths is
tests/unit).
"""

import pytest

import pgwire


@pytest.fixture
def db():
    conn = pgwire.connect()
    try:
        yield conn
    finally:
        conn.close()


def test_a_row_written_is_read_back(db):
    db.query("DROP TABLE IF EXISTS notes")
    db.query("CREATE TABLE notes (body text NOT NULL)")
    db.query("INSERT INTO notes (body) VALUES ('kept')")
    rows = db.query("SELECT body FROM notes")
    assert rows == [("kept",)]
