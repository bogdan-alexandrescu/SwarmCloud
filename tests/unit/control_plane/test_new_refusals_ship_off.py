"""A new refusal in swarm-api ships switched off (observer proposal I).

Owner decision 2026-10-08. On 2026-10-07 #845 deployed 403
REPOSITORY_NOT_GRANTED before #840 (the GitHub App id and slug a person needs
to connect GitHub at all), and every person's submission was refused for about
two hours. `swarm_api.refusals` is the switch; this file is the guard that
makes it the convention rather than an option.

It enumerates every 4xx code swarm-api can raise -- each `ApiError` subclass's
`code`, each `x.code = "..."` override on an instance, and each
`onboarding.COPY` failure code served as `detail.failure_code` -- and fails on
one that is neither ESTABLISHED (on main on 2026-10-08, always enforced) nor
in `refusals.SWITCHES`. The established lists are frozen: a new code goes
behind a switch, it is never appended here. Today's refusals were not
switched off; every one of them is below, unchanged.

Offline: imports and source text only.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pkgutil
from pathlib import Path

import pytest

import swarm_api
from swarm_api import onboarding, refusals
from swarm_api.errors import ApiError, Forbidden
from swarm_api.refusals import Switch

PACKAGE = Path(swarm_api.__file__).resolve().parent

#: Every `ApiError.code` with a 4xx status, and every instance override, on
#: main on 2026-10-08. `forge_unreachable` is served 503; it is here because
#: an instance override's status cannot be read from the source, so every
#: override is held to the list.
ESTABLISHED_CODES = frozenset({
    "LAST_ADMIN",
    "NOT_AN_ADMIN_DOCUMENT",
    "OWNER_FROM_CONFIG",
    "OWNER_PROTECTED",
    # Already behind its own setting, REPOSITORY_GRANTS_ENFORCED (off): see
    # the swarm_api.refusals docstring for why it is not one of SWITCHES.
    "REPOSITORY_NOT_GRANTED",
    "access_refused",
    "artifact_gone",
    "authorisation_refused",
    "auto_merge_unavailable",
    "bad_request",
    "checkpoint_archive_corrupt",
    "checkpoint_scan_budget_exceeded",
    "checks_forbidden",
    "child_depth_exceeded",
    "child_fan_out_exceeded",
    "child_key_taken",
    "child_key_unproven",
    "child_key_window_closed",
    "child_submit_fenced",
    "child_submit_unauthenticated",
    "child_submit_unproven",
    "conflict",
    "forbidden",
    "forge_unreachable",
    "github_not_connected",
    "graph_digest_mismatch",
    "graph_gone",
    "index_digest_mismatch",
    "index_paused",
    "index_unavailable",
    "invalid_dag",
    "invalid_dispatch",
    "invalid_graph",
    "invalid_index",
    "invalid_input",
    "invalid_plan",
    "invalid_run_transition",
    "is_pull_request",
    "no_access",
    "no_forge_credential",
    "no_graph",
    "not_found",
    "not_installed",
    "owner_not_member",
    "plan_changed",
    "rate_limited",
    "run_owner_not_member",
    "tenant_not_member",
    "unauthenticated",
    "validation_failed",
    "writeback_forbidden",
    "writeback_not_found",
    "writeback_unauthorized",
})

#: `onboarding.COPY`'s failure codes on 2026-10-08, served as
#: `detail.failure_code` by AccessRefused and AuthorisationRefused.
ESTABLISHED_FAILURE_CODES = frozenset({
    "AUTHORISATION_DENIED",
    "AUTHORISATION_EXPIRED",
    "CLASSIC_PAT_BLOCKED",
    "FINE_GRAINED_PAT_PENDING",
    "FORGE_UNREACHABLE",
    "ORG_APPROVAL_PENDING",
    "PERMISSION_MISSING",
    "REFRESH_FAILED",
    "REPO_ARCHIVED",
    "REPO_NOT_INSTALLED",
    "SSO_NOT_AUTHORISED",
})


def _import_every_module() -> int:
    count = 0
    for info in pkgutil.walk_packages(swarm_api.__path__, "swarm_api."):
        importlib.import_module(info.name)
        count += 1
    return count


def _class_codes() -> dict[str, str]:
    """4xx code -> the first class that declares it, over every subclass."""
    found: dict[str, str] = {}
    stack: list[type[ApiError]] = [ApiError]
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if not cls.__module__.startswith("swarm_api."):
            continue  # a test's own probe classes, this file's included
        if 400 <= cls.status_code < 500:
            found.setdefault(cls.code, f"{cls.__module__}.{cls.__qualname__}")
    return found


def _override_codes() -> dict[str, str]:
    """`<instance>.code = "literal"` anywhere in the package -> the file it is in."""
    found: dict[str, str] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "code":
                    found.setdefault(node.value.value, str(path.relative_to(PACKAGE)))
    return found


def unlisted(codes: dict[str, str], established: frozenset[str]) -> list[str]:
    """Codes neither established nor switched: what the guard refuses."""
    return sorted(
        f"{code} ({where})" for code, where in codes.items()
        if code not in established and code not in refusals.SWITCHES
    )


@pytest.fixture(scope="module")
def codes() -> dict[str, str]:
    visited = _import_every_module()
    # Rule zero: an enumeration that visited nothing passes vacuously.
    assert visited > 50, f"walked only {visited} swarm_api modules"
    found = _class_codes()
    found.update({k: v for k, v in _override_codes().items() if k not in found})
    return found


def test_every_4xx_code_is_established_or_switched(codes):
    missing = unlisted(codes, ESTABLISHED_CODES)
    assert not missing, (
        "a 4xx code swarm-api can raise is neither established nor behind a switch. "
        "A NEW refusal ships off (observer proposal I): register a Switch for it in "
        "swarm_api.refusals.SWITCHES and raise it through refusals.refuse(). Do not "
        "add it to ESTABLISHED_CODES. Unlisted: " + ", ".join(missing)
    )


def test_every_failure_code_is_established_or_switched():
    found = {code: "onboarding.COPY" for code in onboarding.COPY}
    assert len(found) >= len(ESTABLISHED_FAILURE_CODES)
    missing = unlisted(found, ESTABLISHED_FAILURE_CODES)
    assert not missing, (
        "a failure code is neither established nor behind a switch; register a "
        "Switch in swarm_api.refusals.SWITCHES. Unlisted: " + ", ".join(missing)
    )


def test_the_enumeration_reaches_every_kind_of_declaration(codes):
    """The control: each declaration style is actually seen, so a green guard
    is not a guard that looked at nothing."""
    assert codes["validation_failed"] == "swarm_api.errors.ValidationFailed"
    assert codes["child_key_taken"] == "swarm_api.children.ChildKeyTaken"
    # A constant, not a literal, in the class body.
    assert codes["REPOSITORY_NOT_GRANTED"] == "swarm_api.validation.RepositoryNotGranted"
    # Declared under routes/.
    assert codes["run_owner_not_member"] == "swarm_api.routes.runs.RunOwnerNotMember"
    # An instance override (`refused.code = "not_installed"`).
    assert codes["not_installed"] == "access.py"
    # A 5xx class is not a refusal.
    assert "child_submit_unavailable" not in codes


def test_the_established_lists_name_nothing_that_no_longer_exists(codes):
    """A stale entry is an allow-list slot a new refusal could quietly take."""
    assert sorted(ESTABLISHED_CODES - set(codes)) == []
    assert sorted(ESTABLISHED_FAILURE_CODES - set(onboarding.COPY)) == []


def test_a_switch_is_never_also_established(codes):
    for code, switch in refusals.SWITCHES.items():
        assert switch.code == code
        assert code not in ESTABLISHED_CODES | ESTABLISHED_FAILURE_CODES, code
        assert code in codes or code in onboarding.COPY, f"switch {code} gates nothing"


def test_the_guard_refuses_a_new_unswitched_refusal(codes, monkeypatch):
    """The mutation, committed: a refusal declared the way #845's was, with
    no switch, is caught; registering a switch is what lets it through."""

    class ProbeRefusal(Forbidden):
        code = "probe_new_refusal"

    probe = dict(codes)
    probe[ProbeRefusal.code] = "probe"
    assert unlisted(probe, ESTABLISHED_CODES) == ["probe_new_refusal (probe)"]

    monkeypatch.setitem(refusals.SWITCHES, ProbeRefusal.code,
                        Switch(code=ProbeRefusal.code, why="probe"))
    assert unlisted(probe, ESTABLISHED_CODES) == []


# --------------------------------------------------------------------------
# the switch itself
# --------------------------------------------------------------------------


class _Probe(Forbidden):
    code = "probe_switched"


@pytest.fixture
def switched(monkeypatch):
    switch = Switch(code=_Probe.code, why="probe")
    monkeypatch.setitem(refusals.SWITCHES, _Probe.code, switch)
    return switch


def test_the_env_name_is_derived_from_the_code():
    assert refusals.env_name("probe_switched") == "REFUSAL_PROBE_SWITCHED"
    assert refusals.env_name("REPO_NOT_INSTALLED") == "REFUSAL_REPO_NOT_INSTALLED"


def test_a_new_refusal_is_off_by_default_and_logs_what_it_would_refuse(switched, caplog):
    caplog.set_level(logging.WARNING, logger="swarm_api.refusals")
    refusals.refuse(_Probe("you may not"), environ={})
    assert not refusals.enforced(_Probe.code, environ={})
    record = caplog.records[-1].getMessage()
    assert "report-only" in record
    assert "code=probe_switched" in record
    assert "status=403" in record
    assert "REFUSAL_PROBE_SWITCHED=off" in record


def test_on_raises(switched):
    environ = {switched.env: "on"}
    assert refusals.enforced(_Probe.code, environ=environ)
    with pytest.raises(_Probe):
        refusals.refuse(_Probe("you may not"), environ=environ)


def test_off_lets_the_request_through(switched):
    refusals.refuse(_Probe("you may not"), environ={switched.env: "off"})


def test_a_failure_code_can_be_the_switch(monkeypatch):
    monkeypatch.setitem(refusals.SWITCHES, "PROBE_FAILURE",
                        Switch(code="PROBE_FAILURE", why="probe"))
    error = ApiError("refused", detail={"failure_code": "PROBE_FAILURE"})
    refusals.refuse(error, environ={})
    with pytest.raises(ApiError):
        refusals.refuse(error, environ={"REFUSAL_PROBE_FAILURE": "on"})


def test_an_established_refusal_always_raises():
    assert refusals.enforced("forbidden", environ={})
    with pytest.raises(Forbidden):
        refusals.refuse(Forbidden("no"), environ={"REFUSAL_FORBIDDEN": "off"})


def test_a_misspelt_switch_is_refused_at_start(switched):
    with pytest.raises(ValueError, match="names no refusal switch"):
        refusals.validate_environment({"REFUSAL_PROBE_SWITCHT": "on"})
    with pytest.raises(ValueError, match="must be 'on' or 'off'"):
        refusals.validate_environment({switched.env: "true"})
    refusals.validate_environment({switched.env: "on", "UNRELATED": "x"})


def test_settings_refuse_a_misspelt_switch_at_start(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    ApiSettings.from_env()  # the control: it starts without the variable
    monkeypatch.setenv("REFUSAL_NO_SUCH_REFUSAL", "on")
    with pytest.raises(ValueError, match="REFUSAL_NO_SUCH_REFUSAL"):
        ApiSettings.from_env()
