"""Tell the scheduler a task the reconciler ended has ended (#636).

The worker rings `task_finished` after its own terminal write
(`agent_worker.finishwake`), and swarm-api rings it for a cancel it ends
itself. A task the RECONCILER ends -- FAILED for a worker that cannot start,
FAILED when a lost worker's requeue finds the attempts spent, CANCELLED for a
cancel the execution ignored past its bound (#627) -- has no worker left to
ring, so its dependants, and the capacity the same repair released, waited
for the scheduler's next safety tick (left by #741).

THE SAME MESSAGE AS THE WORKER'S, ON THE SAME TOPIC. The scheduler's
`/pubsub/push` routes `reason=task_finished` to `release_dependants`, which
re-reads the task and acts only if it has ended. This module is not the
worker's import because the reconciler's image does not carry the worker
package (images/swarm-reconciler/Dockerfile builds common, workflow-rollup
and reconciler only); `tests/unit/reconciler/test_reconciler_finish_wake.py`
holds the two requests equal, field for field, so they cannot drift.

A DOORBELL. A lost wake costs one tick: the scheduler's dependency sweep is
unchanged. So `announce` never raises, and it carries identifiers only.

Publish permission is already granted: the reconciler's account is one of
the wake topic's `publisher_members` (terraform/infra/main.tf). The topic is
read from DISPATCH_TOPIC, the name every other publisher reads; unset, no
wake is rung and the tick releases the dependants as before.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Protocol

from swarm_common.states import TaskState

#: The wake reason `scheduler.main` routes to `release_dependants`.
TASK_FINISHED = "task_finished"

#: Seconds one publish may take. The repair has already committed; this only
#: delays the rest of the pass, and a lost wake costs one tick at most.
PUBLISH_TIMEOUT_SECONDS = 5.0

_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
_PUBSUB_ENDPOINT = "https://pubsub.googleapis.com/v1"


class FinishAnnouncer(Protocol):
    def announce(self, *, task_id: str, tenant_id: str, state: TaskState) -> bool:
        """Publish the wake. True only if Pub/Sub accepted it."""


def topic_path(project_id: str, topic: str) -> str:
    """`projects/<project>/topics/<name>` from a bare name; a full path as given."""
    topic = (topic or "").strip()
    if not topic:
        return ""
    if topic.startswith("projects/"):
        return topic
    return f"projects/{project_id}/topics/{topic}"


class PubSubFinishAnnouncer:
    """Publishes one `task_finished` wake per ended task. Never raises.

    `session` is anything with `post(url, json=..., timeout=...)` returning an
    object with `status_code`, as google-auth's `AuthorizedSession` is; built
    on first use, so constructing this costs nothing and a test hands in a fake.
    """

    def __init__(self, topic: str, *, session: Any | None = None) -> None:
        self.topic = topic
        self._session = session

    @classmethod
    def for_topic(cls, project_id: str, topic: str | None) -> "PubSubFinishAnnouncer | None":
        path = topic_path(project_id, topic or "")
        return cls(path) if path else None

    @classmethod
    def from_env(cls) -> "PubSubFinishAnnouncer | None":
        return cls.for_topic(
            os.environ.get("PROJECT_ID", ""), os.environ.get("DISPATCH_TOPIC", "")
        )

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
