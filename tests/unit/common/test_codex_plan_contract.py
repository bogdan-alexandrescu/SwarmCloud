"""Contract request 130, accepted in part: codex names a ChatGPT plan's credential (part of #2688).

The owner accepted request 130 on 2026-11-01 with one change: "Accept, but
keep codex unavailable until the pool lanes land". What is held here:

  * `codex` declares three interchangeable credential names, the tenant's
    metered `OPENAI_API_KEY` or a pool ChatGPT plan's access token and account
    id, with `secrets_any_of=True` (claude-code's shape);
  * it stays `available=False`, and its `disabled_reason` says it waits for
    the ChatGPT-plan pool (the broker leasing OpenAI plan accounts and the
    runner using them), not the 2026-09-23 refusal;
  * the Terraform mirror's `secret_env` names the same three, each read from
    the tenant's `openai` secret slot;
  * docs/contract-change-requests.md records request 130 as accepted in part,
    on a line carrying "accepted by the owner", which is what
    scripts/lib/check-frozen-contract.sh admits a `swarm_common` change on.

No cloud, no emulator: the catalogue is pure and the rest is text.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"
LOCALS = REPO / "terraform" / "infra" / "locals.tf"

PLAN_NAMES = ("OPENAI_API_KEY", "CODEX_PLAN_ACCESS_TOKEN", "CODEX_PLAN_ACCOUNT_ID")


def test_codex_takes_the_tenant_key_or_a_pool_plan():
    codex = RUNNER_PROFILES["codex"]
    # MUTATION: leave `secrets=("OPENAI_API_KEY",)` and a plan leased from the
    # pool has no declared name to arrive under.
    assert codex.secrets == PLAN_NAMES
    # MUTATION: drop `secrets_any_of=True` and every codex step needs a key AND
    # a plan, which no tenant has.
    assert codex.secrets_any_of is True
    assert codex.provider == "openai"


def test_codex_stays_refused_until_the_plan_pool_lands():
    codex = RUNNER_PROFILES["codex"]
    # MUTATION: `available=True` -- the owner's change to the request.
    assert codex.available is False
    reason = codex.disabled_reason
    # MUTATION: keep request 11's reason and the console still blames a refused
    # key nobody is asked to fix.
    assert "focused on Claude" not in reason
    assert "ChatGPT-plan pool" in reason
    assert "broker" in reason and "runner" in reason
    assert "follow-up" in reason
    # The console's picker turns this into "Not enabled yet. Use claude-code"
    # (RunnerPicker.shortReason): it needs "disabled" or "until" and the
    # closing "Use claude-code."
    assert re.search(r"\b(disabled|until)\b", reason)
    assert reason.endswith("Use claude-code.")


def test_the_terraform_mirror_names_the_same_three_secrets():
    text = LOCALS.read_text(encoding="utf-8")
    start = text.index('"codex" = {')
    block = text[start:text.index("}", text.index("secret_env", start)) + 1]
    # MUTATION: leave `secret_env = { OPENAI_API_KEY = "openai" }` and
    # catalogue.tftest.hcl's mirror run reddens.
    for name in PLAN_NAMES:
        assert name in block, f"terraform/infra/locals.tf codex secret_env lacks {name}"
    assert '=> "openai"' in block


def test_request_130_is_recorded_as_accepted_in_part():
    text = REQUESTS.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith("| 130 |")]
    # MUTATION: leave row 130 reading "open" and the index says the catalogue
    # lacks a change it carries.
    assert not row.rstrip().endswith("| open, REQUESTED 2026-11-01 by lane CODEX-PLAN-CONTRACT for the owner's shared ChatGPT-plan pool |")
    assert "accepted in part" in row.lower()
    assert "keep codex unavailable until the pool lanes land" in row
    # MUTATION: drop "accepted by the owner" and check-frozen-contract.sh
    # refuses this pull request's swarm_common diff.
    assert "accepted by the owner" in row.lower()
