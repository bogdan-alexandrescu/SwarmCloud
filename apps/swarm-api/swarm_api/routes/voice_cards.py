"""A person's answer to a Voice it card by a click (docs/design/voice-it.md
§3.12.3; swarm_api/voice/cards.py, voice/readback.py).

    POST /v1/voice/sessions/{session_id}/cards/{card_id}/accept  -> {card}

THE NAVIGATION OFFER. The planner offers a console page as a `navigate` card,
written only as `proposed`. Open on the card is this route, on the person's
cookie: it moves the session's open offer to `accepted` with `accepted_by:
person` (`readback.Offers.accept`, the same path the read-back gate's spoken
yes takes), and the card's `card.upsert` reaches the page. The holder then
navigates through the console's own `go`, from the card's destination name;
nothing here is an address and nothing here navigates, admits, reserves or
runs anything.

WHO. Only the session's own person: the caller on the Voice it list (403),
a session of the caller's own in a workspace the caller belongs to on this
request, else the same 404 as a missing one (invariant 9). A session grant is
refused (403): the planner's side never accepts its own offer. The card must
be a `navigate` card of this session (else 404) and its open offer: one
answered, replaced, outlived or older than two minutes is 409, and so is any
card of a session that has ended. Logs name ids and states only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import Conflict
from ..voice import grant as vgrant
from ..voice import readback as vreadback
from ..voice import sessions as vs
from ..voice import store as vstore
from ..voice import stream as vstream
from .voice import allowed_person
from .voice_readback import vs_check

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def _accept(ctx: AppContext, auth: AuthContext, person: str, session_id: str,
            card_id: str) -> dict:
    vs_check(session_id)
    vstore.check_id(card_id, "vc_", "card_id")
    found = vstream.find_session(ctx.db, person=person, session_id=session_id,
                                 tenants=vstream.tenants_of(auth))
    if found.state != vs.LIVE:
        raise Conflict(f"voice session {session_id!r} has ended; no offer of it is opened")
    where = vreadback.Where(tenant_id=found.tenant_id, person=person,
                            project_id=found.project_id, session_id=found.session_id)
    return vreadback.Offers(ctx.db, now=ctx.now).accept(where, card_id)


@router.post("/sessions/{session_id}/cards/{card_id}/accept")
async def accept_card(
    session_id: str,
    card_id: str,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if auth.via == vgrant.VIA:
        raise vgrant.RouteNotGranted("a navigation offer is accepted by the person, never a grant")
    card = await run_in_threadpool(_accept, ctx, auth, person, session_id, card_id)
    return {"card": card}
