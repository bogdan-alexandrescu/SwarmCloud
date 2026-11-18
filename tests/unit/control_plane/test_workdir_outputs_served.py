"""A standalone agent's working-folder files, as the Artifacts tab reads them (#184).

Owner decision, 2026-09-26: for a task with no repository, the worker uploads
what the agent created in its working folder, and those files "appear under
Artifacts > Outputs through the existing API (redacted at read time)". The
worker names them `workdir/<path>` in the same manifest every artifact is in
(`agent_worker.standalone_outputs`), so no route changed. This pins that the
existing routes list such an entry, serve its bytes, and redact them: a
`workdir/` name is two path segments, and the key is rebuilt segment by
segment (`InspectionService._artifact_key`).

Offline: FakeFirestore and the in-memory object reader. No credentials.
"""

from __future__ import annotations

from swarm_api.redaction import redact

from .test_artifact_outputs import GH_TOKEN, a_finished_task, listing, raw

ANSWER = f"# Answer\n\nThe first ten primes are in primes.txt. token {GH_TOKEN}\n"
PRIMES = "2\n3\n5\n7\n11\n13\n17\n19\n23\n29\n"


def test_working_folder_files_are_listed_and_served_redacted(client, db, objects):
    a_finished_task(
        db,
        objects,
        files={
            "claude-code.stdout.log": '{"type":"result","result":"done"}\n',
            "workdir/answer.md": ANSWER,
            "workdir/primes.txt": PRIMES,
        },
    )

    response = listing(client)
    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["artifacts"]}
    assert rows["workdir/answer.md"]["kind"] == "markdown", rows["workdir/answer.md"]
    assert rows["workdir/primes.txt"]["kind"] == "text", rows["workdir/primes.txt"]
    assert rows["workdir/answer.md"]["attempt_id"] == "att_1"

    served = raw(client, "workdir/answer.md")
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("text/plain")
    assert GH_TOKEN not in served.text
    assert served.text == redact(ANSWER).text

    assert raw(client, "workdir/primes.txt").text == PRIMES
