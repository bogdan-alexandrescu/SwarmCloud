"""The DETAILS a `/sc:swarmcloud` run and each of its rows show when expanded (SC-DESC-DETAILS).

Owner, 2026-10-14 ~22:00 PDT: "We do want to have a section in the workflow
or task description when you expand it in claude, that will provide more
details like user that created the job, tenant or workspace that is using and
maybe even what LLM subscription or config is using all shown here in claude
code under each workflow or task expanded description". And, the same
message: no `[<workspace>]` in front of any run or row name.

ONE LINE, SIX FIELDS, ALWAYS IN THIS ORDER, each `not reported` when the API
served nothing for it -- said, never left out, so a missing fact reads as
missing rather than as a field this plugin does not have:

    Workspace: <display name> (<tenant>) · Started by: <submitted_by> ·
    Profiles: <runner profiles> · LLM: <pool account> (<provider>), model
    <model> · Browser tool: <browser_tool> · Session: <claude_session>

The workspace is first because `launch.DESCRIPTION_CHARS` clips the END of a
run's description: the workspace is the one field the owner asked for twice.

WHERE EACH FIELD COMES FROM -- only what the API already serves, by name:

* Workspace: the document's `tenant_id`, shown by the display name
  `GET /v1/tenants/mine` gives (`launch.workspace_shown`).
* Started by: the task's or workflow's `submitted_by`, as the API stores it
  (the submitter's verified email, or the principal a key acts as). For a spec
  not yet submitted, the `principal.email` of `GET /v1/tenants/me`: the
  identity the submission will be stored under.
* Profiles: each step's or task's `runner_profile`.
* LLM: the task's `account` (`swarm_api.task_accounts`: `account_id`,
  `provider`, `status`), shown by the account's `label` when
  `GET /v1/accounts` lists it for this tenant, and the task's `model`. A run
  started before any account was assigned says `not assigned yet`; its rows
  then say `LLM: <account>` once, on the first progress line after the
  assignment (`follow.render`).
* Browser tool: a `claude-code-browser` step's `input.browser_tool`, which
  absent is `auto` (swarm_common.profiles, contract request 77); `not used`
  when no step runs that profile.
* Session: the Claude Code session tag (`claude_session`), named `this
  session` when it is the one this bridge serves (`session.current()`).

ONE MORE FIELD, SHOWN ONLY WHEN A STEP NAMES IT: `QA identity: <name>`,
between `Browser tool:` and `Session:`, from a `claude-code-browser` step's
`input.qa_identity` (contract request 106; docs/design/qa-identity.md §4.7,
lane QI-MCP). Such a task's live browser is signed in to the console as the
deployment's QA service account, and whoever expands the row is told whose
session it holds. Unlike the six it is left out, not `not reported`, when no
step names it: absence is the fact (the browser is signed in to nothing),
and the field on every plain row would read as a sign-in nobody asked for.

NOTHING SECRET-SHAPED IS EVER SHOWN. The fields are read by an allow-list --
never a whole document, never `metadata`, never an account's other keys -- and
every value is then checked once more (`shown`): a value that looks like a key
or a token (a known prefix, a JWT, or a long unbroken run of key characters)
is replaced by `withheld`. An account's label is free text its owner typed;
that is the case this exists for.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import session

NOT_REPORTED = "not reported"
#: What a value that looks like a credential is shown as.
WITHHELD = "withheld (credential-shaped)"
#: The one profile that has a browser tool to choose (contract request 77).
BROWSER_PROFILE = "claude-code-browser"
#: What `browser_tool` absent means on that profile.
BROWSER_TOOL_DEFAULT = "auto"
#: The longest any one value is shown: a field is a fact, not a paragraph.
VALUE_CHARS = 80
#: How many distinct values of one field are named before `+N more`.
VALUES_NAMED = 3

#: A credential by its shape: a provider's known prefix with a body, a JWT,
#: a PEM header, or 32+ characters of key alphabet with no break. A hyphen
#: breaks the run on purpose: a session UUID is four hyphens of short hex
#: groups, and is an id, not a secret.
_SECRET_SHAPES = re.compile(
    r"(?:sk-|sk_|rk_|pk_|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|glpat-|xox[abposr]-|AIza|ya29\.|AKIA|ASIA)"
    r"[A-Za-z0-9_\-.]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN"
    r"|[A-Za-z0-9+/_=]{32,}"
)


def shown(value: Any) -> str | None:
    """`value` as one line, clipped to VALUE_CHARS, or WITHHELD when it looks
    like a credential; None when it holds no text."""
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    if not flat:
        return None
    if _SECRET_SHAPES.search(flat):
        return WITHHELD
    return flat if len(flat) <= VALUE_CHARS else flat[: VALUE_CHARS - 1] + "…"


def _named(values: Iterable[str | None]) -> str | None:
    """The distinct values, in order of first appearance, `, `-joined, at most
    VALUES_NAMED of them and `+N more` for the rest; None when there are none."""
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    if not seen:
        return None
    head = ", ".join(seen[:VALUES_NAMED])
    return head + (f" +{len(seen) - VALUES_NAMED} more" if len(seen) > VALUES_NAMED else "")


# --------------------------------------------------------------------------
# Facts: what the API served, by name
# --------------------------------------------------------------------------


def empty() -> dict[str, list]:
    """The facts of nothing: every field empty, so each says `not reported`."""
    return {"started_by": [], "profiles": [], "accounts": [], "models": [], "browser_tools": [],
            "qa_identities": [], "sessions": [], "pending": []}


def _browser_tool(profile: Any, raw_input: Any) -> str | None:
    if profile != BROWSER_PROFILE:
        return None
    chosen = raw_input.get("browser_tool") if isinstance(raw_input, Mapping) else None
    return shown(chosen) or BROWSER_TOOL_DEFAULT


def _qa_identity(profile: Any, raw_input: Any) -> str | None:
    """The QA identity a step's browser is signed in as, or None: read only
    on the profile that declares the input (contract request 106)."""
    if profile != BROWSER_PROFILE or not isinstance(raw_input, Mapping):
        return None
    return shown(raw_input.get("qa_identity"))


def account_fields(raw: Any) -> dict[str, str | None] | None:
    """The three account keys this shows -- `account_id`, `provider`,
    `status` -- and none of the rest, or None for no account object."""
    if not isinstance(raw, Mapping):
        return None
    return {
        "account_id": raw.get("account_id") if isinstance(raw.get("account_id"), str) else None,
        "provider": raw.get("provider") if isinstance(raw.get("provider"), str) else None,
        "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
    }


def add_step(facts: dict[str, list], step: Any) -> dict[str, list]:
    """A spec step or a workflow's step: its profile, browser tool and QA identity."""
    if isinstance(step, Mapping):
        profile = step.get("runner_profile")
        facts["profiles"].append(shown(profile))
        facts["browser_tools"].append(_browser_tool(profile, step.get("input")))
        facts["qa_identities"].append(_qa_identity(profile, step.get("input")))
    return facts


def add_task(facts: dict[str, list], task: Any) -> dict[str, list]:
    """A task document as the API served it: who submitted it, its profile,
    its account and model, its browser tool and its session tag."""
    if not isinstance(task, Mapping):
        return facts
    facts["started_by"].append(shown(task.get("submitted_by")))
    add_step(facts, task)
    account = account_fields(task.get("account"))
    if account is not None:
        facts["accounts"].append(account)
    facts["models"].append(shown(task.get("model")))
    facts["sessions"].append(session.owner_of(task))
    return facts


def for_task(task: Any) -> dict[str, list]:
    return add_task(empty(), task)


def for_workflow(envelope: Any) -> dict[str, list]:
    """`GET /v1/workflows/{id}`'s envelope: the workflow's own `submitted_by`
    and session tag, its steps, and every step task it served."""
    facts = empty()
    if not isinstance(envelope, Mapping):
        return facts
    workflow = envelope.get("workflow") if isinstance(envelope.get("workflow"), Mapping) else {}
    facts["started_by"].append(shown(workflow.get("submitted_by")))
    facts["sessions"].append(session.owner_of(workflow))
    for step in workflow.get("steps") or []:
        add_step(facts, step)
    for task in envelope.get("tasks") or []:
        add_task(facts, task)
    return facts


def for_spec(spec: Mapping[str, Any], submitter: Any = None) -> dict[str, list]:
    """A spec about to be submitted: its steps' profiles and browser tools,
    `submitter` (the identity the API says the submission is stored under),
    this session's tag (every submission carries it) and no account yet."""
    facts = empty()
    facts["started_by"].append(shown(submitter))
    for step in spec.get("steps") or []:
        add_step(facts, step)
    facts["sessions"].append(session.current())
    facts["pending"].append(True)
    return facts


def per_step(envelope: Any) -> dict[str, dict[str, list]]:
    """`{step_id: facts}` for each step task in a workflow envelope, each
    with the workflow's own submitter and tag behind the task's."""
    out: dict[str, dict[str, list]] = {}
    if not isinstance(envelope, Mapping):
        return out
    workflow = envelope.get("workflow") if isinstance(envelope.get("workflow"), Mapping) else {}
    steps = {s.get("step_id"): s for s in workflow.get("steps") or [] if isinstance(s, Mapping)}
    for task in envelope.get("tasks") or []:
        if not isinstance(task, Mapping) or not isinstance(task.get("step_id"), str):
            continue
        facts = add_step(empty(), steps.get(task["step_id"])) if task["step_id"] in steps else empty()
        add_task(facts, task)
        facts["started_by"].append(shown(workflow.get("submitted_by")))
        facts["sessions"].append(session.owner_of(workflow))
        out[task["step_id"]] = facts
    return out


def merge(many: Iterable[dict[str, list]]) -> dict[str, list]:
    facts = empty()
    for one in many:
        for key in facts:
            facts[key].extend(one.get(key) or [])
    return facts


# --------------------------------------------------------------------------
# The line
# --------------------------------------------------------------------------


def account_labels(client: Any) -> dict[str, str]:
    """`{account_id: label}` from `GET /v1/accounts`: the accounts this tenant
    may run on, by the label their owner gave. Only those two keys are read.
    Best effort: an account not listed is shown by its id."""
    try:
        listing = client.request("GET", "/v1/accounts")
    except Exception:  # noqa: BLE001 - a label is never worth a failed launch
        return {}
    labels: dict[str, str] = {}
    entries = listing.get("accounts") if isinstance(listing, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("account_id"), str):
            label = shown(entry.get("label"))
            # A credential-shaped label is not a name: the account is then
            # shown by its id, as an unlisted one is.
            if label and label != WITHHELD:
                labels[entry["account_id"]] = label
    return labels


#: What an account's `status` says when no account is held.
_STATUS_WORDS = {
    "not_assigned_yet": "not assigned yet",
    "not_assigned": "never assigned",
    "no_model_call": "no model call",
    "not_pooled": "not from the pool",
}


def account_text(account: Mapping[str, Any], labels: Mapping[str, str] | None = None) -> str | None:
    """`<label or id> (<provider>)` for an assigned account, else None."""
    account_id = account.get("account_id")
    if account.get("status") not in (None, "assigned") or not isinstance(account_id, str) or not account_id:
        return None
    name = (labels or {}).get(account_id) or shown(account_id)
    provider = shown(account.get("provider"))
    return f"{name} ({provider})" if provider and name != WITHHELD else name


def llm_text(facts: Mapping[str, list], labels: Mapping[str, str] | None = None) -> str:
    """The LLM field: the accounts held and the model, or why there is none."""
    accounts = facts.get("accounts") or []
    held = _named(account_text(a, labels) for a in accounts)
    models = _named(facts.get("models") or [])
    if held:
        head = held
    else:
        waiting = _named(_STATUS_WORDS.get(str(a.get("status"))) for a in accounts)
        if not waiting and facts.get("pending"):
            waiting = "not assigned yet"
        head = waiting or NOT_REPORTED
    return f"{head}, model {models}" if models else head


def session_text(facts: Mapping[str, list], sid: str | None = None) -> str:
    tagged = [s for s in facts.get("sessions") or [] if s]
    if not tagged:
        return NOT_REPORTED
    sid = session.current() if sid is None else sid
    named = []
    for tag in tagged:
        text = f"this session ({tag})" if sid and tag == sid else f"another Claude Code session ({tag})"
        named.append(text)
    return _named(named) or NOT_REPORTED


def browser_text(facts: Mapping[str, list]) -> str:
    profiles = [p for p in facts.get("profiles") or [] if p]
    if profiles and BROWSER_PROFILE not in profiles:
        return "not used"
    return _named(facts.get("browser_tools") or []) or NOT_REPORTED


def line(
    workspace: str | None,
    facts: Mapping[str, list],
    labels: Mapping[str, str] | None = None,
    sid: str | None = None,
) -> str:
    """The DETAILS line, the six fields in their fixed order, and `QA
    identity` before `Session` when a step names one. `workspace` is
    `launch.workspace_shown`'s text (None: not reported)."""
    fields = [
        ("Workspace", workspace or NOT_REPORTED),
        ("Started by", _named(facts.get("started_by") or []) or NOT_REPORTED),
        ("Profiles", _named(facts.get("profiles") or []) or NOT_REPORTED),
        ("LLM", llm_text(facts, labels)),
        ("Browser tool", browser_text(facts)),
    ]
    qa_identity = _named(facts.get("qa_identities") or [])
    if qa_identity:
        fields.append(("QA identity", qa_identity))
    fields.append(("Session", session_text(facts, sid)))
    return " · ".join(f"{name}: {value}" for name, value in fields)


def llm_line(task: Any) -> str | None:
    """`<account> (<provider>)[, model <model>]` once the task's latest
    attempt holds a pool account, else None: what a row's progress line says
    once after the assignment (`compact.watch_progress`)."""
    if not isinstance(task, Mapping):
        return None
    account = account_fields(task.get("account"))
    held = account_text(account) if account else None
    if not held:
        return None
    model = shown(task.get("model"))
    return f"{held}, model {model}" if model else held
