"""Onboarding's migration lane, OB10 (#780): the switch is deployed and the docs say what is built.

WHY THIS EXISTS. `REPOSITORY_GRANTS_ENFORCED` (swarm_api.settings
`repository_grants_enforced`) decides whether a PERSON's task on a repository
they never chose is refused (403 REPOSITORY_NOT_GRANTED) or quietly runs with
the tenant token. Until this lane it reached swarm-api only by hand: nothing in
terraform set it, so the next apply would have put it back to off, and the
tenant token would have gone on standing in for every unconnected person
without anyone seeing it. Here terraform renders it from a variable, and dev's
tfvars turns it on.

The docs half: docs/onboarding.md said "Nothing described here is built" after
lanes OB0-OB9 had shipped, so a reader of the design could not tell which
routes exist. And the issue's four acceptance checks are live checks on two
GitHub orgs that only the owner can run, so they are a runbook, held here to
the commands, routes and codes they name: a runbook step that names a verb
`sc` no longer has is the 3am failure CLAUDE.md warns about.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _assert_cites_resolve

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
ONBOARDING = DOCS / "onboarding.md"
GIT_TOKENS = DOCS / "git-tokens.md"
REPO_INDEX = DOCS / "repo-index.md"
REFUSALS_DOC = DOCS / "api-refusals.md"
RUNBOOK = DOCS / "runbooks" / "onboarding-acceptance.md"
LOCALS = REPO / "terraform" / "infra" / "locals.tf"
VARIABLES = REPO / "terraform" / "infra" / "variables.tf"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"
PROD_TFVARS = REPO / "terraform" / "environments" / "prod" / "prod.tfvars"
SETTINGS = REPO / "apps" / "swarm-api" / "swarm_api" / "settings.py"
CODEC = REPO / "apps" / "swarm-api" / "swarm_api" / "codec.py"

ENV_NAME = "REPOSITORY_GRANTS_ENFORCED"
VAR_NAME = "repository_grants_enforced"

#: The issue's "How would you know it worked?", one check each, in its order.
ACCEPTANCE_CHECKS = (
    "Check 1. A new user clones, pushes and opens a pull request as themselves in two orgs",
    "Check 2. Every step shows its verified state",
    "Check 3. A repository the user did not choose is refused",
    "Check 4. Removing an org revokes SwarmCloud's access to it",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """The body under the heading line that starts `heading`, up to the next
    heading of the same or a higher level."""
    match = re.search(rf"^(#+) {re.escape(heading)}.*$", text, flags=re.M)
    assert match, f"no heading {heading!r}"
    level = len(match.group(1))
    rest = text[match.end():]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _uncommented(path: Path) -> str:
    """The HCL with `#` comments removed, so a commented-out line never counts."""
    return "\n".join(line.split("#", 1)[0] for line in _text(path).splitlines())


def _tfvar(path: Path, name: str) -> str | None:
    match = re.search(rf"^\s*{name}\s*=\s*(\S+)\s*$", _uncommented(path), flags=re.M)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# (1) the switch goes through terraform
# --------------------------------------------------------------------------


def test_swarm_api_reads_the_name_terraform_sets():
    assert f'_bool("{ENV_NAME}", False)' in _text(SETTINGS), \
        "swarm_api.settings reads REPOSITORY_GRANTS_ENFORCED; the name below must be the same one"


def test_the_variable_is_a_bool_that_defaults_off():
    body = re.search(rf'^variable "{VAR_NAME}" \{{(.*?)^\}}', _text(VARIABLES), flags=re.M | re.S)
    assert body, f"terraform/infra/variables.tf declares var.{VAR_NAME}"
    assert re.search(r"^\s*type\s*=\s*bool\s*$", body.group(1), flags=re.M)
    assert re.search(r"^\s*default\s*=\s*false\s*$", body.group(1), flags=re.M), \
        "off unless an environment's tfvars turns it on, as settings.py's own default"
    assert "description" in body.group(1)


def test_locals_renders_the_switch_into_swarm_apis_environment_only():
    text = _uncommented(LOCALS)
    rendered = re.findall(rf"^\s*{ENV_NAME}\s*=\s*(.+?)\s*$", text, flags=re.M)
    assert rendered == [f'var.{VAR_NAME} ? "true" : "false"'], \
        f"one rendering of {ENV_NAME}, from var.{VAR_NAME}, as the strings settings._bool reads"
    start = text.index('"swarm-api" = merge(')
    end = text.index('"swarm-scheduler" = merge(')
    assert start < text.index(f"{ENV_NAME} =") < end, "it is in swarm-api's block of local.service_env"


def test_dev_turns_grant_enforcement_on_and_says_why():
    assert _tfvar(DEV_TFVARS, VAR_NAME) == "true"
    lines = _text(DEV_TFVARS).splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith(f"{VAR_NAME} "))
    comment = " ".join(lines[max(0, at - 15):at])
    assert "#780" in comment and "service submission" in comment, \
        "the comment says why: people refused without a grant, service submissions keep the tenant token"


def test_prod_does_not_turn_it_on_before_the_owner_says_so():
    # prod.tfvars registers no GitHub App, so no person there can connect and
    # choose a repository: on, it would refuse every person's task. It turns
    # on only by the owner's answer (questions.json of the OB10 lane).
    assert _tfvar(PROD_TFVARS, VAR_NAME) in (None, "false")


def test_the_settings_comment_points_at_the_tfvars():
    flat = _flat(_text(SETTINGS))
    assert f"var.{VAR_NAME}" in flat, "settings.py says where the deployed value comes from"


# --------------------------------------------------------------------------
# (2) the docs say what is built
# --------------------------------------------------------------------------


def test_onboarding_no_longer_says_nothing_is_built():
    text = _text(ONBOARDING)
    assert "Nothing described here is built" not in text
    head = _flat("\n".join(text.splitlines()[:16]))
    assert "BUILT" in head and "#780" in head


@pytest.mark.parametrize(
    "built",
    ["POST /v1/onboarding/github/token", "POST /v1/onboarding/dismiss", "push test"],
)
def test_onboarding_names_the_last_pieces_as_built(built):
    head = _flat(_text(ONBOARDING).split("\n## 0. ")[0])
    assert built in head, f"the status names {built!r} as built"


def test_every_lane_of_the_build_plan_is_marked_built_with_its_pull_request():
    plan = _section(_text(ONBOARDING), "5. Build plan")
    rows = re.findall(r"^\| (OB\d+[a-z]?) \| \d \| ([^|]*) \|", plan, flags=re.M)
    lanes = [lane for lane, _ in rows]
    assert lanes[:2] == ["OB0", "OB0b"] and lanes[-1] == "OB10" and len(lanes) == 12
    for lane, builds in rows:
        if lane == "OB10":
            assert builds.startswith("**built, this lane**"), "OB10 is this change"
            continue
        assert re.match(r"\*\*built, #\d+(, #\d+)*\*\*", builds), f"{lane} is not marked built with its PR"


@pytest.mark.parametrize(
    "cited",
    [
        "apps/swarm-api/swarm_api/forgeapp.py::ForgeApp.store_owner_token",
        "apps/swarm-api/swarm_api/gittokens.py::owner_suffix",
        "apps/swarm-api/swarm_api/access.py::AccessService._push_test",
        "apps/swarm-api/swarm_api/access.py::AccessService.request_install",
        "apps/swarm-api/swarm_api/onboarding.py::dismiss",
    ],
)
def test_onboarding_points_at_the_code_for_each_late_piece(cited):
    text = _text(ONBOARDING)
    today = _section(text, "0. ")
    api = _section(text, "3. Data model")
    assert f"`{cited}`" in today or f"`{cited}`" in api, f"§0 or §3 cites {cited}"


@pytest.mark.parametrize("doc", [ONBOARDING, GIT_TOKENS, REPO_INDEX, REFUSALS_DOC, RUNBOOK],
                         ids=lambda p: p.name)
def test_every_citation_in_the_touched_docs_resolves(doc):
    _assert_cites_resolve(doc)


def test_the_service_submission_words_are_the_ones_the_api_serves():
    words = "tenant token, service submission"
    assert f'return "{words}"' in _text(CODEC)
    for doc in (GIT_TOKENS, REPO_INDEX):
        assert f"as {words}" in _flat(_text(doc)), f"{doc.name} quotes what a task says"


@pytest.mark.parametrize("doc", [GIT_TOKENS, REPO_INDEX], ids=lambda p: p.name)
def test_the_token_docs_describe_per_person_slots_and_the_migration(doc):
    flat = _flat(_text(doc))
    for needle in (
        "git-u-",                      # the per-person slots
        "owner_suffix",                # the per-owner fallback slot
        ENV_NAME,                      # people refused once enforced
        "REPOSITORY_NOT_GRANTED",
        "retire the tenant token",
    ):
        assert needle in flat, f"{doc.name}: {needle}"


def test_the_refusals_doc_says_the_switch_is_set_from_tfvars():
    flat = _flat(_section(_text(REFUSALS_DOC), "The one refusal that predates this"))
    assert f"var.{VAR_NAME}" in flat and "tfvars" in flat
    assert "dev" in flat


# --------------------------------------------------------------------------
# (3) the issue's four acceptance checks, as an owner-run runbook
# --------------------------------------------------------------------------


def test_the_runbook_lists_the_four_acceptance_checks_from_780():
    text = _text(RUNBOOK)
    assert "#780" in text
    at = [text.index(f"## {check}") for check in ACCEPTANCE_CHECKS]
    assert at == sorted(at), "in the issue's order"


@pytest.mark.parametrize("check", ACCEPTANCE_CHECKS, ids=lambda c: c.split(".")[0])
def test_each_check_names_the_observation_that_would_show_it_failed(check):
    body = _section(_text(RUNBOOK), check)
    assert "**It failed if**" in body, f"{check}: no failing observation"


def test_check_1_covers_both_surfaces_and_both_orgs():
    body = _flat(_section(_text(RUNBOOK), ACCEPTANCE_CHECKS[0]))
    for needle in ("console", "/sc:setup", "sagaxyz", "personal account", "clone", "push",
                   "pull request", "uv run sc setup token --owner sagaxyz", "--push-test"):
        assert needle in body, needle


def test_check_3_and_4_name_the_refusal_and_the_removal():
    refused = _flat(_section(_text(RUNBOOK), ACCEPTANCE_CHECKS[2]))
    assert "REPOSITORY_NOT_GRANTED" in refused and ENV_NAME in refused
    removed = _flat(_section(_text(RUNBOOK), ACCEPTANCE_CHECKS[3]))
    assert "uv run sc access remove-org sagaxyz" in removed
    assert "DELETE /v1/access/orgs/{owner}" in removed


def test_every_sc_command_the_runbook_names_parses():
    from swarm_mcp import sc

    parser = sc.build_parser()
    commands = re.findall(r"uv run sc ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", _text(RUNBOOK))
    assert len(commands) >= 6, f"the runbook drives sc: found {commands}"
    for command in sorted(set(commands)):
        words = command.split()
        # `--help` exits 0 on a command argparse knows and 2 on an unknown
        # verb, before any handler or network call runs.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                parser.parse_args(words + ["--help"])
        except SystemExit as exit_:
            assert exit_.code == 0, f"`sc {command}` is not a command sc parses"
