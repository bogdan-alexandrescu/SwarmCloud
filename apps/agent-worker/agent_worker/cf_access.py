"""A Cloudflare Access service token, the worker's half (CF-ACCESS-TOKEN, part of #2442).

BGD decided on 2026-10-26 that browser visual-QA agents reach a site behind
Cloudflare Access with a SERVICE TOKEN. The token is a workspace secret of
kind `oauth_client` granted to `profile:claude-code-browser` in its
`cf-access` slot, with the hosts it may be sent to set on the grant
(docs/runbooks/cf-access-service-token.md). The gate adds it to requests for
those hosts (`livegate/cfaccess.py`); this module is how it gets there.

THE PATH OF THE VALUE. swarm-api's release answers it to this process alone,
with the grant's `hosts` (`secretrelease`); the release holds it in
`Released.gate` and never writes it to `private/`, the manifest or the
runner's environment. `hand_over` turns each item into `{hosts, client_id,
client_secret}`, sends all of them to the gate in one `cf_access` ctl.sock
operation, and empties the list whatever happened, so the worker's copy
outlives the hand-over by nothing. The agent's environment, prompt, files and
tool results never carry it; every field was registered for redaction when
the release was parsed.

NOTHING TO HAND, NOTHING SENT. An attempt whose workspace granted no
`cf-access` secret has no items: no ctl.sock operation, no log line, and the
gate intercepts nothing.

FAIL CLOSED, NEVER FAIL THE ATTEMPT. No live gate (a profile with no browser),
a heap the agent could read (`unprotected`), an item that is not
`{client_id, client_secret}`, or a gate that refuses: the token is not handed
over, the browser reaches the site as a signed-out visitor (Access's own
sign-in page), and the log line names the cause and the secret's NAME, never
a value. The task is not failed for it: a visual-QA step can still report
what it saw, and the reason is in the worker's log.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: The gate's ctl.sock operation (`livegate.cfaccess.OP`, `livegate.ctl.OPS`).
CTL_OP = "cf_access"

#: Outcomes of `hand_over`, for the log and the tests.
NONE = "none"
HANDED = "handed"
NO_GATE = "no_gate"
UNPROTECTED = "unprotected"
MALFORMED = "malformed"
GATE_REFUSED = "gate_refused"


def tokens(items: list[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    """Each gate item as the gate's `{hosts, client_id, client_secret}`; None
    if any value is not an `oauth_client` object (no partial hand-over)."""
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            parsed = json.loads(str(item.get("value") or ""))
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        client_id, client_secret = parsed.get("client_id"), parsed.get("client_secret")
        if not isinstance(client_id, str) or not isinstance(client_secret, str) or not client_id or not client_secret:
            return None
        out.append({"hosts": list(item.get("hosts") or []), "client_id": client_id,
                    "client_secret": client_secret})
    return out


def hand_over(gate: Any, items: list[dict[str, Any]], log: Any, *, protected: bool) -> str:
    """Send the attempt's gate items to the live gate, once. Never raises.

    `gate` is the registered ctl.sock client, or None when the attempt has no
    live gate; `protected` is whether this heap is safe from the agent. The
    list is emptied before returning, whatever the outcome."""
    if not items:
        return NONE
    names = [str(item.get("name")) for item in items]
    hosts = sorted({str(h) for item in items for h in item.get("hosts") or []})
    try:
        if gate is None:
            outcome = NO_GATE
        elif not protected:
            outcome = UNPROTECTED
        else:
            payload = tokens(items)
            if payload is None:
                outcome = MALFORMED
            else:
                try:
                    gate.call(CTL_OP, {"tokens": payload})
                    outcome = HANDED
                except Exception as exc:  # the gate's refusal is a word it chose; only the class is logged
                    log.warning("the gate did not take the Cloudflare Access token",
                                error=type(exc).__name__, names=names)
                    outcome = GATE_REFUSED
                finally:
                    payload.clear()
    finally:
        items.clear()
    if outcome == HANDED:
        log.info("Cloudflare Access token handed to the gate", names=names, hosts=hosts)
    elif outcome != GATE_REFUSED:
        log.warning("Cloudflare Access token not handed to the gate; the browser reaches its hosts "
                    "signed out", cause=outcome, names=names, hosts=hosts)
    return outcome
