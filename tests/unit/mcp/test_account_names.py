"""The plugin prints an account's Name where it named it by label (#1913).

Owner decision 2026-10-22 (BGD, "Editable display name"): an account's Name
is shown in place of its label, and where the id is printed it stays, so the
Name never hides which account it is. An account with no Name reads as before.

  * `swarm_accounts` (`render.render_accounts`) prints the id in its ACCOUNT
    column; a named account prints its Name with that id beside it.
  * The LLM line (`details.account_labels`, then `account_text`) named an
    account by its label; it names it by its Name when it has one.
  * A Name is free text its owner typed, so it passes `details.shown` like a
    label does: a credential-shaped one is never printed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_mcp import details
from swarm_mcp.render import Style, render_accounts

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
WIDE = Style(width=120, color=False, unicode=True)


def _account(account_id: str, **extra):
    payload = {
        "account_id": account_id,
        "owner_tenant": account_id.split(":")[0],
        "label": account_id.split(":")[-1],
        "provider": "anthropic",
        "state": "AVAILABLE",
        "reason": "",
        "lend_to": [],
        "assigned": 0,
        "windows": {},
        "observed_at": None,
        "stale": True,
    }
    payload.update(extra)
    return payload


def _text(lines) -> str:
    return "\n".join(lines)


class _Listing:
    def __init__(self, accounts):
        self.accounts = accounts

    def request(self, method, path, **_):
        assert (method, path) == ("GET", "/v1/accounts")
        return {"accounts": self.accounts}


def test_swarm_accounts_prints_the_name_with_the_id_beside_it():
    out = _text(render_accounts([_account("eng:ai-eng-2", display_name="AI Eng 2")], WIDE, NOW))
    # MUTATION: print only `account_id` in the ACCOUNT cell -- the Name the
    # owner gave is nowhere in the table.
    assert "AI Eng 2" in out
    # MUTATION: print the Name in place of the id -- which account it is is lost.
    assert "eng:ai-eng-2" in out


def test_swarm_accounts_prints_an_unnamed_account_as_before():
    out = _text(render_accounts([_account("eng:plain", display_name=None)], WIDE, NOW))
    # MUTATION: fall back to `label` in the cell -- `plain (eng:plain)`.
    assert "plain (" not in out
    assert "eng:plain" in out


def test_swarm_accounts_never_prints_a_credential_shaped_name():
    shaped = "sk-ant-" + "x" * 40
    out = _text(render_accounts([_account("eng:odd", display_name=shaped)], WIDE, NOW))
    # MUTATION: print `display_name` without `details.shown` -- the pasted key
    # is in the plugin's reply.
    assert shaped not in out
    assert "eng:odd" in out


def test_the_llm_line_names_an_account_by_its_name():
    labels = details.account_labels(
        _Listing(
            [
                _account("eng:ai-eng-2", display_name="AI Eng 2"),
                _account("eng:plain", display_name=None),
            ]
        )
    )
    # MUTATION: read only `label` in `account_labels` -- the LLM line says
    # `ai-eng-2` after the owner renamed it.
    assert labels["eng:ai-eng-2"] == "AI Eng 2"
    # MUTATION: drop the label fallback -- an unnamed account is shown by its id.
    assert labels["eng:plain"] == "plain"
    line = details.account_text(
        {"status": "assigned", "account_id": "eng:ai-eng-2", "provider": "anthropic"}, labels
    )
    assert line == "AI Eng 2 (anthropic)"


def test_the_llm_line_falls_back_to_the_label_for_a_credential_shaped_name():
    shaped = "sk-ant-" + "y" * 40
    labels = details.account_labels(_Listing([_account("eng:odd", display_name=shaped)]))
    # MUTATION: prefer the Name even when `shown` withheld it -- the line
    # reads `withheld (credential-shaped)` instead of the account's label.
    assert labels["eng:odd"] == "odd"
