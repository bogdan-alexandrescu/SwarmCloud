"""`collect` promises nothing is pushed. This is where that promise is kept.

swarm-api accepts `strategy` on submit and returns it in the 201. `collect` is
the default every caller gets, and it states that patches are harvested and
nothing is pushed.

Until this check existed that guarantee held only because the tenant's token
happened to lack write scope. Granting write scope -- a decision about
credentials, taken somewhere else entirely -- would have silently turned every
`collect` dispatch into one that opens pull requests, while its own API
response promised it would not. A guarantee enforced by an unrelated accident
is not a guarantee.
"""

from __future__ import annotations

import pytest


def _worker(worker_factory, strategy=None):
    worker, config, _ = worker_factory()
    task: dict = {"task_id": config.task_id}
    if strategy is not None:
        task["metadata"] = {"dispatch": {"strategy": strategy}}
    worker._task = task
    return worker


def test_collect_is_the_default_when_nothing_was_asked_for(worker_factory):
    assert _worker(worker_factory)._dispatch_strategy() == "collect"


@pytest.mark.parametrize("strategy", ["collect", "direct-pr", "integrate"])
def test_a_known_strategy_is_read_back(worker_factory, strategy):
    assert _worker(worker_factory, strategy)._dispatch_strategy() == strategy


@pytest.mark.parametrize(
    "value",
    ["push-everything", "", "DIRECT_PR", "collect;direct-pr", None, 7, [], {}],
)
def test_an_unrecognised_strategy_falls_back_to_collect(worker_factory, value):
    """A newer control plane and an older worker disagreeing is the case this
    covers, and in that disagreement the safe reading is the one that pushes
    nothing. A worker guessing "probably direct-pr" would open pull requests
    nobody asked for."""
    assert _worker(worker_factory, value)._dispatch_strategy() == "collect"


def test_malformed_metadata_does_not_crash_the_attempt(worker_factory):
    """An attempt that ran is not a failed attempt because its metadata was
    shaped oddly, and this runs on the teardown path."""
    worker, config, _ = worker_factory()
    for metadata in (None, "a string", [], {"dispatch": "not a dict"}, {"dispatch": {}}):
        worker._task = {"task_id": config.task_id, "metadata": metadata}
        assert worker._dispatch_strategy() == "collect"


def test_collect_refuses_to_publish_and_says_why(worker_factory, tmp_path):
    """The behaviour the promise names. Not 'did not happen to push' -- refused,
    with a reason a caller can act on."""
    from agent_worker import workspace as workspace_mod

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": config.task_id, "metadata": {"dispatch": {"strategy": "collect"}}}
    worker._repo_url = "https://github.com/acme/widgets.git"

    result = worker._publish_git(repo=tmp_path, work_head="abc123", publish=True)

    assert result["published"] is False
    assert result["strategy"] == "collect"
    assert "nothing is pushed" in result["publish_reason"]
    assert "direct-pr" in result["publish_reason"], (
        "a refusal should name the strategy that would have done what the "
        "caller wanted"
    )


def test_collect_is_checked_before_the_forge_is_ever_contacted(worker_factory, tmp_path, monkeypatch):
    """A network call to decide whether we are allowed to make a network call.

    The forge probe is one HTTPS request per terminal attempt. Under `collect`
    the answer cannot change the outcome, so making it would be spending a
    request -- and leaking which repositories this platform touches -- to learn
    something irrelevant.
    """
    from agent_worker import lifecycle, workspace as workspace_mod

    def boom(**kwargs):
        raise AssertionError("the forge was contacted for a collect dispatch")

    monkeypatch.setattr(lifecycle, "probe_repository", boom)

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": config.task_id, "metadata": {"dispatch": {"strategy": "collect"}}}
    worker._repo_url = "https://github.com/acme/widgets.git"

    worker._publish_git(repo=tmp_path, work_head="abc123", publish=True)
