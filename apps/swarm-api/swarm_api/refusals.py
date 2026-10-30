"""Refusal switches: a NEW refusal ships switched off.

Owner decision 2026-10-08 (observer proposal I). On 2026-10-07 #845 deployed
a new refusal -- 403 REPOSITORY_NOT_GRANTED -- before #840, the GitHub App id
and slug a person needs to connect GitHub and choose repositories at all. The
refusal was correct and nobody could satisfy it, so every person's submission
was refused for about two hours. A refusal is only as safe as whatever lets a
caller avoid it, and that is often another change, deployed separately.

So from now on a refusal added to swarm-api ships REPORT-ONLY: the request
goes through, and the refusal it would have made is logged with its code. A
later release turns it on once the log shows only the refusals that were
meant. Every refusal that existed on main on 2026-10-08 is ESTABLISHED and
always enforced; `tests/unit/control_plane/test_new_refusals_ship_off.py`
lists them and fails on a 4xx code that is neither listed there nor in
`SWITCHES` below.

How a refusal is added:

    1. Give it its own code (an `ApiError` subclass with its own `code`, or a
       new `onboarding.COPY` failure code). A new refusal that reuses
       `forbidden` cannot be switched, because the code is the switch's key.
    2. Register a `Switch` for that code in `SWITCHES`, default off.
    3. Raise it through `refuse(error)`, never `raise error`, and write the
       call site so that execution continuing past `refuse` is the request
       being let through.

How it is turned on: `REFUSAL_<CODE>=on` in the service's environment,
rendered from terraform's `api_refusals` map (terraform/infra/locals.tf,
`local.api_refusal_env`). `off` turns it back off without a redeploy of the
code. A value other than on/off, or a REFUSAL_ variable naming no switch, is
refused at process start (`validate_environment`, called from
`ApiSettings.from_env`): a typo in a switch is a refusal someone believes is
on, the REQUIRE_AUTH mistake settings.py describes.

`REPOSITORY_GRANTS_ENFORCED` predates this module and is NOT one of these
switches: its off position is not "let the request through unchanged" but
"run with the tenant token instead" (`SubmissionService._resolve_forge`),
so it keeps its own setting. REPOSITORY_NOT_GRANTED is in the established
list because it is on main, behind that setting, today.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ApiError

log = logging.getLogger(__name__)

ENV_PREFIX = "REFUSAL_"
ON = "on"
OFF = "off"


def env_name(code: str) -> str:
    """The environment variable that switches the refusal `code`."""
    return ENV_PREFIX + re.sub(r"[^A-Z0-9]+", "_", code.upper()).strip("_")


@dataclass(frozen=True)
class Switch:
    """One refusal that can be off. `code` is the `ApiError.code` (or the
    `detail.failure_code`) it gates; `why` says what a caller needs before it
    can be on, so whoever turns it on knows what to check first."""

    code: str
    why: str
    #: Enforced when the environment says nothing. False for every new
    #: refusal; a later release may flip it once the switch has been on in
    #: every environment.
    default: bool = False

    @property
    def env(self) -> str:
        return env_name(self.code)


#: Every switched refusal, by code. Empty on the day this module landed: every
#: refusal on main then was established and stays enforced.
SWITCHES: dict[str, Switch] = {
    # An admin's manual approval of a workspace whose tenant predates the
    # workspace job (docs/workspaces.md §1.3, §3.3). The People pane offers
    # no Approve on such a record, so only a direct API call meets it.
    "WORKSPACE_MIGRATING": Switch(
        code="WORKSPACE_MIGRATING",
        why="lane W9's migration has written the record of every tenant "
        "Terraform made, so no admin needs to approve one by hand",
    ),
    # The schedule tick's budget and failure stops (docs/schedules.md §4.3,
    # §4.4, lane S2). Each refuses a SCHEDULE'S OWN next firing, never a
    # person's request, and is raised by `schedulefire` through `refuse`.
    # Off, the firing goes ahead and the log names the stop it would have
    # made. A departed owner (OWNER_NOT_MEMBER) is NOT here: submitting as
    # someone who left the tenant is an isolation breach (invariant 9), and
    # it is the established `owner_not_member` refusal.
    "BUDGET_EXHAUSTED": Switch(
        code="BUDGET_EXHAUSTED",
        why="a schedule's firing is skipped when today's reported spend plus the per-run "
        "cap for every live item and unreported attempt passes per_day_usd (§4.3). On "
        "once the budget figures of S1 have been read against real firings' costs",
    ),
    "RUN_OVER_BUDGET": Switch(
        code="RUN_OVER_BUDGET",
        why="a firing whose work recorded more than per_run_usd ends failed and pauses its "
        "schedule (§4.3). On once attempts report cost_usd for the schedule's profiles",
    ),
    # The §4.4 holds on an issue run (docs/schedules.md §4.5, lane S5): an
    # approval, auto-approval or merge of a run whose plan or pull request
    # touches IAM or Terraform bootstrap, the frozen contract in a
    # `platform: true` repository, or a registration's `hard_stop_paths`,
    # by someone the hold does not admit. Off, the hold is still recorded and
    # shown in the inbox, and the log names what it would have refused.
    # NOT switched, by the owner's SD3 decision: the security-class hold,
    # which `routes.runs` raises directly, and the `.github/workflows/`
    # refusal, which fails the run at plan (`issueruns.WORKFLOWS_PATH`) --
    # each records what the platform already cannot do or the owner already
    # decided, so a report-only phase would only let through work that
    # cannot succeed.
    "hold_approver_required": Switch(
        code="hold_approver_required",
        why="an issue run held by an IAM, bootstrap, frozen-contract or protected-path stop "
        "(docs/schedules.md §4.4) is approved only by the hold's approvers -- PLATFORM_OWNER "
        "in a platform: true repository, a second member elsewhere -- and never "
        "auto-approved or auto-merged. On once lane S11's `platform` flag is set on the "
        "platform's own registrations and the inbox (S7) shows held runs",
    ),
    # Schedule firings refused at create (lanes S6, S10d and the repo index).
    "index_refresh_failed": Switch(
        code="index_refresh_failed",
        why="a repo-index-refresh firing whose every repository failed to queue ends refused "
        "instead of succeeding empty. On once the per-repository codes in its detail have "
        "been read in the log and show only deleted or unreadable branches",
    ),
    "observer_file_issues_repo_required": Switch(
        code="observer_file_issues_repo_required",
        why="an observer schedule with file_issues true names no file_issues_repo_id, the "
        "repository its epic is filed in (owner decision 2026-10-11). Off, it is created and "
        "files nothing. On once no stored observer schedule sets file_issues without one",
    ),
    "observer_file_issues_repo_not_registered": Switch(
        code="observer_file_issues_repo_not_registered",
        why="an observer schedule's file_issues_repo_id is not a registration of its tenant. "
        "Off, it is created and files nothing: an unregistered repository is never written to. "
        "On with observer_file_issues_repo_required",
    ),
    "observer_file_issues_repo_not_writable": Switch(
        code="observer_file_issues_repo_not_writable",
        why="the owner of an observer schedule cannot write the repository file_issues_repo_id "
        "names. Off, it is created and files nothing there. On with "
        "observer_file_issues_repo_required",
    ),
    "epic_triage_unreadable": Switch(
        code="epic_triage_unreadable",
        why="an epic-triage firing whose every repository failed to list its epics with "
        "the tenant's forge credential, and that applied and took in nothing, ends refused "
        "instead of succeeding empty. On once the per-repository codes in the log show only "
        "credentials that really lack issues: read",
    ),
    "pr_shepherd_forge_refused": Switch(
        code="pr_shepherd_forge_refused",
        why="a pr-shepherd firing that could read no repository in scope, because the "
        "tenant's forge credential is missing or GitHub refused it, ends refused instead of "
        "failed with its refusals recorded. On once the refusals in such firings have been "
        "read and show only credentials that lack pull_requests access",
    ),
    "ci_flake_unreadable": Switch(
        code="ci_flake_unreadable",
        why="a ci-flake-hunter firing whose every repository's CI could not be read ends "
        "refused instead of failed (lane S10c). On once the per-repository codes in its "
        "detail have been read in the log and show only credentials lacking checks: read, "
        "contents: read or issues: write",
    ),
    "cve_scan_unreadable": Switch(
        code="cve_scan_unreadable",
        why="a dependency-cve-refresh firing that submitted nothing because no repository's "
        "image scan could be read for packages ends refused instead of succeeding empty. "
        "The scan's table is only in its job log, which needs the GitHub App's Actions: "
        "Read (docs/schedules.md §0.1 check 1). On once the App holds it, or the scan "
        "writes its packages to its check run",
    ),
    # Lane S10f (docs/schedules.md §3.10): a docs-drift `fix: true` firing
    # whose token the last probe read without push or pull-request rights.
    # Off, the fix tasks are submitted and the worker fails them
    # forge_read_only, as it does for any task.
    "docs_drift_cannot_push": Switch(
        code="docs_drift_cannot_push",
        why="a docs-drift firing with fix true is refused when the last token probe read push "
        "or open_pull_requests missing for a repository in scope. On once the token probes "
        "(git_token_checks) have run for the repositories docs-drift schedules name",
    ),
    # Lane S10h. Only the repository bound is switched: the spec digest's
    # admin approval is a document a person decides, never a refusal, and is
    # enforced from the first firing (schedtypes/custom_prompt.py).
    "custom_prompt_repository_out_of_scope": Switch(
        code="custom_prompt_repository_out_of_scope",
        why="a custom-prompt firing whose spec names a repository outside the schedule's scope "
        "is refused instead of submitted (docs/schedules.md §3.12, blast radius). On once the "
        "log shows no stored custom-prompt schedule whose spec names a repository it does not scope",
    ),
    # LOAN-NEW (docs/workspaces.md §3.3): a lend to a READY workspace whose
    # IAM grant the quota broker refused. Off, the broker's own 422 is served
    # unchanged, as it was before this code existed; nothing was changed either way.
    "LOAN_GRANT_FAILED": Switch(
        code="LOAN_GRANT_FAILED",
        why="a lend to a ready workspace is answered 422 LOAN_GRANT_FAILED naming the member "
        "and the broker's reason, instead of the broker's bare refusal. On once People shows "
        "the loan pane's error and the log shows the named members really are missing service accounts",
    ),
    # Lane G9 (docs/runner-git-tools.md §6.3). Off, a workflow read under another
    # tenant answers the bare 404 `workflow '<id>' not found`, as before.
    "workflow_not_in_tenant": Switch(
        code="workflow_not_in_tenant",
        why="a workflow read under a tenant it is not in is answered 404 workflow_not_in_tenant "
        "naming the tenant the call resolved to, instead of a bare not found. On once swarm-mcp "
        "and the coordinator callers that re-read with the launch's tenant are deployed",
    ),
    # Lane MCP-KEY-PARK (docs/design/registered-mcp-servers.md §4.10): the
    # re-store of a workflow's MCP credentials. The refusals are ALWAYS made;
    # off, each is answered with the same status and message under the
    # established code (`conflict` 409, `forbidden` 403), as the egress codes
    # below are.
    "workflow_finished": Switch(
        code="workflow_finished",
        why="a re-store of a finished workflow's MCP credentials is answered 409 "
        "workflow_finished instead of 409 conflict with the same message. On once Product "
        "Oracle's re-store call reads the code",
    ),
    "mcp_credential_restore_forbidden": Switch(
        code="mcp_credential_restore_forbidden",
        why="a re-store of a workflow's MCP credentials by anyone but its submitter or a "
        "workspace admin is answered 403 mcp_credential_restore_forbidden instead of 403 "
        "forbidden with the same message. On once a caller reads the code",
    ),
    # Lane EGRESS-SVC (docs/browser-egress.md §2.2): the attempt egress ticket
    # route. Off, each is answered with the same status and message under the
    # established code (`conflict` 409 or `forbidden` 403); egress_unavailable
    # is a 503 and not a refusal. Turn on with REFUSAL_<CODE>=on once
    # BROWSER_EGRESS_ENABLED is on and the gate reads the codes.
    "egress_off": Switch(
        code="egress_off",
        why="the egress-ticket route answers 409 egress_off naming disabled, tenant_off or "
        "profile_off. Off, it answers 409 conflict with the same message. On once the "
        "browser gate in the worker image reads the code to run direct",
    ),
    "egress_fenced": Switch(
        code="egress_fenced",
        why="the egress-ticket route answers 409 egress_fenced when the task is not RUNNING "
        "under the attempt's live lease. Off, 409 conflict with the same message. On with "
        "egress_off",
    ),
    "egress_unproven": Switch(
        code="egress_unproven",
        why="the egress-ticket route answers 403 egress_unproven when the attempt proof does "
        "not verify. Off, 403 forbidden with the same message. On with egress_off",
    ),
    "egress_unauthenticated": Switch(
        code="egress_unauthenticated",
        why="the egress-ticket route answers 403 egress_unauthenticated for a caller that is "
        "not the tenant's worker service account. Off, 403 forbidden with the same message. "
        "On with egress_off",
    ),
    # Lane SEC-INJECT-A (docs/design/workspace-secrets.md §3.5): the attempt
    # TypeSafe ticket route. Off, each is answered with the same status and
    # message under the established code (`conflict` 409 or `forbidden` 403),
    # and NOTHING is minted either way; typesafe_unavailable is a 503 and not
    # a refusal. Turn on with REFUSAL_<CODE>=on once the worker
    # (SEC-MIGRATE-TYPESAFE) reads the codes.
    "typesafe_fenced": Switch(
        code="typesafe_fenced",
        why="the typesafe-ticket route answers 409 typesafe_fenced when the task is not "
        "RUNNING under the attempt's live lease. Off, 409 conflict with the same message. On "
        "once the worker's jev launch reads the code",
    ),
    "typesafe_unproven": Switch(
        code="typesafe_unproven",
        why="the typesafe-ticket route answers 403 typesafe_unproven when the attempt proof "
        "does not verify. Off, 403 forbidden with the same message. On with typesafe_fenced",
    ),
    "typesafe_unauthenticated": Switch(
        code="typesafe_unauthenticated",
        why="the typesafe-ticket route answers 403 typesafe_unauthenticated for a caller that "
        "is not the tenant's worker service account. Off, 403 forbidden with the same "
        "message. On with typesafe_fenced",
    ),
    "typesafe_backend_off": Switch(
        code="typesafe_backend_off",
        why="the typesafe-ticket route answers 403 typesafe_backend_off when the workspace's "
        "judgment backend is not TypeSafe. Off, 403 forbidden with the same message. On with "
        "typesafe_fenced",
    ),
    "typesafe_no_key": Switch(
        code="typesafe_no_key",
        why="the typesafe-ticket route answers 403 typesafe_no_key when neither the submitter "
        "nor the workspace has a TypeSafe key set. Off, 403 forbidden with the same message. "
        "On with typesafe_fenced",
    ),
    # The one-time key-form link (KEYS-API, docs/provider-keys.md §3). The
    # link proves only that a write came through the /sc flow; the caller's
    # own console session authorises it, and the admin and personal-workspace
    # checks run before it whatever this switch says. Off, a bad link is
    # logged and the write goes through on that session, as a direct visit to
    # the settings page does. The runner-key link (`POST /v1/tenants/me/
    # credentials/links`, lane SSC-LINKS) is refused under the same code: it
    # fires only on a token that was presented, so no form needs to exist first.
    "KEY_LINK_INVALID": Switch(
        code="KEY_LINK_INVALID",
        why="a key write that presents a one-time link refuses one that is unknown, used, "
        "expired, minted by someone else or for another scope. On once the console's "
        "settings/keys form (the swarm-ui lane) sends the link token it was opened with",
    ),
    # Lane SSC-LINKS (docs/design/secrets-scopes.md §6.2, §11 Q8): a QA
    # target's challenge bypass value written without a one-time link. Off,
    # the write goes through on the admin's own session, as the console's
    # target form sends it today, and is logged without the value.
    "qa_bypass_link_required": Switch(
        code="qa_bypass_link_required",
        why="PUT /v1/qa-targets/{id}/challenge-bypass refuses a value sent without the token "
        "of a credential link minted with field challenge_bypass. On once the console's "
        "bypass form (lane SSC-UI) opens from such a link and sends its token, and the log "
        "shows no 'challenge bypass set without a link' line",
    ),
    # Lane TRAIN-WS (docs/merge-trains.md §3.2): a member of a workspace who
    # is neither its owner nor a platform admin changing its merge settings.
    # Off, the change goes through and is audited under the caller's name. A
    # caller who is not a member at all is the established 404 either way.
    "merge_settings_owner_required": Switch(
        code="merge_settings_owner_required",
        why="a member of a workspace who is neither its owner (a personal workspace's "
        "principal) nor a platform admin is refused 403 when changing its merge settings. "
        "On once the console's Settings > Workspace > Merging (TRAIN-WS-UI) offers the "
        "change only to those who may make it, and the log shows no member relying on it",
    ),
    # Lane CTXQ-SET (codeintel.py): a member of a workspace who is neither its
    # owner nor a platform admin changing its code-intelligence setting, or a
    # repository's `context` override. SHIPS ON (default=True) by owner
    # decision 2026-10-14, unlike a new refusal's usual report-only start:
    # this setting decides whether code goes to Jev. Set
    # REFUSAL_CODE_INTELLIGENCE_OWNER_REQUIRED=off to let the change through
    # (audited under the caller's name). A non-member is the established 404.
    "code_intelligence_owner_required": Switch(
        code="code_intelligence_owner_required",
        why="a member of a workspace who is neither its owner (a personal workspace's "
        "principal) nor a platform admin is refused 403 when changing its code-intelligence "
        "setting or a repository's context override. Ships on by owner decision 2026-10-14 "
        "because this setting decides whether code goes to Jev; the console offers the change "
        "only to those who may make it (the setting's can_edit)",
        default=True,
    ),
    # The browser tool a submission names (BSTATUS-API, contract request 77):
    # `jev` with no TypeSafe key for the submitter or the tenant, or with no
    # jev in the image. Off, the task runs and its runner falls back to
    # Playwright, which the live session's `browser.jev.reason` says.
    "browser_tool_unavailable": Switch(
        code="browser_tool_unavailable",
        why="a claude-code-browser submission naming browser_tool jev is refused when jev is "
        "not installed, or when the workspace's judgment backend is TypeSafe and no TypeSafe "
        "key resolves for its submitter or tenant (the default Internal Jev and Internal Laya "
        "need no key and are never refused for one). On once the Submit form disables Jev "
        "when GET /v1/provider-keys/typesafe resolves none on the TypeSafe backend, so the "
        "form never offers what is refused",
    ),
    # Epic #1334 (cifix.py `_claim`): a second CI-fix request for a failing
    # (pull request, head, run) that already has a live fix task. Off, the
    # request goes through and makes a second fix task, as before the claim.
    "ci_fix_in_flight": Switch(
        code="ci_fix_in_flight",
        why="a CI-fix submission for a pull request's red run at a head that already has a "
        "live fix task is refused 409 naming that task. On once the log shows it would have "
        "refused only second deliveries of one failure, and the CI-fix callers read the 409 "
        "as that failure being handled",
    ),
    # CI-FIXER-GATE (cifix.py `_refuse_if_lane_owns`, part of #1822): a
    # CI-fix submission for a pull request whose lane's merge step is live, or
    # whose issue run is not terminal (#2216, the same switch: an issue run
    # runs its own CI-fix rounds from issueci and has no merge step), or
    # while a conflict fix (#2456) or a plain continuation of the lane --
    # `continues_task` or `publish_to_pr` onto its branch, no writer mark
    # (#2583, `_refuse_if_continuation_live`) -- is live and pushes to it.
    # SHIPS ON (default=True) by owner decision 2026-10-21 (~15:15Z, asked
    # after this lane was held), unlike a new refusal's usual report-only
    # start (the 2026-10-08 rule): about 20% of standalone ci-fix pushes were
    # rejected racing a lane's own round (#1821), and this PR's own fixer lost
    # its push the same way. Off, it refuses nothing in production, which is
    # the 404 CI-FIXER-ONE's script-side read already got. Its one caller, scripts/ci-fix.sh, reads
    # this 409 as "skip, the lane owns this PR" in the same change, and the
    # refused fix is one the lane's own round makes. Set
    # REFUSAL_CI_FIX_LANE_OWNS_PR=off to let the standalone fix through again.
    "ci_fix_lane_owns_pr": Switch(
        code="ci_fix_lane_owns_pr",
        why="a CI-fix submission whose continued task's workflow has a merge step that is "
        "not terminal is refused 409 naming that merge step, which runs the pull request's "
        "CI-fix rounds itself, or while a non-terminal issue run of the tenant owns the pull "
        "request (its pr_task_id is the continued root, or the root's workflow is one of its "
        "ci_fix_workflows), refused naming the run, which runs its own rounds, or while a "
        "conflict fix or a plain continuation of the lane (continues_task or publish_to_pr "
        "onto its branch) is live and pushing to it, refused naming that task. Ships on by owner decision 2026-10-21: scripts/ci-fix.sh reads "
        "the 409 as a skip, and off "
        "the standalone fixer races the lane's round for the branch again",
        default=True,
    ),
    # #2535 (cifix.py `_refuse_if_fix_live`): a `continues_task` or
    # `publish_to_pr` request that pushes and carries no fix mark of its own,
    # onto a pull request that has a live CI fix or conflict fix. SHIPS ON
    # (default=True), pending the owner's answer to this lane's question
    # (2026-10-27), unlike
    # a new refusal's usual report-only start (the 2026-10-08 rule): on
    # 2026-10-26 PR #2473 got a CI fix and a `publish_to_pr` `implement` step
    # at one red head 377c3010, and the second publish was refused head_moved
    # after its agent had done the whole fix. Off -- and nothing in
    # terraform's api_refusals turns it on, as ci_fix_in_flight shows -- it
    # refuses nothing in production. The issue run's own CI-fix round meets
    # it too; #2535's second step makes issueci read it as "wait". Set
    # REFUSAL_PR_FIX_IN_FLIGHT=off to admit such a request beside the fix again.
    "pr_fix_in_flight": Switch(
        code="pr_fix_in_flight",
        why="a continues_task or publish_to_pr request that pushes to a pull request's "
        "branch and is not itself a CI fix or conflict fix is refused 409 naming the live "
        "CI fix or conflict fix on that pull request; off, both push to one branch and the later publish is refused "
        "head_moved, as on PR #2473",
        default=True,
    ),
    # #1435 (docs/design/model-per-step.md §4.4, owner's Q4): a `model` that
    # is not the pin of the step's model class. It can refuse a caller that
    # exists today -- `swarm dispatch --model` sends attribution, and any
    # string was recorded -- so it ships off. Off, the caller's `model` is
    # recorded as sent, as before; it selects nothing on any path.
    "model_is_chosen_by_model_class": Switch(
        code="model_is_chosen_by_model_class",
        why="a task, child or plugin dispatch whose `model` is not the pin of its model "
        "class is refused 422 naming the model_class to send. On once the plugin's "
        "--model-class has shipped (MODEL-PLUGIN) and the log shows no caller still "
        "sending a model of its own",
    ),
    # Workspace roles (docs/design/workspace-roles.md §4.9, lane
    # ROLES-API-CORE, part of #1518): one switch per code (Q10), each on in
    # dev first, then in prod after a week of report-only lines. Off, the
    # request goes through and `refusal report-only code=...` names who would
    # have been refused. The role write routes raise role_admin_required
    # directly, whatever this switch says: they are new, and a non-admin
    # writing a role is the breach itself.
    "role_member_required": Switch(
        code="role_member_required",
        why="a viewer is refused 403 on every member route (submit, cancel, decide, answer, "
        "schedules, repositories, access, accounts, live control, browser logins). On once "
        "GET /v1/tenants/me carries role, the console disables Submit, Approve and Cancel for "
        "a viewer, and a week of report-only lines in prod names only people an admin narrowed",
    ),
    "role_admin_required": Switch(
        code="role_admin_required",
        why="a member who is not the workspace's admin is refused 403 on its secrets, git-token "
        "tenant and repository slots, MCP servers, tenant provider keys, POST "
        "/v1/tenants/me/credentials and its settings. On once the report-only log shows no "
        "member relying on POST /v1/tenants/me/credentials and every group workspace that "
        "manages secrets has a named admin",
    ),
    # Lane FR-3 (docs/schedules.md, failure-review): a firing whose tenant
    # holds no `platform: true` registration. Off, the firing is let through
    # with nothing reviewed, written or submitted, and the log names it.
    "failure_review_no_platform_repository": Switch(
        code="failure_review_no_platform_repository",
        why="a failure-review firing whose schedule's tenant has no `platform: true` "
        "registration is refused instead of succeeding empty. On once the platform's own "
        "registration carries `platform: true` in every environment that has a "
        "failure-review schedule",
    ),
    # Repository secrets (docs/design/secrets-scopes.md §2.2, §2.5, §3.6;
    # lane SSC-REPO-SCOPE, part of #2158). Neither switch lets a write
    # through that should be refused. github_admin_required off: the caller
    # is still refused, with the established `forbidden` code
    # (`github_repo_admin.require_repo_admin`). grant_key_required off: the
    # grant is written unattested and the release drops it, because a
    # repository grant is released only when attested.
    "github_admin_required": Switch(
        code="github_admin_required",
        why="a member who is not the workspace's admin, setting a repository's secret, is "
        "refused 403 github_admin_required with detail.reason when GitHub, asked with their "
        "own token, does not call them admin of it; off, the same refusal is served as "
        "`forbidden`. On once the console (SSC-UI) shows detail.reason's copy",
    ),
    "grant_key_required": Switch(
        code="grant_key_required",
        why="enabling a grant on a repository secret where the deployment has no "
        "swarm-workspace-grant-key is refused 422 instead of writing a grant the release "
        "will never honour. On once every environment holds the grant key",
    ),
    # Lane BUDGET-CAP (#2459, `budget`): a step's budget above the workspace
    # default or the platform ceiling, and a workspace default above the
    # ceiling. SHIP ON (default=True) by BGD's answer of 2026-10-26 (~22:15Z,
    # question 5, "enforce at once"), unlike a new refusal's usual
    # report-only start (the 2026-10-08 rule): no caller sends a `budget`
    # before this feature, so the first deploy refuses nobody who worked
    # before it. Off loses no limit, which is why the switch stays: every
    # attempt's limit is min(step, workspace, ceiling), so off, the step still
    # runs under the lower limit and the default is still capped by the
    # ceiling. Set REFUSAL_BUDGET_ABOVE_LIMIT=off or
    # REFUSAL_BUDGET_ABOVE_CEILING=off to let the request through again.
    "budget_above_limit": Switch(
        code="budget_above_limit",
        why="a task or workflow step whose `budget` is above the workspace's default or the "
        "platform ceiling in the same unit is refused 422, naming both, instead of running "
        "under the lower limit. Ships on by BGD's decision 2026-10-26 (enforce at once); "
        "off, the step runs under the lower limit",
        default=True,
    ),
    "budget_above_ceiling": Switch(
        code="budget_above_ceiling",
        why="a workspace budget default above the platform ceiling in its unit is refused 422 "
        "naming the ceiling, instead of being saved and capped at every resolution. Ships on "
        "by BGD's decision 2026-10-26 (enforce at once); off, the default is saved and capped",
        default=True,
    ),
    # A step's `metadata.review_gate` (#2524, `validation.resolve_review_gate`).
    # Before it, the key was a caller label stored as sent, so refusing a bad
    # one narrows what a caller could send. Off, a malformed gate is dropped
    # and the review runs ungated.
    "invalid_review_gate": Switch(
        code="invalid_review_gate",
        why="a workflow step's metadata.review_gate with an unknown key or a bad type is "
        "refused 422. Ships on by BGD's decision 2026-10-27 (#2524: an unknown key or a bad "
        "type is a 422); off, the block is dropped and the review always runs",
        default=True,
    ),
    "CONSECUTIVE_FAILURES": Switch(
        code="CONSECUTIVE_FAILURES",
        why="a schedule pauses after N failed or refused firings in a row (§4.4, default "
        "3). On once a schedule's failures are visible in the console (S7), so a "
        "pause has a screen that says why",
    ),
    # The inbox's reject rule (docs/schedules.md §4.5; BGD 2026-10-26, #2417
    # A5f): only someone who may APPROVE an item may reject it. Until then
    # any member could. SHIPS ON (default=True), unlike a new refusal's usual
    # report-only start: BGD's picked decision page (A5f) drew the 403 and
    # hides Reject from a member who may not approve, and every caller can
    # read who may beforehand from the item's `may_decide`. Set
    # REFUSAL_REJECT_APPROVER_REQUIRED=off to let any member reject again.
    "reject_approver_required": Switch(
        code="reject_approver_required",
        why="a member whom an item's approve would refuse -- its gate's approvers, its hold, the "
        "last-editor rule or the custom-prompt spec rule -- is refused 403 on :reject of it too. "
        "Ships on by BGD's decision 2026-10-26 (#2417 A5f); the console reads may_decide.reject "
        "and offers Reject only to those who may approve",
        default=True,
    ),
    # #2726 (docs/workflows.md, "A `repository_ref` the forge does not have").
    # Off, a submission on a branch GitHub says is gone is admitted, as before,
    # and fails in the worker's clone after a lease and a pod.
    "repository_ref_not_found": Switch(
        code="repository_ref_not_found",
        why="a submission whose repository_ref is neither a branch nor a tag of a repository "
        "the submitter's forge slot can read is answered 422 repository_ref_not_found before "
        "any task is stored. On once the report-only log shows only refs that really were gone",
    ),
    # Join requests (docs/design/join-requests.md §4.9, lane JOIN-STORE, part
    # of #2325) have NO switch here, deliberately: join_link_unknown,
    # join_already_member, join_already_reaches, join_not_offered,
    # join_request_unknown, join_request_invalid, join_request_decided,
    # join_workspace_changed, join_role_not_allowed, join_decider_required and
    # the four 429s (join_requests_open_limit, join_requests_daily_limit,
    # join_denied_recently, join_workspace_queue_full) gate routes and stores
    # that did not exist before that design, so none narrows anything a
    # person could do, and continuing past any of them is the breach itself.
    # They are raised with `raise`, and listed in the guard's
    # NOT_SWITCHABLE_CODES with that reason.
}


def _value(name: str, raw: str) -> bool | None:
    value = raw.strip().lower()
    if not value:
        return None
    if value == ON:
        return True
    if value == OFF:
        return False
    raise ValueError(f"{name} must be {ON!r} or {OFF!r}, got {raw!r}")


def validate_environment(environ: Mapping[str, str] | None = None) -> None:
    """Refuse, at start, a REFUSAL_ variable that names no switch or says
    neither on nor off."""
    environ = os.environ if environ is None else environ
    known = {switch.env for switch in SWITCHES.values()}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        if name not in known:
            raise ValueError(
                f"{name} names no refusal switch in swarm_api.refusals.SWITCHES; "
                "a switch that matches nothing would be a refusal believed on. "
                "Remove the variable or correct its name."
            )
        _value(name, raw)


def _switch_for(error: ApiError) -> Switch | None:
    switch = SWITCHES.get(error.code)
    if switch is None:
        failure_code = error.detail.get("failure_code")
        if isinstance(failure_code, str):
            switch = SWITCHES.get(failure_code)
    return switch


def enforced(code: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the refusal `code` is on. A code with no switch is established
    and always on."""
    switch = SWITCHES.get(code)
    if switch is None:
        return True
    environ = os.environ if environ is None else environ
    value = _value(switch.env, environ.get(switch.env, ""))
    return switch.default if value is None else value


def refuse(error: ApiError, environ: Mapping[str, str] | None = None) -> None:
    """Raise `error` if its refusal is on; otherwise log it and RETURN.

    The environment is read on each call, so a revision that changes a
    switch takes effect without a code change; Cloud Run starts a new
    revision for any environment change, so nothing flips mid-process.
    """
    switch = _switch_for(error)
    if switch is None:
        raise error
    environ = os.environ if environ is None else environ
    value = _value(switch.env, environ.get(switch.env, ""))
    if switch.default if value is None else value:
        raise error
    # Code, status and the served sentence: what the caller WOULD have been
    # told. ApiError messages are served to callers, so they are token-free
    # by the same rule (errors.py).
    log.warning(
        "refusal report-only code=%s status=%s switch=%s=off message=%s",
        switch.code, error.status_code, switch.env, error.message,
    )
