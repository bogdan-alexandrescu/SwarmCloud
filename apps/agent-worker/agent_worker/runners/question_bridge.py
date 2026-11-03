"""The question bridge (spike, behind a switch that is off): a headless claude-code
agent's question to a person is captured, the turn ends, and the same session
resumes later with the answer as its next message.

Part of the agent plugins plan (docs/design/agent-plugins.md, owner's decisions
of 3 November 2026): in unattended mode gstack answers the questions that carry
a recommendation itself, and any question that reaches the question tool parks
the step until a person answers; in planner and designer modes every interview
question parks. The findings this module is built on, the exact flags and paths
and what the trusted state package must provide are in
docs/design/agent-plugins-question-bridge.md. In short, measured against the
pinned CLI (2.1.283) in print mode:

  * The question tool, `AskUserQuestion`, is NOT offered in plain `--print`
    mode, even when `--tools` names it. `--permission-prompt-tool stdio` offers
    it (with `EnterPlanMode` and `ExitPlanMode`, which `cli_args` withholds).
  * A PreToolUse hook matching `AskUserQuestion` receives the session id, the
    transcript path, the tool use id and the questions with their options and
    previews.
  * A hook answer of `"continue": false` ends the turn with no further model
    request: the result event says `terminal_reason: "hook_stopped"`, exit 0.
    A bare deny would hand the model an error and another turn instead.
  * `claude --print --resume <session>` continues the session in a new process
    once `~/.claude/projects/<slug>/<session>.jsonl` is restored, whatever the
    working directory is now.

TWO ROLES, ONE FILE. The worker side imports `enabled`, `cli_args`,
`hook_entry`, `classify`, `session_files`, `answer_message` and
`resume_args`. The hook side is this file run as `python -I <this file>
<pending path>` from the runner's `--settings`: standard library only and
nothing imported from the worker, because `-I` drops the package's path and a
hook must not depend on it (as `stdin_hook.py`).

STRICT, NEVER ADVISORY. Whatever the hook reads, it stops the turn: a question
it cannot record is never let through to the CLI's own prompt (which, with no
host on stdin, fails with "Stream closed" and lets the model carry on as if
asked and unanswered). `classify` then reports `bridge_failed`, never a normal
end, so missing evidence of the question is not read as a run that finished.

Nothing here parks a step: the worker lifecycle is not wired to it (the spike's
brief). `claude_code` registers the hook and the flags only while
`ENABLE_ENV` is "1".
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: The runner switch. Off unless exactly "1"; nothing in the platform sets it.
ENABLE_ENV = "SWARM_QUESTION_BRIDGE"

#: Claude Code's question tool (2.1.283).
QUESTION_TOOL = "AskUserQuestion"

#: The flag that makes print mode offer the question tool: the CLI then asks
#: its "host" for any decision a hook leaves open. The hook below always
#: decides, so the host channel is never used.
PERMISSION_PROMPT_ARGS = ("--permission-prompt-tool", "stdio")

#: Offered by the same flag and withheld: a headless agent entering plan mode
#: stops editing, and leaving it asks the host. `--disallowedTools` is
#: variadic and repeated flags add up (measured), so this pair goes right
#: before another flag.
WITHHELD_TOOLS = ("EnterPlanMode", "ExitPlanMode")

#: The pending question, beside the runner's settings in `work/` (HOME, which
#: is checkpointed), never in the checkout.
PENDING_NAME = "pending-question.json"

#: This file, named by absolute path in the settings, so an agent cannot edit
#: the copy a later attempt runs.
HOOK = Path(__file__).resolve()

#: The CLI's result-event `terminal_reason` when a hook stopped the turn.
HOOK_STOPPED = "hook_stopped"

#: Outcomes of `classify`.
PARKED = "parked"
BRIDGE_FAILED = "bridge_failed"
NOT_ASKED = "not_asked"

#: Bounds on what the hook reads and records. The CLI's own schema allows
#: 1-4 questions of 2-4 options; these are looser and only refuse the absurd.
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_QUESTIONS = 8
MAX_OPTIONS = 8
MAX_TEXT = 8 * 1024

#: How a recommended option is marked, by gstack and by the CLI's own prompt.
RECOMMENDED_SUFFIX = "(Recommended)"

#: A Claude Code session id: a UUID. Checked before it names a path.
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: What the model is told when its question is recorded. It gets no further
#: turn in this process; the text is what it reads when the session resumes.
PARKED_REASON = (
    "SwarmCloud sent this question to a person. The step is paused until they answer, "
    "and their answer arrives as your next message. Do not ask it again or guess the answer."
)
#: What the model is told when the hook could not record the question.
FAILED_REASON = (
    "SwarmCloud could not record this question, so nobody will answer it. The step ends here."
)


@dataclasses.dataclass(frozen=True)
class Option:
    label: str
    description: str = ""
    preview: str | None = None


@dataclasses.dataclass(frozen=True)
class Question:
    question: str
    header: str
    multi_select: bool
    options: tuple[Option, ...]

    @property
    def recommended(self) -> str | None:
        """The one option labelled recommended, or None (none, or more than one)."""
        marked = [o.label for o in self.options if o.label.rstrip().endswith(RECOMMENDED_SUFFIX)]
        return marked[0] if len(marked) == 1 else None


@dataclasses.dataclass(frozen=True)
class PendingQuestion:
    """One question tool call, as the hook recorded it."""

    session_id: str
    tool_use_id: str
    transcript_path: str
    cwd: str
    questions: tuple[Question, ...]
    asked_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tool_use_id": self.tool_use_id,
            "transcript_path": self.transcript_path,
            "cwd": self.cwd,
            "asked_at": self.asked_at,
            "questions": [
                {
                    "question": q.question,
                    "header": q.header,
                    "multi_select": q.multi_select,
                    "recommended": q.recommended,
                    "options": [dataclasses.asdict(o) for o in q.options],
                }
                for q in self.questions
            ],
        }

    @classmethod
    def from_json(cls, data: Any) -> "PendingQuestion":
        if not isinstance(data, dict):
            raise ValueError("pending question is not an object")
        questions = []
        for q in _list(data.get("questions"), "questions", MAX_QUESTIONS):
            if not isinstance(q, dict):
                raise ValueError("a question is not an object")
            options = []
            for o in _list(q.get("options"), "options", MAX_OPTIONS):
                if not isinstance(o, dict):
                    raise ValueError("an option is not an object")
                preview = o.get("preview")
                options.append(Option(
                    label=_text(o.get("label"), "label"),
                    description=_text(o.get("description", ""), "description", empty=True),
                    preview=None if preview is None else _text(preview, "preview", empty=True),
                ))
            if not isinstance(q.get("multi_select"), bool):
                raise ValueError("multi_select is not a boolean")
            questions.append(Question(
                question=_text(q.get("question"), "question"),
                header=_text(q.get("header", ""), "header", empty=True),
                multi_select=q["multi_select"],
                options=tuple(options),
            ))
        return cls(
            session_id=_session_id(data.get("session_id")),
            tool_use_id=_text(data.get("tool_use_id"), "tool_use_id"),
            transcript_path=_text(data.get("transcript_path", ""), "transcript_path", empty=True),
            cwd=_text(data.get("cwd", ""), "cwd", empty=True),
            questions=tuple(questions),
            asked_at=_text(data.get("asked_at"), "asked_at"),
        )


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a string")
    if not empty and not value.strip():
        raise ValueError(f"{name} is empty")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{name} is longer than {MAX_TEXT} characters")
    return value


def _list(value: Any, name: str, most: int) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} is not a non-empty list")
    if len(value) > most:
        raise ValueError(f"more than {most} {name}")
    return value


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise ValueError("session_id is not a Claude Code session id")
    return value


def parse_call(payload: Any, *, now: datetime.datetime | None = None) -> PendingQuestion:
    """The PreToolUse payload of one question tool call as a PendingQuestion.

    ValueError for anything that is not one: another tool, a missing field,
    an option with no label. The tool's own field is `multiSelect`.
    """
    if not isinstance(payload, dict):
        raise ValueError("hook input is not an object")
    if payload.get("tool_name") != QUESTION_TOOL:
        raise ValueError(f"tool is not {QUESTION_TOOL}")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input is not an object")
    when = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="seconds")
    questions = []
    for q in _list(tool_input.get("questions"), "questions", MAX_QUESTIONS):
        if not isinstance(q, dict):
            raise ValueError("a question is not an object")
        questions.append({
            "question": q.get("question"),
            "header": q.get("header", ""),
            "multi_select": bool(q.get("multiSelect", False)),
            "options": q.get("options"),
        })
    return PendingQuestion.from_json({
        "session_id": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "transcript_path": payload.get("transcript_path", ""),
        "cwd": payload.get("cwd", ""),
        "asked_at": when,
        "questions": questions,
    })


def hook_output(*, recorded: bool) -> dict[str, Any]:
    """The hook's answer to the CLI: always stop the turn and refuse the call."""
    reason = PARKED_REASON if recorded else FAILED_REASON
    return {
        "continue": False,
        "stopReason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def write_pending(path: Path, pending: PendingQuestion) -> None:
    """Write `pending` to `path` atomically, owner-only. A link at the name is
    replaced, never followed."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pending.to_json(), handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def clear_pending(path: Path) -> None:
    """Remove the pending question: before a run, so a file there is that run's."""
    if path.is_symlink() or path.exists():
        path.unlink()


def read_pending(path: Path) -> PendingQuestion | None:
    """The recorded question, or None when there is none. ValueError when the
    file is there but not the hook's: a link, too large, not a question."""
    if path.is_symlink():
        raise ValueError("pending question is a link")
    if not path.exists():
        return None
    if path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise ValueError("pending question is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"pending question is unreadable: {exc}") from exc
    return PendingQuestion.from_json(data)


def run_hook(argv: Sequence[str], stdin: Any, stdout: Any) -> int:
    """The hook: record the question at argv[0] and stop the turn. Exit 0 always,
    so the CLI reads the JSON answer; on any failure the turn still stops."""
    recorded = False
    pending_path = Path(argv[0]) if argv else None
    try:
        raw = stdin.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("hook input is too large")
        if pending_path is None:
            raise ValueError("no pending path")
        write_pending(pending_path, parse_call(json.loads(raw)))
        recorded = True
    except Exception:  # noqa: BLE001 - every failure stops the turn unrecorded
        if pending_path is not None:
            try:
                clear_pending(pending_path)
            except OSError:
                pass
    stdout.write(json.dumps(hook_output(recorded=recorded)) + "\n")
    return 0


# --- the worker side ---------------------------------------------------------


def enabled(environ: Mapping[str, str]) -> bool:
    """The runner switch: exactly "1" turns the bridge on."""
    return str(environ.get(ENABLE_ENV, "")).strip() == "1"


def cli_args(environ: Mapping[str, str]) -> list[str]:
    """The CLI flags the bridge adds, or none while it is off."""
    if not enabled(environ):
        return []
    return [*PERMISSION_PROMPT_ARGS, "--disallowedTools", ",".join(WITHHELD_TOOLS)]


def hook_entry(pending: Path, *, python: str | None = None) -> dict[str, Any]:
    """The `--settings` PreToolUse entry that runs this file on the question tool."""
    interpreter = python or sys.executable
    return {
        "matcher": QUESTION_TOOL,
        "hooks": [
            {
                "type": "command",
                "command": f"{shlex.quote(interpreter)} -I {shlex.quote(str(HOOK))} {shlex.quote(str(pending))}",
                "timeout": 30,
            }
        ],
    }


def _last_tool_use(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    last = None
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                last = block
    return last


def classify(events: Sequence[Mapping[str, Any]], pending_path: Path) -> tuple[str, PendingQuestion | None]:
    """How one print-mode run ended, from its stream-json events and the
    pending file: `parked` with the question, `bridge_failed`, or `not_asked`.

    Parked only when all agree: the result says a hook stopped the turn, the
    last tool call was the question tool, and the recorded question is that
    call in that session. Any disagreement is `bridge_failed`, never a normal
    end: a question the run asked and nobody recorded is missing evidence.
    """
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    stopped = result is not None and result.get("terminal_reason") == HOOK_STOPPED
    last = _last_tool_use(events)
    asked = stopped and last is not None and last.get("name") == QUESTION_TOOL
    try:
        pending = read_pending(pending_path)
    except (OSError, ValueError):
        return BRIDGE_FAILED, None
    if not asked:
        return (BRIDGE_FAILED, None) if pending is not None else (NOT_ASKED, None)
    if pending is None:
        return BRIDGE_FAILED, None
    if pending.tool_use_id != last.get("id") or pending.session_id != result.get("session_id"):
        return BRIDGE_FAILED, None
    return PARKED, pending


def session_files(home: Path, session_id: str) -> list[Path]:
    """What the CLI keeps for `session_id` under HOME, to checkpoint before a
    park: `.claude/projects/<slug>/<session>.jsonl` (the transcript --resume
    reads) and `.claude/projects/<slug>/<session>/` (tool results and
    subagent transcripts it points at), in any project slug."""
    sid = _session_id(session_id)
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return []
    found = []
    for slug in sorted(projects.iterdir()):
        if slug.is_symlink() or not slug.is_dir():
            continue
        for path in (slug / f"{sid}.jsonl", slug / sid):
            if path.exists() and not path.is_symlink():
                found.append(path)
    return found


def resume_args(session_id: str) -> list[str]:
    """The flags that continue `session_id` in a new process."""
    return ["--resume", _session_id(session_id)]


def answer_message(pending: PendingQuestion, answers: Mapping[str, Any]) -> str:
    """The next message of the resumed session: every question with its answer.

    `answers` maps each question's text to its answer: one string for a
    single-choice question, a non-empty list of strings for a multi-select
    one. An answer that is not an option's label is the person's own words
    and is said to be. ValueError for a missing, extra or malformed answer:
    a partial answer never resumes the step.
    """
    asked = {q.question: q for q in pending.questions}
    extra = sorted(set(answers) - set(asked))
    if extra:
        raise ValueError(f"answers for questions that were not asked: {extra}")
    lines = [
        f"A person answered the question you asked with {QUESTION_TOOL} "
        f"(tool use {pending.tool_use_id}). Continue the task with these answers:",
    ]
    for q in pending.questions:
        if q.question not in answers:
            raise ValueError(f"no answer for: {q.question}")
        given = answers[q.question]
        picks = given if isinstance(given, list) else [given]
        if not picks or not all(isinstance(p, str) and p.strip() for p in picks):
            raise ValueError(f"an empty answer for: {q.question}")
        if len(picks) > 1 and not q.multi_select:
            raise ValueError(f"more than one answer for a single-choice question: {q.question}")
        labels = {o.label for o in q.options}
        shown = [p if p in labels else f"(in their own words) {p}" for p in picks]
        lines.append(f"- {q.question} -> {'; '.join(shown)}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run_hook(sys.argv[1:], sys.stdin, sys.stdout))
