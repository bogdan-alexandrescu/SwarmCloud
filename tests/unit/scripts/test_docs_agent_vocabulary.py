"""The docs call the running unit an agent, not a task.

OWNER DECISION, 2026-10-20 (#1680, lane VOCAB-AGENTS-DOCS). "Agent" names the
running unit everywhere; "task" stays only in identifiers: `task_...` ids, API
routes (`/v1/tasks`, `POST /v1/tasks`), Firestore collection and field names,
Python and TypeScript symbols, CLI commands and their flags, profile names. At
about 15:45Z the owner extended the rule from the console to docs/. A reader
who meets "task" in one paragraph and "agent" in the next has to work out that
they are the same thing; the API's own nouns are the one place the old word
has to survive, because they are what a caller types.

WHAT IS SCANNED. Every docs/**/*.md and README.md, prose only. Removed first:
fenced code blocks, inline code, link targets and reference definitions, bare
URLs, HTML tags and comments, and quoted text ("...", curly quotes, and short
'...' labels) -- an owner's words, a measured log line or a UI string quoted
as it was are history and stay as written. A word joined to `/ . : _ # = < >
{ } $ @` is an identifier (`/v1/tasks`, `tasks.py`, `task:read`) and stays;
an ALL-CAPS word joined by `-` or `_` is a lane or variable name
(`TASK-ANSWERS`) and stays.

WHAT IS KEPT ON PURPOSE (API_OBJECT, OTHER_PRODUCT, KEPT_FILES):
  * "task" followed by a word that makes it the API object -- task id,
    document, collection, index, record, model, route, spec, body, metadata,
    store, query ... ("the agent's task document" is the reading the brief
    asks for);
  * Google's and other products' own "task": a Cloud Run task, a Job's task;
  * docs/contract-change-requests.md history rows (its `|` index table and the
    `##` headings each row mirrors): records of what was requested, frozen as
    written;
  * the runner-input tables between `<!-- runner-inputs:<profile> -->`
    markers: generated from swarm_common.profiles.RUNNER_PROFILES (frozen) and
    held equal to it by tests/unit/mcp/test_runner_input_prose.py;
  * two blockquotes that quote a source comment and a measured screen word
    for word (KEPT_LINES);
  * docs/child-tasks-for-agents.md: a verbatim copy of
    agent_worker.children.AGENT_GUIDE, held equal to it by
    tests/unit/worker/test_child_tasks_worker.py. The worker's string is the
    source; the copy follows it, never the other way.
CONTRACT.md is not under docs/ and is not scanned: its invariant wording is
frozen-contract text.

BREAK IT: write "a task is admitted" in any doc's prose. This test names the
file and line. The planted-text tests below prove the scanner finds prose and
skips each kind of identifier, so a scanner that silently skips everything
cannot pass.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
README = REPO / "README.md"

CONTRACT_REQUESTS = "docs/contract-change-requests.md"
KEPT_FILES = {
    # verbatim copy of agent_worker.children.AGENT_GUIDE (see the docstring)
    "docs/child-tasks-for-agents.md",
}
#: Blockquotes that quote a source file or a measured screen word for word.
#: A blockquote is not masked the way "..." is, because most of them here are
#: notes, not quotations; these are the quotations.
KEPT_LINES = {
    # apps/swarm-ui/src/types.ts:372 as it read when the audit quoted it
    ("docs/web-ui/ui-audit-and-build-prompt.md", "> NOT on the task: `current_generation`."),
    # the Overview panel's text, read 2026-09-22
    ("docs/web-ui/ui-audit-and-build-prompt.md", "> Nothing is running — No task on the 7 most recently created"),
}

WORD = re.compile(r"\b(?:tasks|task|Tasks|Task|TASKS|TASK)\b")
FENCE = re.compile(r"^\s*(?:```|~~~)")
API_OBJECT = re.compile(
    r"^(?:s|S)?(?:'s)?\s+(?:ids?|IDs?|documents?|docs?|collections?|index|indexes|indices|items?|"
    r"records?|model|schema|routes?|API|api|objects?|fields?|payload|list route|listings?|"
    r"endpoints?|dataclass|dict|body|bodies|keys?|store|specs?|metadata|URLs?|url|lookup|"
    r"vocabulary|query|queries|methods?|class)\b"
    r"|^-(?:doc|docs|document|documents|id|ids|level field)\b"
)
OTHER_PRODUCT = re.compile(
    r"(?:Cloud Run|Cloud|Job's|job's|Job|execution's|Kubernetes|asyncio|background|Celery)\s+$"
)
JOINERS = set("/.:_#=<>{}$@\\")


def _masked(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat, flags in (
        (r"(`+)(?:(?!\1).)+?\1", re.S),
        (r"<!--.*?-->", re.S),
        # the runner-input tables generated from the frozen RUNNER_PROFILES
        (r"<!-- runner-inputs:([a-z0-9-]+) .*?<!-- /runner-inputs:\1 -->", re.S),
        (r"\]\([^)]*\)", 0),
        (r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M),
        (r"https?://\S+", 0),
        (r"</?[a-zA-Z][^>]*>", 0),
    ):
        spans += [(m.start(), m.end()) for m in re.finditer(pat, text, flags)]
    quoted = []
    for pat in (r'"[^"]*"', r"“[^”]*”", r"(?<![\w'’])'[^'\n]{1,60}'(?![\w])"):
        for m in re.finditer(pat, text, re.S):
            if not any(a <= m.start() < b for a, b in spans):
                quoted.append((m.start(), m.end()))
    return spans + quoted


def _is_identifier(text: str, s: int, e: int) -> bool:
    a = s
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    b = e
    while b < len(text) and not text[b].isspace():
        b += 1
    before, after = text[a:s], text[e:b]
    core = text[a:b].strip("()[]*,;!?\"'“”‘’`")
    if before and before[-1] in JOINERS:
        return True
    if len(after) > 1 and after[0] in JOINERS and (after[1].isalnum() or after[1] in "_{"):
        return True
    if before == "-" or before.startswith("--"):
        return True
    if any(c in core for c in "/_=<>{}$@\\"):
        return True
    return bool(re.search(r"\w[.:]\w", core))


def prose_tasks(text: str, *, history_rows: bool = False) -> list[tuple[int, str]]:
    """(line number, line) of every prose 'task' that names the running unit."""
    found: list[tuple[int, str]] = []
    lines = text.split("\n")
    block: list[tuple[int, str]] = []
    infence = False

    def scan(chunk: list[tuple[int, str]]) -> None:
        if not chunk:
            return
        body = "\n".join(line for _, line in chunk)
        starts = []
        pos = 0
        for _, line in chunk:
            starts.append(pos)
            pos += len(line) + 1
        spans = _masked(body)
        for m in WORD.finditer(body):
            s, e, w = m.start(), m.end(), m.group(0)
            idx = max(i for i, st in enumerate(starts) if st <= s)
            lineno, line = chunk[idx]
            if any(a <= s < b for a, b in spans):
                continue
            if w.isupper() and ((s and body[s - 1] in "-_") or (e < len(body) and body[e] in "-_")):
                continue
            if history_rows and (line.startswith("#") or line.startswith("|")):
                continue
            if _is_identifier(body, s, e):
                continue
            if API_OBJECT.match(body[e:]) or OTHER_PRODUCT.search(body[max(0, s - 30): s]):
                continue
            found.append((lineno, line.strip()))

    for n, line in enumerate(lines, 1):
        if FENCE.match(line):
            scan(block)
            block = []
            infence = not infence
            continue
        if infence:
            continue
        if not line.strip() or line.startswith("#"):
            scan(block)
            block = []
            if line.startswith("#"):
                scan([(n, line)])
            continue
        block.append((n, line))
    scan(block)
    return found


def _docs() -> list[Path]:
    return sorted(DOCS.rglob("*.md")) + [README]


def test_no_doc_calls_the_running_unit_a_task():
    offenders = []
    visited = 0
    for path in _docs():
        rel = path.relative_to(REPO).as_posix()
        if rel in KEPT_FILES:
            continue
        visited += 1
        for lineno, line in prose_tasks(
            path.read_text(encoding="utf-8"), history_rows=rel == CONTRACT_REQUESTS
        ):
            if any(rel == f and line.startswith(kept) for f, kept in KEPT_LINES):
                continue
            offenders.append(f"{rel}:{lineno}: {line[:140]}")
    # MUTATION: a doc whose glob stops matching (DOCS pointed elsewhere) would
    # pass on zero files; the count says the scan reached the tree.
    assert visited > 100, f"scanned only {visited} docs"
    # MUTATION: write "a task is admitted" in any doc's prose -> red, naming the line.
    assert not offenders, (
        "prose calls the running unit a task; say agent (identifiers, routes and the "
        "API object keep task):\n" + "\n".join(offenders[:200])
        + (f"\n... and {len(offenders) - 200} more" if len(offenders) > 200 else "")
    )


def test_the_scanner_finds_prose_tasks():
    text = "An agent is admitted.\n\nA task is admitted, and every Task\nends; tasks park.\n"
    # MUTATION: a WORD pattern that misses a case (drop `Task` or `tasks`) -> red.
    assert [n for n, _ in prose_tasks(text)] == [3, 3, 4]


def test_the_scanner_finds_a_task_in_a_heading_and_a_table():
    text = "# How a task ends\n\n| state | what the task holds |\n|---|---|\n"
    # MUTATION: skip headings or `|` rows outside the history file -> red.
    assert [n for n, _ in prose_tasks(text)] == [1, 3]


def test_the_scanner_keeps_identifiers_routes_and_code():
    task_id = "task_" + "0" * 20
    text = (
        f"The `task_id` {task_id} is read from `GET /v1/tasks/{{id}}` or /v1/tasks.\n"
        "See [the guide](child-tasks.md#a-task) and tasks.py, scope task:read.\n"
        "Lane TASK-ANSWERS ran `swarm task list --task x`.\n"
        "\n```bash\nswarm task cancel  # a task\n```\n"
    )
    # MUTATION: drop the inline-code, link-target, joiner or fence rule -> red.
    assert prose_tasks(text) == []


def test_the_scanner_keeps_the_api_object_and_other_products_task():
    text = (
        "The agent's task document records the task id; the tasks collection is indexed.\n"
        "A Cloud Run task is read through Cloud Logging, and the Job's task too.\n"
    )
    # MUTATION: drop API_OBJECT or OTHER_PRODUCT -> red.
    assert prose_tasks(text) == []


def test_the_scanner_keeps_quoted_text():
    text = 'The owner said "a task must never sleep" and the button read “Tasks”.\n'
    # MUTATION: drop the quote masks -> red.
    assert prose_tasks(text) == []


def test_contract_request_history_rows_keep_their_wording():
    text = (REPO / CONTRACT_REQUESTS).read_text(encoding="utf-8")
    # The index row is history: it is kept exactly as filed.
    # MUTATION: convert the history rows too (row 23 -> "an agent's end") -> red.
    assert "| 23 | `models.py`: a task's end has no typed cause," in text
    # MUTATION: run the scan without history_rows -> the rows are reported -> red.
    assert prose_tasks("| 23 | a task's end |\n", history_rows=False)
    assert prose_tasks("| 23 | a task's end |\n", history_rows=True) == []


def test_the_readme_status_names_agents():
    raw = README.read_text(encoding="utf-8")
    text = " ".join(re.sub(r"^[ \t]*>[ \t]?", "", raw, flags=re.M).split())
    # MUTATION: restore "Agents run end to end: a task is" in README.md -> red.
    assert "Agents run end to end: an agent is authenticated" in text


def test_the_agent_guide_copy_is_kept_verbatim():
    guide = (DOCS / "child-tasks-for-agents.md").read_text(encoding="utf-8")
    # MUTATION: convert the guide copy without changing AGENT_GUIDE -> red here
    # and in test_child_tasks_worker.py.
    assert "This task may submit CHILD TASKS" in guide


def test_the_kept_quotations_are_still_there():
    # MUTATION: convert a KEPT_LINES quotation (or delete it) -> its exemption
    # is dead and this names it, so the list cannot rot into a blanket pass.
    for rel, kept in KEPT_LINES:
        lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
        assert any(line.startswith(kept) for line in lines), (rel, kept)


def test_the_scanner_keeps_a_generated_runner_inputs_table():
    text = (
        "<!-- runner-inputs:codex generated from RUNNER_PROFILES -->\n"
        "| `issue` | integer | written to the task's workspace |\n"
        "<!-- /runner-inputs:codex -->\n"
    )
    # MUTATION: drop the runner-inputs mask -> red (and the generated table
    # could only be "fixed" by breaking test_runner_input_prose.py).
    assert prose_tasks(text) == []
