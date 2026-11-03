"""Versioned work contracts (`swarm_api.workcontract`), the agent quality program.

Canonical serialization, the compiler into today's step fields, the signed
reference in `metadata.dispatch.work_contract`, tamper detection, the
amendment protocol, refusal of a strict contract no worker enforces, and the
compatibility matrix: a spec without a contract stores and signs exactly what
it did before. No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone

import pytest

from swarm_common import specsign
from swarm_common.models import Task, TaskState

from swarm_api import workcontract as wc
from swarm_api.validation import DISPATCH_METADATA_KEY, DispatchOptions

from .spec_signer import LocalSpecSigner

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def _doc(**over):
    doc = {
        "schema": 1,
        "contract_id": "wc-login-flow",
        "tenant_id": "acme",
        "revision": 1,
        "previous_digest": None,
        "strictness": "advisory",
        "requirements": [
            {"id": "REQ-1", "text": "The login page accepts SSO.", "verification": ["unit_tests"]},
            {"id": "REQ-2", "text": "Docs say how.", "verification": ["review", "ci"]},
        ],
        "repositories": [{"repository_url": "https://github.com/acme/app", "ref": "main",
                          "commit": COMMIT}],
        "scope": {"read": ["src/**", "docs/**"], "write": ["src/login/**"]},
        "protected": {"paths": ["apps/common/**"], "interfaces": ["swarm_common.models.Task"]},
        "capabilities": {
            "runtime": ["screenshots", "dom_automation"],
            "plugins": {"gstack": {"mode": "unattended", "skills": ["review", "qa"]}},
        },
        "limits": {"runner_profile": "claude-code", "model_class": "sonnet",
                   "budget": {"usd": 5}, "max_turns": 80, "timeout_seconds": 3600},
        "artifacts": [{"kind": "pull_request"}, {"kind": "file", "path": "docs/login.md"}],
        "ownership": {"owner": "owner@example.com", "depends_on": ["wc-sso-config"]},
    }
    doc.update(over)
    return doc


def _strict_enforceable(**over):
    doc = _doc(strictness="strict", scope={}, protected={}, capabilities={}, artifacts=[])
    for r in doc["requirements"]:
        r["verification"] = []
    doc.update(over)
    return doc


# --- schema ------------------------------------------------------------------


def test_a_full_contract_decodes():
    c = wc.decode(_doc())
    assert c.requirement_ids() == ("REQ-1", "REQ-2")
    assert c.runtime_capabilities == ("dom_automation", "screenshots")
    assert c.plugins == {"gstack": {"mode": "unattended", "skills": ("qa", "review")}}


@pytest.mark.parametrize("mutate, field", [
    (lambda d: d.update(extra=1), "contract"),
    (lambda d: d["limits"].update(image="evil:latest"), "limits"),
    (lambda d: d["requirements"].append(dict(d["requirements"][0])), "requirements[2]"),
    (lambda d: d["requirements"][0].update(id="req one"), "requirements[0]"),
    (lambda d: d.update(requirements=[]), "requirements"),
    (lambda d: d["capabilities"]["plugins"].update(other={"mode": "planner", "skills": ["x"]}),
     "capabilities.plugins.other"),
    (lambda d: d["capabilities"]["plugins"]["gstack"].update(skills=["careful"]),
     "capabilities.plugins.gstack.skills"),
    (lambda d: d["capabilities"]["plugins"]["gstack"].update(mode="planner"),
     "capabilities.plugins.gstack.skills"),
    (lambda d: d["capabilities"].update(runtime=["root_shell"]), "capabilities.runtime"),
    (lambda d: d["scope"].update(write=["../etc/**"]), "scope.write"),
    (lambda d: d["scope"].update(write=["/abs"]), "scope.write"),
    (lambda d: d["repositories"][0].update(commit="abc"), "repositories[0]"),
    (lambda d: d["repositories"][0].update(repository_url="http://x/y"), "repositories[0]"),
    (lambda d: d["requirements"][0].update(verification=["run: make test"]),
     "requirements[0].verification"),
    (lambda d: d.update(revision=2), "previous_digest"),
    (lambda d: d.update(strictness="lenient"), "strictness"),
    (lambda d: d["ownership"].update(withdrawn=["REQ-1"]), "requirements"),
])
def test_a_malformed_contract_is_refused_with_the_field(mutate, field):
    doc = _doc()
    mutate(doc)
    with pytest.raises(wc.ContractInvalid) as raised:
        wc.decode(doc)
    assert raised.value.detail["field"] == field


def test_every_runtime_capability_the_routing_package_needs_is_named():
    assert set(wc.RUNTIME_CAPABILITIES) == {
        "dom_automation", "javascript_execution", "pixel_rendering", "screenshots",
        "accessibility_tree", "persistent_session", "human_takeover",
        "checkpoint_support", "write_isolation",
    }


def test_the_catalogue_never_offers_a_skill_the_plan_excludes():
    offered = {s for modes in wc.PLUGIN_CATALOGUE.values() for skills in modes.values()
               for s in skills}
    assert not offered & {"careful", "guard", "gstack-upgrade", "land-and-deploy", "canary",
                          "pair-agent", "connect-chrome", "setup-browser-cookies", "plan-tune"}


# --- canonical serialization -------------------------------------------------


def test_the_digest_is_independent_of_key_and_set_order():
    a = _doc()
    b = json.loads(json.dumps(a), object_pairs_hook=lambda kv: dict(reversed(kv)))
    b["scope"]["read"].reverse()
    b["capabilities"]["runtime"].reverse()
    b["capabilities"]["plugins"]["gstack"]["skills"].reverse()
    b["artifacts"].reverse()
    assert wc.digest(wc.decode(a)) == wc.digest(wc.decode(b))


def test_requirement_order_is_in_the_digest():
    a = _doc()
    b = copy.deepcopy(a)
    b["requirements"].reverse()
    assert wc.digest(wc.decode(a)) != wc.digest(wc.decode(b))


def test_the_canonical_bytes_are_jcs_with_the_purpose_and_round_trip():
    c = wc.decode(_doc())
    raw = wc.canonical_bytes(c)
    assert raw == specsign.jcs({"purpose": wc.CONTRACT_PURPOSE, "contract": c.to_document()})
    assert wc.digest(c) == hashlib.sha256(raw).hexdigest()
    assert wc.decode(c.to_document()) == c
    assert wc.digest(wc.decode(c.to_document())) == wc.digest(c)


def test_no_contract_refusal_is_an_api_error_until_a_route_switches_it():
    """A new refusal ships off (swarm_api.refusals): these are domain errors
    until the wiring lane maps them through refusals.refuse()."""
    from swarm_api.errors import ApiError

    for cls in (wc.ContractInvalid, wc.ContractNotEnforceable, wc.AmendmentRefused,
                wc.ScopeWideningRefused):
        assert issubclass(cls, wc.WorkContractError) and not issubclass(cls, ApiError)


def test_the_purpose_separates_a_contract_from_a_step_spec():
    assert wc.CONTRACT_PURPOSE not in (specsign.SPEC_PURPOSE, specsign.SETTINGS_PURPOSE)


def test_a_pinned_contract_keeps_its_pinned_digest():
    """A change to the encoding or the projection is a new schema, never an
    edit: this pins schema 1's bytes for a minimal contract."""
    c = wc.decode({
        "schema": 1, "contract_id": "wc-pin", "tenant_id": "t", "revision": 1,
        "strictness": "strict", "requirements": [{"id": "REQ-1", "text": "x"}],
        "limits": {"runner_profile": "claude-code"}, "ownership": {"owner": "o@example.com"},
    })
    assert wc.canonical_bytes(c) == (
        b'{"contract":{"artifacts":[],"capabilities":{"plugins":{},"runtime":[]},'
        b'"contract_id":"wc-pin","limits":{"budget":null,"max_turns":null,"model_class":null,'
        b'"runner_profile":"claude-code","timeout_seconds":null},"ownership":{"depends_on":[],'
        b'"owner":"o@example.com","withdrawn":[]},"previous_digest":null,'
        b'"protected":{"interfaces":[],"paths":[]},"repositories":[],'
        b'"requirements":[{"id":"REQ-1","text":"x","verification":[]}],"revision":1,'
        b'"schema":1,"scope":{"read":[],"write":[]},"strictness":"strict","tenant_id":"t"},'
        b'"purpose":"swarm.work-contract/v1"}'
    )


# --- compilation -------------------------------------------------------------


def test_an_advisory_contract_compiles_into_todays_step_fields():
    compiled = wc.compile_contract(wc.decode(_doc()), step_id="build")
    assert compiled.step == {
        "step_id": "build", "runner_profile": "claude-code", "model_class": "sonnet",
        "budget": {"usd": 5}, "max_turns": 80, "timeout_seconds": 3600,
        "repository_url": "https://github.com/acme/app", "repository_ref": COMMIT,
    }
    assert compiled.advisory == ("artifacts", "plugins", "protected", "read_scope",
                                 "runtime_capabilities", "verification", "write_scope")
    assert "REQ-1: The login page accepts SSO." in compiled.brief
    assert "Advisory, not enforced by the platform" in compiled.brief


def test_the_reference_names_the_exact_revision():
    c = wc.decode(_doc())
    ref = wc.compile_contract(c, step_id="s").reference
    assert ref == {"id": "wc-login-flow", "revision": 1, "digest": wc.digest(c),
                   "strictness": "advisory", "requirements": ["REQ-1", "REQ-2"]}
    assert wc.verify_reference(c, ref)


@pytest.mark.parametrize("component, over", [
    ("write_scope", {"scope": {"write": ["src/**"]}}),
    ("read_scope", {"scope": {"read": ["src/**"]}}),
    ("protected", {"protected": {"paths": ["apps/common/**"]}}),
    ("runtime_capabilities", {"capabilities": {"runtime": ["screenshots"]}}),
    ("plugins", {"capabilities": {"plugins": {"gstack": {"mode": "planner",
                                                         "skills": ["spec"]}}}}),
    ("artifacts", {"artifacts": [{"kind": "pull_request"}]}),
])
def test_a_strict_contract_a_worker_cannot_enforce_is_refused(component, over):
    contract = wc.decode(_strict_enforceable(**over))
    with pytest.raises(wc.ContractNotEnforceable) as raised:
        wc.compile_contract(contract, step_id="s")
    assert raised.value.detail["unenforced"] == [component]
    assert raised.value.code == "work_contract_not_enforceable"


def test_a_strict_contract_with_verification_is_refused_not_degraded():
    doc = _strict_enforceable()
    doc["requirements"][0]["verification"] = ["ci"]
    with pytest.raises(wc.ContractNotEnforceable):
        wc.compile_contract(wc.decode(doc), step_id="s")


def test_a_strict_contract_of_enforced_components_compiles_with_nothing_advisory():
    compiled = wc.compile_contract(wc.decode(_strict_enforceable()), step_id="s")
    assert compiled.advisory == ()
    assert compiled.reference["strictness"] == "strict"


def test_a_strict_contract_compiles_once_a_worker_enforces_the_component():
    contract = wc.decode(_strict_enforceable(scope={"write": ["src/**"]}))
    enforced = wc.WORKER_ENFORCED_TODAY | {wc.COMPONENT_WRITE_SCOPE}
    assert wc.compile_contract(contract, step_id="s", enforced=enforced).advisory == ()


def test_a_step_binds_one_repository():
    doc = _doc(repositories=[{"repository_url": "https://github.com/acme/a"},
                             {"repository_url": "https://github.com/acme/b"}])
    with pytest.raises(wc.ContractInvalid):
        wc.compile_contract(wc.decode(doc), step_id="s")


# --- the switch and the signed carrier --------------------------------------


def test_the_switch_is_off_by_default_and_attaches_nothing():
    compiled = wc.compile_contract(wc.decode(_doc()), step_id="s")
    options = DispatchOptions()
    assert wc.attach(options, compiled, environ={}) is options
    assert wc.attach(options, compiled, environ={wc.SWITCH_ENV: "yes"}) is options
    on = wc.attach(options, compiled, environ={wc.SWITCH_ENV: "on"})
    assert on.to_metadata()[wc.DISPATCH_FIELD] == dict(compiled.reference)


def test_the_reference_rides_in_the_signed_dispatch_block():
    assert DISPATCH_METADATA_KEY in specsign.SIGNED_METADATA_KEYS
    assert wc.DISPATCH_FIELD == "work_contract"


def _task(dispatch: dict) -> Task:
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    return Task(
        id="task-1", tenant_id="acme", workflow_id=None, step_id=None,
        submitted_by="owner@example.com", state=TaskState.QUEUED, runner_profile="claude-code",
        resource_class="standard", timeout_seconds=3600, max_attempts=3, provider="anthropic",
        model=None, input={"prompt": "p"}, created_at=now, updated_at=now,
        metadata={DISPATCH_METADATA_KEY: dispatch},
    )


def _signed(signer: LocalSpecSigner, dispatch: dict) -> dict:
    from swarm_api.specsigning import sign_task_specs

    task = _task(dispatch)
    sign_task_specs([task], signer)
    return task.to_firestore()


def test_a_rewritten_reference_fails_the_step_spec_signature():
    signer = LocalSpecSigner()
    compiled = wc.compile_contract(wc.decode(_doc()), step_id="s")
    block = DispatchOptions().with_work_contract(compiled.reference).to_metadata()
    doc = _signed(signer, block)
    assert signer.verifies(doc, "task-1")
    for key, value in (("digest", secrets.token_hex(32)), ("revision", 2),
                       ("strictness", "strict"), ("requirements", ["REQ-1"])):
        tampered = copy.deepcopy(doc)
        tampered["metadata"][DISPATCH_METADATA_KEY][wc.DISPATCH_FIELD][key] = value
        assert not signer.verifies(tampered, "task-1"), key
    stripped = copy.deepcopy(doc)
    del stripped["metadata"][DISPATCH_METADATA_KEY][wc.DISPATCH_FIELD]
    assert not signer.verifies(stripped, "task-1")


def test_a_changed_contract_body_fails_its_signed_reference():
    contract = wc.decode(_doc())
    reference = wc.compile_contract(contract, step_id="s").reference
    for mutate in (
        lambda d: d["requirements"][0].update(text="Anything goes."),
        lambda d: d["scope"].update(write=["**"]),
        lambda d: d["limits"].update(max_turns=500),
        lambda d: d["capabilities"]["plugins"]["gstack"]["skills"].append("ship"),
        lambda d: d["protected"].update(paths=[]),
    ):
        body = contract.to_document()
        mutate(body)
        assert not wc.verify_reference(wc.decode(body), reference)
    assert not wc.verify_reference(contract, None)


# --- compatibility matrix: a legacy spec decodes and behaves as today --------

LEGACY_BLOCKS = [
    DispatchOptions(),
    DispatchOptions(strategy="direct-pr"),
    DispatchOptions(strategy="integrate", role="integrator", integrates=("t1", "t2")),
    DispatchOptions(switches=(("code_intelligence", "on"),)),
    DispatchOptions(allow_empty_diff=True, pr_label="ready"),
]


@pytest.mark.parametrize("options", LEGACY_BLOCKS)
def test_a_legacy_dispatch_block_is_unchanged(options):
    block = options.to_metadata()
    assert wc.DISPATCH_FIELD not in block
    assert options.work_contract is None


@pytest.mark.parametrize("options", LEGACY_BLOCKS)
def test_a_legacy_spec_signs_the_same_bytes_and_still_verifies(options):
    signer = LocalSpecSigner()
    doc = _signed(signer, options.to_metadata())
    assert signer.verifies(doc, "task-1")
    assert doc["spec_format"] == specsign.signing_format(doc)
    # The switch on with no contract compiled changes nothing either: only
    # `attach` writes the field.
    assert wc.DISPATCH_FIELD not in doc["metadata"][DISPATCH_METADATA_KEY]


def test_a_contract_changes_only_its_own_key_in_the_block():
    base = DispatchOptions(strategy="direct-pr")
    compiled = wc.compile_contract(wc.decode(_doc()), step_id="s")
    with_contract = base.with_work_contract(compiled.reference).to_metadata()
    without = dict(with_contract)
    del without[wc.DISPATCH_FIELD]
    assert without == base.to_metadata()


# --- verification evidence --------------------------------------------------


def _pass(c, rid, kind, commit=COMMIT, ok=True, digest=None):
    return wc.Evidence(requirement_id=rid, contract_digest=digest or wc.digest(c),
                       commit=commit, kind=kind, passed=ok)


def test_missing_verification_is_not_a_pass():
    c = wc.decode(_doc())
    assert wc.unmet(c, [], commit=COMMIT) == ("REQ-1", "REQ-2")
    assert wc.unmet(c, [_pass(c, "REQ-1", "unit_tests"), _pass(c, "REQ-2", "ci")],
                    commit=COMMIT) == ("REQ-2",)
    assert wc.unmet(c, [_pass(c, "REQ-1", "unit_tests"), _pass(c, "REQ-2", "ci"),
                        _pass(c, "REQ-2", "review")], commit=COMMIT) == ()


def test_evidence_counts_only_at_its_exact_commit_and_revision():
    c = wc.decode(_doc())
    assert wc.unmet(c, [_pass(c, "REQ-1", "unit_tests", commit=OTHER_COMMIT)],
                    commit=COMMIT)[0] == "REQ-1"
    assert wc.unmet(c, [_pass(c, "REQ-1", "unit_tests", digest="0" * 64)],
                    commit=COMMIT)[0] == "REQ-1"
    assert wc.unmet(c, [_pass(c, "REQ-1", "unit_tests", ok=False)], commit=COMMIT)[0] == "REQ-1"


def test_a_requirement_naming_no_kind_still_needs_a_passing_record():
    c = wc.decode(_strict_enforceable())
    assert wc.unmet(c, [], commit=COMMIT) == ("REQ-1", "REQ-2")


# --- amendments --------------------------------------------------------------


def _proposal(c: wc.WorkContract, mutate=None) -> dict:
    body = c.to_document()
    if mutate:
        mutate(body)
    return body


def test_an_amendment_is_a_new_immutable_revision_naming_its_parent():
    old = wc.decode(_doc())
    result = wc.amend(old, _proposal(old, lambda d: d["requirements"].append(
        {"id": "REQ-3", "text": "Log out works.", "verification": ["unit_tests"]})),
        author_is_agent=False)
    new = result.contract
    assert (new.revision, new.previous_digest) == (2, wc.digest(old))
    assert result.added == ("REQ-3",)
    assert result.reverify == ("REQ-1", "REQ-2", "REQ-3")
    assert old.revision == 1 and len(old.requirements) == 2


def test_an_amendment_invalidates_earlier_approvals_and_evidence():
    old = wc.decode(_doc())
    approvals = [wc.Approval(contract_digest=wc.digest(old), approved_by="owner@example.com")]
    evidence = [_pass(old, "REQ-1", "unit_tests")]
    assert wc.approved(old, approvals) == tuple(approvals)
    new = wc.amend(old, _proposal(old, lambda d: d["requirements"][1].update(text="Docs.")),
                   author_is_agent=False).contract
    assert wc.approved(new, approvals) == ()
    assert "REQ-1" in wc.unmet(new, evidence, commit=COMMIT)


def test_a_silently_dropped_requirement_is_refused():
    old = wc.decode(_doc())
    with pytest.raises(wc.AmendmentRefused) as raised:
        wc.amend(old, _proposal(old, lambda d: d["requirements"].pop()), author_is_agent=False)
    assert raised.value.detail == {"dropped": ["REQ-2"]}


def test_a_person_withdraws_a_requirement_with_a_reason_and_its_id_is_retired():
    old = wc.decode(_doc())
    new = wc.amend(old, _proposal(old, lambda d: d["requirements"].pop()),
                   author_is_agent=False, withdraw={"REQ-2": "moved to wc-docs"}).contract
    assert new.withdrawn == ("REQ-2",)
    with pytest.raises(wc.ContractInvalid):
        wc.amend(new, _proposal(new, lambda d: d["requirements"].append(
            {"id": "REQ-2", "text": "back"})), author_is_agent=False)
    with pytest.raises(wc.AmendmentRefused):
        wc.amend(old, _proposal(old, lambda d: d["requirements"].pop()),
                 author_is_agent=False, withdraw={"REQ-2": " "})


def test_an_agent_never_withdraws_an_unmet_requirement():
    old = wc.decode(_doc())
    drop = _proposal(old, lambda d: d["requirements"].pop())
    with pytest.raises(wc.AmendmentRefused):
        wc.amend(old, drop, author_is_agent=True, withdraw={"REQ-2": "done"})
    with pytest.raises(wc.AmendmentRefused) as raised:
        wc.amend(old, drop, author_is_agent=True, withdraw={"REQ-2": "done"}, commit=COMMIT,
                 evidence=[_pass(old, "REQ-2", "ci")])
    assert raised.value.detail == {"unmet": ["REQ-2"]}
    met = [_pass(old, "REQ-2", "ci"), _pass(old, "REQ-2", "review")]
    new = wc.amend(old, drop, author_is_agent=True, withdraw={"REQ-2": "done"},
                   commit=COMMIT, evidence=met).contract
    assert new.requirement_ids() == ("REQ-1",)


def test_an_agent_never_rewrites_a_requirement():
    old = wc.decode(_doc())
    for mutate in (lambda d: d["requirements"][0].update(text="Something easier."),
                   lambda d: d["requirements"][1].update(verification=["ci"])):
        with pytest.raises(wc.AmendmentRefused):
            wc.amend(old, _proposal(old, mutate), author_is_agent=True)


@pytest.mark.parametrize("mutate, widens, scope", [
    (lambda d: d["scope"].update(write=["src/**"]), "write_scope", None),
    (lambda d: d["scope"].update(write=[]), "write_scope", None),
    (lambda d: d["scope"].update(read=["**"]), "read_scope", None),
    (lambda d: d["protected"].update(paths=[]), "protected", None),
    (lambda d: d["capabilities"].update(runtime=["screenshots", "human_takeover"]),
     "runtime_capabilities", None),
    (lambda d: d["capabilities"]["plugins"]["gstack"]["skills"].append("ship"), "plugins", None),
    (lambda d: d["capabilities"]["plugins"]["gstack"].update(mode="planner", skills=["spec"]),
     "plugins", None),
    (lambda d: d["limits"].update(max_turns=81), "limits", None),
    (lambda d: d["limits"].update(budget={"usd": 6}), "limits", None),
    (lambda d: d["limits"].update(budget=None), "limits", None),
    (lambda d: d["limits"].update(model_class="opus"), "limits", None),
    (lambda d: d["limits"].update(runner_profile="claude-code-large"), "limits", None),
    (lambda d: d["repositories"][0].update(repository_url="https://github.com/acme/other"),
     "repositories", None),
    (lambda d: d.update(strictness="advisory"), "strictness", None),
    # A glob in the old pattern must not match the new pattern's glob as text.
    (lambda d: d["scope"].update(write=["src/*"]), "write_scope", {"write": ["src/?"]}),
    (lambda d: d["scope"].update(write=["docs/a*"]), "write_scope", {"write": ["docs/a?"]}),
    (lambda d: d["scope"].update(write=["src/[!x]*"]), "write_scope",
     {"write": ["src/[!z]*"]}),
    (lambda d: d["scope"].update(read=["src/*"]), "read_scope",
     {"read": ["src/?"], "write": []}),
    (lambda d: d["scope"].update(write=["src*"]), "write_scope", {"write": ["src/**"]}),
])
def test_an_agent_cannot_widen_its_own_scope(mutate, widens, scope):
    base = _doc(strictness="strict") if widens == "strictness" else _doc()
    if scope is not None:
        base["scope"] = scope
    old = wc.decode(base)
    with pytest.raises(wc.ScopeWideningRefused) as raised:
        wc.amend(old, _proposal(old, mutate), author_is_agent=True)
    assert widens in raised.value.detail["widens"]
    assert raised.value.code == "work_contract_scope_widening"
    # A person may widen it; the new revision then waits for approval.
    assert wc.amend(old, _proposal(old, mutate), author_is_agent=False).contract.revision == 2


def test_an_agent_may_narrow_its_own_scope():
    old = wc.decode(_doc())
    new = wc.amend(old, _proposal(old, lambda d: (
        d["scope"].update(write=["src/login/form/**"]),
        d["limits"].update(max_turns=40),
        d["capabilities"]["plugins"]["gstack"].update(skills=["review"]),
    )), author_is_agent=True).contract
    assert new.write_scope == ("src/login/form/**",)


def test_an_amendment_cannot_fork_the_lineage_or_move_tenant():
    old = wc.decode(_doc())
    forked = wc.amend(old, _proposal(old, lambda d: d.update(revision=9,
                                                             previous_digest="0" * 64)),
                      author_is_agent=False).contract
    assert (forked.revision, forked.previous_digest) == (2, wc.digest(old))
    with pytest.raises(wc.AmendmentRefused):
        wc.amend(old, _proposal(old, lambda d: d.update(tenant_id="other")),
                 author_is_agent=False)


# --- from an issue-run plan --------------------------------------------------


def test_an_issue_run_plan_step_becomes_an_advisory_contract():
    plan = {"summary": "s", "requirements": ["SSO works.", "Docs updated."],
            "steps": [{"step_id": "login", "title": "Login", "prompt": "p",
                       "files": ["src/login.py"], "tests": ["uv run pytest tests/x.py"]}]}
    c = wc.from_plan_step(run_id="run-42", tenant_id="acme", owner="owner@example.com",
                          plan=plan, step=plan["steps"][0],
                          repository_url="https://github.com/acme/app", commit=COMMIT,
                          runner_profile="claude-code")
    assert c.contract_id == "wc-run-42-login"
    assert [(r.id, r.text, r.verification) for r in c.requirements] == [
        ("REQ-1", "SSO works.", ("unit_tests",)), ("REQ-2", "Docs updated.", ("unit_tests",))]
    assert (c.strictness, c.write_scope) == ("advisory", ("src/login.py",))
    compiled = wc.compile_contract(c, step_id="login")
    assert compiled.step["repository_ref"] == COMMIT
    assert "write_scope" in compiled.advisory


def test_a_plan_without_requirements_still_states_one():
    plan = {"summary": "s", "steps": [{"step_id": "a", "title": "Do A", "prompt": "p"}]}
    c = wc.from_plan_step(run_id="Run_1", tenant_id="t", owner="o@example.com", plan=plan,
                          step=plan["steps"][0], repository_url="https://github.com/o/r",
                          commit=COMMIT, runner_profile="claude-code")
    assert c.contract_id == "wc-run-1-a"
    assert [(r.id, r.text, r.verification) for r in c.requirements] == [("REQ-1", "Do A", ("ci",))]
