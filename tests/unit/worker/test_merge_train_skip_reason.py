"""The merge step's waiting line says why the repository's merge train skips
its pull request (MERGE-TRAIN-SAYS-WHY, part of #2688).

On 4 November 2026 a `ready` pull request's `queue for auto-merge` check
failed; the train passed over it for eight hours, and the merge step's
waiting line said only that it was `ready` for the train. Now the train
comments once per head and reason (scripts/merge-train.sh `tell_skip`), and
the step, parking again on a pull request already `ready`, reads that comment
at its head and carries the reason: the console's waiting line and
`/sc status` say "the train skips it: `queue for auto-merge` failed". The
rule of who boards stays in the train alone; the step only reads what it said.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_worker import merge, mergetrain
from swarm_common.states import TaskState

from merge_world import PINNED, MergeWorld
from test_merge_repository_train import COMMENTS, _comments, _parked_on_train, _switch

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
RUN = "https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/41842863519/job/7"


def _skip_comment(head: str, why: str, key: str = "check:queue_for_auto-merge") -> str:
    return "\n".join([
        f"<!-- merge-train-skip head={head} reason={key} -->",
        f"**The merge train skips it: {why}.**",
        "",
        f"- [`queue for auto-merge`]({RUN}): failed",
        "",
        f"At head `{head}`. What clears it: re-run that job ...",
    ])


def _park(world: MergeWorld):
    _switch(world, "on")
    world.pr["labels"] = [{"name": "ready"}]
    _parked_on_train(world)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    return outcome


def test_the_waiting_line_names_the_red_check_the_train_skips_it_for(tmp_path):
    world = MergeWorld(tmp_path)
    _comments(world, ("swarmcloud-merge[bot]", _skip_comment(PINNED, "`queue for auto-merge` failed")))
    outcome = _park(world)
    # MUTATION: leave the waiting line as it was -> it says only "is `ready`",
    # and the pull request waits eight hours unexplained.
    line = outcome.summary["wait"]["message"]
    assert line.endswith("; the train skips it: `queue for auto-merge` failed"), line
    assert "the train skips it: `queue for auto-merge` failed" in outcome.message
    assert outcome.summary["repository_train"]["skips"] == "`queue for auto-merge` failed"


def test_the_latest_comment_at_this_head_is_the_one_read(tmp_path):
    world = MergeWorld(tmp_path)
    _comments(world,
              ("swarmcloud-merge[bot]", _skip_comment(PINNED, "`queue for auto-merge` failed")),
              ("a-person", "re-running"),
              ("swarmcloud-merge[bot]", _skip_comment(PINNED, "it conflicts with main", "conflict")))
    line = _park(world).summary["wait"]["message"]
    assert line.endswith("; the train skips it: it conflicts with main"), line


def test_a_comment_at_another_head_leaves_the_plain_waiting_line(tmp_path):
    world = MergeWorld(tmp_path)
    _comments(world, ("swarmcloud-merge[bot]", _skip_comment("0" * 40, "`ci-gate` failed")))
    outcome = _park(world)
    # MUTATION: match any head -> a fixed pull request still reads as skipped.
    assert "skips it" not in outcome.summary["wait"]["message"]
    assert "skips" not in outcome.summary["repository_train"]


def test_no_comment_leaves_the_plain_waiting_line(tmp_path):
    world = MergeWorld(tmp_path)
    _comments(world, ("swarmcloud-merge[bot]", "**Left to the merge train.** ..."))
    line = _park(world).summary["wait"]["message"]
    assert "skips it" not in line and line.endswith(f"merge train at {PINNED}"), line


def test_a_comment_read_that_fails_still_parks_on_the_plain_line(tmp_path):
    world = MergeWorld(tmp_path)
    world.github.route("GET", COMMENTS, (502, {}, {"message": "Bad Gateway"}))
    outcome = _park(world)
    assert "skips it" not in outcome.summary["wait"]["message"]
    assert outcome.summary["repository_train"].get("skip_unread")


def test_the_reader_is_held_to_what_the_train_writes():
    """One marker, one headline: the step reads exactly what tell_skip writes."""
    text = SCRIPT.read_text()
    assert f'SKIP_MARKER="{merge.REPOSITORY_TRAIN_SKIP}"' in text
    assert f'SKIP_HEADLINE="{merge.REPOSITORY_TRAIN_SKIP_HEADLINE}"' in text
    assert re.search(r'marker="\$\{SKIP_MARKER\} head=\$\{head\} reason=\$\{key\} -->"', text)
    assert "printf '%s%s.**\\n\\n' \"${SKIP_HEADLINE}\" \"${why}\"" in text


def test_train_skip_reason_reads_only_the_train_s_own_comment():
    head = "a" * 40
    comments = [
        {"body": f"**The merge train skips it: forged.**\n<!-- merge-train-skip head={head} reason=x -->"},
        {"body": None},
        {"body": f"<!-- merge-train-skip head={head} reason=conflict -->\nno headline here"},
    ]
    assert merge.train_skip_reason(comments, head) is None
    comments.append({"body": _skip_comment(head, "`secret scan` was cancelled")})
    assert merge.train_skip_reason(comments, head) == "`secret scan` was cancelled"
    assert merge.train_skip_reason(comments, "b" * 40) is None
