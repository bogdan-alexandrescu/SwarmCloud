"""Tell the scheduler a task has ended, so its dependants run now (#636).

Measured 2026-10-05 (history report §2.4): parent done -> child READY p50
27 s, p90 54 s, max 76 s. The scheduler releases a dependant in its dependency
sweep, and only a drain runs that sweep; nothing woke a drain when a task
ended, so every hand-off waited for the one-minute safety tick.

The worker now publishes a `task_finished` wake on the scheduler's EXISTING
wake topic (DISPATCH_TOPIC, the one swarm-api publishes submissions on), once
its terminal write and its lease release are done. The scheduler's
`/pubsub/push` answers it with `Scheduler.release_dependants`.

WHY THE WORKER, AND NOT A FIRESTORE TRIGGER. The wake topic, its push
subscription, its OIDC check, its retry policy and its dead-letter topic
already exist and are already the platform's one way of waking the
scheduler. A Firestore trigger would add Eventarc to a shared project, fire on
every write to a task document -- heartbeats and checkpoints included -- and
carry the whole document, input and all, to decide something the worker
already knows at the one moment it matters.

A DOORBELL, NOT A MESSAGE THAT MUST ARRIVE. A wake that is lost costs what it
cost before #636: the dependant waits for the safety tick, whose dependency
sweep is unchanged. So `announce` never raises, and a failure is logged by its
caller and nothing else. It carries identifiers only -- the task, its tenant,
the state it ended in -- because the scheduler re-reads the task anyway, and
anything else in a message is something else that could leak.

The request is Pub/Sub's REST `publish`, through google-auth's authorized
session: the worker already depends on google-auth (through the Firestore
client), and a second gRPC client library in every worker image for one
message per attempt is not worth its weight. Imported lazily, as every GCP
client in this package is.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol

from swarm_common.states import TaskState

#: The wake reason `scheduler.main` routes to `release_dependants`.
TASK_FINISHED = "task_finished"

#: Seconds one publish may take. The attempt has already ended; this only
#: delays the container's exit, and a lost wake costs one tick at most.
PUBLISH_TIMEOUT_SECONDS = 5.0

_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
_PUBSUB_ENDPOINT = "https://pubsub.googleapis.com/v1"


class FinishAnnouncer(Protocol):
    def announce(self, *, task_id: str, tenant_id: str, state: TaskState) -> bool:
        """Publish the wake. True only if Pub/Sub accepted it."""


def topic_path(project_id: str, topic: str) -> str:
    """`projects/<project>/topics/<name>` from a bare name; a full path as given.

    DISPATCH_TOPIC is rendered as a bare name (terraform/infra/locals.tf
    `wake_topic`), and Pub/Sub's publish takes only the full path.
    """
    topic = (topic or "").strip()
    if not topic:
        return ""
    if topic.startswith("projects/"):
        return topic
    return f"projects/{project_id}/topics/{topic}"


class PubSubFinishAnnouncer:
    """Publishes one `task_finished` wake per ended task. Never raises.

    `session` is anything with `post(url, json=..., timeout=...)` returning
    an object with `status_code`, the way `google.auth.transport.requests.
    AuthorizedSession` has it; built on first use so constructing this costs
    nothing and a test hands in a fake.
    """

    def __init__(self, topic: str, *, session: Any | None = None) -> None:
        self.topic = topic
        self._session = session

    @classmethod
    def for_topic(cls, project_id: str, topic: str | None) -> "PubSubFinishAnnouncer | None":
        path = topic_path(project_id, topic or "")
        return cls(path) if path else None

    def _client(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[_PUBSUB_SCOPE])
            self._session = AuthorizedSession(credentials)
        return self._session

    def announce(self, *, task_id: str, tenant_id: str, state: TaskState) -> bool:
        fields = {
            "reason": TASK_FINISHED,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "state": state.value,
        }
        body = {
            "messages": [
                {
                    "data": base64.b64encode(json.dumps(fields).encode("utf-8")).decode("ascii"),
                    "attributes": fields,
                }
            ]
        }
        try:
            response = self._client().post(
                f"{_PUBSUB_ENDPOINT}/{self.topic}:publish",
                json=body,
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:
            return False
        return 200 <= int(getattr(response, "status_code", 0)) < 300
