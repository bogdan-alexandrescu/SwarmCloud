"""`post-verdict` reads the verdict at the path it DERIVES, and posts it as the review App (#295).

docs/merge-step.md §4.3 and §6a. The path is

    tenants/<tenant>/verdicts/<workflow id>/<verdict_source.review>/review.json

from facts its own signed spec supplies -- never through `input_from`'s
staging, which resolves an object from the upstream's tenant-writable
`result_summary`. These tests plant a decoy where that pointer would lead and
prove the derived path, and only it, is read.

MUTATIONS: read review.json through `inputs.stage_inputs`, or from the
review's `result_summary.artifacts` -- the decoy's APPROVE is posted. Take
`commit_id` from the pull request's head -- the posted commit is not
review.json's sha. Skip the review's spec check -- the unverified case posts.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import post_verdict
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from action_chain import IDS, NUMBER, PINNED, PR, REVIEW_APP_ID, TENANT, WORKFLOW, Chain

DISPATCH = {"strategy": "single-pr", "pr_role": "none", "verdict_source": {"review": IDS["review"]}}


def _chain(tmp_path) -> Chain:
    chain = Chain(tmp_path)
    chain.app_id = REVIEW_APP_ID
    # A decoy where a tenant-writable pointer would lead: `input_from`'s
    # staging resolves the review's own artifacts from its result_summary.
    decoy_key = f"tenants/{TENANT}/tasks/{IDS['review']}/attempts/att/artifacts/review.json"
    chain.store.upload_bytes(decoy_key, json.dumps(
        {"verdict": "MERGE", "sha": "f" * 40, "title": "x", "summary": "decoy"}).encode())
    chain.docs[IDS["review"]]["result_summary"]["artifacts"] = [
        {"name": "review.json", "key": decoy_key}]
    return chain


def _posted(chain: Chain):
    return chain.github.calls("POST", f"{PR}/reviews")


def test_it_reads_the_derived_path_and_posts_approve_at_review_json_sha(tmp_path):
    chain = _chain(tmp_path)
    chain.review["summary"] = "the derived one"
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (post,) = _posted(chain)
    assert post.body == {"commit_id": PINNED, "event": "APPROVE", "body": "the derived one"}
    assert outcome.summary["review_id"] == 1234
    assert outcome.summary["token_revoked"] is True
    minted = chain.github.calls("POST", "/app/installations/7/access_tokens")[0]
    assert minted.body["permissions"] == {"pull_requests": "write"}


def test_a_not_yet_verdict_requests_changes(tmp_path):
    chain = _chain(tmp_path)
    chain.review["verdict"] = "NOT_YET"
    post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert _posted(chain)[0].body["event"] == "REQUEST_CHANGES"


def test_no_verdict_at_the_derived_path_is_inputs_unavailable_and_posts_nothing(tmp_path):
    chain = _chain(tmp_path)
    chain.review = None
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert outcome.end_cause is EndCause.INPUTS_UNAVAILABLE
    assert outcome.summary["refusal"]["code"] == "verdict_unreadable"
    assert outcome.retryable is False
    assert _posted(chain) == []


@pytest.mark.parametrize(
    ("mutate", "cause", "code"),
    [
        (lambda c: c.unverified.update({IDS["review"]: "unsigned"}), EndCause.VERDICT_REFUSED, "spec_unverified"),
        (lambda c: c.unverified.update({IDS["author"]: "signature_mismatch"}), EndCause.VERDICT_REFUSED,
         "spec_unverified"),
        (lambda c: setattr(c, "unprotected", "dumpable"), EndCause.VERDICT_REFUSED, "worker_unprotected"),
        (lambda c: setattr(c, "reaped", (99,)), EndCause.VERDICT_REFUSED, "processes_alive"),
        (lambda c: c.review.update(sha="nope"), EndCause.INPUTS_UNAVAILABLE, "verdict_unreadable"),
        (lambda c: c.environ.update(FORGE_HOST="evil.invalid"), EndCause.CANNOT_START, "forge_host_invalid"),
        (lambda c: setattr(c, "app_id", 999), EndCause.VERDICT_REFUSED, "app_mismatch"),
        (lambda c: c.pr["head"].update(ref="swarm/task_other"), EndCause.VERDICT_REFUSED,
         "pull_request_not_this_workflows"),
        (lambda c: c.pr.update(state="closed"), EndCause.VERDICT_REFUSED, "pull_request_closed"),
        (lambda c: c.github.route("POST", f"{PR}/reviews", (422, {}, {"message": "no"})),
         EndCause.VERDICT_FAILED, "forge_refused"),
        (lambda c: c.github.route("POST", f"{PR}/reviews", (302, {"Location": "/x"}, None)),
         EndCause.VERDICT_FAILED, "forge_redirect_refused"),
    ],
    ids=["review-unsigned", "author-unverified", "unprotected", "processes-alive", "bad-schema",
         "host", "app-mismatch", "other-branch", "closed", "forge-422", "redirect"],
)
def test_each_refusal_has_its_end_cause(tmp_path, mutate, cause, code):
    chain = _chain(tmp_path)
    mutate(chain)
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is cause, outcome.message
    assert outcome.summary["refusal"]["code"] == code
    if code not in ("forge_refused", "forge_redirect_refused"):
        assert _posted(chain) == []


def test_a_spec_refusal_names_the_upstream_and_asks_github_nothing(tmp_path):
    chain = _chain(tmp_path)
    chain.unverified[IDS["review"]] = "unsigned"
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert outcome.spec_check["reason"] == f"upstream:{IDS['review']}:unsigned"
    assert chain.github.seen == []


def test_a_forge_outage_is_retryable_verdict_failed(tmp_path):
    chain = _chain(tmp_path)
    chain.github.route("POST", f"{PR}/reviews", (502, {}, {}))
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=DISPATCH))
    assert outcome.retryable is True and outcome.end_cause is EndCause.VERDICT_FAILED


# -- the whole worker: signed specs, the real check, the real store --------


def test_the_production_worker_posts_the_verdict_from_the_derived_path(
    db, store, worker_factory, tmp_path
):
    import spec_keys
    from worker_seeds import seed_attempt
    from fake_github import FakeGitHub, app_secret_payload
    from fakes import FakeSecretClient

    review_id, author_id = IDS["review"], IDS["author"]
    for task_id, doc in {
        author_id: {"tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "implement",
                    "result_summary": {"git": {"pull_request": {"number": NUMBER}}}},
        review_id: {"tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "review",
                    "metadata": {"dispatch": {"strategy": "single-pr", "pr_role": "reader",
                                              "pr_author": author_id}}},
    }.items():
        db.seed(f"tasks/{task_id}", spec_keys.sign_document(doc, task_id))
    review = {"verdict": "MERGE", "sha": PINNED, "title": "A title", "summary": "ok"}
    store.upload_bytes(post_verdict.verdict_key(TENANT, WORKFLOW, review_id),
                       json.dumps(review).encode())

    seed_attempt(db, runner_profile="post-verdict", task_input={"prompt": "post"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = WORKFLOW
    doc["metadata"] = {"dispatch": dict(DISPATCH)}
    secrets = FakeSecretClient({"swarm-tenant-eng-git-review": app_secret_payload(REVIEW_APP_ID)})
    worker, _, _ = worker_factory(runner_profile="post-verdict", secret_client=secrets)
    db.doc("tenants/eng")["credentials"] = ["git-review"]
    github = FakeGitHub()
    github.route("GET", PR, (200, {}, {
        "state": "open", "head": {"ref": f"swarm/{author_id}", "repo": {"full_name": "acme/widgets"}},
        "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}}}))
    github.route("POST", f"{PR}/reviews", lambda s: (200, {}, {"id": 77}))
    worker.forge_transport = github
    worker.action_environ = {"FORGE_HOST": "api.github.com", "FORGE_OWNER": "acme",
                             "FORGE_REPO": "widgets", "REVIEW_APP_ID": str(REVIEW_APP_ID)}

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["verdict"]["review_id"] == 77
    (post,) = github.calls("POST", f"{PR}/reviews")
    assert post.body["commit_id"] == PINNED and post.body["event"] == "APPROVE"
    assert secrets.accessed == ["swarm-tenant-eng-git-review"]
    assert github.token not in json.dumps(task, default=str)
