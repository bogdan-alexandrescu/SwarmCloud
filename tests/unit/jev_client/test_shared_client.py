"""The Jev client is one package, `swarm_jev` (apps/jev-client), shared by the
worker and swarm-api (docs/design/agent-quality/jev-contract.md section 13,
lane 1; the owner's answer 10, 3 November 2026: a new shared package, as
swarm-redaction is).

What this holds:

* the worker's `agent_worker.judge.client` and `agent_worker.judge.sanitize`
  hand out the shared package's OWN objects, not copies, so a fix in
  `swarm_jev` is a fix in the worker and the two can never drift;
* `swarm_jev` imports nothing of the worker or of swarm-api, so swarm-api can
  install it without the worker;
* outside the worker, the shared scan still drops what the redaction rules
  and the registered secrets catch; inside it, the worker's publish-gate
  predicate is added, so the worker sends exactly what it sent before;
* every project and image that installs the worker or swarm-api installs
  `swarm-jev` too.

Every fake credential is built at runtime.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import swarm_jev
from swarm_jev import client as shared_client
from swarm_jev import sanitize as shared_sanitize

from agent_worker.judge import budget, settings
from agent_worker.judge import client as worker_client
from agent_worker.judge import sanitize as worker_sanitize

REPO = Path(__file__).resolve().parents[3]

#: Every name the worker's client module offered before the move that a caller
#: or a test reads, public and private alike.
CLIENT_NAMES = (
    "CALL_TIMEOUT_SECONDS", "RETRIES", "BACKOFF_SECONDS", "MAX_STATE_CHARS",
    "MAX_CHOICE_OPTIONS", "MAX_QUESTIONS", "MAX_INSTRUCTIONS_CHARS", "QUESTION_TYPES",
    "Transport", "QuestionError", "Decision", "urllib_transport", "validate_questions",
    "JudgeClient", "_NAME", "_DIRECT", "_opener", "_stderr_log", "_question_strings",
    "_probability", "_numbers_only", "_valid_answer", "_count", "_usage",
)
#: The same for the worker's sanitize module.
SANITIZE_NAMES = (
    "DENIED_NAME_GLOBS", "DENIED_DIRECTORIES", "IDENTITY_KEYS", "NEVER_SENT_KEYS",
    "Sanitized", "is_denied_path", "is_flagged", "sanitize_state", "_PATH_KEYS",
    "_hard_stop_hit", "_Walker",
)


@pytest.mark.parametrize("name", CLIENT_NAMES)
def test_the_worker_client_name_is_the_shared_object(name: str) -> None:
    assert getattr(worker_client, name) is getattr(shared_client, name), name


@pytest.mark.parametrize("name", SANITIZE_NAMES)
def test_the_worker_sanitize_name_is_the_shared_object(name: str) -> None:
    assert getattr(worker_sanitize, name) is getattr(shared_sanitize, name), name


def test_the_shared_client_sanitises_through_the_shared_sanitize() -> None:
    assert shared_client.sanitize is shared_sanitize
    assert worker_client.sanitize is worker_sanitize or worker_client.sanitize is shared_sanitize


def test_the_package_names_its_wire_shape_and_client() -> None:
    for name in ("JudgeClient", "Decision", "QuestionError", "validate_questions",
                 "sanitize_state", "is_flagged", "is_denied_path"):
        assert hasattr(swarm_jev, name), name
    assert swarm_jev.JudgeClient is shared_client.JudgeClient
    assert swarm_jev.sanitize_state is shared_sanitize.sanitize_state


def test_the_token_floor_is_the_worker_budgets() -> None:
    """Restated in the shared package (it cannot import the worker's budget);
    held equal here, as swarm_api.jevaccount's copy is."""
    assert shared_client.MEASURED_TOKENS_PER_CALL == budget.MEASURED_TOKENS_PER_CALL


def test_the_worker_backend_and_ledger_meet_the_shared_shapes(tmp_path: Path) -> None:
    backend = settings.Backend(name="typesafe", url="http://127.0.0.1:1/v1/decide",
                               model="m", stringify_state=False, key=None)
    ledger = budget.Ledger.create(tmp_path / "ledger.json")
    assert isinstance(backend, shared_client.JevBackend)
    assert isinstance(ledger, shared_client.JevLedger)


def test_swarm_jev_imports_nothing_of_the_worker_or_swarm_api() -> None:
    probe = (
        "import sys, swarm_jev, swarm_jev.client, swarm_jev.sanitize\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('agent_worker', 'swarm_api', 'swarm_common'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True, timeout=60)
    assert out.stdout.strip() == "", out.stdout


def test_without_the_worker_the_shared_scan_still_drops_a_credential() -> None:
    """swarm-api's process: no worker, no publish-gate predicate registered."""
    probe = (
        "import json, secrets, sys\n"
        "from swarm_jev import sanitize\n"
        "assert sanitize.credential_checks() == (), sanitize.credential_checks()\n"
        "token = 'ghp_' + secrets.token_hex(18)\n"
        "secret = secrets.token_hex(8)\n"
        "out = sanitize.sanitize_state({'a': 'x = ' + token, 'b': 'keep',\n"
        "                               'c': 'has ' + secret}, secrets=[secret])\n"
        "print(json.dumps([out.state, out.dropped]))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True, timeout=60)
    assert out.stdout.strip() == '[{"b": "keep"}, 2]', out.stdout


def test_the_worker_adds_its_publish_gate_predicate() -> None:
    checks = shared_sanitize.credential_checks()
    assert len(checks) == 1, checks
    # The predicate is the publish gate's own: a string only it refuses (a
    # free run of random key material no redaction rule names) is flagged.
    from agent_worker.lifecycle import _credential_in
    probe = "x = '" + secrets.token_urlsafe(48) + "'"
    assert checks[0](probe) == (_credential_in("", probe) is not None)
    assert checks[0]("plain words") is False


def test_registering_the_same_check_twice_keeps_one() -> None:
    before = shared_sanitize.credential_checks()
    for check in before:
        shared_sanitize.add_credential_check(check)
    assert shared_sanitize.credential_checks() == before


# -- packaging ---------------------------------------------------------------

def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _depends(project: dict, name: str) -> bool:
    return any(re.split(r"[\s\[<>=!~;]", d, maxsplit=1)[0] == name
               for d in project.get("project", {}).get("dependencies", []))


def test_the_package_is_plain_and_depends_only_on_swarm_redaction() -> None:
    project = _pyproject(REPO / "apps/jev-client/pyproject.toml")
    assert project["project"]["name"] == "swarm-jev"
    assert project["project"]["dependencies"] == ["swarm-redaction"]
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["swarm_jev"]


def test_every_project_that_installs_the_worker_or_swarm_api_sources_swarm_jev() -> None:
    projects = [REPO / "pyproject.toml", *sorted((REPO / "apps").glob("*/pyproject.toml"))]
    visited = 0
    for path in projects:
        project = _pyproject(path)
        name = project["project"]["name"]
        if name not in {"swarm-agent-worker", "swarm-api"} and not (
                _depends(project, "swarm-agent-worker") or _depends(project, "swarm-api")):
            continue
        visited += 1
        assert _depends(project, "swarm-jev"), f"{path}: does not depend on swarm-jev"
        source = project["tool"]["uv"]["sources"].get("swarm-jev")
        assert source and source.get("editable") is True, f"{path}: no swarm-jev source"
        assert (path.parent / source["path"]).resolve() == REPO / "apps/jev-client", path
    # the root, swarm-api, the worker and swarm-graph
    assert visited == 4, visited


def test_every_lock_that_holds_the_worker_or_swarm_api_holds_swarm_jev() -> None:
    locks = [REPO / "uv.lock", *sorted((REPO / "apps").glob("*/uv.lock"))]
    visited = 0
    for path in locks:
        names = {p["name"] for p in tomllib.loads(path.read_text())["package"]}
        if not names & {"swarm-agent-worker", "swarm-api"}:
            continue
        visited += 1
        assert "swarm-jev" in names, f"{path}: lock does not hold swarm-jev"
    assert visited >= 4, visited


def test_every_image_that_builds_the_worker_or_swarm_api_builds_swarm_jev() -> None:
    dockerfiles = sorted([*(REPO / "images").glob("*/Dockerfile"),
                          *(REPO / "apps").glob("*/Dockerfile")])
    visited = 0
    for path in dockerfiles:
        text = path.read_text(encoding="utf-8")
        if not re.search(r"uv build .*/src/apps/(agent-worker|swarm-api)\b", text):
            continue
        visited += 1
        assert "COPY apps/jev-client/ /src/apps/jev-client/" in text, path
        assert re.search(r"uv build --wheel --out-dir /wheels /src/apps/jev-client\b", text), path
        install = re.search(r"--no-index --find-links /wheels --no-deps \\\n(.*)\n", text)
        assert install and "swarm-jev" in install.group(1).split(), path
    # agent-runtime-base, swarm-api and swarm-graph
    assert visited == 3, visited
