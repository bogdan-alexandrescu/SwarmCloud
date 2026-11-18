"""`swarm team add` and the LLM providers' Day One (docs/design/llm-providers.md
§7, lane LP-I1), on the fakes of `test_team_add`.

What has to hold:

  * the installer asks "plans, API keys, or both?" once and records the
    answer in the install state; a resumed install that recorded one never
    asks again, and one that recorded none asks once;
  * the team gate checks that choice: `plans` waits for a plan the team may
    use, `keys` for a key owned by or lent to the team, `both` for one of each;
  * a keys answer never prompts for a key in the terminal: it prints the
    console page where the one-step key form is;
  * the final summary prints the choice and each chain of §5.3 with what it
    has now, flags a chosen kind with nothing registered, says Voice it Live
    needs a key when Voice it is on, and names the tier the smoke ran on.

No credentials, no emulator, no cluster.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_install import team as team_mod
from swarm_install.waits import WaitGaveUp

from .test_team_add import ADMIN, CONSOLE, GROUP, TEAM, Api, Asked, Ctx, GitSecret, Pages, _flow, key_row, plan_row

PLAN = plan_row()
KEY = key_row()
KEYS_PAGE = f"{CONSOLE}/capacity/llm-providers/api-keys"


def _smoked(api: Api) -> bool:
    return any(c.startswith("smoke") for c in api.calls)


# ---------------------------------------------------------------------------
# the gate checks the choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("choice", "rows", "passes"), [
    ("plans", [PLAN], True),
    ("plans", [KEY], False),
    ("plans", [PLAN, KEY], True),
    ("keys", [PLAN], False),
    ("keys", [KEY], True),
    ("keys", [PLAN, KEY], True),
    ("both", [PLAN], False),
    ("both", [KEY], False),
    ("both", [PLAN, KEY], True),
])
def test_the_gate_waits_for_the_kind_chosen(choice: str, rows: list[dict[str, Any]], passes: bool) -> None:
    api = Api(accounts=rows)
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx(choice))
    if passes:
        flow.run(GROUP, first_admin=ADMIN)
        assert _smoked(api), api.calls
    else:
        with pytest.raises(WaitGaveUp):
            flow.run(GROUP, first_admin=ADMIN)
        assert not _smoked(api), "the smoke started on a kind the operator did not choose"


def test_a_key_lent_to_the_team_counts_and_another_teams_key_does_not() -> None:
    lent = Api(teams=["g-first"], accounts=[key_row(owner="g-first", lent_to=[TEAM])])
    flow, _out, _ = _flow(lent, git=GitSecret(lent, standing=True), ctx=Ctx("keys"))
    flow.run(GROUP, first_admin=ADMIN)
    assert _smoked(lent)

    unlent = Api(teams=["g-first"], accounts=[key_row(owner="g-first")])
    flow, _out, _ = _flow(unlent, git=GitSecret(unlent, standing=True), ctx=Ctx("keys"))
    with pytest.raises(WaitGaveUp):
        flow.run(GROUP, first_admin=ADMIN)
    assert not _smoked(unlent)


def test_an_openai_plan_does_not_open_the_claude_smoke() -> None:
    """The smoke agent is claude-code: its chain is Anthropic plans, then Anthropic keys (§5.3)."""
    api = Api(accounts=[plan_row(provider="openai"), key_row(provider="openai-api")])
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("both"))
    with pytest.raises(WaitGaveUp):
        flow.run(GROUP, first_admin=ADMIN)
    assert not _smoked(api)


# ---------------------------------------------------------------------------
# a keys answer: the console's one-step form, never the terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["keys", "API keys", "both"])
def test_a_keys_answer_prints_the_console_link_and_never_asks_for_a_key(answer: str) -> None:
    api = Api(accounts=[PLAN, KEY])
    asked = Asked(answer)
    ctx = Ctx(None)
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=ctx, ask=asked)
    flow.run(GROUP, first_admin=ADMIN)
    # One question, the plans / keys / both one; nothing else is read (the
    # autouse fixture fails the test on any read of the terminal itself).
    assert asked.questions == [team_mod.LLM_PROVIDERS_QUESTION], asked.questions
    text = out.getvalue()
    assert KEYS_PAGE in text, text
    assert "never typed here" in text, text
    # The link is in the summary at the end, after the smoke.
    assert text.rindex(KEYS_PAGE) > text.index("a green smoke agent"), text


def test_a_plans_answer_prints_no_key_link() -> None:
    api = Api()
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx(None), ask=Asked("plans"))
    flow.run(GROUP, first_admin=ADMIN)
    assert KEYS_PAGE not in out.getvalue()


def test_the_key_wait_opens_the_api_keys_page() -> None:
    api = Api(accounts=[KEY], accounts_after=2)
    pages = Pages()
    flow, _out, events = _flow(api, git=GitSecret(api, standing=True), pages=pages, ctx=Ctx("keys"))
    flow.run(GROUP, first_admin=ADMIN)
    assert KEYS_PAGE in [u for u, _ in pages.opened], pages.opened
    waits = [e for e in events.of_kind("wait_countdown") if e["step"] == "team.pool-account"]
    assert waits and "API key" in waits[0]["reason"], waits


# ---------------------------------------------------------------------------
# asked once, recorded; a resumed install never asks twice
# ---------------------------------------------------------------------------


def test_the_answer_is_recorded_and_a_resumed_install_does_not_ask_again() -> None:
    ctx = Ctx(None)
    asked = Asked("both")
    api = Api(accounts=[PLAN, KEY])
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=ctx, ask=asked)
    flow.run(GROUP, first_admin=ADMIN)
    assert ctx.state.outputs[team_mod.LLM_PROVIDERS] == {"choice": "both"}
    assert ctx.saves == 1

    # The same install, resumed: the recorded answer stands, nothing is asked.
    again = Asked()
    api = Api(accounts=[PLAN, KEY])
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=ctx, ask=again)
    flow.run(GROUP, first_admin=ADMIN)
    assert again.questions == []
    assert ctx.saves == 1


def test_the_question_is_asked_before_anything_is_registered() -> None:
    api = Api()
    seen: list[list[str]] = []

    def ask(question: str) -> str:
        seen.append(list(api.calls))
        return "plans"

    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx(None), ask=ask)  # type: ignore[arg-type]
    flow.run(GROUP, first_admin=ADMIN)
    assert len(seen) == 1 and not any(c.startswith("create") for c in seen[0]), seen


def test_an_unclear_answer_is_asked_again_and_none_records_nothing() -> None:
    ctx = Ctx(None)
    asked = Asked("maybe", "Both")
    api = Api(accounts=[PLAN, KEY])
    flow, _out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=ctx, ask=asked)
    flow.run(GROUP, first_admin=ADMIN)
    assert len(asked.questions) == 2 and "'maybe'" in asked.questions[1], asked.questions
    assert ctx.state.outputs[team_mod.LLM_PROVIDERS] == {"choice": "both"}

    ctx = Ctx(None)
    api = Api()
    flow, _out, _ = _flow(api, ctx=ctx, ask=Asked("x", "y", "z"))
    with pytest.raises(team_mod.TeamError, match="nothing was recorded"):
        flow.run(GROUP, first_admin=ADMIN)
    assert team_mod.LLM_PROVIDERS not in ctx.state.outputs
    assert not any(c.startswith("create") for c in api.calls), api.calls


def test_a_recorded_answer_that_is_not_one_of_the_three_is_refused() -> None:
    ctx = Ctx("plenty")
    flow, _out, _ = _flow(Api(), ctx=ctx, ask=Asked())
    with pytest.raises(team_mod.TeamError, match="plans, keys or both"):
        flow.run(GROUP, first_admin=ADMIN)


# ---------------------------------------------------------------------------
# the final summary
# ---------------------------------------------------------------------------


def test_the_summary_prints_the_choice_and_each_chain_and_flags_an_empty_kind() -> None:
    api = Api(accounts=[PLAN, KEY])
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("both"))
    flow.run(GROUP, first_admin=ADMIN)
    text = out.getvalue()
    assert "Agents run on: plans and API keys (both)" in text, text
    assert "claude-code: 1 plan, 1 key" in text, text
    assert "browser: 1 key" in text, text
    assert "codex: 0 plans, 0 keys" in text, text
    # codex: both kinds were chosen and it has neither.
    flagged = [line for line in text.splitlines() if line.lstrip().startswith("codex")]
    assert flagged and "nothing registered" in flagged[0], flagged
    claude = [line for line in text.splitlines() if line.lstrip().startswith("claude-code")]
    assert "nothing registered" not in claude[0], claude
    # Voice it is not on: no Voice it chain, and the pool is not read.
    assert "Voice it" not in text and "voice-pool" not in api.calls, text


def test_the_summary_flags_only_the_kind_chosen() -> None:
    api = Api(accounts=[PLAN])
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("plans"))
    flow.run(GROUP, first_admin=ADMIN)
    lines = out.getvalue().splitlines()
    # browser has no plan tier: with plans chosen it is not flagged for keys.
    browser = [line for line in lines if line.lstrip().startswith("browser")]
    assert browser and "nothing registered" not in browser[0], browser
    codex = [line for line in lines if line.lstrip().startswith("codex")]
    assert "nothing registered" in codex[0] and "no plan" in codex[0] and "no key" not in codex[0], codex


def test_the_summary_with_voice_it_on_lists_its_chains_and_says_live_needs_a_key() -> None:
    api = Api(accounts=[PLAN, plan_row(provider="openai", pools=("voice-it",), label="chatgpt")])
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("plans", voice=True))
    flow.run(GROUP, first_admin=ADMIN)
    text = out.getvalue()
    assert "voice-pool" in api.calls
    assert "Voice it Live: no key" in text, text
    assert "Voice it conversation: 1 plan, 0 keys" in text, text
    assert "Voice it planner: 0 plans, 0 keys" in text, text
    assert f"{CONSOLE}/voice/settings" in text and "Live needs a key" in text, text


def test_the_summary_names_the_voice_it_key_the_pool_would_lease() -> None:
    dedicated = key_row(provider="openai-api", pools=("voice-it",), label="voice", dedicated=True)
    voice = {"pool": "voice-it", "account": {"label": "voice", "display_name": None, "provider": "openai-api"},
             "reason": ""}
    api = Api(accounts=[PLAN, dedicated], voice=voice)
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("plans", voice=True))
    flow.run(GROUP, first_admin=ADMIN)
    text = out.getvalue()
    assert "Voice it Live: 0 shared keys, the dedicated key; the next session leases voice" in text, text


@pytest.mark.parametrize(("tier", "words"), [("plan", "a plan"), ("api_key", "an API key"),
                                             (None, "a tier the attempt did not record")])
def test_the_summary_names_the_tier_the_smoke_ran_on(tier: str | None, words: str) -> None:
    api = Api(accounts=[PLAN, KEY], tier=tier)
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("both"))
    flow.run(GROUP, first_admin=ADMIN)
    assert f"The smoke agent t-smoke-1 ran on {words}" in out.getvalue(), out.getvalue()


def test_a_summary_read_that_fails_does_not_fail_a_green_team() -> None:
    api = Api(accounts=[PLAN])

    def broken(team: str) -> Any:
        raise RuntimeError("swarm-api answered 503")

    api.summary_accounts = broken  # type: ignore[method-assign]
    flow, out, _ = _flow(api, git=GitSecret(api, standing=True), ctx=Ctx("plans"))
    result = flow.run(GROUP, first_admin=ADMIN)
    assert result["smoke_task"] == "t-smoke-1"
    assert "could not read the accounts (RuntimeError)" in out.getvalue(), out.getvalue()
