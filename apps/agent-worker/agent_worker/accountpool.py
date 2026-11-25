"""The account an attempt runs on: an assignment leased from the pool or the
tenant's own key, the runner's account channel watched while it runs, a move
to another account when the held one runs out or its credential dies, and the
hold given back.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `AccountPoolMixin`, so the methods keep their
`self` access and every `worker._lease_account(...)` call and test still
resolves. The `MOVE_*` reasons, `ACCOUNT_STATE_REAUTH_REQUIRED` and
`POOL_ASKS` live here alone (issue 193); `lifecycle` no longer binds them, nor
the imports only these methods read. `MAX_ACCOUNT_TRIES`, `MAX_ACCOUNT_SWAPS`,
`MAX_CREDENTIAL_RELOADS`, `POOL_UNANSWERED`, the `SWAP_*` reasons and
`_rfc3339_now` stay in `lifecycle`, which uses them outside this concern or
whose tests read or patch them there, and are imported where they are used, at
call time, so a patch of `lifecycle.MAX_ACCOUNT_SWAPS` still reaches
`_swap_account`: a module-level import of `lifecycle` from here would be
circular.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm_common.states import EventType

from . import budget as budget_mod
from .accountlease import (
    ACCOUNT_UNREADABLE,
    BROKER_REFUSED,
    POOL_PAUSED,
    SOURCE_ACCOUNT_POOL,
    SOURCE_TENANT_OWN,
    SWAP_PLAN_AVAILABLE,
    TENANT_KEY_FIRST,
    AccountUnreadable,
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
    NoAccountAvailable,
    ReadingForwarder,
    credential_env_from_account,
    spend_figure,
    takes_account,
    takes_key,
)
from .errors import FencedError, TenantMismatchError
from .procman import ChildResult
from .runners.base import EXIT_QUOTA_EXHAUSTED
from .runners.cliagent import (
    ACCOUNT_ERROR_KINDS,
    ACCOUNT_MOVE_ENV,
    ACCOUNT_STREAM_ENV,
    OUTCOME_CREDIT_EXHAUSTED,
    RESUME_MOVED,
    RESUME_REASON_ENV,
    RESUME_RELOADED,
    RESUME_SESSION_ENV,
    SESSION_FILE_ENV,
    STOP_DRAIN,
    STOP_EXHAUSTED,
)
from .runners.streams import cli_agent_spec
from .secrets import load_tenant

if TYPE_CHECKING:
    from .lifecycle import Outcome

#: Why a dead credential moves an attempt to another account (archived issue 1855; the owner
#: 2026-10-28: "it should automatically move in the future on this error").
#: The `why` of the `account_moved` event; to the broker every one of them is a
#: `SWAP_UNUSABLE` swap, which marks the dead account for this tenant.
#:   * the token the secret still holds is the one the provider just refused,
#:     so a reload would start the CLI on the same dead token;
#:   * the refresher wrote successors and the provider refused those too, past
#:     `MAX_CREDENTIAL_RELOADS`;
#:   * the broker marked the account `reauth_required` (its refresh failed):
#:     a person has to log it in again, and no reload will help;
#:   * the secret could not be read at all on the reload.
MOVE_TOKEN_UNCHANGED = "token_unchanged"
MOVE_REFUSED_AFTER_RELOAD = "refused_after_reload"
MOVE_REAUTH_REQUIRED = "reauth_required"
MOVE_TOKEN_UNREADABLE = "token_unreadable"

#: The account state the broker stores when its refresh failed, read off the
#: hold's status: `quota_broker.accounts.AccountState.REAUTH_REQUIRED.value`,
#: which `account_hold_status` returns as is. The lowercase `reauth_required`
#: is the refresh-outcome reason (`CREDENTIAL_NEEDS_A_HUMAN`), never a state.
ACCOUNT_STATE_REAUTH_REQUIRED = "REAUTH_REQUIRED"

#: Asks of the broker for one assignment when it does not answer: the first,
#: and one more (archived issue 2560). A broker that is down for a second is not one that is
#: down for the next quarter of an hour, which is what the park waits.
POOL_ASKS = 2


class AccountPoolMixin:
    """The account pool methods of `lifecycle.Worker`, which inherits them."""

    # What `lifecycle.Worker` supplies to these methods; annotations only (no
    # value, so nothing is bound at runtime), so the type checker resolves them.
    _account: Any
    _account_broker: Any
    _account_declined: Any
    _account_rejected: Any
    _account_released: Any
    _account_token_digests: Any
    _build_child_env: Any
    _channel_seen: Any
    _checkpoint: Any
    _control_plane_down: Any
    _credential_kept: Any
    _credential_refusal: Any
    _credit_reported: Any
    _fenced_exit: Any
    _hold_spend_base: Any
    _just_swapped: Any
    _move_asked: Any
    _move_why: Any
    _park_no_account: Any
    _readings: Any
    _resume_reason: Any
    _resume_session: Any
    _runs: Any
    _session_id: Any
    _spend: Any
    _swap_log: Any
    _swaps: Any
    _task: Any
    _turns_checked: Any
    _writes_forbidden: Any
    budget: Any
    cfg: Any
    control: Any
    db: Any
    log: Any
    secret_client: Any
    spend_pricer: Any
    ws: Any

    # -- the account pool --------------------------------------------------
    def _pool_credential_env(self, profile: Any) -> dict[str, str] | None:
        """The child env an assigned account supplies, or None for the tenant secret.

        THE LOOP IS THE POINT. `choose()` is deterministic, so an account this
        worker cannot read is one it would be handed again on every retry --
        and the three real ways that happens (a freshly onboarded account whose
        secret has no version yet, an account lent by a tenant that never
        granted this worker's service account access to it, an empty version)
        are all invisible to the broker, which knows about Firestore documents
        and not about IAM. So: hand the unusable one back, say WHY, ask again
        excluding it, and park if nothing works.

        Parking rather than failing, because none of this is the task's fault
        and an unusable pool must cost nothing. Failing would burn all three
        attempts on the same account in a row.

        AN EARLIER REJECTION OUTRANKS A LATER WAIT. Once an account has been
        handed back as unreadable, the next ask excludes it, and when it was
        the only one the broker answers `no_account_available` -- true, but a
        consequence, not the cause. Measured 2026-10-06: smoke borrowed
        eng:team with no grant on its secret, and every attempt parked as
        PROVIDER_QUOTA_EXHAUSTED for a quarter of an hour, telling the operator
        to wait for a window instead of to fix an IAM binding. Only a wait is
        replaced: `broker_refused` is already a configuration error, with a
        more specific name.
        """
        from .lifecycle import MAX_ACCOUNT_TRIES

        rejected = False
        for _ in range(MAX_ACCOUNT_TRIES):
            try:
                account = self._lease_account(profile)
            except NoAccountAvailable as exc:
                if rejected and exc.decision.reason != BROKER_REFUSED:
                    raise NoAccountAvailable(
                        NoAccount(reason=ACCOUNT_UNREADABLE), exc.provider
                    ) from None
                raise
            if account is None:
                return None
            try:
                return self._account_credential_env(profile, account)
            except AccountUnreadable as exc:
                self._reject_account(account, exc)
                rejected = True
        raise NoAccountAvailable(
            NoAccount(reason=ACCOUNT_UNREADABLE),
            profile.provider or "unknown",
        )

    def _lease_account(self, profile: Any) -> Assignment | None:
        """The account this attempt runs on, or None to use the tenant secret.

        None is returned -- rather than raised -- for every case that means
        "the pool is not how this runs":

          * no broker is configured, which is every deployment that has not
            adopted the pool;
          * the profile does not declare `CLAUDE_CODE_OAUTH_TOKEN`, so it wants
            a metered API key and an account has none to give;
          * the tenant has no account registered at all.

        Each of those is a decline, and every decline emits
        `account_pool_declined` (archived issue 2560): which one fired used to be in a log
        line alone, and task_d212c88b's record could not say.

        `NoAccountAvailable` is raised for the cases that are genuinely a wait
        or genuinely broken: accounts exist and every one is spent, paused,
        draining or unobserved; the broker REFUSED this worker; or the broker
        did not answer twice (`POOL_UNANSWERED`, archived issue 2560 -- it used to fall back
        to the tenant secret). The caller parks in every case -- an attempt is
        never failed over the pool.
        """
        from .lifecycle import POOL_UNANSWERED

        if self._account is not None:
            # Already held. Re-reading it is what the credential-reload path
            # wants; re-ASSIGNING would double-count this agent on the pool and
            # could move it onto a different subscription halfway through a run.
            return self._account
        if self._account_declined is not None:
            # Decided once, at step 6, and not revisited. See the field.
            return None
        if self._account_broker is None:
            return self._decline_pool("no_broker_configured", profile.provider)
        # A profile that takes its vendor's plan may also run on its vendor's
        # key (`key_tier`, §5.3). A profile that declares the key alone
        # (`browser`) still runs on the tenant secret: the scheduler's
        # admission expects the pool only where the plan token is declared
        # (`scheduler.credentials`), and the two must agree.
        key_tier = takes_key(profile.provider, profile.secrets)
        if not takes_account(profile.provider, profile.secrets):
            self.log.info(
                "runner profile does not accept a subscription token; using the "
                "tenant credential rather than the account pool",
                runner_profile=profile.name,
                declared=sorted(profile.secrets or ()),
            )
            return self._decline_pool("profile_takes_no_subscription", profile.provider)
        if profile.provider in TENANT_KEY_FIRST and self._tenant_supplies_key(profile.provider):
            # A codex step leases a ChatGPT plan only when the tenant supplies
            # no OPENAI_API_KEY of its own (the owner, 2026-11-01, part of archived issue 2688): a
            # tenant paying for metered access keeps running on it.
            self.log.info(
                "the tenant supplies its own API key; using it rather than the "
                "account pool",
                provider=profile.provider,
            )
            return self._decline_pool("tenant_supplies_api_key", profile.provider)

        provider = profile.provider
        cause = POOL_UNANSWERED[0]
        for ask in range(1, POOL_ASKS + 1):
            try:
                # `key_tier` when the profile declares its vendor's key
                # variable (§5.3): the broker walks the plans, then the keys.
                outcome = self._account_broker.assign(
                    provider, exclude=tuple(self._account_rejected), key_tier=key_tier
                )
            except BrokerRefused as exc:
                # REFUSE, DO NOT CARRY ON. A 401/403/404 means the pool is
                # configured and this worker may not use it -- almost always a
                # missing `run.invoker` grant for this tenant's worker service
                # account, or a URL that is not the broker. Falling back here
                # would put every agent back on the one shared tenant
                # subscription while the pool's own dashboards showed it
                # healthy and idle, which is the failure nobody would ever
                # find. Parking costs nothing and puts the cause in the task's
                # own blocked_by.
                self.log.error(
                    "the quota broker refused this worker; the account pool is "
                    "configured and this tenant cannot use it",
                    provider=provider,
                    error=str(exc),
                )
                raise NoAccountAvailable(
                    NoAccount(reason=BROKER_REFUSED), provider or "unknown"
                ) from None
            except BrokerUnavailable as exc:
                # ASK AGAIN, THEN PARK -- NEVER THE TENANT SECRET (archived issue 2560, the owner
                # 2026-10-28). This used to degrade to the tenant's own secret
                # with nothing but a log line, and in task_d212c88b that secret
                # held a key the provider refused: three tasks FAILED on a pool
                # that was healthy a second later. The workspace HAS a pool, so
                # its own secret is not how it runs. Parking costs nothing.
                cause = "broker_unreachable"
                self.log.warning(
                    "could not reach the quota broker for an account",
                    provider=provider,
                    ask=ask,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
                # Defence in depth: whatever the client raised, the pool is
                # there and did not answer. The same as an unreachable broker.
                cause = "broker_client_error"
                self.log.warning(
                    "the account pool client raised",
                    provider=provider,
                    ask=ask,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                break
            self._emit_pool_declined(
                cause, provider, then="parking" if ask == POOL_ASKS else "asking_again"
            )
        else:
            raise NoAccountAvailable(NoAccount(reason=cause), provider or "unknown")

        if isinstance(outcome, NoAccount):
            if outcome.is_pool_absent:
                self.log.info(
                    "no account is registered for this tenant; using the tenant credential",
                    provider=provider,
                )
                return self._decline_pool("no_accounts_registered", provider)
            raise NoAccountAvailable(outcome, provider or "unknown")

        self._account = outcome
        self._account_released = False
        self._hold_began()
        self._keep_credential(outcome)
        self.log.info(
            "account assigned from the pool",
            account_id=outcome.account_id,
            provider=provider,
            tier=outcome.tier,
            # Whether this tenant borrowed it. Worth a field of its own: a
            # deployment leaning on borrowed capacity looks healthy right up to
            # the day the lender revokes the loan.
            borrowed=bool(outcome.owner_tenant and outcome.owner_tenant != self.cfg.tenant_id),
        )
        self.control.emit(
            EventType.RUNNING,
            {
                "cause": "account_assigned",
                "account_id": outcome.account_id,
                "provider": provider,
                # The tier the broker answered (§5.3, §5.4): `plan` or
                # `api_key`. Never the key, nor its secret's name.
                "tier": outcome.tier,
            },
        )
        return outcome

    def _tenant_supplies_key(self, provider: str | None) -> bool:
        """Whether the tenant registered its own credential for `provider`.

        The same tenant read the fallback makes (`load_tenant`, under the
        startup budget), and the same test `resolve_credentials` refuses a
        missing credential by: the provider in `tenant.credentials`.
        """
        tenant = load_tenant(
            self.db, self.cfg.tenant_id, call_options=self.control.call_options("tenant")
        )
        return bool(provider) and provider in (tenant.credentials or ())

    # -- the account mid-run (S13-S15) ---------------------------------------
    def _budget_env(self) -> dict[str, str]:
        """The runner's budget files (archived issue 2459), building the guard on first use.

        Only for a coding-agent CLI (`cli_agent_spec`) whose verified task
        carries a budget -- its signed `Task.budget`, or for a task stored
        before that field its signed `metadata.dispatch.budget`: every other
        attempt gets nothing, and its runner, settings and supervision are
        what they were before.
        """
        if self.budget is None:
            spec = cli_agent_spec(self.cfg.runner_profile)
            limits = budget_mod.Limits.from_task(self._task)
            if spec is None or limits is None or self.ws is None:
                return {}
            self.budget = budget_mod.BudgetGuard(
                limits,
                private=self.ws.private,
                emit=lambda detail: self.control.emit(budget_mod.WARNING_EVENT, detail),
                log=self.log,
                pricer=self.spend_pricer.price,
                # claude-code's PostToolUse hook is the one way to tell an
                # agent mid-run; `codex exec` takes nothing once started.
                can_tell=spec.name == "claude-code",
            )
            self.log.info("this attempt has a budget", limit=limits.as_dict())
        return self.budget.env()

    def _account_channel_paths(self) -> tuple[Path, Path]:
        """(stream, move): the runner's channel, in the worker's own `private/`.

        Not in `work/`, which a checkpoint archives and the agent can read.
        """
        assert self.ws is not None
        return (
            self.ws.private / "account-stream.json",
            self.ws.private / "account-move.json",
        )

    def _account_channel_env(self) -> dict[str, str]:
        """What a runner on a held account is told: where its channel is, and
        the session to continue when the last move set one."""
        if self._account is None:
            return {}
        stream, move = self._account_channel_paths()
        env = {ACCOUNT_STREAM_ENV: str(stream), ACCOUNT_MOVE_ENV: str(move)}
        if self._resume_session:
            env[RESUME_SESSION_ENV] = self._resume_session
            env[RESUME_REASON_ENV] = self._resume_reason or RESUME_MOVED
        return env

    def _session_file_path(self) -> Path:
        """Where a runner on no account writes its session (archived issue 626), in `private/`."""
        assert self.ws is not None
        return self.ws.private / "session.json"

    def _session_channel_env(self) -> dict[str, str]:
        """What a runner on the tenant's own credential is told about its session.

        Only for a CLI that can resume (`CliAgentSpec.resume_flag`): where to
        write the session it ran, and -- once a credential reload set one --
        the session to continue. An attempt on an account has the channel
        (`_account_channel_env`) instead, and every other profile nothing.
        """
        spec = cli_agent_spec(self.cfg.runner_profile)
        if self._account is not None or spec is None or not spec.resume_flag:
            return {}
        env = {SESSION_FILE_ENV: str(self._session_file_path())}
        if self._resume_session:
            env[RESUME_SESSION_ENV] = self._resume_session
            env[RESUME_REASON_ENV] = self._resume_reason or RESUME_RELOADED
        return env

    def _read_session_file(self) -> str | None:
        """The session the last runner on no account named, or None. Consumed."""
        path = self._session_file_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        finally:
            path.unlink(missing_ok=True)
        session = data.get("session_id") if isinstance(data, dict) else None
        return session if isinstance(session, str) and session else None

    def _read_account_channel(self) -> dict[str, Any]:
        stream, _move = self._account_channel_paths()
        try:
            data = json.loads(stream.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _note_session(self, session_id: Any) -> None:
        """Remember the run's session id, as a secret: never in a log or event."""
        if isinstance(session_id, str) and session_id and session_id != self._session_id:
            self.log.register_secret(session_id)
            self._session_id = session_id

    def _forward_readings(self, readings: Any, *, final: bool = False) -> None:
        account = self._account
        if account is None or self._account_broker is None or not readings:
            return
        if self._readings is None:
            broker = self._account_broker
            self._readings = ReadingForwarder(
                lambda windows: broker.report_reading(account, windows), logger=self.log
            )
        self._readings.offer(readings, final=final)

    def _watch_account(self) -> None:
        """One look at the runner's channel while it runs. Never raises.

        Forwards each window's latest reading (S15) and, once per new turn,
        asks the broker whether this hold is marked to move (S14) -- a drain.
        If it is, the move file asks the runner to stop the CLI at its next
        turn boundary; the swap itself happens when the runner has exited.
        """
        from .lifecycle import SWAP_UNUSABLE

        try:
            stream, move = self._account_channel_paths()
            try:
                seen = stream.stat().st_mtime_ns
            except OSError:
                return
            if seen == self._channel_seen:
                return
            self._channel_seen = seen
            data = self._read_account_channel()
            self._note_session(data.get("session_id"))
            self._forward_readings(data.get("readings"))
            turns = data.get("turns")
            if not isinstance(turns, int) or turns <= self._turns_checked:
                return
            self._turns_checked = turns
            if self._account_broker is None or self._account is None or move.exists():
                return
            status = self._account_broker.hold_status(
                self._account, cost_usd=self._hold_spend()
            )
            if status.get("move") == SWAP_PLAN_AVAILABLE:
                # BACK TO A PLAN (Q1, §5.3): this attempt holds a key and a
                # plan would take it now. Stopped at the next turn boundary
                # like a drain, then swapped to the plans alone; if the plan
                # filled in between, it restarts on the same key.
                self._move_asked = SWAP_PLAN_AVAILABLE
                move.write_text(json.dumps({"reason": SWAP_PLAN_AVAILABLE}))
                self.log.info(
                    "a plan is free for this attempt; the agent moves back to it "
                    "at its next turn boundary",
                    account_id=self._account.account_id,
                )
            elif status.get("move"):
                move.write_text(json.dumps({"reason": str(status["move"])}))
                self.log.warning(
                    "this attempt's account is draining; the agent moves to another "
                    "account at its next turn boundary",
                    account_id=self._account.account_id,
                    move=str(status["move"]),
                )
            elif status.get("state") == ACCOUNT_STATE_REAUTH_REQUIRED:
                # BEFORE THE TOKEN DIES (archived issue 1855). The broker's refresh failed
                # and the access token the agent holds expires on its own
                # within the hour: move at the next turn boundary, as a drain
                # does, rather than at the 401.
                self._move_why = MOVE_REAUTH_REQUIRED
                move.write_text(json.dumps({"reason": SWAP_UNUSABLE}))
                self.log.warning(
                    "this attempt's account needs re-authentication; the agent "
                    "moves to another account at its next turn boundary",
                    account_id=self._account.account_id,
                )
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not read the account channel or the hold's status; the run "
                "carries on as it is",
                error_type=type(exc).__name__,
            )

    def _move_account_after(self, result: ChildResult) -> dict[str, str] | Outcome | None:
        """After a runner on a held account exits: move the attempt, or None.

        None is "nothing to move": not on an account, or the run ended for a
        reason a move does not answer -- and the caller carries on exactly as
        before. A dict is the child environment to restart with `--resume`
        under the new account. An Outcome is a park.
        """
        from .lifecycle import SWAP_DRAIN, SWAP_EXHAUSTED, SWAP_UNUSABLE

        if self._account is None or self._account_broker is None:
            return None
        data = self._read_account_channel()
        # CONSUMED: what this run said is never read again as the next run's.
        # The session id is kept in memory; the next runner writes its own.
        stream, _move = self._account_channel_paths()
        stream.unlink(missing_ok=True)
        self._channel_seen = None
        self._note_session(data.get("session_id"))
        # AT THE LATEST AT THE END OF THE RUN, every window not yet sent.
        self._forward_readings(data.get("readings"), final=True)
        # Before any move: the error belongs to the account the run was on.
        self._report_runner_error(data.get("error"))
        stopped_for = data.get("stopped_for")
        just_swapped, self._just_swapped = self._just_swapped, False
        why, self._move_why = self._move_why, None
        asked, self._move_asked = self._move_asked, None
        if data.get("outcome") == OUTCOME_CREDIT_EXHAUSTED:
            # The KEY's credit is gone (§5.2): reported as the key's, so the
            # broker skips it until it is resumed, then the attempt moves on
            # as for any spent account. A plan's answer stays a rate limit.
            self._report_credit_exhausted()
        reason: str | None = None
        if stopped_for == STOP_DRAIN and why is not None:
            # Stopped for a dead credential, not a drain (`_watch_account`).
            reason = SWAP_UNUSABLE
        elif stopped_for == STOP_DRAIN and asked == SWAP_PLAN_AVAILABLE:
            reason = SWAP_PLAN_AVAILABLE
        elif stopped_for == STOP_DRAIN:
            why = None
            reason = SWAP_DRAIN
        elif stopped_for == STOP_EXHAUSTED or result.exit_code == EXIT_QUOTA_EXHAUSTED:
            reason = SWAP_EXHAUSTED
        elif just_swapped and self._credential_refusal() is not None:
            # The account this attempt just moved to could not start the CLI.
            reason = SWAP_UNUSABLE
            assert self.ws is not None
            self.ws.credential_path.unlink(missing_ok=True)
        if reason is None:
            return None
        if why is not None or reason == SWAP_PLAN_AVAILABLE:
            # A dead credential moves with or without a session: one that
            # named none restarts from the prompt in the same workspace. So
            # does a move back to a plan, which was stopped on purpose.
            return self._swap_account(reason, why=why)
        if self._session_id is None:
            # Nothing to resume. A rate limit goes the way it always went; a
            # drain stop with no session cannot be continued, so it parks.
            self.log.warning(
                "the run named no session to continue; not moving it",
                reason=reason,
            )
            if reason == SWAP_DRAIN:
                return self._park_no_account(
                    NoAccountAvailable(NoAccount(reason=POOL_PAUSED), self.cfg.provider or "unknown")
                )
            return None
        return self._swap_account(reason)

    def _report_runner_error(self, error: Any) -> None:
        """The provider error the runner classified, on the held account's
        health record (RUNNER-ERROR-KINDS, archived issue 2302). Never raises.

        An overloaded 529, a 5xx or a network failure the CLI gave up on
        (`cliagent.classify_account_error`). Health only: the broker's
        `/account-error` route never touches AIMD, and the run itself fails
        exactly as before. An `auth` error is not read from here; the
        credential path reports it (`_credential_refusal`). The message was
        redacted by the runner and is again by `report_account_error`.
        """
        account = self._account
        if account is None or not isinstance(error, dict):
            return
        kind = error.get("kind")
        if kind not in ACCOUNT_ERROR_KINDS:
            return
        message = error.get("message")
        self.control.report_account_error(
            provider=str(self.cfg.provider or ""),
            account=account,
            kind=str(kind),
            message=message if isinstance(message, str) else "",
        )

    def _dead_credential(self, reloads: int) -> str | None:
        """Why the held account's refused credential moves the attempt, or None
        to reload it in place (archived issue 1855). Never raises for the broker or the secret.

        A reload helps only when the refresher has written a successor. The
        move is right when it has not and will not: the reloads are spent, the
        broker marked the account `reauth_required`, or the secret holds the
        very token the provider refused (or cannot be read at all).
        """
        from .lifecycle import MAX_CREDENTIAL_RELOADS

        account = self._account
        assert account is not None
        if reloads >= MAX_CREDENTIAL_RELOADS:
            return MOVE_REFUSED_AFTER_RELOAD
        if self._account_broker is not None:
            try:
                status = self._account_broker.hold_status(account)
            except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
                # A fenced attempt's status read is refused; the swap checks
                # the fence itself, so this only loses the reauth hint.
                self.log.warning(
                    "could not read the held account's status",
                    error_type=type(exc).__name__,
                )
                status = {}
            if isinstance(status, dict) and status.get("state") == ACCOUNT_STATE_REAUTH_REQUIRED:
                return MOVE_REAUTH_REQUIRED
        before = self._account_token_digests.get(account.account_id)
        try:
            self._account_credential_env(self.cfg.profile, account)
        except AccountUnreadable:
            return MOVE_TOKEN_UNREADABLE
        if before is not None and before == self._account_token_digests.get(account.account_id):
            return MOVE_TOKEN_UNCHANGED
        return None

    def _swap_account(
        self, reason: str, *, why: str | None = None
    ) -> dict[str, str] | Outcome | None:
        """Move the hold through the broker and rebuild the child's environment.

        ONE BROKER CALL PER MOVE (`AccountBroker.swap`), which releases this
        hold and takes another in one transaction. If the new account cannot
        be read, it moves again with reason `unusable` -- the broker marks the
        unreadable one for this tenant -- up to MAX_ACCOUNT_TRIES; then the
        account is given back unusable (`_give_back`) and the attempt parks.

        Re-checks the fence first: nothing continues under a stale generation
        (invariant 5), and the broker refuses a fenced attempt's swap anyway.

        `why` is set for a dead credential (archived issue 1855, `_dead_credential`): the
        attempt is CHECKPOINTED before it moves (invariant 8), so a worker lost
        mid-move resumes from here; the move is announced as `account_moved`
        naming both accounts by their pool label; and with no healthy account
        the dead one goes back unusable and the attempt parks on the pool --
        never a failure.
        """
        from .lifecycle import MAX_ACCOUNT_SWAPS, MAX_ACCOUNT_TRIES, SWAP_UNUSABLE

        profile = self.cfg.profile
        provider = profile.provider or "unknown"
        current = self._account
        assert current is not None and self._account_broker is not None
        if self._swaps >= MAX_ACCOUNT_SWAPS:
            self.log.warning(
                "this attempt has moved accounts as often as it may; not moving again",
                swaps=self._swaps,
            )
            return self._cannot_swap(
                reason, current, NoAccount(reason="no_account_available"), dead=why is not None
            )
        self.control.validate_generation()
        if why is not None:
            self.log.warning(
                "the held account's credential is dead; checkpointing and moving "
                "this attempt to another account",
                account_id=current.account_id,
                why=why,
            )
            self._checkpoint("account-move")
        elif reason == SWAP_PLAN_AVAILABLE:
            # A move between tiers is checkpoint, then swap (§5.5, invariants
            # 4 and 8): a worker lost mid-move resumes from here.
            self._checkpoint("account-move")
        key_tier = takes_key(profile.provider, profile.secrets)
        for _ in range(MAX_ACCOUNT_TRIES):
            try:
                outcome = self._account_broker.swap(
                    current,
                    provider=provider,
                    reason=reason,
                    exclude=tuple(self._account_rejected),
                    key_tier=key_tier,
                    cost_usd=self._hold_spend() if current.is_key else None,
                )
            except (BrokerRefused, BrokerUnavailable) as exc:
                self.log.warning(
                    "the quota broker did not move this attempt to another account",
                    reason=reason,
                    refused=isinstance(exc, BrokerRefused),
                    error_type=type(exc).__name__,
                )
                if isinstance(exc, BrokerRefused):
                    # A refused swap is what a fenced attempt meets; the fence
                    # check says so and ends the attempt the fenced way.
                    self.control.validate_generation()
                return self._cannot_swap(
                    reason, current, NoAccount(reason="no_account_available"),
                    dead=why is not None,
                )
            if isinstance(outcome, NoAccount):
                if reason == SWAP_PLAN_AVAILABLE:
                    # The plan filled in between: the broker left the hold on
                    # the key, so the run resumes on it, as it was.
                    return self._stay_on_key(current, outcome)
                return self._cannot_swap(reason, current, outcome, dead=why is not None)
            self._swaps += 1
            self._account = outcome
            self._account_released = False
            self._hold_began()
            self._keep_credential(outcome)
            self._emit_swapped(current, outcome, reason)
            if why is not None:
                self._emit_moved(current, outcome, why)
                why = None
            try:
                self._account_credential_env(profile, outcome)
            except AccountUnreadable as exc:
                self.log.error(
                    "the account this attempt moved to cannot be read; moving again",
                    account_id=outcome.account_id,
                    error=exc.detail,
                )
                self._account_rejected.append(outcome.account_id)
                current, reason = outcome, SWAP_UNUSABLE
                continue
            self._resume_session = self._session_id
            self._resume_reason = RESUME_MOVED
            self._just_swapped = True
            self._readings = None
            self._turns_checked = 0
            self._channel_seen = None
            _stream, move = self._account_channel_paths()
            move.unlink(missing_ok=True)
            return self._build_child_env()
        # Every account it moved to was unreadable: give the last one back as
        # such and park, as a fresh attempt does (`_pool_credential_env`).
        return self._cannot_swap(SWAP_UNUSABLE, current, NoAccount(reason=ACCOUNT_UNREADABLE))

    def _stay_on_key(self, current: Assignment, outcome: NoAccount) -> dict[str, str]:
        """A move back to a plan that found none free: resume on the key held.

        The broker's swap is one transaction and left the hold where it was
        (§5.3: never two, never none), so nothing is given back. The runner
        was stopped at a turn boundary; it restarts with `--resume` on the
        same key's environment.
        """
        self.log.info(
            "the plan filled before this attempt could move back to it; carrying "
            "on on the key it holds",
            account_id=current.account_id,
            pool_reason=outcome.reason,
        )
        self._resume_session = self._session_id
        self._resume_reason = RESUME_MOVED
        self._readings = None
        self._turns_checked = 0
        self._channel_seen = None
        _stream, move = self._account_channel_paths()
        move.unlink(missing_ok=True)
        return self._build_child_env()

    def _hold_began(self) -> None:
        """Mark the attempt's spend at the start of the hold just taken."""
        self._hold_spend_base = self._spend_total()

    def _spend_total(self) -> float:
        cost = self._spend.get("total_cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return 0.0
        return float(cost)

    def _hold_spend(self) -> float | None:
        """What has run on the held account's hold so far, by the attempt's
        own `cost_usd` estimate (§4.6, Q5), or None when nothing has: the
        figure the hold's close carries, and a key holder's hold-status read
        carries while it runs."""
        return spend_figure(self._spend_total() - self._hold_spend_base)

    def _keep_credential(self, account: Assignment | None) -> None:
        """The credential this attempt now runs on, for its record (request 152)."""
        if account is None:
            self._credential_kept = {
                "account_id": None,
                "credential_source": SOURCE_TENANT_OWN,
                "credential_tier": None,
            }
            return
        self._credential_kept = {
            "account_id": account.account_id,
            "credential_source": SOURCE_ACCOUNT_POOL,
            "credential_tier": account.tier,
        }

    def _report_credit_exhausted(self) -> None:
        """The held KEY's credit is gone (§5.2): say so to the broker, naming it.

        Only for a key: a plan's `insufficient_quota` is its window, which the
        rate-limit report already carries. Reported to the broker's
        `exhausted` route under the key's own provider, with the hold's proof,
        so the broker marks that key `exhausted_at` and the next lease moves
        on; the tenant's own quota is not the key's. Never raises.
        """
        account = self._account
        if account is None or not account.is_key:
            return
        if self._credit_reported == account.account_id:
            return
        self._credit_reported = account.account_id
        self.log.warning(
            "the held API key's credit is gone; reporting it exhausted",
            account_id=account.account_id,
        )
        self.control.report_credit_exhausted(
            provider=account.provider,
            account=account,
            message="the provider answered that this key's credit is gone",
        )

    def _quota_account(self) -> Assignment | None:
        """The held account a runner's 429 names, or None when there is none
        -- or when it was the key's credit, already reported as such."""
        account = self._account
        if account is not None and self._credit_reported == account.account_id:
            return None
        return account

    def _cannot_swap(
        self, reason: str, current: Assignment, outcome: NoAccount, *, dead: bool = False
    ) -> Outcome | None:
        """No other account took this attempt. Fall back to what happens today.

        An exhausted account: None, and the caller's quota path checkpoints
        and parks exactly as `_park_for_quota` always has. A drain: park on
        the pool, holding the hold until the exit path gives it back. An
        account that could not be read: give it back unusable, then park.
        A dead credential (`dead`, archived issue 1855): give it back unusable and park on
        the broker's own answer -- `no_account_available` waits
        NO_ACCOUNT_RETRY_SECONDS -- never a failure.
        """
        from .lifecycle import SWAP_EXHAUSTED, SWAP_UNUSABLE

        self.log.warning(
            "no other account can take this attempt; falling back to a checkpoint and park",
            reason=reason,
            pool_reason=outcome.reason,
        )
        if reason == SWAP_EXHAUSTED:
            return None
        if dead:
            self._account = None
            self._account_released = False
            self._give_back(current, unusable="the provider refused this account's credential")
        elif reason == SWAP_UNUSABLE:
            self._account = None
            self._account_released = False
            self._give_back(current, unusable="the agent could not start on this account")
            outcome = NoAccount(reason=ACCOUNT_UNREADABLE)
        return self._park_no_account(NoAccountAvailable(outcome, self.cfg.provider or "unknown"))

    def _emit_swapped(self, left: Assignment, taken: Assignment, reason: str) -> None:
        """The move, on the task's events and in the log. Never a token or a session id.

        `account_assigned` for the account taken -- the cause swarm-api's task
        account read already follows, so the task shows the account it is on
        now -- carrying where it came from and why.
        """
        from .lifecycle import _rfc3339_now

        self.log.info(
            "moved this attempt to another account",
            from_account_id=left.account_id,
            account_id=taken.account_id,
            reason=reason,
            tier=taken.tier,
            from_tier=left.tier,
        )
        # The summary's record of it (SWAP-RUNS): after which run, and when.
        self._swap_log.append({
            "at": _rfc3339_now(),
            "from": left.account_id,
            "to": taken.account_id,
            "reason": reason,
            "after_run": len(self._runs),
        })
        try:
            self.control.emit(
                EventType.RUNNING,
                {
                    "cause": "account_assigned",
                    "account_id": taken.account_id,
                    "provider": self.cfg.provider,
                    "swapped_from": left.account_id,
                    "swap_reason": reason,
                    # The tiers on either side (§5.4): a move from a plan to a
                    # key, or back, is one line on the attempt's timeline.
                    "tier": taken.tier,
                    "from_tier": left.tier,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not record the account move on the task's events",
                error_type=type(exc).__name__,
            )

    def _emit_moved(self, left: Assignment, taken: Assignment, why: str) -> None:
        """`account_moved` for a dead credential's move (archived issue 1855), naming both
        accounts by their pool label. Never a token, a secret name or a
        session id. An audit record: one that cannot be written costs the move
        nothing -- except a fence."""
        try:
            self.control.emit(
                EventType.RETRYING,
                {
                    "cause": "account_moved",
                    "why": why,
                    "from_account": left.label,
                    "to_account": taken.label,
                    "provider": self.cfg.provider,
                },
            )
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not record the account move on the task's events",
                error_type=type(exc).__name__,
            )

    def _decline_pool(self, cause: str, provider: str | None) -> None:
        """Record that this attempt is NOT on the pool, once and for good.

        Returns None so call sites can `return self._decline_pool(...)`. The
        stickiness is the behaviour: `_build_child_env` runs again whenever a
        credential is reloaded mid-attempt, and a second ask there could move a
        running agent onto a pool account it did not start on, or raise from a
        call site whose job is to restart the child rather than to park.

        Only for a workspace with no pool for this run: the broker that did
        not answer parks instead (`POOL_UNANSWERED`). Announced either way.
        """
        self._account_declined = cause
        self._keep_credential(None)
        self._emit_pool_declined(cause, provider, then="tenant_secret")
        return None

    def _emit_pool_declined(self, reason: str, provider: str | None, *, then: str) -> None:
        """`account_pool_declined` on the task's events (archived issue 2560). Never a key.

        `reason` is the decline's cause, `pool` the provider whose pool it is,
        `then` what this attempt does next: `tenant_secret` (no pool),
        `asking_again` or `parking`. The task and attempt are named in the
        detail as well as stamped by `control.emit`, so a reader of the detail
        alone has them. An audit record: one that cannot be written costs the
        attempt nothing, as for a credential reload -- except a fence.
        """
        try:
            self.control.emit(
                EventType.RUNNING,
                {
                    "cause": "account_pool_declined",
                    "reason": reason,
                    "pool": provider or "unknown",
                    "task_id": self.cfg.task_id,
                    "attempt_id": self.cfg.attempt_id,
                    "then": then,
                },
            )
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not record the account pool's decline",
                reason=reason,
                error_type=type(exc).__name__,
            )

    def _account_credential_env(self, profile: Any, account: Assignment) -> dict[str, str]:
        """Read the ASSIGNED account's secret and shape it for the child.

        `{base}` holds only the access token -- the `{base}-refresh` half that
        can mint successors is readable by the broker alone -- so what lands in
        the child's environment expires on its own. Read on EVERY call, which
        is what makes the credential-reload path work: `access()` always takes
        `latest`, so a token the refresher rotated under a running agent is
        picked up by re-reading the same name.

        EVERY WAY THIS CAN FAIL BECOMES `AccountUnreadable`, including the ones
        that arrive as a raw google-cloud exception. `access()` raises NotFound
        for a secret with no version -- which is exactly the state
        `scripts/account.sh` leaves a freshly added account in until the
        broker's next sweep publishes the access token -- and PermissionDenied
        for an account lent by a tenant that never granted this worker's
        service account `secretmanager.secretAccessor` on it. Both used to
        travel straight out of here into `run()`'s generic handler and fail the
        attempt; both are "this account, not this task", so the caller hands it
        back and asks for another.
        """
        assert self.secret_client is not None
        try:
            payload = self.secret_client.access(account.secret)
            env = credential_env_from_account(
                payload,
                secret_env_names=profile.secrets,
                secret_name=account.secret,
                # The ACCOUNT's provider: a key fills its key variable, a plan
                # its token, whatever the profile's own provider is named.
                provider=account.provider or profile.provider or "anthropic",
            )
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            raise AccountUnreadable(
                account.account_id, f"{type(exc).__name__}: {exc}"
            ) from None
        for value in env.values():
            self.log.register_secret(value)
        # A one-way digest, never the token: what `_dead_credential` compares.
        self._account_token_digests[account.account_id] = hashlib.sha256(
            json.dumps(sorted(env.items())).encode("utf-8")
        ).hexdigest()
        self.log.info(
            "account credentials resolved",
            account_id=account.account_id,
            secret=account.secret,
            variables=sorted(env),
        )
        return env

    def _reject_account(self, account: Assignment, exc: AccountUnreadable) -> None:
        """Hand back an account this worker cannot read, and remember not to ask for it.

        Three things, and all three matter. The hold goes back, or the pool
        counts an agent that never started. The id joins `exclude`, or the next
        ask returns the same account, because `choose()` is deterministic. And
        the reason travels with the release, so the broker can stop offering
        this account to THIS tenant for a while and an operator can see the
        difference between an account nobody wants and an account nobody can
        read.
        """
        self.log.error(
            "the assigned account cannot be read; handing it back and asking "
            "for another",
            account_id=account.account_id,
            secret=account.secret,
            error=exc.detail,
        )
        self.control.emit(
            EventType.RETRYING,
            {
                "cause": "account_unreadable",
                "account_id": account.account_id,
                "provider": self.cfg.provider,
            },
        )
        self._account_rejected.append(account.account_id)
        self._account = None
        self._account_released = False
        self._credential_kept = None
        self._give_back(account, unusable=exc.detail)

    def _give_back(self, account: Assignment, *, unusable: str = "") -> None:
        """One release call, and it never raises.

        A failed release costs one over-counted hold until it EXPIRES. It is an
        expiry and not a reconciler: `apps/reconciler/` has no account code at
        all, and a comment here used to claim it did -- which is worse than no
        comment, because the next person reads it as a reason not to build the
        backstop. The real one is in the broker: every hold carries a deadline
        and the quota sweep prunes the expired ones, so a worker that is
        SIGKILLed, OOM-killed or preempted costs a few stale minutes on one
        account's counter rather than a slot that never comes back.
        """
        if self._account_broker is None:
            return
        try:
            self._account_broker.release(
                account.account_id,
                account.assignment_id,
                unusable=unusable,
                # What ran on this hold (§4.6): a key's close meters it.
                cost_usd=self._hold_spend() if account.is_key else None,
            )
            self.log.info(
                "account released",
                account_id=account.account_id,
                unusable=bool(unusable),
            )
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not release the account; its hold expires on its own and "
                "the broker's quota sweep prunes it",
                account_id=account.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._emit_account_released(account, unusable=bool(unusable))

    def _emit_account_released(self, account: Assignment, *, unusable: bool) -> None:
        """`account_released` on the task's events, beside `account_assigned` (archived issue 380).

        ONCE PER RELEASE THE BROKER ACCEPTED: `_give_back` calls it only after
        `release` returned, so a failed release, whose hold is left to expire,
        claims nothing. The same shape as `account_assigned` -- a `cause` on an
        event the frozen `EventType` already has -- because the API reads every
        `account_*` cause through one range query (`swarm_api.task_accounts`).
        LEASE_RELEASED rather than RUNNING: it is a hold given back, and it is
        written on the way out, after the attempt's own end.

        WHAT IT CARRIES: the account id, the provider, and whether it went back
        unusable, as a bool. `control.emit` stamps the task, the attempt, the
        lease and the generation on the event itself. Never the secret's name
        or payload, and never the unusable reason's text, which quotes Secret
        Manager's error and names the secret.

        Not on a fenced exit or a tenant mismatch: the stream is not this
        attempt's to write then (`_stand_down`, `_exit_tenant_mismatch`).
        Never raises.
        """
        if self._fenced_exit or self._writes_forbidden or self._control_plane_down:
            return
        try:
            self.control.emit(
                EventType.LEASE_RELEASED,
                {
                    "cause": "account_released",
                    "account_id": account.account_id,
                    "provider": self.cfg.provider,
                    "unusable": unusable,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- baselined in lifecycle.py before this move (issue 54)
            self.log.warning(
                "could not record the account's release on the task's events",
                account_id=account.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _release_account(self) -> None:
        """Give the account back, on whatever path this attempt is leaving by.

        CALLED FROM `_cleanup`, which is the `finally` of `run()`, because that
        is the ONE place every exit runs through: success, failure, a park on
        quota or on a missing credential, a cancellation, a crash, and a
        mid-run fencing exit. Releasing at each of those sites instead would
        mean a new exit path is a leaked assignment, discovered weeks later as
        an account that `choose()` has quietly stopped picking.

        Never raises. An exception here would replace the attempt's real
        outcome with this one.
        """
        account, self._account = self._account, None
        if account is None or self._account_released or self._account_broker is None:
            return
        self._account_released = True
        self._give_back(account)
