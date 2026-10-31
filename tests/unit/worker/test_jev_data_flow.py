"""docs/jev-data-flow.md: every §5.4 rule names the code and the test that hold it (#1336).

The record is what the security pass (#476) signs off before Jev is turned on
for a workspace beyond the owner's. A record that cites a function renamed
away, or a test deleted, would read as enforced when nothing is. So:

* the guard parses the record's rule table, and every `path::Symbol` cited in
  it must resolve -- a function, class or assigned name in that file, found by
  `ast`, so a rename fails here instead of leaving the record quietly wrong;
* every rule row cites at least one enforcing symbol and one test, and the six
  rules of context-quality.md §5.4 are all there;
* the rules that had no test failing on their removal get one below. Each says
  which removal turns it red, and each carries the control that shows the
  removal is the only thing it could be catching.

Every fake credential is built at runtime (`secrets.token_hex`, a repeated
pattern, random bytes): a literal would be refused by the worker's publish scan.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import secrets
from pathlib import Path

import pytest

from agent_worker.judge import budget, client, poststeps, register, sanitize, settings, shapes

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "jev-data-flow.md"
DESIGN = REPO / "docs" / "design" / "context-quality.md"

#: `apps/.../x.py::Name.attr` or `tests/.../test_x.py::test_name`.
_CITE = re.compile(r"`((?:apps|tests)/[\w./-]+\.py)::([\w.]+)`")
#: The §5.4 rules, by the words each row's rule cell must hold.
RULES = {
    "1": "path deny",
    "2": "credential scan",
    "3": "swarm_redaction.rules.redact",
    "4": "scrub_text",
    "5": "never sent",
    "6": "audit",
}


def _rule_rows() -> list[list[str]]:
    """The rows of the table under `## The rules`, as cells, header excluded."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("\n## The rules\n")
    rest = text[start + len("\n## The rules\n"):]
    section = rest.split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert len(rows) > 2, "the rules section has no table"
    header, divider, *body = rows
    assert re.fullmatch(r"\|(\s*:?-+:?\s*\|)+", divider), divider
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in body]


def _defines(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def _resolves(path: str, qualname: str) -> bool:
    file = REPO / path
    if not file.is_file():
        return False
    scope: ast.AST | None = ast.parse(file.read_text(encoding="utf-8"), filename=path)
    for part in qualname.split("."):
        scope = next((n for n in ast.iter_child_nodes(scope) if _defines(n, part)), None)
        if scope is None:
            return False
    return True


def test_the_record_has_one_row_per_section_5_4_rule() -> None:
    rows = _rule_rows()
    by_rule: dict[str, list[list[str]]] = {}
    for row in rows:
        assert len(row) == 5, f"a rule row has 5 cells (rule, says, enforced by, test, audit): {row}"
        number = re.match(r"\d+", row[0])
        assert number, f"a rule row starts with its §5.4 number: {row[0]}"
        by_rule.setdefault(number.group(), []).append(row)
    assert sorted(by_rule) == sorted(RULES), sorted(by_rule)
    for number, words in RULES.items():
        said = " ".join(r[1].lower() for r in by_rule[number])
        assert words in said, f"rule {number} does not say {words!r}: {said[:200]}"


def test_every_rule_row_cites_an_enforcing_symbol_and_a_test() -> None:
    for row in _rule_rows():
        enforced = _CITE.findall(row[2])
        tests = _CITE.findall(row[3])
        assert enforced and all(p.startswith("apps/") for p, _ in enforced), row[:3]
        assert tests and all(p.startswith("tests/unit/") for p, _ in tests), (row[0], row[3])
        assert all(q.startswith("test_") for _, q in tests), row[3]


def test_every_cited_symbol_and_test_exists() -> None:
    cites = _CITE.findall(DOC.read_text(encoding="utf-8"))
    missing = [f"{p}::{q}" for p, q in cites if not _resolves(p, q)]
    assert not missing, f"docs/jev-data-flow.md cites what does not exist: {missing}"


def test_the_guard_reads_real_cites_and_refuses_a_dead_one() -> None:
    """Not an empty sweep: the table is found and its cites are read, and a
    cite to a renamed function would fail."""
    cites = _CITE.findall(DOC.read_text(encoding="utf-8"))
    assert len(cites) >= 20, cites
    assert ("apps/agent-worker/agent_worker/judge/sanitize.py", "is_denied_path") in cites
    assert _resolves("apps/agent-worker/agent_worker/judge/sanitize.py", "NEVER_SENT_KEYS")
    assert _resolves("apps/agent-worker/agent_worker/judge/client.py", "JudgeClient.decide")
    assert not _resolves("apps/agent-worker/agent_worker/judge/sanitize.py", "is_denied_paths")
    assert not _resolves("tests/unit/worker/test_judge_sanitize.py", "test_no_such_test")


def test_the_record_states_where_jev_runs_its_key_its_egress_and_the_url_gap() -> None:
    text = DOC.read_text(encoding="utf-8")
    for heading in ("## Where Jev runs", "## Which key", "## Which egress path",
                    "## What each audit line records", "## Known gaps"):
        assert f"\n{heading}" in text, heading
    gaps = text.split("\n## Known gaps", 1)[1]
    assert "SWARM_JUDGE_INTERNAL_URL" in gaps and "no_internal_url" in gaps


LAUNCHER = REPO / "images" / "agent-runtime-base" / "jev-gates" / "bin" / "swarm-jev-gate.mjs"


def test_the_record_states_what_the_lane_hooks_send_and_where_their_audit_lands() -> None:
    """JEV-LANES: what each hook sends, the redaction, the audit and the gap.
    Red when the section, a cited symbol or the launcher's named function goes."""
    text = DOC.read_text(encoding="utf-8")
    assert "\n## The lane hooks" in text
    hooks = text.split("\n## The lane hooks", 1)[1].split("\n## ", 1)[0]
    # What each hook sends.
    for sent in ("the final reply", "edited file's written text", "the rule's text",
                 "quality_gates.jev", "JEV_ENFORCE_MODE=end"):
        assert sent in hooks, sent
    # The redaction: the launcher's scrub of the registered secrets and the key.
    assert "secret_env_names" in hooks and "<redacted>" in hooks
    launcher = LAUNCHER.read_text(encoding="utf-8")
    for name in ("guardedFetch", "scrub"):
        assert f"`{name}`" in hooks, name
        assert re.search(rf"export function {name}\(", launcher), name
    assert 'export const REDACTED = "<redacted>"' in launcher
    # Where the audit lands: counts in the summary and one RUNNING event.
    assert "private/jev-gates.counts" in hooks and "`quality_gate`" in hooks
    cites = _CITE.findall(hooks)
    for path, name in (("apps/agent-worker/agent_worker/qualitygates.py", "prepare"),
                       ("apps/agent-worker/agent_worker/qualitygates.py", "finish"),
                       ("apps/agent-worker/agent_worker/lifecycle.py", "Worker._quality_gates_env"),
                       ("apps/agent-worker/agent_worker/lifecycle.py", "Worker._record_quality_gates"),
                       ("tests/unit/worker/test_jev_data_flow.py",
                        "test_the_record_states_what_the_lane_hooks_send_and_where_their_audit_lands")):
        assert (path, name) in cites, (path, name)
    for path, name in cites:
        assert _resolves(path, name), f"{path}::{name}"
    # The gap is said, not hidden.
    gaps = text.split("\n## Known gaps", 1)[1]
    assert "lane hooks get only the registered-secret scrub" in gaps


def test_context_quality_links_the_record() -> None:
    assert "](../jev-data-flow.md)" in DESIGN.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rules with no test that failed on their removal
# ---------------------------------------------------------------------------


def _token() -> str:
    return "ghp_" + "A1b2C3d4E5" * 4  # the classic PAT shape, 44 characters


def test_the_credential_scan_alone_drops_a_headerless_key() -> None:
    """Rule 2. Red when `_credential_in` leaves `sanitize.is_flagged`: random
    key material has no prefix any redaction rule knows (the control)."""
    from swarm_redaction.rules import redact

    # Random base64 occasionally holds a run some redaction rule matches (CI,
    # run 39388236975), so draw until one does not: the control is that the
    # blob passed below is one the redaction rules leave alone.
    blobs = ("blob = " + base64.b64encode(os.urandom(512)).decode() for _ in range(50))
    blob = next((b for b in blobs if redact(b).count == 0), None)
    assert blob is not None, "every random blob matched a redaction rule"
    out = sanitize.sanitize_state({"candidates": [{"i": 0, "path": "a.py", "excerpt": blob}]})
    assert out.state == {"candidates": [{"i": 0, "path": "a.py"}]} and out.dropped == 1


def test_the_redaction_rules_alone_drop_a_token_glued_to_a_word() -> None:
    """Rule 3. Red when `swarm_redaction.rules.redact` leaves `is_flagged`: the
    publish scan wants a match to start a token, so a token glued to a word
    is the redaction rules' alone (the control)."""
    from agent_worker.lifecycle import _credential_in

    glued = "x" + _token()
    assert _credential_in("", glued) is None
    out = sanitize.sanitize_state({"task": {"type": "fix", "brief": f"see {glued}"}})
    assert out.state == {"task": {"type": "fix"}} and out.dropped == 1


#: Rule 5, spelled out here rather than read from NEVER_SENT_KEYS, so a key
#: removed from that set turns this red.
NEVER_SENT = ("author", "authors", "email", "emails", "user", "login",
              "env", "environ", "environment", "transcript")


@pytest.mark.parametrize("key", NEVER_SENT)
def test_every_never_sent_key_is_removed_at_any_depth_and_any_case(key: str) -> None:
    value = secrets.token_hex(8)
    state = {"issue": {"title": "t", key: value, "rows": [{key.upper(): value, "i": 1}]}}
    out = sanitize.sanitize_state(state)
    assert out.state == {"issue": {"title": "t", "rows": [{"i": 1}]}}
    assert value not in json.dumps(out.state)


def test_j5_sends_an_in_flight_lane_as_title_and_files_only() -> None:
    """Rule 5 (in-flight beyond titles and paths). Red when `j5_duplicate`
    copies a lane's other fields."""
    lane = {"title": "lane t", "files": ["a.py"], "body": "the lane's brief",
            "prompt": "do it", "branch": "swarm/x", "diff": "+x", "ref": "PR 1"}
    state, _ = shapes.j5_duplicate("issue", "body", [lane])
    assert state["in_flight"] == [{"i": 0, "title": "lane t", "files": ["a.py"]}]


def test_j3_sends_a_file_outside_the_checkout_by_path_only(tmp_path: Path) -> None:
    """Rule 5 (another repository's content). Red when `code_window` reads a
    path that resolves outside the attempt's checkout; a file inside it is
    read (the control)."""
    checkout = tmp_path / "repo"
    other = tmp_path / "other-repo"
    checkout.mkdir()
    other.mkdir()
    (other / "x.py").write_text("one\ntwo\nthree\n")
    (checkout / "y.py").write_text("one\ntwo\nthree\n")
    assert poststeps.code_window(checkout, "../other-repo/x.py", 2) == {"path": "../other-repo/x.py"}
    assert "two" in poststeps.code_window(checkout, "y.py", 2)["window"]


def test_the_runner_client_drops_the_runners_own_key(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 4 in the runner. Red when `cliagent._jev_client` stops adding the
    runner's credential variables to the client's registered secrets: the
    config `attempt.setup` writes names none (the control)."""
    from agent_worker.judge import attempt
    from agent_worker.runners import claude_code, cliagent

    done = register.prepare(attempt_dir=tmp_path, settings=settings.JudgeSettings(code_opt_in=True),
                            key=None, environ={settings.INTERNAL_URL_ENV: "http://decide.internal/v1"})
    assert done.registered and done.config_path is not None
    assert json.loads(done.config_path.read_text())["secret_env_names"] == []
    own = "k" + secrets.token_hex(24)
    monkeypatch.setenv(attempt.CONFIG_ENV, str(done.config_path))
    monkeypatch.setenv(claude_code.SPEC.key_env, own)
    jev = cliagent._jev_client(claude_code.SPEC)
    assert jev is not None and own in jev.secrets
    out = sanitize.sanitize_state({"task": {"brief": f"use {own}"}}, secrets=jev.secrets)
    assert out.state == {"task": {}}


def _typesafe(tmp_path: Path, key: str, transport: client.Transport) -> client.JudgeClient:
    chosen = settings.resolve_backend(
        settings.JudgeSettings(code_opt_in=True, backend=settings.TYPESAFE), key=key)
    assert isinstance(chosen, settings.Backend)
    return client.JudgeClient(chosen, budget.Ledger.create(tmp_path / "ledger.json"),
                              transport=transport, sleep=lambda s: None,
                              log=lambda *a, **k: None)


QUESTION = {"q": {"type": "noul", "instructions": "Is `x` relevant?"}}
AUDIT_KEYS = {"call_id", "kind", "backend", "bytes", "input_tokens", "ms", "outcome"}


@pytest.mark.parametrize("status", [200, 503])
def test_the_audit_row_holds_counts_never_the_payload_the_answer_or_the_key(
        tmp_path: Path, status: int) -> None:
    """Rule 6, on a success and on a failure. Red when `decide` puts the
    state, the answer or the bearer into the ledger's row."""
    key = "k-" + secrets.token_hex(20)
    marker = "payload-" + secrets.token_hex(6)
    answer = "answer-" + secrets.token_hex(6)
    sent: list[bytes] = []

    def transport(url, body, headers, timeout):
        sent.append(body)
        return status, json.dumps({"answers": {"q": {"noul": 0.5}}, "usage": {"input_tokens": 7},
                                   "note": answer}).encode()

    jev = _typesafe(tmp_path, key, transport)
    jev.decide("J1", {"task": {"brief": marker}}, QUESTION)
    assert sent and marker.encode() in sent[0]  # the control: it was sent
    raw = (tmp_path / "ledger.json").read_text()
    assert marker not in raw and answer not in raw and key not in raw
    (row,) = json.loads(raw)["audit"]
    assert set(row) == AUDIT_KEYS
    assert row["outcome"] == ("ok" if status == 200 else "failed")
    assert row["bytes"] == len(sent[0])
