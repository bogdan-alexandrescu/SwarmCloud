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

#: Refusals that arrived with the personal workspace (#868) and are NOT
#: switches, each for a stated reason. Not ESTABLISHED (they were not on main
#: on 2026-10-08) and not a precedent: a refusal on an existing path still
#: goes behind a switch. Owner to confirm (questions.json on #868's fix).
NOT_SWITCHABLE_CODES = frozenset({
    # Already behind their own setting, WORKSPACE_GATE (off, read once in
    # `workspaces.gate_from_env`), as REPOSITORY_NOT_GRANTED is behind
    # REPOSITORY_GRANTS_ENFORCED. The gate also withholds a person's tenant
    # (`withholds_tenant`), so a REFUSAL_ switch that let the submission
    # through with the gate on would run it without a tenant.
    "WORKSPACE_NOT_READY",
    "NO_CLAUDE_ACCOUNT",
    # Raised only by the workspace endpoints this change introduces, so no
    # existing caller can be refused by them (#845's failure). Continuing past
    # any of them is the breach itself: a service account or a secret admin
    # owning a workspace, a workspace on another identity's tenant, lending
    # another tenant's account, or a record moved out of its state machine.
    "WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS",
    "WORKSPACE_PRINCIPAL_FORBIDDEN",
    "WORKSPACE_ID_TAKEN",
    "WORKSPACE_REQUEST_TOO_SOON",
    "WORKSPACE_FAILED",
    "WORKSPACE_NOT_REQUESTED",
    "WORKSPACE_NOT_FOUND",
    "WORKSPACE_WRONG_STATE",
    "ACCOUNT_NOT_LENDABLE",
    "ACCOUNT_IS_THEIRS",
    # The forge-credential broker (#1041; docs/design/user-scoped-secrets.md
    # §3.2), for the same two reasons: raised only by the new route
    # `POST /v1/attempts/forge-credential`, which no existing caller uses, and
    # continuing past any of them IS the breach -- report-only would hand a
    # person's GitHub token to another member's attempt, a stale generation,
    # or another tenant. Owner to confirm (questions.json on #1041 step 2).
    "forge_credential_unauthenticated",
    "forge_credential_unproven",
    "task_not_found",
    "attempt_superseded",
    "spec_unverified",
    "credential_not_user_slot",
    "credential_not_submitters",
    "grant_removed",
    "grant_read_only",
    "credential_unavailable",
    # The broker's answer for a service submission's run when no installation
    # token is minted (DAYONE-SERVICE-APP-TOKEN, part of #2688). A credential
    # route, so never switchable, and there is nothing to continue past to:
    # report-only would release no token at all. It refuses nobody -- the
    # worker reads it as "keep the slot", today's service rule.
    "app_installation_unavailable",
    # The child route's check of its parent's signature (#1041, the security
    # review of PR 1070, 2026-10-18). On an EXISTING route, and still not a
    # switch: a credential route is never switchable, and continuing past it
    # IS the breach -- swarm-api signing a child over a submitter and a
    # person's forge slot that an agent wrote into its own task, which the
    # broker then releases. Every deployed swarm-api signs every task
    # (`specsigning.signer_from_settings` refuses to start without the key),
    # so a parent a deployed worker runs has a signature to verify; only a
    # rewritten one is refused.
    "child_parent_unsigned",
    # An issue run's owner signature (#1569, lane RUN-OWNER-SIG; owner
    # decision 2026-10-23 ~17:27Z, "sign the owner fields now"), for the same
    # reason: continuing past it IS the breach -- the run's workflow submitted
    # as a member an agent wrote into its own run document, and that member's
    # forge slot resolved for it. Enforced wherever runs are signed (a
    # deployed swarm-api always has the signer), never report-only.
    "run_owner_unverified",
    # The loan decline (LOAN-DECLINE, docs/workspaces.md §6.4), as the
    # workspace codes above: raised only by the POST
    # /v1/admin/people/{workspace_id}/loan/decline that change introduces, so
    # no existing caller can be refused by it. Continuing past it is the
    # breach itself: a request already lent against, or never made, written
    # as `declined` out of its state machine.
    "LOAN_REQUEST_NOT_OPEN",
    # The live-browser hand-off (LB-L10, docs/design/live-browser.md §4), as
    # the workspace codes above: raised only by the three routes that change
    # introduces (POST /tasks/{id}/handoff, /handoff/resolve, /human-ready),
    # so no existing caller can be refused by them. Continuing past any of
    # them is the breach itself: an unsigned or unauthenticated worker
    # parking a task, a stale generation writing past its fence (invariant
    # 5), a second wait or a wait past the cap, someone other than the
    # submitter promoting a parked task, or a task that is not
    # PARKED(HUMAN_REQUIRED) moved to READY out of its state machine.
    "handoff_unauthenticated",
    "handoff_unproven",
    "handoff_fenced",
    "handoff_pending",
    "handoff_limit",
    "human_ready_not_eligible",
    "not_waiting_for_a_person",
    # Named browser logins (LB-L9b, docs/design/live-browser.md §5.5.1), as the
    # hand-off codes above: raised by the routes that change introduces
    # (/v1/browser-logins, POST /tasks/{id}/browser-login and its worker-only
    # /pending and /report). Continuing past any of them is the breach itself:
    # a session saved in a group tenant whose workers share one identity, an
    # unsigned or stale worker reading or marking a login, or an expired,
    # unknown or never-stored login handed to an agent.
    # `browser_login_personal_only` is also raised on POST /v1/tasks and
    # /v1/workflows, which exist; it is not a switch there either, because no
    # caller can be newly refused by it: `browser_login` is not declared by any
    # runner profile (contract request in LB-L9b's PR), so the same submission
    # is already refused as `invalid_input`. Off, it would say that instead.
    "browser_login_personal_only",
    "browser_login_not_live",
    "browser_login_unknown",
    "browser_login_expired",
    "browser_login_unauthenticated",
    "browser_login_unproven",
    "browser_login_fenced",
    # The browser-session slot (LB-L8c), raised only by the new worker-only
    # POST /v1/tasks/{id}/browser-session/slot. Continuing past either is the
    # breach itself: swarm-api creating a secret outside the caller's own
    # browser prefix, or rewriting another workspace's secret's policy.
    "browser_slot_outside_prefix",
    "browser_slot_taken",
    # The live browser (LB-L9, docs/design/live-browser.md §3.3, §4.2, §5.1),
    # as the hand-off codes above: raised only by the routes that change
    # introduces (POST /attempts/{id}/live-session and /tasks/{id}/live/...),
    # so no existing caller can be refused by them. Continuing past any of
    # them is the breach itself: an unsigned or unauthenticated worker given
    # a tunnel ticket, a stale generation streaming past its fence (invariant
    # 5), a live path on a profile or backend that has none (§8.1), a ticket
    # for a closed session, a member controlling without the submitter's
    # leave, a second controller at once, or a non-controller ending
    # someone's control.
    "live_unauthenticated",
    "live_unproven",
    "live_fenced",
    "live_not_available",
    "live_session_closed",
    "control_not_eligible",
    "control_held",
    "not_controller",
    # The runner git tools (G5, docs/runner-git-tools.md §1.2-§1.4), as the
    # live codes above: raised only by the route that change introduces
    # (POST /v1/attempts/{id}/git/{tool}), so no existing caller can be
    # refused by them. Continuing past any of them is the breach itself: an
    # unproven caller handed a tenant's forge token, a stale generation
    # pushing past its fence (invariant 5), or a push or private read the
    # signed spec's policy or the tenant's allowlist does not admit.
    "git_tool_unproven",
    "git_fenced",
    "git_tool_refused",
    # The search tool (SEARCH-TOOL, docs/search-tool.md), as the git tools'
    # codes above: raised only by the route that change introduces (POST
    # /v1/attempts/{id}/search), which is a 404 while SEARCH_TOOL_ENABLED is
    # off, so no existing caller can be refused by them. Continuing past any
    # of them is the breach itself: an unproven caller spending the
    # platform's provider key, a stale generation searching past its fence
    # (invariant 5), or a tenant past the daily cap the owner's budget sets.
    "search_unproven",
    "search_fenced",
    "search_refused",
    # The workspace secrets store (SEC-STORE-API, docs/design/
    # workspace-secrets.md §5), as the search tool's codes above: raised only
    # by the management routes that change introduces, when a secrets-form
    # link is presented, so no existing caller can be refused by it.
    # Continuing past it is the breach itself: an unknown, used or expired
    # link, or one minted for another person, workspace, action or secret,
    # accepted as authority to add or rotate a secret.
    "SECRET_LINK_INVALID",
    # The workspace secrets release (SEC-RELEASE, docs/design/
    # workspace-secrets.md §3.3), as the store's code above: raised only by
    # the route that change introduces (POST /v1/attempts/{id}/secrets:release),
    # so no existing caller can be refused by them. Continuing past any of
    # them is the breach itself: an unproven caller handed a workspace's
    # secrets, a stale generation holding them past its fence (invariant 5),
    # a second release of one attempt, or a release computed from a task
    # document whose signature does not verify.
    "secrets_release_unproven",
    "secrets_release_fenced",
    "secrets_release_spent",
    "secrets_release_spec_unverified",
    # The worker's state through swarm-api (ISO-STATE-API, docs/design/
    # tenant-settings-integrity.md §4.2), as the secrets release's codes
    # above: raised only by the /v1/attempts/{id}/state/... routes that
    # change introduces, which nothing calls until SWARM_STATE_VIA_API is on
    # for a tenant, so no existing caller can be refused by them. Continuing
    # past any of them is the breach itself (#1457): another tenant's worker
    # account or an agent writing a worker's state, a stale generation
    # writing past its fence (invariant 5), an agent-chosen transition, a
    # wake on an upstream the task never declared, or a write over a
    # document that changed under it.
    "attempt_unauthenticated",
    "attempt_unproven",
    "attempt_fenced",
    "transition_refused",
    "upstream_not_declared",
    "merge_wake_refused",
    "state_conflict",
    # The tenant's MCP servers (MCP-API, part of #1433), as the secrets
    # release's codes above: raised only by the routes that change introduces
    # (/v1/mcp-servers, POST /v1/attempts/{id}/mcp:attach) and by the sweep's
    # credential writer, all of which exist only while SWARM_MCP_SERVERS is
    # on (dark by default), so no existing caller can be refused by them.
    # Continuing past any of them is the breach itself: an unproven caller or
    # a stale generation (invariant 5) handed a per-run MCP credential, a
    # second attach of one attempt, an attach computed from a task document
    # whose signature does not verify, or a server registered with a URL or
    # description shaped like a credential or an unregistered, non-public
    # address. The feature flag is the switch; a REFUSAL_ switch that let
    # these through would serve credentials unproven.
    "mcp_credential_shape",
    "mcp_server_invalid",
    "mcp_server_not_found",
    "mcp_server_exists",
    "mcp_attach_unproven",
    "mcp_attach_fenced",
    "mcp_attach_spent",
    "mcp_attach_spec_unverified",
    # The submission's MCP checks (MCP-SUBMIT, docs/design/
    # registered-mcp-servers.md §4.3, part of #1433), as MCP-API's codes
    # above: raised only for a submission that names a server in
    # `input.mcp_servers` or carries `mcp_credentials`, both introduced by
    # that change, and after `require_enabled` while SWARM_MCP_SERVERS is off
    # (dark by default), so no existing caller can be refused by them.
    # Continuing past any of them is the breach itself: a run attached to a
    # server its tenant never registered or its profile is not allowed, a
    # child widening what its tree reaches past its parent's signed input, a
    # `dispatch` server run with no credential, or a credential stored for a
    # server whose key is not the caller's to send.
    "mcp_server_unregistered",
    "mcp_server_profile_refused",
    "mcp_server_not_inherited",
    "mcp_credential_missing",
    "mcp_credential_unexpected",
    # A step's additional repositories (MR-API, docs/design/
    # multi-repository-steps.md §4.1, part of #1434), as MCP-SUBMIT's codes
    # above: raised only for a submission that names an entry in
    # `additional_repositories`, introduced by that change, and only while
    # ADDITIONAL_REPOSITORIES_ENABLED is on (off by default; off, any entry is
    # `additional_repositories_disabled`), so no existing caller can be
    # refused by them. Continuing past any of them is the breach itself: a
    # task cloning a repository its tenant never registered or an admin never
    # let its tasks read, one its slot cannot read, past the platform's count
    # or size limits, or on a profile whose backend cannot clone it. The
    # feature flag is the switch.
    "additional_repositories_disabled",
    "repository_not_registered",
    "repository_not_readable_by_tasks",
    "additional_repository_unreadable",
    "additional_repository_duplicate",
    "too_many_additional_repositories",
    "additional_repositories_too_large",
    "additional_repositories_unsupported_profile",
    # The preferences store (PROFILE-STORE, docs/design/user-profile.md §4.2,
    # §4.4, part of #1505), as MCP-API's codes above: raised only by
    # `preferences.save` and `display_names`, which that change introduces and
    # no existing route calls (the routes are PROFILE-API's), so no existing
    # caller can be refused by them. Continuing past any of them is the
    # breach itself: a save that silently overwrites another tab's, or a
    # display name that breaks §4.4's format rules, reads as a platform role,
    # or impersonates another person.
    "preferences_changed",
    "display_name_invalid",
    "display_name_reserved",
    "display_name_taken",
    # A step's model class (MODEL-API, docs/design/model-per-step.md §4.4,
    # part of #1435): raised only for a submission that names `model_class`,
    # introduced by that change, so no existing caller can be refused by
    # them. Continuing past any of them is the breach itself: a step stored
    # with a class that is not in the frozen vocabulary, one its profile does
    # not run, or one this deployment pins no model for, each of which the
    # scheduler refuses only at dispatch, after admission has leased it.
    # `model_is_chosen_by_model_class` is NOT here: it can refuse today's
    # `model` callers, so it is a switch.
    # An issue run's planner class (MODEL-RUNS, same section) raises the
    # first two as well: only for a POST /v1/runs body that names
    # `model_class`, a field `RunCreate` gains in lane MODEL-API. Continuing
    # past either is not the request let through: an unknown class, or one
    # the planner's profile does not run, has no model to run, and
    # `submit_tasks` would refuse it later as a schema error, after the
    # issue's open work was read.
    "unknown_model_class",
    "model_class_not_accepted",
    "model_class_not_yet_available",
    # Workspace roles (ROLES-API-CORE, docs/design/workspace-roles.md §4.9,
    # part of #1518): raised only by the role write routes in
    # `routes/roles.py`, which that change introduces, so no existing caller
    # can be refused by them. Continuing past any of them is the breach
    # itself: a grant to a service account would widen the scope its listing
    # gives it, a role written in a personal workspace would let someone
    # else hold one there, an `admin` default would make every member an
    # admin, a write that changes nothing would append a grant and an audit
    # row for no change, and a role outside the ladder would be stored as a
    # grant every reader skips. `role_member_required` and
    # `role_admin_required` are NOT here: they gate existing routes and are
    # switches (refusals.SWITCHES).
    "role_person_required",
    "personal_workspace_roles_fixed",
    "default_role_not_allowed",
    "role_unchanged",
    "unknown_role",
    # Workspace kinds (WK-RECORD, docs/design/workspace-kinds.md §4.2.3, part
    # of #847): raised only by `POST /v1/admin/workspaces`, which that change
    # introduces, so no existing caller can be refused by them (design §4.2.2
    # says so). Continuing past any of them is the breach itself: a workspace
    # for an address outside the allowed domains, built over a Terraform
    # tenant, nesting another tenant's forge slots, a group taking a person's
    # `u-` id, an admin group made a tenant, a service account the job cannot
    # check, one of the platform's own accounts admitted as a tenant, or an
    # account given a second tenant.
    "WORKSPACE_DOMAIN_NOT_ALLOWED",
    "WORKSPACE_ID_TERRAFORM",
    "WORKSPACE_ID_NESTED",
    "WORKSPACE_GROUP_ID_RESERVED",
    "WORKSPACE_GROUP_IS_ADMIN",
    "WORKSPACE_SERVICE_NOT_IN_PROJECT",
    "WORKSPACE_SERVICE_PLATFORM",
    "WORKSPACE_SERVICE_ALREADY_RESOLVES",
    # Raised by the existing approve and retry routes, but only on a
    # `service` record, which only the new route above creates: no record an
    # existing caller acts on can meet it. Report-only would let an admin who
    # is not the owner admit a new principal at the front door (Q2 (a)).
    "WORKSPACE_APPROVAL_OWNER_ONLY",
    # The QA identity (QI-API, docs/design/qa-identity.md §4.3, part of
    # #1649). The signing route's codes are raised only by the route that
    # change introduces (POST /v1/attempts/{id}/qa-identity), and the admin
    # route's by its own routes, so no existing caller can be refused by them.
    # Continuing past any of them is the breach itself: an unproven caller, a
    # stale generation (invariant 5) or a released lease handed a credential
    # that signs in to the console, one signed for a task whose signed input
    # never asked for it or whose tenant holds no grant, or the QA workspace
    # granted its own identity. `qa_identity_not_granted` and
    # `qa_identity_unconfigured` are also raised on POST /v1/tasks,
    # /v1/tasks/batch and /v1/workflows, which exist, and on the child route,
    # but only for an input naming `qa_identity`, which contract request 106
    # declared the day before and nothing submits yet (the console offers it
    # with QI-UI, after this). Report-only there would not let the request
    # through: the signing route refuses the same tenant, so the task would
    # fail at its first renewal instead of at submission. `qa_identity_origin`
    # gates every route, but only for a request IAP authenticated as the
    # QA account, which no deployment configures until this change renders
    # QA_IDENTITY_CONSOLE_ACCOUNT; continuing past it is a cross-site write
    # made as the QA account (§3.2). `spec_unverified` is listed above.
    "bad_signature",
    "stale_generation",
    "lease_not_live",
    "qa_identity_not_granted",
    "qa_identity_not_requested",
    "qa_identity_unconfigured",
    "qa_identity_origin",
    "qa_identity_self",
    # Runner sizes (SIZES-API, docs/design/runner-sizes.md §4.5, part of
    # #1689): raised only by PUT /v1/admin/runner-sizes and POST
    # /v1/admin/runner-sizes/restore, which that change introduces, so no
    # existing caller can be refused by them. Continuing past any of them is
    # the breach itself: a size table outside the frozen bounds stored as the
    # one every dispatch reads (invariant 7), a memory cut under a class's
    # recent peak written unconfirmed, or a second admin's write silently
    # replacing the first's.
    "stale_base_version",
    "size_out_of_bounds",
    "below_recent_peak",
    # QA targets (VQ2-TARGETS, docs/design/visual-qa-any-app.md §1, §2, §6,
    # part of #1933): raised only by the /v1/qa-targets routes and POST
    # /v1/attempts/{id}/qa-target, which that change introduces, so no
    # existing caller can be refused by them. Continuing past any of them is
    # the breach itself: a run, or a worker's read of its target, against a
    # host no platform admin approved (§1); a run filing issues in a
    # repository whose agents may not file them; or a test credential stored
    # through a link that is unknown, used, expired, or minted for another
    # person, workspace or target (§2.1).
    "qa_target_host_pending",
    "issue_writes_off",
    "qa_credential_link_invalid",
    # Child issue runs and child workflows (AU-6, docs/design/child-tasks.md
    # §10): on the existing child route, but raised only for a request naming
    # `kind: "issue_run"`, `kind: "workflow"` or `after_merged`, which that
    # change introduces -- a request with no `kind` is an agent child, as it
    # always was -- so no existing caller can be refused by them. Continuing
    # past any of them is the breach itself: an agent starting a run on
    # another repository's issue, or on no repository at all, a workflow from
    # a spec no platform admin approved at its current digest (invariant 10),
    # or a run HELD behind a predecessor this parent never started.
    "child_run_other_repository",
    "child_run_no_repository",
    "child_workflow_unapproved",
    "child_after_merged_unknown",
    # A step's runtime policy (RUNTIME-BY-DIFF, docs/workflows.md, part of
    # #2322), as MODEL-API's codes above: raised only for a submission whose
    # metadata names `runtime`, the block that change introduces and which no
    # caller sent before it (the plugin and the issue-run planner learn to
    # write it in the same change), so no existing caller can be refused by
    # it. Continuing past it is the breach itself: a policy swarm-api did not
    # check would let the scheduler move a step between profiles it never
    # signed an alternate for -- a pair, a shape or a starting profile chosen
    # by the caller (invariant 10) -- or put every step of a workflow on the
    # browser profile.
    "invalid_runtime",
    # Join requests (JOIN-STORE, docs/design/join-requests.md §4.6, §4.9, part
    # of #2325): raised only by the join-request and join-link routes and the
    # stores behind them, which that design introduces, so no existing caller
    # can be refused by them. Continuing past any of them is the breach
    # itself: a person admitted through a link nobody turned on, a request
    # whose requester was edited after it was signed, a decision on a request
    # already decided or on a workspace whose group changed, a person made
    # `admin` without being named, a request decided by someone who is not
    # that workspace's admin (`join_decider_required`, gated from its first
    # build like WORKSPACE_APPROVAL_OWNER_ONLY), or a queue nobody owns
    # growing past its limits.
    "join_link_unknown",
    "join_already_member",
    "join_already_reaches",
    "join_not_offered",
    "join_request_unknown",
    "join_request_invalid",
    "join_request_decided",
    "join_workspace_changed",
    "join_role_not_allowed",
    "join_decider_required",
    "join_requests_open_limit",
    "join_requests_daily_limit",
    "join_denied_recently",
    "join_workspace_queue_full",
})

#: What the guard lets through without a switch.
ALLOWED_CODES = ESTABLISHED_CODES | NOT_SWITCHABLE_CODES

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
    missing = unlisted(codes, ALLOWED_CODES)
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
    assert sorted(ALLOWED_CODES - set(codes)) == []
    assert not ESTABLISHED_CODES & NOT_SWITCHABLE_CODES
    assert sorted(ESTABLISHED_FAILURE_CODES - set(onboarding.COPY)) == []


def test_a_switch_is_never_also_established(codes):
    for code, switch in refusals.SWITCHES.items():
        assert switch.code == code
        assert code not in ALLOWED_CODES | ESTABLISHED_FAILURE_CODES, code
        assert code in codes or code in onboarding.COPY, f"switch {code} gates nothing"


def test_the_guard_refuses_a_new_unswitched_refusal(codes, monkeypatch):
    """The mutation, committed: a refusal declared the way #845's was, with
    no switch, is caught; registering a switch is what lets it through."""

    class ProbeRefusal(Forbidden):
        code = "probe_new_refusal"

    probe = dict(codes)
    probe[ProbeRefusal.code] = "probe"
    assert unlisted(probe, ALLOWED_CODES) == ["probe_new_refusal (probe)"]

    monkeypatch.setitem(refusals.SWITCHES, ProbeRefusal.code,
                        Switch(code=ProbeRefusal.code, why="probe"))
    assert unlisted(probe, ALLOWED_CODES) == []


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
