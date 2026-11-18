"""The checklist each task type's CONTEXT.md ends its header with (§4.5).

Every item comes from a class of §3's taxonomy, so a checklist is the misses
that measurably happened, not general advice:

  G  a guard or house rule not met          T  a covering test not run
  P  a parallel lane it could not see       X  a caller or callee contract
  D  a doc restating the changed fact       W  a path it cannot push
  TA a test that cannot fail                L  the same latent defect fixed twice

The agent answers it in `$SWARM_ARTIFACTS_DIR/self-check.md`, one line per
item, done or not done, with the evidence. The pre-publish gate (lane CTXQ-G)
only checks the file exists and appends it to the pull request body: the
self-check is advisory, and no merge path reads it.
"""

from __future__ import annotations

SELF_CHECK_FILE = "self-check.md"

_IMPLEMENT = (
    "Run every test the pack lists for what you changed, and the guards, before you finish (T, G).",
    "Obey every guard and house rule listed; read the rule's section before editing the file it names (G).",
    "Update every doc row that restates a fact you changed, in the same change (D).",
    "Update or check every caller listed for a symbol whose behaviour or signature you changed (X).",
    "Never edit a cannot-push path; if the work needs one, stop and say so in the PR body (W).",
    "On an in-flight overlap, stop and say which lane holds the file before you edit it (P).",
)

CHECKLISTS: dict[str, tuple[str, ...]] = {
    "implement": _IMPLEMENT,
    "review": (
        "Answer one row per requirement: met or not, with the file and line that shows it.",
        "Name every caller listed outside the diff that the change should have updated and did not (X).",
        "Name every covering test outside the diff that the change could break, and whether it was run (T).",
        "Say for each new test whether it fails without the change; a test that cannot fail is a finding (TA).",
        "Compare the change with the sibling idiom listed; a divergence with no reason is a finding.",
        "Check the change touches no cannot-push path and no file a listed in-flight lane holds (W, P).",
    ),
    "fix": (
        "Read each similar past change listed first: if an earlier fix of the same test or fixture exists, quote it and reuse it (L).",
        "Fix the cause in the code the red test exercises, not the assertion, unless the test is wrong; say which.",
        "Run the red tests and every test listed for the code you changed before finishing (T).",
        "Change nothing outside the finding or the failing check; obey every guard listed (G).",
    ),
    "test": (
        "Each new test must fail without the change: break the code under test, watch it fail, and say how that was checked (TA).",
        "Follow the sibling test files' layout and fixtures listed; reuse a fixture before writing a new one.",
        "No wall-clock or date literals in fixtures; build time-dependent values from a fixed clock (L).",
        "Build any fake credential at runtime, never as one literal; obey the test-side guards listed (G).",
        "Run the new tests and the existing tests listed for the same code before finishing (T).",
    ),
    "design": (
        "Write each open choice as an owner decision: options, a recommendation, and the constraint behind it.",
        "Cite code by symbol (path and name), never by line number, so a citation survives the code moving.",
        "Name every change the design needs in a frozen contract as a request, never as an edit.",
        "Reconcile with the design docs and rules listed: say which decision this extends, replaces or contradicts (D).",
        "Name the lanes, their territory and the tests each must prove; no two parallel lanes share a file (P).",
    ),
    "plan": (
        "Map every requirement of the issue to a step; a requirement no step delivers is said, not dropped.",
        "For every in-flight overlap listed, say none or required, with the reason; a duplicate of a running lane is NOT_READY, kind duplicate (P).",
        "Give each step the files and the tests it will touch, from the pack's callers and tests, so territory checks can see it (X, T).",
        "Keep parallel steps on disjoint files; a step that needs a cannot-push path is said in the plan (W).",
        "Read the similar past changes listed: a step that repeats one says what is different this time (L).",
    ),
}


def checklist(task_type: str) -> tuple[str, ...]:
    return CHECKLISTS.get(task_type, _IMPLEMENT)
