"""Versioned work contracts: what a step must do, where, with what, proven how.

The owner's agent quality program (BGD, 3 November 2026), work-contract
package; the design is docs/design/agent-quality/work-contract.md. A contract
is a closed, versioned document: stable requirement ids, the repositories and
commits it binds, read and write scope, protected paths and interfaces, the
capabilities it may use (runtime capability names and catalogue plugins), the
verification each requirement needs, references to the step's existing
limits, the artifacts it must leave, its owner and dependencies, and its
revision and approval lineage.

This module decodes one into a canonical form, digests it, compiles it into
TODAY's step fields plus a reference that rides inside the signed dispatch
block (`metadata.dispatch.work_contract`, covered by
`swarm_common.specsign.SIGNED_METADATA_KEYS`), applies amendments, and refuses
a strict contract whose components no worker enforces. It adds no second
source of truth for limits: `runner_profile`, `model_class`, `budget`,
`max_turns` and `timeout_seconds` are compiled into the step fields the
existing validation resolves and the existing signature covers.

Off by default: `enabled()` reads SWARM_WORK_CONTRACTS, and nothing attaches a
reference to a dispatch block unless it is `on`. Pure: no Firestore, no
network, no clock.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.specsign import SpecNotCanonical, jcs


#: Domain separation: a contract's bytes can never be read as a step spec's
#: (`swarm.step-spec`) or a settings block's (`swarm.settings/v1`).
CONTRACT_PURPOSE = "swarm.work-contract/v1"
SCHEMA_VERSION = 1

#: The switch. Off unless the environment says exactly `on`: a contract that
#: is compiled but not attached changes nothing a task stores.
SWITCH_ENV = "SWARM_WORK_CONTRACTS"

#: The key inside `metadata.dispatch` the reference is written under.
DISPATCH_FIELD = "work_contract"

STRICT = "strict"
ADVISORY = "advisory"
STRICTNESS = (STRICT, ADVISORY)

#: The runtime capability names the routing package matches runners on.
RUNTIME_CAPABILITIES = (
    "dom_automation",
    "javascript_execution",
    "pixel_rendering",
    "screenshots",
    "accessibility_tree",
    "persistent_session",
    "human_takeover",
    "checkpoint_support",
    "write_isolation",
)

PLUGIN_MODES = ("unattended", "planner", "designer")

#: The platform's closed plugin catalogue (docs/design/agent-plugins.md, "A
#: closed plugin catalogue"): per plugin, per mode, the skills it may offer.
#: A contract names plugins and skills from here by name only, never a path,
#: command, URL or version (invariant 10).
PLUGIN_CATALOGUE: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "gstack": {
        "unattended": (
            "review", "investigate", "health", "cso", "qa-only", "qa",
            "design-review", "document-generate", "document-release", "ship",
            "plan-eng-review", "plan-design-review", "autoplan",
        ),
        "planner": (
            "office-hours", "plan-ceo-review", "plan-eng-review",
            "plan-devex-review", "autoplan", "spec",
        ),
        "designer": (
            "design-consultation", "plan-design-review", "design-review", "design-html",
        ),
    },
}

#: How a requirement is proven. Names only: a contract never carries a command.
VERIFICATION_KINDS = ("unit_tests", "ci", "review", "browser_check", "artifact_present")
ARTIFACT_KINDS = ("pull_request", "file", "report")

#: The contract's components, as `enforceable` and the dispatch reference name them.
COMPONENT_REPOSITORIES = "repositories"
COMPONENT_LIMITS = "limits"
COMPONENT_READ_SCOPE = "read_scope"
COMPONENT_WRITE_SCOPE = "write_scope"
COMPONENT_PROTECTED = "protected"
COMPONENT_RUNTIME = "runtime_capabilities"
COMPONENT_PLUGINS = "plugins"
COMPONENT_VERIFICATION = "verification"
COMPONENT_ARTIFACTS = "artifacts"

#: What today's worker enforces of a contract, because each compiles into a
#: signed step field it already acts on: the repository and commit
#: (`repository_url`, `repository_ref`), and the limits (`runner_profile`,
#: `model_class`, `budget`, `max_turns`, `timeout_seconds`). Nothing else: no
#: worker reads a write scope, protected paths, runtime capabilities or
#: plugins today, and verification is judged after the attempt, not by it. A
#: strict contract that sets any other component is refused by `compile`.
WORKER_ENFORCED_TODAY = frozenset({COMPONENT_REPOSITORIES, COMPONENT_LIMITS})

_REQ_ID = re.compile(r"^[A-Z][A-Z0-9]{0,15}-[0-9]{1,6}$")
_CONTRACT_ID = re.compile(r"^wc-[a-z0-9][a-z0-9-]{2,62}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REPO_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_ITEMS = 200
_MAX_TEXT = 4000

_TOP_KEYS = frozenset({
    "schema", "contract_id", "tenant_id", "revision", "previous_digest", "strictness",
    "requirements", "repositories", "scope", "protected", "capabilities",
    "limits", "artifacts", "ownership",
})
_LIMIT_KEYS = ("runner_profile", "model_class", "budget", "max_turns", "timeout_seconds")


class WorkContractError(ValueError):
    """A contract refusal, as a domain error with a stable `code` and `detail`.

    Deliberately NOT an `ApiError`: no route raises these yet, and a new
    refusal in swarm-api ships switched off (`swarm_api.refusals`, observer
    proposal I). The lane that wires a route maps each code to an `ApiError`,
    registers a `refusals.Switch` for it and raises it through
    `refusals.refuse()`; until then nothing a caller sends can meet one."""

    code = "work_contract_refused"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ContractInvalid(WorkContractError):
    """`work_contract_invalid`: the document is not a work contract."""

    code = "work_contract_invalid"


class ContractNotEnforceable(WorkContractError):
    """`work_contract_not_enforceable`: a strict contract sets a component
    no worker enforces. Never compiled as advisory instead."""

    code = "work_contract_not_enforceable"


class AmendmentRefused(WorkContractError):
    """`work_contract_amendment_refused`: the amendment drops an unmet
    requirement silently, reuses a withdrawn id, or breaks the lineage."""

    code = "work_contract_amendment_refused"


class ScopeWideningRefused(WorkContractError):
    """`work_contract_scope_widening`: an agent's amendment widens its own
    contract. Only a person widens a contract (a 403 once a route serves it)."""

    code = "work_contract_scope_widening"


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    verification: tuple[str, ...]


@dataclass(frozen=True)
class WorkContract:
    """A decoded, canonical work contract. Immutable: an amendment is a new one."""

    contract_id: str
    tenant_id: str
    revision: int
    previous_digest: str | None
    strictness: str
    requirements: tuple[Requirement, ...]
    repositories: tuple[Mapping[str, Any], ...]
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    protected_paths: tuple[str, ...]
    protected_interfaces: tuple[str, ...]
    runtime_capabilities: tuple[str, ...]
    plugins: Mapping[str, Mapping[str, Any]] = field(hash=False)
    limits: Mapping[str, Any] = field(hash=False)
    artifacts: tuple[Mapping[str, Any], ...]
    owner: str
    depends_on: tuple[str, ...]
    withdrawn: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        """The canonical document: what `digest` covers and what is stored."""
        return {
            "schema": SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "tenant_id": self.tenant_id,
            "revision": self.revision,
            "previous_digest": self.previous_digest,
            "strictness": self.strictness,
            "requirements": [
                {"id": r.id, "text": r.text, "verification": list(r.verification)}
                for r in self.requirements
            ],
            "repositories": [dict(r) for r in self.repositories],
            "scope": {"read": list(self.read_scope), "write": list(self.write_scope)},
            "protected": {
                "paths": list(self.protected_paths),
                "interfaces": list(self.protected_interfaces),
            },
            "capabilities": {
                "runtime": list(self.runtime_capabilities),
                "plugins": {
                    name: {"mode": p["mode"], "skills": list(p["skills"])}
                    for name, p in sorted(self.plugins.items())
                },
            },
            "limits": {k: self.limits.get(k) for k in _LIMIT_KEYS},
            "artifacts": [dict(a) for a in self.artifacts],
            "ownership": {
                "owner": self.owner,
                "depends_on": list(self.depends_on),
                "withdrawn": list(self.withdrawn),
            },
        }

    def requirement_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.requirements)

    def components(self) -> frozenset[str]:
        """The components this contract sets, in `enforceable`'s names."""
        out = {COMPONENT_LIMITS}
        if self.repositories:
            out.add(COMPONENT_REPOSITORIES)
        if self.read_scope:
            out.add(COMPONENT_READ_SCOPE)
        if self.write_scope:
            out.add(COMPONENT_WRITE_SCOPE)
        if self.protected_paths or self.protected_interfaces:
            out.add(COMPONENT_PROTECTED)
        if self.runtime_capabilities:
            out.add(COMPONENT_RUNTIME)
        if self.plugins:
            out.add(COMPONENT_PLUGINS)
        if any(r.verification for r in self.requirements):
            out.add(COMPONENT_VERIFICATION)
        if self.artifacts:
            out.add(COMPONENT_ARTIFACTS)
        return frozenset(out)


# --- decoding ---------------------------------------------------------------


def _fail(message: str, **detail: Any) -> ContractInvalid:
    return ContractInvalid(message, detail=detail)


def _map(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _fail(f"{where} is not a map", field=where)
    return value


def _closed(value: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise _fail(f"{where} has unknown keys: {', '.join(map(str, extra))}",
                    field=where, unknown=extra)


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail(f"{where} is not a list", field=where)
    if len(value) > _MAX_ITEMS:
        raise _fail(f"{where} holds more than {_MAX_ITEMS} items", field=where)
    return value


def _text(value: Any, where: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _fail(f"{where} is a non-empty string of at most {limit} characters", field=where)
    return value


def _glob(value: Any, where: str) -> str:
    pattern = _text(value, where, limit=256)
    if pattern.startswith("/") or ".." in pattern.split("/") or "\\" in pattern:
        raise _fail(f"{where} is a repository-relative pattern", field=where, value=pattern)
    return pattern


def _names(value: Any, where: str, allowed: Sequence[str] | None = None) -> tuple[str, ...]:
    """A set of names: deduplicated and sorted, so order never changes the digest."""
    out = set()
    for item in _list(value, where):
        if not isinstance(item, str) or not _NAME.match(item):
            raise _fail(f"{where} holds names", field=where, value=item)
        if allowed is not None and item not in allowed:
            raise _fail(f"{where}: {item!r} is not one of {', '.join(allowed)}",
                        field=where, value=item)
        out.add(item)
    return tuple(sorted(out))


def _globs(value: Any, where: str) -> tuple[str, ...]:
    return tuple(sorted({_glob(item, where) for item in _list(value, where)}))


def _positive_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(f"{where} is a positive integer", field=where, value=value)
    return value


def _requirements(value: Any) -> tuple[Requirement, ...]:
    seen: set[str] = set()
    out = []
    for i, raw in enumerate(_list(value, "requirements")):
        where = f"requirements[{i}]"
        raw = _map(raw, where)
        _closed(raw, ("id", "text", "verification"), where)
        rid = raw.get("id")
        if not isinstance(rid, str) or not _REQ_ID.match(rid):
            raise _fail(f"{where}.id is a stable id such as REQ-1", field=where, value=rid)
        if rid in seen:
            raise _fail(f"requirement id {rid} appears twice", field=where, value=rid)
        seen.add(rid)
        out.append(Requirement(
            id=rid,
            text=_text(raw.get("text"), f"{where}.text"),
            verification=_names(raw.get("verification"), f"{where}.verification",
                                VERIFICATION_KINDS),
        ))
    if not out:
        raise _fail("a contract states at least one requirement", field="requirements")
    return tuple(out)


def _repositories(value: Any) -> tuple[Mapping[str, Any], ...]:
    out = []
    seen = set()
    for i, raw in enumerate(_list(value, "repositories")):
        where = f"repositories[{i}]"
        raw = _map(raw, where)
        _closed(raw, ("repository_url", "ref", "commit"), where)
        url = raw.get("repository_url")
        if not isinstance(url, str) or not _REPO_URL.match(url):
            raise _fail(f"{where}.repository_url is https://github.com/<owner>/<repo>",
                        field=where, value=url)
        if url in seen:
            raise _fail(f"{url} is bound twice", field=where, value=url)
        seen.add(url)
        ref = raw.get("ref")
        if ref is not None:
            _text(ref, f"{where}.ref", limit=255)
        commit = raw.get("commit")
        if commit is not None and (not isinstance(commit, str) or not _SHA.match(commit)):
            raise _fail(f"{where}.commit is a 40-hex commit", field=where, value=commit)
        out.append({"repository_url": url, "ref": ref, "commit": commit})
    return tuple(out)


def _plugins(value: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, raw in _map(value, "capabilities.plugins").items():
        where = f"capabilities.plugins.{name}"
        if name not in PLUGIN_CATALOGUE:
            raise _fail(f"{name!r} is not in the plugin catalogue", field=where,
                        catalogue=sorted(PLUGIN_CATALOGUE))
        raw = _map(raw, where)
        _closed(raw, ("mode", "skills"), where)
        mode = raw.get("mode")
        if mode not in PLUGIN_MODES:
            raise _fail(f"{where}.mode is one of {', '.join(PLUGIN_MODES)}",
                        field=where, value=mode)
        offered = PLUGIN_CATALOGUE[name].get(mode, ())
        skills = _names(raw.get("skills"), f"{where}.skills", offered)
        if not skills:
            raise _fail(f"{where} names at least one skill its mode offers",
                        field=where, offered=list(offered))
        out[name] = {"mode": mode, "skills": skills}
    return out


def _limits(value: Any) -> dict[str, Any]:
    raw = _map(value, "limits")
    _closed(raw, _LIMIT_KEYS, "limits")
    profile = raw.get("runner_profile")
    if not isinstance(profile, str) or not _NAME.match(profile):
        raise _fail("limits.runner_profile names a runner profile", field="limits")
    model_class = raw.get("model_class")
    if model_class is not None and (not isinstance(model_class, str) or not _NAME.match(model_class)):
        raise _fail("limits.model_class names a model class", field="limits")
    budget = raw.get("budget")
    if budget is not None:
        budget = dict(_map(budget, "limits.budget"))
        _closed(budget, ("usd", "tokens"), "limits.budget")
        usd, tokens = budget.get("usd"), budget.get("tokens")
        if usd is not None and (isinstance(usd, bool) or not isinstance(usd, (int, float))
                                or not 0 < usd < 1e9):
            raise _fail("limits.budget.usd is a positive number", field="limits")
        _positive_int(tokens, "limits.budget.tokens")
        budget = {k: v for k, v in (("usd", usd), ("tokens", tokens)) if v is not None} or None
    return {
        "runner_profile": profile,
        "model_class": model_class,
        "budget": budget,
        "max_turns": _positive_int(raw.get("max_turns"), "limits.max_turns"),
        "timeout_seconds": _positive_int(raw.get("timeout_seconds"), "limits.timeout_seconds"),
    }


def _artifacts(value: Any) -> tuple[Mapping[str, Any], ...]:
    out = []
    for i, raw in enumerate(_list(value, "artifacts")):
        where = f"artifacts[{i}]"
        raw = _map(raw, where)
        _closed(raw, ("kind", "path"), where)
        kind = raw.get("kind")
        if kind not in ARTIFACT_KINDS:
            raise _fail(f"{where}.kind is one of {', '.join(ARTIFACT_KINDS)}", field=where)
        path = raw.get("path")
        if kind == "file":
            path = _glob(path, f"{where}.path")
        elif path is not None:
            raise _fail(f"{where}: only a file artifact names a path", field=where)
        out.append({"kind": kind, "path": path})
    return tuple(sorted(out, key=lambda a: (a["kind"], a["path"] or "")))


def decode(document: Any) -> WorkContract:
    """A work contract from a caller's or the store's document, or ContractInvalid.

    Closed at every level: an unknown key is refused, never ignored, so a
    field a later schema adds cannot ride along unsigned in this one."""
    raw = _map(document, "contract")
    _closed(raw, _TOP_KEYS, "contract")
    if raw.get("schema") != SCHEMA_VERSION or isinstance(raw.get("schema"), bool):
        raise _fail(f"schema is {SCHEMA_VERSION}", field="schema", value=raw.get("schema"))
    contract_id = raw.get("contract_id")
    if not isinstance(contract_id, str) or not _CONTRACT_ID.match(contract_id):
        raise _fail("contract_id is wc-<lowercase name>", field="contract_id")
    tenant_id = _text(raw.get("tenant_id"), "tenant_id", limit=128)
    revision = _positive_int(raw.get("revision"), "revision")
    if revision is None:
        raise _fail("revision is a positive integer", field="revision")
    previous = raw.get("previous_digest")
    if revision == 1 and previous is not None:
        raise _fail("revision 1 has no previous_digest", field="previous_digest")
    if revision > 1 and (not isinstance(previous, str) or not _DIGEST.match(previous)):
        raise _fail("a later revision names its previous revision's digest",
                    field="previous_digest")
    strictness = raw.get("strictness")
    if strictness not in STRICTNESS:
        raise _fail(f"strictness is one of {', '.join(STRICTNESS)}", field="strictness")
    scope = _map(raw.get("scope"), "scope")
    _closed(scope, ("read", "write"), "scope")
    protected = _map(raw.get("protected"), "protected")
    _closed(protected, ("paths", "interfaces"), "protected")
    capabilities = _map(raw.get("capabilities"), "capabilities")
    _closed(capabilities, ("runtime", "plugins"), "capabilities")
    ownership = _map(raw.get("ownership"), "ownership")
    _closed(ownership, ("owner", "depends_on", "withdrawn"), "ownership")
    depends_on = []
    for item in _list(ownership.get("depends_on"), "ownership.depends_on"):
        if not isinstance(item, str) or not _CONTRACT_ID.match(item) or item == contract_id:
            raise _fail("ownership.depends_on names other contracts", field="ownership")
        depends_on.append(item)
    withdrawn = []
    for item in _list(ownership.get("withdrawn"), "ownership.withdrawn"):
        if not isinstance(item, str) or not _REQ_ID.match(item):
            raise _fail("ownership.withdrawn holds requirement ids", field="ownership")
        withdrawn.append(item)
    requirements = _requirements(raw.get("requirements"))
    reused = sorted(set(withdrawn) & {r.id for r in requirements})
    if reused:
        raise _fail("a withdrawn requirement id is never reused", field="requirements",
                    ids=reused)
    interfaces = []
    for item in _list(protected.get("interfaces"), "protected.interfaces"):
        interfaces.append(_text(item, "protected.interfaces", limit=256))
    return WorkContract(
        contract_id=contract_id,
        tenant_id=tenant_id,
        revision=revision,
        previous_digest=previous,
        strictness=strictness,
        requirements=requirements,
        repositories=_repositories(raw.get("repositories")),
        read_scope=_globs(scope.get("read"), "scope.read"),
        write_scope=_globs(scope.get("write"), "scope.write"),
        protected_paths=_globs(protected.get("paths"), "protected.paths"),
        protected_interfaces=tuple(sorted(set(interfaces))),
        runtime_capabilities=_names(capabilities.get("runtime"), "capabilities.runtime",
                                    RUNTIME_CAPABILITIES),
        plugins=_plugins(capabilities.get("plugins")),
        limits=_limits(raw.get("limits")),
        artifacts=_artifacts(raw.get("artifacts")),
        owner=_text(ownership.get("owner"), "ownership.owner", limit=320),
        depends_on=tuple(sorted(set(depends_on))),
        withdrawn=tuple(sorted(set(withdrawn))),
    )


# --- canonical form ---------------------------------------------------------


def canonical_bytes(contract: WorkContract) -> bytes:
    """RFC 8785 (JCS) of the purpose and the canonical document, through the
    frozen `swarm_common.specsign.jcs`: one encoder for every signed thing."""
    try:
        return jcs({"purpose": CONTRACT_PURPOSE, "contract": contract.to_document()})
    except SpecNotCanonical as exc:
        raise _fail(f"the contract has no canonical form: {exc}", field="contract") from exc


def digest(contract: WorkContract) -> str:
    """SHA-256 of `canonical_bytes`, hex. Names exactly one revision's content."""
    return hashlib.sha256(canonical_bytes(contract)).hexdigest()


# --- enforceability and compilation -----------------------------------------


def unenforced(contract: WorkContract,
               enforced: frozenset[str] = WORKER_ENFORCED_TODAY) -> tuple[str, ...]:
    """The components the contract sets that the worker does not enforce."""
    return tuple(sorted(contract.components() - enforced))


@dataclass(frozen=True)
class CompiledContract:
    """What one contract revision compiles into.

    `step` holds today's step fields; the submission path validates and
    resolves them exactly as it does a hand-written step's, so the contract
    is never a second source of truth for a limit. `reference` is the block
    written at `metadata.dispatch.work_contract`, inside the step-spec
    signature. `advisory` names the components carried only as text, which
    is never empty on a strict contract (that one is refused instead)."""

    step: Mapping[str, Any] = field(hash=False)
    reference: Mapping[str, Any] = field(hash=False)
    advisory: tuple[str, ...]
    brief: str


def compile_contract(contract: WorkContract, *, step_id: str,
                     enforced: frozenset[str] = WORKER_ENFORCED_TODAY) -> CompiledContract:
    """The step fields and the signed reference for one contract revision.

    Refuses (ContractNotEnforceable) a strict contract that sets a component
    outside `enforced`: a strict contract never silently degrades to
    advisory. An advisory one compiles, and names those components."""
    gaps = unenforced(contract, enforced)
    if contract.strictness == STRICT and gaps:
        raise ContractNotEnforceable(
            f"contract {contract.contract_id} is strict, and no worker enforces "
            f"{', '.join(gaps)}: make it advisory or remove those components",
            detail={"contract_id": contract.contract_id, "revision": contract.revision,
                    "unenforced": list(gaps)},
        )
    if len(contract.repositories) > 1:
        raise _fail("a compiled step binds one repository; name the others as their own "
                    "contracts' dependencies", field="repositories")
    limits = contract.limits
    step: dict[str, Any] = {"step_id": step_id, "runner_profile": limits["runner_profile"]}
    for key in ("model_class", "budget", "max_turns", "timeout_seconds"):
        if limits.get(key) is not None:
            step[key] = limits[key]
    if contract.repositories:
        repo = contract.repositories[0]
        step["repository_url"] = repo["repository_url"]
        ref = repo["commit"] or repo["ref"]
        if ref is not None:
            step["repository_ref"] = ref
    reference = {
        "id": contract.contract_id,
        "revision": contract.revision,
        "digest": digest(contract),
        "strictness": contract.strictness,
        "requirements": list(contract.requirement_ids()),
    }
    return CompiledContract(step=step, reference=reference, advisory=gaps,
                            brief=render_brief(contract, gaps))


def render_brief(contract: WorkContract, advisory: Sequence[str] = ()) -> str:
    """The contract as the agent reads it: requirements by id, then scope."""
    lines = [f"Work contract {contract.contract_id}, revision {contract.revision} "
             f"({contract.strictness}).", "", "Requirements:"]
    for r in contract.requirements:
        proof = f" [verified by: {', '.join(r.verification)}]" if r.verification else ""
        lines.append(f"- {r.id}: {r.text}{proof}")
    if contract.write_scope:
        lines += ["", "You may write only: " + ", ".join(contract.write_scope)]
    if contract.read_scope:
        lines += ["", "You may read only: " + ", ".join(contract.read_scope)]
    if contract.protected_paths or contract.protected_interfaces:
        lines += ["", "Never change: " + ", ".join(
            contract.protected_paths + contract.protected_interfaces)]
    if contract.artifacts:
        lines += ["", "Leave: " + ", ".join(
            a["kind"] + (f" {a['path']}" if a["path"] else "") for a in contract.artifacts)]
    if advisory:
        lines += ["", "Advisory, not enforced by the platform: " + ", ".join(advisory)]
    return "\n".join(lines)


def verify_reference(contract: WorkContract, reference: Mapping[str, Any] | None) -> bool:
    """Does the signed dispatch reference name exactly this contract revision?

    The reference is inside the step-spec signature; this binds the stored
    contract body to it. A changed body changes the digest and fails here."""
    if not isinstance(reference, Mapping):
        return False
    return (reference.get("id") == contract.contract_id
            and reference.get("revision") == contract.revision
            and reference.get("digest") == digest(contract)
            and reference.get("strictness") == contract.strictness
            and reference.get("requirements") == list(contract.requirement_ids()))


def enabled(environ: Mapping[str, str]) -> bool:
    """The switch: on only when SWARM_WORK_CONTRACTS is exactly `on`."""
    return environ.get(SWITCH_ENV, "").strip() == "on"


def attach(options: Any, compiled: CompiledContract, *, environ: Mapping[str, str]) -> Any:
    """`options` (a `validation.DispatchOptions`) carrying the reference when
    the switch is on; `options` unchanged when it is off."""
    if not enabled(environ):
        return options
    return options.with_work_contract(compiled.reference)


# --- on submission ----------------------------------------------------------

#: The key a caller's task or workflow-step `metadata` carries its contract
#: document under. While the switch is off it is the caller's free-form
#: metadata, stored as sent, exactly as before contracts existed.
METADATA_KEY = "work_contract"


class ContractMismatch(WorkContractError):
    """`work_contract_mismatch`: the contract and the step it rides on name
    different limits or a different repository. The contract is never a
    second source of truth for a limit, so the two must agree."""

    code = "work_contract_mismatch"


@dataclass(frozen=True)
class Prepared:
    """What `prepare` makes of one step's contract.

    `fields` holds the limits the contract sets and the step left unset, for
    the submission path to put on the step before its usual validation;
    `metadata` is the step's metadata with the canonical document at
    `METADATA_KEY`, which `compiled.reference` (signed in the dispatch
    block) binds by digest."""

    contract: WorkContract
    compiled: CompiledContract
    fields: Mapping[str, Any] = field(hash=False)
    metadata: Mapping[str, Any] = field(hash=False)


def _same_repository(a: str | None, b: str | None) -> bool:
    def norm(url: str | None) -> str | None:
        if url is None:
            return None
        url = url.rstrip("/")
        return (url[:-4] if url.endswith(".git") else url).lower()
    return norm(a) == norm(b)


def prepare(metadata: Mapping[str, Any], *, tenant_id: str, step_id: str | None,
            step_fields: Mapping[str, Any], repository_url: str | None,
            repository_ref: str | None, environ: Mapping[str, str],
            enforced: frozenset[str] = WORKER_ENFORCED_TODAY) -> Prepared | None:
    """One task's or workflow step's contract, decoded, compiled and checked
    against the step, or None when the switch is off or the step carries none.

    `step_fields` are the step's own `runner_profile`, `model_class`,
    `budget` (a `{usd, tokens}` map), `max_turns` and `timeout_seconds`, None
    where unset; `repository_url` and `repository_ref` are the ones the step
    runs at. Refuses (a `WorkContractError`) a document that is not a
    contract, one of another tenant (invariant 9), a strict one no worker
    enforces (`compile_contract`), a limit that differs from the step's, a
    repository other than the step's, and a strict contract pinning a ref the
    step does not run at. An advisory contract pinning a ref on a step with
    none keeps the pin as its text: the digest still names it."""
    if not enabled(environ) or METADATA_KEY not in metadata:
        return None
    contract = decode(metadata[METADATA_KEY])
    if contract.tenant_id != tenant_id:
        raise _fail("the contract is another workspace's", field="tenant_id")
    compiled = compile_contract(contract, step_id=step_id or "", enforced=enforced)
    fill: dict[str, Any] = {}
    for key in _LIMIT_KEYS:
        want, have = compiled.step.get(key), step_fields.get(key)
        if want is None:
            continue
        if have is None:
            fill[key] = want
        elif have != want:
            raise ContractMismatch(
                f"the contract's {key} is {want!r} and the step's is {have!r}",
                detail={"field": f"limits.{key}", "contract": want, "step": have})
    want_url = compiled.step.get("repository_url")
    if want_url is not None and not _same_repository(want_url, repository_url):
        raise ContractMismatch("the contract binds another repository than the step's",
                               detail={"field": "repositories", "contract": want_url,
                                       "step": repository_url})
    want_ref = compiled.step.get("repository_ref")
    if want_ref is not None and repository_ref != want_ref and (
            repository_ref is not None or contract.strictness == STRICT):
        raise ContractMismatch("the contract pins a ref the step does not run at",
                               detail={"field": "repositories", "contract": want_ref,
                                       "step": repository_ref})
    out = dict(metadata)
    out[METADATA_KEY] = contract.to_document()
    return Prepared(contract=contract, compiled=compiled, fields=fill, metadata=out)


# --- verification evidence --------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One verification result: which requirement, which contract revision,
    which exact source commit, which kind, and whether it passed."""

    requirement_id: str
    contract_digest: str
    commit: str
    kind: str
    passed: bool


def unmet(contract: WorkContract, evidence: Iterable[Evidence], *, commit: str) -> tuple[str, ...]:
    """The requirement ids not yet proven at `commit` under this revision.

    Missing verification is not a pass: a requirement is met only when, for
    EVERY kind it names, a passing record binds this contract's digest and
    this commit. Evidence for another revision or another commit counts for
    nothing. A requirement that names no kind is unmet until some passing
    record of any kind exists for it."""
    if not isinstance(commit, str) or not _SHA.match(commit):
        raise _fail("evidence is judged at a 40-hex commit", field="commit")
    current = digest(contract)
    passed: dict[str, set[str]] = {}
    for record in evidence:
        if record.passed and record.contract_digest == current and record.commit == commit:
            passed.setdefault(record.requirement_id, set()).add(record.kind)
    out = []
    for r in contract.requirements:
        kinds = passed.get(r.id, set())
        if (r.verification and not set(r.verification) <= kinds) or not kinds:
            out.append(r.id)
    return tuple(out)


# --- approvals and amendments -----------------------------------------------


@dataclass(frozen=True)
class Approval:
    """A person's approval of exactly one revision, by its digest."""

    contract_digest: str
    approved_by: str


def approved(contract: WorkContract, approvals: Iterable[Approval]) -> tuple[Approval, ...]:
    """The approvals that hold for this revision: those of its digest only.
    An amendment makes a new digest, so every earlier approval stops holding."""
    current = digest(contract)
    return tuple(a for a in approvals if a.contract_digest == current)


@dataclass(frozen=True)
class Amendment:
    """The result of `amend`: the new immutable revision and what it invalidated."""

    contract: WorkContract
    #: Requirement ids whose earlier evidence no longer counts: every id
    #: (evidence binds the digest), listed for the console and the log.
    reverify: tuple[str, ...]
    #: Requirements added, changed in text or verification, or withdrawn.
    added: tuple[str, ...]
    changed: tuple[str, ...]
    withdrawn: tuple[str, ...]


_GLOB = re.compile(r"[*?\[]")


def _literal_prefix(pattern: str) -> str:
    found = _GLOB.search(pattern)
    return pattern if found is None else pattern[:found.start()]


def _covered(pattern: str, by: Sequence[str]) -> bool:
    """Whether every path `pattern` matches is matched by some pattern in `by`.

    Matching the new pattern as a literal string against an old one is not a
    subset test (old `src/?` matches the text `src/*`), so a new pattern
    counts as covered only when it equals an old one, when it is a plain
    path the old one matches, or when the old one is a literal prefix
    followed by nothing but `*` (fnmatch's `*` crosses `/`) and the new
    pattern's own literal prefix starts with it. Anything else is widening.
    """
    literal = _literal_prefix(pattern)
    for old in by:
        if pattern == old:
            return True
        if literal == pattern and fnmatch.fnmatchcase(pattern, old):
            return True
        stem = _literal_prefix(old)
        if old != stem and old[len(stem):].strip("*") == "" and literal.startswith(stem):
            return True
    return False


def _scope_wider(old: Sequence[str], new: Sequence[str]) -> bool:
    """An empty scope is unrestricted: anything narrows it, and returning to
    it from a pattern widens. Otherwise every new pattern must be covered."""
    if not old:
        return False
    return not new or not all(_covered(p, old) for p in new)


def widening(old: WorkContract, new: WorkContract) -> tuple[str, ...]:
    """How `new` grants more than `old`, as component names; empty when it does not."""
    out = []
    if _scope_wider(old.write_scope, new.write_scope):
        out.append(COMPONENT_WRITE_SCOPE)
    if _scope_wider(old.read_scope, new.read_scope):
        out.append(COMPONENT_READ_SCOPE)
    if not set(old.protected_paths) <= set(new.protected_paths) or \
            not set(old.protected_interfaces) <= set(new.protected_interfaces):
        out.append(COMPONENT_PROTECTED)
    if not set(new.runtime_capabilities) <= set(old.runtime_capabilities):
        out.append(COMPONENT_RUNTIME)
    for name, plugin in new.plugins.items():
        before = old.plugins.get(name)
        if before is None or before["mode"] != plugin["mode"] \
                or not set(plugin["skills"]) <= set(before["skills"]):
            out.append(COMPONENT_PLUGINS)
            break
    if [r["repository_url"] for r in new.repositories] != \
            [r["repository_url"] for r in old.repositories]:
        out.append(COMPONENT_REPOSITORIES)
    if _limits_wider(old.limits, new.limits):
        out.append(COMPONENT_LIMITS)
    if new.strictness == ADVISORY and old.strictness == STRICT:
        out.append("strictness")
    return tuple(out)


def _limits_wider(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    if new["runner_profile"] != old["runner_profile"] or new["model_class"] != old["model_class"]:
        return True
    for key in ("max_turns", "timeout_seconds"):
        if old[key] is not None and (new[key] is None or new[key] > old[key]):
            return True
    old_budget, new_budget = old["budget"] or {}, new["budget"] or {}
    for unit, amount in old_budget.items():
        if unit not in new_budget or new_budget[unit] > amount:
            return True
    return False


def amend(old: WorkContract, proposed: Any, *, author_is_agent: bool,
          withdraw: Mapping[str, str] | None = None,
          evidence: Iterable[Evidence] = (), commit: str | None = None) -> Amendment:
    """A new immutable revision of `old` from `proposed` (a contract document).

    The new revision is `old.revision + 1` and names `old`'s digest; the
    caller's `revision` and `previous_digest` are ignored and set here, so a
    fork of the lineage cannot be proposed. `withdraw` maps each requirement
    id the amendment removes to the reason: a requirement missing from
    `proposed` and not in `withdraw` is a silent drop and is refused. An unmet
    requirement (judged by `evidence` at `commit` against `old`) is never
    withdrawn by an agent, and an agent's amendment never widens its own
    contract (`widening`), never changes its owner, and never drops a
    dependency or an expected artifact. The result still needs a person's
    approval of the new digest; `approved` holds no earlier approval for it."""
    raw = dict(_map(proposed, "contract"))
    raw["revision"] = old.revision + 1
    raw["previous_digest"] = digest(old)
    withdraw = dict(withdraw or {})
    previously = set(old.withdrawn)
    ownership = dict(_map(raw.get("ownership"), "ownership"))
    ownership["withdrawn"] = sorted(previously | set(withdraw))
    raw["ownership"] = ownership
    new = decode(raw)
    if new.contract_id != old.contract_id or new.tenant_id != old.tenant_id:
        raise AmendmentRefused("an amendment keeps its contract's id and tenant",
                               detail={"contract_id": old.contract_id})
    old_ids, new_ids = set(old.requirement_ids()), set(new.requirement_ids())
    for rid, reason in withdraw.items():
        if rid not in old_ids:
            raise AmendmentRefused(f"{rid} is not a requirement of revision {old.revision}",
                                   detail={"requirement": rid})
        if not isinstance(reason, str) or not reason.strip():
            raise AmendmentRefused(f"withdrawing {rid} says why", detail={"requirement": rid})
    dropped = sorted(old_ids - new_ids - set(withdraw))
    if dropped:
        raise AmendmentRefused(
            "an amendment never drops a requirement silently: withdraw each one with a reason",
            detail={"dropped": dropped})
    kept_withdrawn = sorted(set(withdraw) & new_ids)
    if kept_withdrawn:
        raise AmendmentRefused("a withdrawn requirement is not also kept",
                               detail={"requirements": kept_withdrawn})
    if author_is_agent:
        wider = widening(old, new)
        if wider:
            raise ScopeWideningRefused(
                "an agent cannot widen its own contract; a person amends it",
                detail={"contract_id": old.contract_id, "widens": list(wider)})
        if withdraw:
            if commit is None:
                raise AmendmentRefused("an agent withdraws a requirement only once it is met, "
                                       "judged at a commit", detail={"withdraw": sorted(withdraw)})
            still_unmet = sorted(set(unmet(old, evidence, commit=commit)) & set(withdraw))
            if still_unmet:
                raise AmendmentRefused("an agent never withdraws an unmet requirement",
                                       detail={"unmet": still_unmet})
    before = {r.id: r for r in old.requirements}
    added = tuple(r.id for r in new.requirements if r.id not in before)
    changed = tuple(r.id for r in new.requirements if r.id in before and before[r.id] != r)
    if author_is_agent and changed:
        # Rewording a requirement, or dropping a kind of proof from it, is a
        # drop by another name: only a person does it.
        raise AmendmentRefused("an agent never rewrites a requirement or its verification",
                               detail={"changed": list(changed)})
    if author_is_agent:
        _refuse_dropped_obligations(old, new)
    return Amendment(contract=new, reverify=new.requirement_ids(), added=added,
                     changed=changed, withdrawn=tuple(sorted(withdraw)))


def _refuse_dropped_obligations(old: WorkContract, new: WorkContract) -> None:
    """An agent's amendment keeps who owns the contract, every contract it
    depends on and every artifact it must leave: dropping one is dropping an
    unmet requirement by another name. It may add a dependency or an
    artifact, which only asks more of it. A person may do any of these."""
    if new.owner != old.owner:
        raise AmendmentRefused("an agent never changes its contract's owner",
                               detail={"field": "ownership.owner"})
    dropped = sorted(set(old.depends_on) - set(new.depends_on))
    if dropped:
        raise AmendmentRefused("an agent never drops a dependency of its contract",
                               detail={"field": "ownership.depends_on", "dropped": dropped})
    kept = {(a["kind"], a["path"]) for a in new.artifacts}
    lost = [dict(a) for a in old.artifacts if (a["kind"], a["path"]) not in kept]
    if lost:
        raise AmendmentRefused("an agent never drops an artifact its contract expects",
                               detail={"field": "artifacts", "dropped": lost})


# --- from an approved issue-run plan ----------------------------------------


def from_plan_step(*, run_id: str, tenant_id: str, owner: str, plan: Mapping[str, Any],
                   step: Mapping[str, Any], repository_url: str, commit: str | None,
                   runner_profile: str) -> WorkContract:
    """An advisory contract for one step of an approved issue-run plan
    (`swarm_api.issueruns.PlanSpec`), so issue runs can carry contracts
    without the planner learning a new shape.

    The plan's `requirements` become REQ-1.. in the plan's order (the plan's
    digest, which the person approved, fixes that order); the step's `files`
    become its write scope; a step that names `tests` asks `unit_tests` of
    every requirement, else `ci`. The repository is bound at the commit the
    run planned against. Advisory, because no worker enforces a write scope
    or verification yet (`WORKER_ENFORCED_TODAY`). `commit` None binds the
    repository alone, for a run that planned from no indexed commit."""
    step_id = str(step.get("step_id") or "")
    verification = ["unit_tests"] if step.get("tests") else ["ci"]
    lines = [line for line in (plan.get("requirements") or []) if isinstance(line, str)]
    if not lines:
        lines = [str(step.get("title") or step_id)]
    return decode({
        "schema": SCHEMA_VERSION,
        "contract_id": "wc-" + re.sub(r"[^a-z0-9-]+", "-", f"{run_id}-{step_id}".lower())[:62],
        "tenant_id": tenant_id,
        "revision": 1,
        "strictness": ADVISORY,
        "requirements": [{"id": f"REQ-{i}", "text": text, "verification": verification}
                         for i, text in enumerate(lines, start=1)],
        "repositories": [{"repository_url": repository_url, "ref": None, "commit": commit}],
        "scope": {"write": list(step.get("files") or [])},
        "limits": {"runner_profile": runner_profile, "model_class": step.get("model_class")},
        "artifacts": [{"kind": "pull_request"}],
        "ownership": {"owner": owner},
    })
