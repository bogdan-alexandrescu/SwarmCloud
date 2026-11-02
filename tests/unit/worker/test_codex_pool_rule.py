"""The scheduler and swarm-api expect a ChatGPT plan from the pool for codex, as the worker asks for one.

Contract request 130, item 6 (part of #2688). The worker leases an openai plan
for a keyless codex step (`accountlease.ACCOUNT_ENVS`); the scheduler's pool
rule keyed on CLAUDE_CODE_OAUTH_TOKEN alone, so with codex turned on
(2026-11-02) a keyless tenant whose pool holds a ChatGPT plan would have parked
CREDENTIAL_MISSING ("profile_takes_no_subscription") and never been leased,
and swarm-api's providers listing and task-account column would have called
codex "not pooled".

No cloud, no emulator: the catalogue and the credential rule are pure.
"""

from __future__ import annotations

from agent_worker.accountlease import ACCOUNT_ENVS, takes_account
from swarm_common.profiles import RUNNER_PROFILES

from test_pool_credential_parity import scheduler_expects_the_pool


def test_the_scheduler_and_swarm_api_name_the_same_account_variables_as_the_worker():
    from scheduler.credentials import ACCOUNT_ENVS as SCHEDULER_ACCOUNT_ENVS
    from swarm_api.task_accounts import ACCOUNT_ENVS as API_ACCOUNT_ENVS

    assert SCHEDULER_ACCOUNT_ENVS == ACCOUNT_ENVS
    assert API_ACCOUNT_ENVS == ACCOUNT_ENVS


def test_the_scheduler_swarm_api_and_the_worker_agree_on_every_profile():
    from scheduler.credentials import takes_pool_account
    from swarm_api.task_accounts import takes_pool_account as api_takes_pool_account

    visited = 0
    for name, profile in sorted(RUNNER_PROFILES.items()):
        visited += 1
        worker = takes_account(profile.provider, profile.secrets)
        assert takes_pool_account(profile) == worker, name
        assert api_takes_pool_account(profile) == worker, name
    assert visited == len(RUNNER_PROFILES) > 1


def test_a_keyless_codex_step_is_expected_on_a_chatgpt_plan():
    # MUTATION: key the scheduler's rule on CLAUDE_CODE_OAUTH_TOKEN alone.
    assert scheduler_expects_the_pool("codex")
    assert scheduler_expects_the_pool("claude-code")
    # A metered-key-only profile still takes no account.
    assert not scheduler_expects_the_pool("browser")
