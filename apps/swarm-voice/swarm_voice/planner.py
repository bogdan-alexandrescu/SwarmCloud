"""The planner: each delegation becomes cards and at most two spoken sentences
(docs/design/voice-it.md §3.3, §3.4, §3.6, §3.9; section 10, phase two).

ONE TURN, as §3.3 lays it out. When the live voice delegates (live.py sees
`session.delegation.created` on the sideband), `Planner.delegate`:

  1. refuses at once when the session holds no grant (tools_read.NoGrant): the
     planner reads and writes nothing in swarm-api except as the session's
     person;
  2. reads the person's Anthropic key from Secret Manager, for this turn only;
  3. reads the card catalogue from swarm-api's schema route WITH THE GRANT, and
     offers the model exactly its `draw_<kind>` tools (strict schemas), the
     read tools (tools_read.py) and `speak`;
  4. runs Claude Opus with those tools, executing each read through swarm-api
     with the grant and writing each card through swarm-api's card write path,
     which validates it against the catalogue and appends it to the session's
     stream;
  5. hands the live voice at most two sentences to say (prompts.spoken).

AN UNKNOWN KIND IS REFUSED. A tool the model calls that is not a read tool,
not `speak` and not one of the catalogue's `draw_<kind>` tools as served this
turn is answered with an error and never reaches swarm-api; the refusal is
recorded on the turn's result. swarm-api refuses an unknown kind again on write
(cards.validate), so the console never sees one.

THE MODEL AND ITS SETTINGS. Claude Opus 5.5 (`claude-opus-5-5`, §3.3) through
the Messages API over httpx, the way live.py calls OpenAI (no SDK is added to
swarm-voice's locked dependencies). Opus 5.5 always thinks, so no `thinking`
field is sent; `output_config.effort` is set explicitly. Forced tool choice is
refused by this model, so `tool_choice` stays automatic and the prompt asks
for `speak`; every tool is `strict: true`. Each response's content is appended
whole, thinking blocks included, as the API requires for the next request.

THE KEY (§3.6). An Anthropic API key -- the planner calls the Messages API, so
not a subscription -- in the PERSON'S OWN secret,
`swarm-tenant-<personal tenant>-anthropic` of this deployment's project, the
name `TenantConfig.secret_name("anthropic")` gives a tenant's. It is read on
every turn and held only for that turn, so one session's key is never used in
another (§7, invariant 9). swarm-voice needs a per-secret accessor binding to
read it, an IAM change the owner approves (terraform/infra/voice.tf); until
it is applied the read is 403 and the planner refuses with that reason, which
the voice says.

NOTHING SECRET IN A PROMPT, A LOG OR A CARD. The key and the grant are scrubbed
from the transcript, from every tool result and from every card before it is
written (prompts.scrub); errors carry an HTTP status, never a body; log lines
name the session, kinds, counts and statuses only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from swarm_common.identity import tenant_id_for_user

from . import prompts, tools_read
from .credential import CredentialError
from .grant import SessionGrant

log = logging.getLogger("swarm_voice.planner")

ANTHROPIC_API = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-opus-5-5"
#: Opus 5.5's default effort is `medium`; set explicitly so a default change
#: upstream never changes what a turn costs.
EFFORT = "medium"
MAX_TOKENS = 16_000
#: Model calls in one turn: reads, draws and the closing `speak`.
MAX_ROUNDS = 8
#: Cards one turn may write: a delegation usually needs one (§3.3).
MAX_CARDS = 4
#: One Messages call; Opus thinks before it answers.
CALL_SECONDS = 120.0
#: The whole turn, after which the voice is told the planner ran out of time.
TURN_SECONDS = 240.0

DRAW_PREFIX = "draw_"

#: What the voice says when a turn could not run, by cause. Short, plain, and
#: never a status code or an id.
SAY_NO_GRANT = "I can't reach SwarmCloud for this conversation, so I couldn't draw anything."
SAY_NO_KEY = "The planner has no Anthropic key yet, so I can't draw cards for now."
SAY_NO_ACCESS = ("The planner isn't allowed to read its Anthropic key yet; "
                 "that access waits for the owner's approval.")
SAY_FAILED = "The planner couldn't finish that one, so nothing new is on screen."
SAY_REFUSED = "I can't plan that one."


def anthropic_secret_id(person: str) -> str:
    """The person's own Anthropic secret, as `TenantConfig.secret_name("anthropic")` names a tenant's."""
    return f"swarm-tenant-{tenant_id_for_user(person)}-anthropic"


class PlannerUnavailable(RuntimeError):
    """The turn cannot run. `say` is what the voice tells the person; the
    message is the reason for the log, never a value."""

    def __init__(self, reason: str, say: str) -> None:
        super().__init__(reason)
        self.say = say


class ApiKey:
    """An Anthropic API key whose repr and str never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "ApiKey(<redacted>)"

    __str__ = __repr__


SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"


class SecretStore(Protocol):
    async def access(self, version_ref: str) -> bytes: ...


class SecretManagerStore:
    """Secret Manager READ over REST with the pod's own Workload Identity, for the
    planner's Anthropic key only (this module's docstring, "THE KEY"). It adds no
    version and reads no OpenAI credential: the voice's ChatGPT plan account is
    leased from the quota broker's Voice it pool (accountlease.py), and nothing in
    swarm-voice reads a person's ChatGPT sign-in. google-auth's session is
    synchronous, so each call runs in a thread."""

    def __init__(self) -> None:
        self._session = None

    def _authorized(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _access(self, version_ref: str) -> bytes:
        response = self._authorized().get(SECRET_MANAGER + version_ref + ":access", timeout=30)
        if response.status_code != 200:
            # The status only: an error body may quote the request.
            raise CredentialError(f"the secret version could not be read (HTTP {response.status_code})")
        try:
            return base64.b64decode(response.json()["payload"]["data"])
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise CredentialError("Secret Manager returned no payload") from None

    async def access(self, version_ref: str) -> bytes:
        return await asyncio.to_thread(self._access, version_ref)


class AnthropicKeys:
    """Reads the person's own Anthropic key, once per turn, from Secret Manager."""

    def __init__(self, *, store: SecretStore, project: str) -> None:
        if not project:
            raise RuntimeError("PROJECT_ID is required to name the planner's key")
        self._store = store
        self._project = project

    async def for_person(self, person: str) -> ApiKey:
        try:
            secret = anthropic_secret_id(person)
        except ValueError:
            raise PlannerUnavailable("the session's person is not an email address", SAY_NO_KEY) from None
        # `latest`: an API key is replaced, never refreshed, so the newest
        # version is the key.
        ref = f"projects/{self._project}/secrets/{secret}/versions/latest"
        try:
            raw = await self._store.access(ref)
        except CredentialError as exc:
            status = re.search(r"HTTP (\d{3})", str(exc))
            code = status.group(1) if status else ""
            if code == "403":
                raise PlannerUnavailable(
                    "swarm-voice may not read the person's Anthropic secret: the accessor "
                    "binding (terraform/infra/voice.tf, voice_reads_anthropic) is not applied",
                    SAY_NO_ACCESS) from None
            if code == "404":
                raise PlannerUnavailable("the person has no Anthropic secret (provider anthropic)",
                                         SAY_NO_KEY) from None
            raise PlannerUnavailable(f"the Anthropic secret could not be read (HTTP {code or '?'})",
                                     SAY_FAILED) from None
        key = _key_from(raw)
        if not key:
            raise PlannerUnavailable("the person's Anthropic secret holds no API key", SAY_NO_KEY)
        return ApiKey(key)


def _key_from(raw: bytes) -> str:
    """The key in a secret's payload: the bare key, or JSON with `api_key`."""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    if text.startswith("{"):
        try:
            value = json.loads(text).get("api_key")
        except (ValueError, AttributeError):
            return ""
        return value.strip() if isinstance(value, str) else ""
    return text if text and not any(c.isspace() for c in text) else ""


class AnthropicError(RuntimeError):
    """The Messages API refused or failed. Names a status, never a body."""

    def __init__(self, status: int | None, what: str = "") -> None:
        self.status = status
        super().__init__(f"the Messages API answered HTTP {status}" if status is not None
                         else f"the Messages API could not be reached ({what})")


class MessagesApi:
    """POST /v1/messages over one shared HTTP client."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str = ANTHROPIC_API) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def create(self, key: ApiKey, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-api-key": key.reveal(), "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        try:
            response = await self._http.post(f"{self._base}/v1/messages", json=body, headers=headers,
                                             timeout=CALL_SECONDS)
        except httpx.HTTPError as exc:
            raise AnthropicError(None, type(exc).__name__) from None
        if response.status_code != 200:
            raise AnthropicError(response.status_code)
        try:
            out = response.json()
        except ValueError:
            raise AnthropicError(200, "not JSON") from None
        if not isinstance(out, dict) or not isinstance(out.get("content"), list):
            raise AnthropicError(200, "no content")
        return out


@dataclass
class TurnResult:
    """What one delegation produced. Holds no secret."""

    delegation_id: str | None
    spoken: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    #: Tool calls refused: unknown kinds and tools, and cards swarm-api refused.
    refused: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    #: Why the turn could not run or finish, for the log; None when it did.
    error: str | None = None


def _scrub_all(value: Any, known: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return prompts.scrub(value, known)
    if isinstance(value, list):
        return [_scrub_all(v, known) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_all(v, known) for k, v in value.items()}
    return value


def _card_from(kind: str, args: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    """The card a `draw_<kind>` call asks for, as swarm-api's write path takes it.
    The project, session, version and time are swarm-api's to set."""
    card = {"card_id": args.get("card_id"), "kind": kind, "state": args.get("state"),
            "title": args.get("title"), "created_by": "planner",
            "references": args.get("references"), "summary": args.get("summary"),
            "body": args.get("body")}
    return _scrub_all(card, known)


class Planner:
    """Claude Opus over the catalogue's draw tools and the read tools."""

    def __init__(self, http: httpx.AsyncClient, *, api_base: str, id_tokens: tools_read.IdTokens,
                 keys: AnthropicKeys, messages: MessagesApi | None = None, model: str = MODEL,
                 effort: str = EFFORT, max_rounds: int = MAX_ROUNDS,
                 turn_seconds: float = TURN_SECONDS) -> None:
        self._http = http
        self._api_base = api_base
        self._id_tokens = id_tokens
        self._keys = keys
        self._messages = messages or MessagesApi(http)
        self._model = model
        self._effort = effort
        self._max_rounds = max_rounds
        self._turn_seconds = turn_seconds
        #: One turn at a time per voice session (§3.8: turns run one at a time).
        self._locks: dict[str, asyncio.Lock] = {}

    def forget(self, session_id: str) -> None:
        self._locks.pop(session_id, None)

    async def delegate(self, *, person: str, grant: SessionGrant | None,
                       transcript: list[tuple[str, str]], delegation_id: str | None = None) -> TurnResult:
        """Run one turn for a delegation; never raises for a refusal or a failure:
        the result's `spoken` always says what happened."""
        try:
            api = tools_read.SwarmApi.for_session(self._http, base_url=self._api_base,
                                                  id_tokens=self._id_tokens, grant=grant)
        except tools_read.NoGrant as exc:
            log.warning("planner refused: %s", exc)
            return TurnResult(delegation_id, SAY_NO_GRANT, error=str(exc))
        lock = self._locks.setdefault(api.session_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(
                    self._turn(api, person=person, transcript=transcript, delegation_id=delegation_id),
                    self._turn_seconds)
            except asyncio.TimeoutError:
                log.warning("voice session %s: planner turn ran out of time", api.session_id)
                return TurnResult(delegation_id, SAY_FAILED, error="the turn ran out of time")

    async def _turn(self, api: tools_read.SwarmApi, *, person: str, transcript: list[tuple[str, str]],
                    delegation_id: str | None) -> TurnResult:
        result = TurnResult(delegation_id, "")
        try:
            key = await self._keys.for_person(person)
        except PlannerUnavailable as exc:
            log.warning("voice session %s: planner unavailable: %s", api.session_id, exc)
            result.spoken, result.error = exc.say, str(exc)
            return result
        known = (key.reveal(), *api.known_secrets())
        try:
            catalogue = await api.catalogue()
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: planner could not read the catalogue: %s", api.session_id, exc)
            result.spoken, result.error = SAY_FAILED, str(exc)
            return result
        draw_tools = [t for t in catalogue["tools"]
                      if isinstance(t, dict) and str(t.get("name", "")).startswith(DRAW_PREFIX)]
        kinds = {str(t["name"])[len(DRAW_PREFIX):] for t in draw_tools}
        tools = [*draw_tools, *tools_read.tool_schemas(), prompts.speak_tool()]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompts.turn_message(transcript, secrets_=known)}]
        said: str | None = None
        try:
            for _ in range(self._max_rounds):
                response = await self._messages.create(key, self._body(tools, messages))
                self._count(result, response.get("usage"))
                content = response["content"]
                messages.append({"role": "assistant", "content": content})
                if response.get("stop_reason") == "refusal":
                    said = said or SAY_REFUSED
                    break
                uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if not uses:
                    if said is None:
                        said = " ".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text")
                    break
                answers = []
                for use in uses:
                    text, is_error, spoken_now = await self._tool(api, use, kinds, known, result)
                    if spoken_now is not None:
                        said = spoken_now
                    answers.append({"type": "tool_result", "tool_use_id": use.get("id"),
                                    "content": text, **({"is_error": True} if is_error else {})})
                if said is not None:
                    break
                messages.append({"role": "user", "content": answers})
        except AnthropicError as exc:
            log.warning("voice session %s: planner call failed: %s", api.session_id, exc)
            result.error = str(exc)
            result.spoken = prompts.spoken(said or SAY_FAILED, secrets_=known)
            return result
        result.spoken = prompts.spoken(said or "", secrets_=known) or (
            "It's on screen." if result.cards else SAY_FAILED)
        log.info("voice session %s: planner turn drew %d card(s) [%s], refused %d, read %d",
                 api.session_id, len(result.cards), ",".join(c.get("kind", "?") for c in result.cards),
                 len(result.refused), len(result.reads))
        return result

    def _body(self, tools: list[dict], messages: list[dict]) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": prompts.SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": self._effort},
        }

    @staticmethod
    def _count(result: TurnResult, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for name in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                     "cache_creation_input_tokens"):
            value = usage.get(name)
            if isinstance(value, int):
                result.usage[name] = result.usage.get(name, 0) + value

    async def _tool(self, api: tools_read.SwarmApi, use: dict, kinds: set[str], known: tuple[str, ...],
                    result: TurnResult) -> tuple[str, bool, str | None]:
        """(the tool_result text, whether it is an error, the spoken summary if `speak`)."""
        name = str(use.get("name") or "")
        args = use.get("input") if isinstance(use.get("input"), dict) else {}
        if name == prompts.SPEAK_TOOL:
            summary = args.get("summary")
            return "said", False, summary if isinstance(summary, str) else ""
        if tools_read.is_read_tool(name):
            result.reads.append(name)
            try:
                body = await tools_read.run_read_tool(api, name, args)
            except tools_read.BadInput as exc:
                return str(exc), True, None
            except tools_read.ApiRefused as exc:
                return str(exc), True, None
            return prompts.fence(name, prompts.scrub(prompts.as_text(body), known)), False, None
        kind = name[len(DRAW_PREFIX):] if name.startswith(DRAW_PREFIX) else ""
        if not kind or kind not in kinds:
            result.refused.append(name)
            log.warning("voice session %s: planner called an unknown kind or tool; refused",
                        api.session_id)
            return (f"refused: {name[:64]!r} is not a card kind in the catalogue or a tool offered "
                    "this turn; nothing was drawn"), True, None
        if len(result.cards) >= MAX_CARDS:
            result.refused.append(name)
            return f"refused: a turn draws at most {MAX_CARDS} cards", True, None
        try:
            written = await api.write_card(_card_from(kind, args, known))
        except tools_read.ApiRefused as exc:
            result.refused.append(name)
            log.warning("voice session %s: a %s card was not written: %s", api.session_id, kind, exc)
            return f"the card was not written: {exc}", True, None
        card = written.get("card") if isinstance(written.get("card"), dict) else written
        result.cards.append({"kind": kind, "card_id": card.get("card_id"), "title": card.get("title")})
        return prompts.as_text({"written": True, "card_id": card.get("card_id"),
                                "version": card.get("version")}), False, None


def build_planner(http: httpx.AsyncClient, *, store: SecretStore | None = None) -> Planner | None:
    """The deployment's planner from the environment, or None when swarm-api's
    URL is not configured (then no session offers delegation).

    * `SWARM_API_URL` -- swarm-api's base URL;
    * `SWARM_API_AUDIENCE` -- the audience swarm-api verifies (defaults to the URL);
    * `PROJECT_ID` -- the project whose Secret Manager holds the person's key."""
    base = os.environ.get("SWARM_API_URL", "").strip()
    if not base:
        return None
    audience = os.environ.get("SWARM_API_AUDIENCE", "").strip() or base
    keys = AnthropicKeys(store=store or SecretManagerStore(), project=os.environ.get("PROJECT_ID", "").strip())
    return Planner(http, api_base=base, id_tokens=tools_read.google_id_tokens(audience), keys=keys)
