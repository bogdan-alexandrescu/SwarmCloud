"""swarm-api's submission wake is published to a topic PATH, not a bare name (#636).

terraform renders DISPATCH_TOPIC as the bare topic name
(terraform/infra/locals.tf `wake_topic`), and Pub/Sub's publish accepts only
`projects/<project>/topics/<name>`. Handed the bare name, every submission
wake was refused, counted in `swarm_api_scheduler_wake_failures_total` and
otherwise silent: the queue still drained, on the one-minute safety tick --
the ~60 s ready -> lease shape #636 measured.
"""

from __future__ import annotations

from typing import Any

from swarm_api.waker import PubSubWaker, topic_path


class _Done:
    def result(self, timeout: float | None = None) -> str:
        return "1"


class RecordingPublisher:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def publish(self, topic: str, data: bytes, **attributes: Any) -> _Done:
        self.topics.append(topic)
        return _Done()


def test_a_bare_topic_name_is_published_as_a_full_path():
    publisher = RecordingPublisher()
    waker = PubSubWaker("swarm-scheduler-wake", project_id="p1", publisher=publisher)

    assert waker.wake("task_submitted", tenant_id="eng") is True
    assert publisher.topics == ["projects/p1/topics/swarm-scheduler-wake"]


def test_a_full_topic_path_is_left_alone():
    publisher = RecordingPublisher()
    waker = PubSubWaker("projects/p2/topics/t", project_id="p1", publisher=publisher)
    waker.wake("task_submitted")
    assert publisher.topics == ["projects/p2/topics/t"]


def test_topic_path():
    assert topic_path("p1", " wake ") == "projects/p1/topics/wake"
    assert topic_path("p1", "") == ""


def test_the_deployment_waker_is_built_with_the_project():
    """`for_settings` is what deps.py builds the waker with."""
    from types import SimpleNamespace

    from swarm_api.waker import NullWaker, waker_for

    built = waker_for(SimpleNamespace(dispatch_topic="swarm-scheduler-wake", project_id="p1"))
    assert isinstance(built, PubSubWaker)
    assert built.topic == "projects/p1/topics/swarm-scheduler-wake"
    assert isinstance(waker_for(SimpleNamespace(dispatch_topic="", project_id="p1")), NullWaker)
