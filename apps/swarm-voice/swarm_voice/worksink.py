"""A delegation's lifecycle reaches the session's stream (docs/design/voice-it.md
§3.12.5, "The working card": the `work.delegation` row of its feed table).

THE LIVE FINDING (12 November 2026): a planner delegation ran about a minute,
the voice paused after 15 seconds of waiting as designed, and nothing on screen
said the agent was still working: the session's stream had no
`work.delegation` event, because nothing in swarm-voice wrote one.

`WorkSink` writes it. claudefront.py hands it each delegation's state --
`started` when the delegation begins running, `done`, `failed` or `refused`
when it ends, a delegation cut short by the session's end among them -- and
the sink posts `{delegation: {delegation_id, state}}` to swarm-api's
`POST /v1/voice/sessions/{session_id}/work` (routes/voice.py `work`,
voice/grant.py `record_work`) exactly as the planner reaches swarm-api
(tools_read.SwarmApi: this service's ID token and the session's own grant, a
route on the grant's write list). swarm-api stamps the event's `at` itself
when it writes it, and its whitelist (voice/workfeed.py `delegation_event`)
refuses any other key, so the time is never sent from here.

WHAT IS NEVER SENT. The delegation's id and the state, nothing else: no
request, no transcript, no tool argument and no model text is an input to the
post.

NOTHING WAITS ON IT. `report` queues the post behind the session's previous
one (swarm-api takes `started` before an end, so the order is kept) and
returns at once. A refused or failed post is logged once at WARNING with the
route and its status (tools_read.ApiRefused), never the grant, a token or
anything said, and is dropped: the turn and the delegation's result are the
same whether swarm-api heard it or not.

EXCEPT `started`, WHICH IS ASKED (the spend caps, voice-it.md §3.8; the
owner's decision of 23 November 2026). `admit` posts a delegation's `started`
behind the session's previous post and waits for the answer: swarm-api
refuses it (409, `detail.reason` one of CAP_REASONS) when the session is at a
cap or one more planner turn would pass one, writes nothing, and the caller
says that reason's one sentence (`CAP_SENTENCES`) instead of starting the
work. Any other failure admits it, as before: the cap is swarm-api's, and a
post that never arrived is not a refusal. Nor does an answer that has not come
within ADMIT_SECONDS hold the work longer: it runs, and the post goes on in
the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from . import tools_read
from .grant import SessionGrant

log = logging.getLogger("swarm_voice.worksink")

#: swarm-api's write route for a delegation's lifecycle (swarm_api/voice/
#: workfeed.py WORK_ROUTE), on the session grant's write list.
WORK_ROUTE = "/v1/voice/sessions/{session_id}/work"

#: A delegation's states, as swarm-api's workfeed.DELEGATION_STATES.
STARTED, PROGRESS, DONE, FAILED, REFUSED = "started", "progress", "done", "failed", "refused"
DELEGATION_STATES = (STARTED, PROGRESS, DONE, FAILED, REFUSED)

#: swarm-api's refusals of a `started` at a cap (swarm_api/voice/ledger.py
#: SESSION_CAPPED, SESSION_CAP_REACHED, DAY_CAP_REACHED), and the one sentence
#: the voice says for each instead of starting the work.
CAP_SENTENCES = {
    "voice_budget_capped": "This session has reached its budget cap, so I won't start that.",
    "voice_session_cap": "That would take this session past its budget cap, so I won't start it.",
    "voice_day_cap": "That would take today past the day's budget cap, so I won't start it.",
}
CAP_REASONS = tuple(CAP_SENTENCES)
#: The longest a delegation waits for swarm-api's answer to its `started`
#: before it runs anyway: the cap's check is a ledger read, well under it.
ADMIT_SECONDS = 5.0


class WorkSink:
    """Posts each delegation's lifecycle to swarm-api through the session's
    grant, in order per session, never holding the caller."""

    def __init__(self, http: Any, *, api_base: str, id_tokens: tools_read.IdTokens,
                 admit_seconds: float = ADMIT_SECONDS) -> None:
        self._http = http
        self._admit_seconds = admit_seconds
        self._base = api_base
        self._id_tokens = id_tokens
        #: Each voice session's last post in flight: the next one waits for it.
        self._posts: dict[str, asyncio.Task[str | None]] = {}

    def report(self, grant: SessionGrant | None, delegation_id: str, state: str) -> None:
        """Queue `{delegation_id, state}` behind the session's previous post
        and return at once. No grant, nothing is posted."""
        if grant is None or state not in DELEGATION_STATES:
            return
        key = grant.session_id
        previous = self._posts.get(key)
        task = asyncio.create_task(self._post(grant, {"delegation_id": delegation_id, "state": state}, previous),
                                   name=f"voice-work-{delegation_id}-{state}")
        self._posts[key] = task
        task.add_done_callback(lambda done: self._posts.pop(key, None) if self._posts.get(key) is done else None)

    async def admit(self, grant: SessionGrant | None, delegation_id: str) -> str | None:
        """Post `started` behind the session's previous post and wait for it:
        the cap's refusal reason (one of CAP_REASONS) when swarm-api refused
        it at a cap, else None (admitted, or not heard: the work runs)."""
        if grant is None:
            return None
        key = grant.session_id
        previous = self._posts.get(key)
        task = asyncio.create_task(self._post(grant, {"delegation_id": delegation_id, "state": STARTED},
                                              previous),
                                   name=f"voice-work-{delegation_id}-{STARTED}")
        self._posts[key] = task
        task.add_done_callback(lambda done: self._posts.pop(key, None) if self._posts.get(key) is done else None)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self._admit_seconds)
        except asyncio.TimeoutError:
            log.warning("delegation %s: swarm-api has not answered its start; it runs", delegation_id)
            return None

    async def _post(self, grant: SessionGrant, delegation: dict[str, str],
                    previous: asyncio.Task[str | None] | None) -> str | None:
        if previous is not None:
            # Its own failure is already logged; only its order matters here.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await previous
        api = tools_read.SwarmApi.for_session(self._http, base_url=self._base, id_tokens=self._id_tokens,
                                              grant=grant)
        try:
            await api._call("POST", WORK_ROUTE, WORK_ROUTE.format(session_id=grant.session_id),  # noqa: SLF001
                            json_body={"delegation": delegation})
        except tools_read.ApiRefused as exc:
            if delegation["state"] == STARTED and exc.status == 409 and exc.reason in CAP_REASONS:
                log.info("delegation %s: swarm-api refused its start at a cap (%s)",
                         delegation["delegation_id"], exc.reason)
                return exc.reason
            # The route and status only: never the grant, a token or what was said.
            log.warning("delegation %s: swarm-api was not told it is %s: %s", delegation["delegation_id"],
                        delegation["state"], exc)
            return None
        except Exception as exc:  # noqa: BLE001 -- the type only; the turn never sees it
            log.warning("delegation %s: swarm-api was not told it is %s: %s %s", delegation["delegation_id"],
                        delegation["state"], WORK_ROUTE, type(exc).__name__)
            return None
        log.info("delegation %s: swarm-api was told it is %s", delegation["delegation_id"], delegation["state"])
        return None

    async def drain(self) -> None:
        """Wait for every post in flight."""
        while self._posts:
            await asyncio.gather(*list(self._posts.values()), return_exceptions=True)


def build_work_sink(http: Any) -> WorkSink | None:
    """The sink from SWARM_API_URL (and SWARM_API_AUDIENCE, else the URL), the
    planner's own address; None when it is unset."""
    base = os.environ.get("SWARM_API_URL", "").strip()
    if not base:
        return None
    audience = os.environ.get("SWARM_API_AUDIENCE", "").strip() or base
    return WorkSink(http, api_base=base, id_tokens=tools_read.google_id_tokens(audience))
