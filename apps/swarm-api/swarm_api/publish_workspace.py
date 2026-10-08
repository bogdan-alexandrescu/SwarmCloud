"""Start the personal-workspace job: one Pub/Sub message per dispatch
(docs/workspaces.md §2.1-§2.2; #847, lane W7).

An approval, an admin's retry, a ceiling change and the dispatch sweep each
publish ONE message to `swarm-workspace-apply` (ApiSettings
`workspace_apply_topic`). A Cloud Build trigger of the Pub/Sub kind, subscribed
to that topic and building only `main`, runs `scripts/register-tenant.sh
--workspace` under the call guard (lanes W4, W6). swarm-api holds
`roles/pubsub.publisher` on that one topic and nothing else: it records and
authorises, it never creates an identity or changes IAM.

THE MESSAGE NAMES NOBODY. Its data is exactly

    {"workspace_id": "w-3f9a2c", "mode": "create", "request_id": "<uuid>"}

-- the opaque, random workspace id (§1.1), the mode (`create` or `limits`),
and the record's request id, which the job logs privately so a run can be
matched to the request it serves. Never an email, never the tenant id
(`u-alice` is derived from the email, so it is a name), never a limit, an
image or a command: the job reads everything else from the private record by
workspace id. `message_data` builds it and refuses anything that does not
look like those three values, so a caller cannot widen it by accident.

A FAILED PUBLISH IS NOT A FAILED APPROVAL. The decision is already durable
when this runs; the record stays `approved` and the sweep publishes it again
(§2.2). So `publish` returns False and never raises.

PUBLISHING IS BEHIND A SETTING (`WORKSPACE_APPLY_PUBLISH`, off until the
owner's W4 apply has made the topic). Off, `publisher_for` answers a
`NullWorkspacePublisher`, which publishes nothing and says so (`enabled`
False); the tests inject a recording fake through `app.state`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from .waker import topic_path

log = logging.getLogger(__name__)

MODE_CREATE = "create"
MODE_LIMITS = "limits"
MODES = (MODE_CREATE, MODE_LIMITS)

#: §2.2's validation, the same pattern the build's first step applies.
WORKSPACE_ID = re.compile(r"^w-[0-9a-f]{6}$")
#: `uuid.uuid4()` as text: what `Workspaces.request` and a retry write.
REQUEST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: How long one publish may block the request that made it. The decision is
#: written before this, so a slow Pub/Sub costs the admin a wait, never a
#: lost approval; the sweep covers a timeout.
PUBLISH_TIMEOUT_SECONDS = 5.0


def message_data(workspace_id: str, mode: str, request_id: str) -> dict[str, str]:
    """The message, and nothing else. Raises ValueError on a value that is not
    an opaque workspace id, a known mode or a request id: an email, a tenant
    id or free text can never reach the topic through here."""
    if not WORKSPACE_ID.fullmatch(workspace_id or ""):
        raise ValueError("workspace_id must be w- and six hex digits")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not REQUEST_ID.fullmatch(request_id or ""):
        raise ValueError("request_id must be a UUID")
    return {"workspace_id": workspace_id, "mode": mode, "request_id": request_id}


class WorkspacePublisher(Protocol):
    enabled: bool

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool: ...


class PubSubWorkspacePublisher:
    """Publishes to the topic. Builds no client until the first publish, so
    `create_app()` needs no credentials."""

    enabled = True

    def __init__(self, topic: str, *, project_id: str = "", publisher: Any | None = None) -> None:
        self._topic = topic_path(project_id, topic) if project_id else topic.strip()
        self._publisher = publisher

    @property
    def topic(self) -> str:
        return self._topic

    def _client(self) -> Any:
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool:
        data = json.dumps(message_data(workspace_id, mode, request_id),
                          sort_keys=True).encode("utf-8")
        try:
            # No attributes: the trigger reads the two substitutions from the
            # data, and an attribute is one more place a field could leak.
            future = self._client().publish(self._topic, data)
            future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
            return True
        except Exception as exc:  # noqa: BLE001 - the type only; its text can name the request
            log.warning("workspace apply publish failed workspace=%s mode=%s: %s",
                        workspace_id, mode, type(exc).__name__)
            return False


class NullWorkspacePublisher:
    """Publishing switched off. Publishes nothing and answers False, so the
    record stays `approved` for the sweep once publishing is on."""

    enabled = False

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool:
        message_data(workspace_id, mode, request_id)  # the same refusal as the real one
        return False


def publisher_for(settings: Any) -> WorkspacePublisher:
    """The deployment's publisher: Pub/Sub on `workspace_apply_topic` when
    `workspace_apply_publish` is on, else none."""
    topic = str(getattr(settings, "workspace_apply_topic", "") or "").strip()
    if not getattr(settings, "workspace_apply_publish", False) or not topic:
        return NullWorkspacePublisher()
    return PubSubWorkspacePublisher(topic, project_id=str(getattr(settings, "project_id", "") or ""))
