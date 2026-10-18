"""Per-run copies of `/sc:swarmcloud`'s script, each titled after its SwarmCloud workflow.

Owner, 2026-10-04: every SwarmCloud workflow run in Claude Code is titled
after its workflow -- since 2026-10-05 `SC · <name> · N steps`, at most 150
characters (the task panel shows ~28; Enter shows the whole) -- instead of
the fixed `swarmcloud` and its generic description. A workflow script's `meta` must be a
PURE LITERAL -- Claude Code reads the name before the script runs, and a
running script cannot rename itself -- so the name cannot be computed inside
run.js. Instead the bridge writes a copy of the plugin's run.js with
`meta.name` and `meta.description` filled in, and the session launches that
copy (`Workflow({scriptPath, args})`). Nothing else in the copy differs: the
two lines are replaced exactly, and a run.js whose first three lines are not
the shape this expects is refused rather than patched somewhere else.

The bridge finds run.js through `SWARM_SC_PLUGIN_ROOT`, which the plugin's
manifest sets to `${CLAUDE_PLUGIN_ROOT}`: the bridge itself runs from a `uv
tool` install of `apps/swarm-mcp` and has no plugin directory of its own.

THE REPO-LOCAL BRIDGE (#888 box 101, observed 2026-10-08). This checkout's
`.mcp.json` starts the bridge with `SWARM_MCP_CONFIG_FROM=repo` and no
`SWARM_SC_PLUGIN_ROOT`, so every launch there failed with "not set". In repo
mode the root therefore defaults to the checkout's own `plugin/`, and the
copies go under the checkout (REPO_RUN_DIR) rather than the system temp
directory: the Workflow tool refused a `/var/folders/...` script path until
it was copied into the repository. That folder carries a `.gitignore` of `*`
it writes itself, so the copies never show up as changes to commit. An
explicit `SWARM_SC_PLUGIN_ROOT` still wins in either mode.

`{attach: "all"}` is one run PER WORKFLOW (owner, 2026-10-04): a workflow
script cannot start a separate run -- `workflow()` nests the child inside the
caller's run, sharing its agent counter -- so the bridge writes one copy per
running workflow and the session launches each.

THE WORKSPACE IS IN THE DESCRIPTION, NOT THE NAME (SC-WS-DESC, owner request
2026-10-14, seeing the rows after plugin 0.5.35: "instead of [eng@saga.xyz] in
the main row name, we should have it in the description of the workflow or
task instead"). The name is `SC · <name> · N steps` or `SC · <label> ·
task`, and the description STARTS with `Workspace: <workspace> (<tenant>) · `
-- first, because DESCRIPTION_CHARS clips the end, so a long description
loses its tail and never the workspace -- or `Workspace: not reported` when
the API named none.

THE DETAILS, AND NO WORKSPACE IN ANY NAME (SC-DESC-DETAILS, owner 2026-10-14
~22:00 PDT: "workflows in here should not have the [Engineering] or
[eng@saga.xyz] prefix in front of the names. We do want to have a section in
the workflow or task description when you expand it in claude, that will
provide more details like user that created the job, tenant or workspace that
is using and maybe even what LLM subscription or config is using"). The
description now STARTS with the whole DETAILS line `details.line` writes --
`Workspace: ... · Started by: ... · Profiles: ... · LLM: ... · Browser tool:
... · Session: ...`, each `not reported` when the API served nothing -- and
the workspace is still its first field, so the clip still takes the end. Each
row gets the same line for its own task: the copy's args carry it
(`details.steps` for a workflow, `details` on each `attach_tasks` entry) and
run.js puts it on the row's prompt, which is what Claude Code shows when the
row is expanded. No row label carries `[<workspace>]` any more.

THE SESSION'S PROJECT, FOR EVERY BRIDGE (SC-TITLES, #1511, owner decision
2026-10-18). Measured that day: the installed plugin's bridge wrote its copies
to the system temp directory, the Workflow tool refused a script path outside
the session's allowed directories, and the session fell back to
`sc:swarmcloud` by name -- 46 of 56 runs in one session were labelled
`swarmcloud`. So every bridge now writes into the session's project, as
`<project>/.claude/sc-swarmcloud-runs/` with the same `.gitignore` of `*`
(`run_folder`). The project is CLAUDE_PROJECT_DIR when Claude Code hands it
over, else the repo-local bridge's checkout, else the bridge's own working
directory when that is a git work tree -- Claude Code starts the bridge in the
session's directory, which `checkout.directory()` already relies on. Never
its git top level: a top level above the session's directory is outside it,
which is the refusal this exists to avoid. The temp folder is the last
fallback, when no project is known or its folder cannot be written, and the
bridge says on stderr which folder it used and why. Each copy is named after
its run, `SC-<slug of the title>-<12 hex>.js`, so a label taken from the file
name says something too.

THE PROJECT RECORD (owner, 2026-10-18). The same folder holds
`launched.jsonl`: one JSON object per line for every workflow submitted or
attached and every single task dispatched from this project (`record`),
append-only. It holds ids, the run's title, the Claude Code session, the
repository as `host/owner/repo` and the time -- never a prompt, a spec body or
anything from a URL's userinfo. `session.ownership` reads it: `/sc check` and
`/sc attach --all` take work that is this session's AND in this record, and
fall back to the repository filter only for this session's work the record
does not hold (older work). A submission is recorded before SwarmCloud has
named the workflow, so its line carries the spec's `name` and a null id, and
`session.recorded` matches it to this session's workflow of that name.

SINGLE TASKS (#830, owner 2026-10-07). A task sent with `swarm_dispatch`
belongs to no workflow, so it never had a row. `for_tasks` writes ONE copy
whose args are `{attach_tasks: [...]}`: run.js starts one `sc:task` row per
task in it, titled from the task's label. `for_all` adds that copy for the
caller's running single tasks, and `swarm_dispatch` hands one back for the
task ids it just sent, so a dispatched task gets a row without anyone asking.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import unicodedata
import threading
import time
from pathlib import Path
from typing import Any

from . import checkout, config, details, workflows
from .client import SwarmClient, SwarmError, _repo_root

PLUGIN_ROOT_ENV = "SWARM_SC_PLUGIN_ROOT"
RUN_SCRIPT = Path("workflows") / "run.js"

#: run.js's first three lines, as this module expects them.
META_OPEN = "export const meta = {"
NAME_LINE = "  name: 'swarmcloud',"
DESCRIPTION_OPEN = "  description: '"

#: How long the per-run description may be: one line in the permission
#: dialog and the workflow list, not a paragraph.
DESCRIPTION_CHARS = 300

#: The DEFAULT cap on `{attach: "all"}`, and run.js's (MAX_ATTACHED_WORKFLOWS):
#: 30 since SC-ROWS (owner request 2026-10-14: "increase live rows to 30"),
#: 10 before. Every unfinished step is a live row, an agent of its own in the
#: session, so it stays a cap: the plugin's `max_live_rows` option moves it
#: within 1..50 (`config.max_live_rows`, read per launch by `max_live_rows()`
#: below). The rest are listed in `not_followed` with the call that attaches
#: each.
MAX_ATTACHED_WORKFLOWS = config.DEFAULT_MAX_LIVE_ROWS

#: The default cap on single-task rows in one copy: each is an agent of its
#: own in the session for as long as its task runs, like a step row, and
#: run.js holds the same number (MAX_ATTACHED_TASKS). The same option moves
#: it. The rest are listed with the command that shows them.
MAX_ATTACHED_TASKS = config.DEFAULT_MAX_LIVE_ROWS


def max_live_rows() -> int:
    """The live-row cap this launch uses: the sc plugin's `max_live_rows`,
    held to 1..50, else 30. Read per launch, so the copies and run.js (which
    each copy's args hand it as `max_rows`) agree on one number."""
    return config.max_live_rows()

#: The folder's name, under the project's `.claude/` or, as the last
#: fallback, under the system's temporary directory.
RUN_DIR = "sc-swarmcloud-runs"
#: Where the copies go, relative to the session's project (SC-TITLES, #1511)
#: and to the repo-local bridge's checkout (#888 box 101): the Workflow tool
#: refuses a script path in the system temp directory.
REPO_RUN_DIR = Path(".claude") / RUN_DIR
#: What Claude Code names the session's project directory with.
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"
#: Each copy: `SC-<slug of its title>-<12 hex>.js`. The copies written before
#: SC-TITLES were `swarmcloud-<12 hex>.js`; pruning takes both.
COPY_PREFIX = "SC-"
OLD_COPY_GLOB = "swarmcloud-*.js"
#: The slug's length at most: a file name, not the title.
SLUG_CHARS = 60
#: The project record, beside the copies: one JSON object per line.
RECORD_FILE = "launched.jsonl"
#: A title in the record, at most: the run's, never more.
RECORD_TITLE_CHARS = 150
#: A per-run copy older than this is removed when the next one is written, so
#: the folder does not grow without bound. A week: Claude Code reads a copy at
#: launch and again only to resume that run, which happens within a session.
RUN_COPY_MAX_AGE_S = 7 * 24 * 3600


def _checkout() -> Path:
    return Path(_repo_root())


def plugin_root() -> Path:
    """The sc plugin's directory: SWARM_SC_PLUGIN_ROOT, else this checkout's
    `plugin/` on the repo-local bridge. Raises when neither applies."""
    root = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    if root:
        return Path(root).expanduser()
    if config.repo_mode():
        return _checkout() / "plugin"
    raise SwarmError(
        f"{PLUGIN_ROOT_ENV} is not set, so this bridge cannot find the plugin's run.js to "
        "title a run with. The sc plugin's manifest sets it to ${CLAUDE_PLUGIN_ROOT}, and the "
        "repo-local bridge (SWARM_MCP_CONFIG_FROM=repo) uses its checkout's plugin/; a "
        "bridge started some other way has none. Launch the /sc:swarmcloud workflow by "
        "name instead -- its run is then titled `swarmcloud`. Nothing was written"
    )


def template() -> str:
    path = plugin_root() / RUN_SCRIPT
    if not path.is_file():
        if not os.environ.get(PLUGIN_ROOT_ENV, "").strip():
            raise SwarmError(
                f"{path} is not a file: {PLUGIN_ROOT_ENV} is not set and this repo-local bridge "
                f"found no sc plugin in its checkout ({path.parent.parent}). Nothing was written"
            )
        raise SwarmError(f"{path} is not a file: {PLUGIN_ROOT_ENV} does not point at the sc plugin")
    return path.read_text(encoding="utf-8")


#: What `_js_string` escapes beyond JSON: the line and paragraph separators
#: and every surrogate code point.
_UNSAFE_IN_SOURCE = re.compile("[\u2028\u2029\ud800-\udfff]")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _js_string(text: str) -> str:
    """`text` as a JavaScript string literal (H3-ROWS, measured 2026-10-11).
    JSON escapes every quote, backslash and control character, so no title
    can end the literal early or add code. Two more are escaped by hand and
    the rest of the title stays readable in the copy: U+2028/U+2029, which
    JSON leaves raw but every line-based reader of the copy takes for a line
    break, and a lone surrogate -- which a title can arrive as through JSON --
    which cannot be written as UTF-8 at all, so the launch raised instead of
    titling the run. `\\uXXXX` is the same character to JavaScript."""
    return _UNSAFE_IN_SOURCE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(text, ensure_ascii=False))


def write_script(title: str, description: str) -> Path:
    """A copy of run.js with `meta.name` = `title` and `meta.description` =
    `description`, as `_js_string` literals. A title reaches the copy only
    here; everything else about a run travels in its `args`, a JSON value the
    session hands the Workflow tool, never spliced into source."""
    lines = template().split("\n")
    if len(lines) < 3 or lines[0] != META_OPEN or lines[1] != NAME_LINE or not lines[2].startswith(DESCRIPTION_OPEN):
        raise SwarmError(
            "the plugin's run.js does not open with the meta name and description lines this "
            "bridge replaces, so no titled copy was written; the plugin and this bridge are "
            "from different releases"
        )
    lines[1] = "  name: " + _js_string(title) + ","
    lines[2] = "  description: " + _js_string(_clip(description, DESCRIPTION_CHARS)) + ","
    folder = run_folder()
    _prune(folder)
    target = folder / f"{COPY_PREFIX}{slug(title)}-{secrets.token_hex(6)}.js"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def slug(title: str) -> str:
    """`title` as a file-name slug: ASCII letters, digits and single hyphens,
    at most SLUG_CHARS, `run` when nothing of it is left. Accents fold to
    their letter (`ünï` -> `uni`); every other run of characters, the `·`
    separators included, is one hyphen. The title's own leading `SC · ` is
    dropped: the file name already starts `SC-`."""
    title = re.sub(r"^\s*SC\s*·\s*", "", title)
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")
    text = text[:SLUG_CHARS].rstrip("-")
    return text or "run"


def _project(environ: dict[str, str] | None = None) -> tuple[Path, str] | None:
    """`(directory, where it came from)` for the session's project, or None.

    CLAUDE_PROJECT_DIR first; then the repo-local bridge's checkout; then the
    bridge's working directory (`checkout.directory()`) when it is inside a
    git work tree -- a bare working directory may be the home directory, and
    that is no project. The directory itself, never its git top level."""
    environ = dict(os.environ) if environ is None else environ
    given = environ.get(PROJECT_DIR_ENV, "").strip()
    if given and Path(given).expanduser().is_dir():
        return Path(given).expanduser(), PROJECT_DIR_ENV
    if config.repo_mode(environ):
        try:
            return _checkout(), "the repo-local bridge's checkout"
        except Exception:  # noqa: BLE001 - no checkout is no project, never a failed launch
            pass
    here = checkout.directory()
    try:
        got = subprocess.run(["git", "-C", str(here), "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if got.returncode == 0 and got.stdout.strip() == "true":
        return here, "the bridge's working directory (a git work tree)"
    return None


def _prepared(folder: Path) -> bool:
    """Create `folder` with its `.gitignore` of `*`; False when it cannot be
    written."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        ignore = folder / ".gitignore"
        if not ignore.is_file():
            ignore.write_text("*\n", encoding="utf-8")
        return os.access(folder, os.W_OK)
    except OSError:
        return False


def record_path() -> Path:
    """Where `run_folder` would put the project record, found without
    creating or saying anything: a read (`/sc check`) writes nothing."""
    found = _project()
    if found is not None:
        folder = found[0] / REPO_RUN_DIR
        if folder.is_dir() or os.access(found[0], os.W_OK):
            return folder / RECORD_FILE
    return Path(tempfile.gettempdir()) / RUN_DIR / RECORD_FILE


#: The folder `run_folder` last reported, so stderr says it once per change.
_REPORTED: list[str] = []


def _report(folder: Path, why: str) -> None:
    said = f"{folder} ({why})"
    if _REPORTED and _REPORTED[-1] == said:
        return
    _REPORTED[:] = [said]
    print(f"swarm-mcp: run copies and launched.jsonl go to {said}", file=sys.stderr)


def run_folder() -> Path:
    """Where the titled copies and the project record go: the session's
    project's `.claude/sc-swarmcloud-runs/` (`_project`), else the system temp
    directory's `sc-swarmcloud-runs/` -- only when no project is known or its
    folder cannot be written. Created on first use; the folder used is said
    on stderr."""
    found = _project()
    if found is not None:
        project, source = found
        folder = project / REPO_RUN_DIR
        if _prepared(folder):
            _report(folder, f"the session's project, from {source}")
            return folder
        why = f"the project's folder {folder} could not be written"
    else:
        why = (f"no project directory is known ({PROJECT_DIR_ENV} is not set and the bridge's "
               "working directory is not a git work tree)")
    folder = Path(tempfile.gettempdir()) / RUN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    _report(folder, f"the system temp directory: {why}")
    return folder


def _prune(folder: Path) -> None:
    """Remove this bridge's own run copies older than RUN_COPY_MAX_AGE_S. Only
    `SC-*-<12 hex>.js` and the older `swarmcloud-*.js` in its own folder --
    never the record or the `.gitignore`; a file that vanishes or cannot be
    removed meanwhile is left alone, never an error for the launch."""
    cutoff = time.time() - RUN_COPY_MAX_AGE_S
    copies = [p for p in folder.glob(f"{COPY_PREFIX}*.js") if _COPY_NAME.match(p.name)]
    for old in copies + list(folder.glob(OLD_COPY_GLOB)):
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


#: A copy's name as `write_script` writes it.
_COPY_NAME = re.compile(r"^SC-[A-Za-z0-9-]*-[0-9a-f]{12}\.js$")


# --------------------------------------------------------------------------
# The project record (SC-TITLES, owner 2026-10-18)
# --------------------------------------------------------------------------


def _repository(url: Any) -> str | None:
    """`host/owner/repo` for a repository URL, or None: never the URL itself,
    which can carry userinfo."""
    from .check import repo_key

    return repo_key(url) if isinstance(url, str) and url.strip() else None


def record(kind: str, *, title: str, workflow_id: str | None = None, task_id: str | None = None,
           repository: Any = None, name: str | None = None) -> None:
    """Append one line to the project's `launched.jsonl`: `{workflow_id` or
    `task_id, kind, title, claude_session, repository, launched_at}`, and
    `name` on a submission, whose id is not known yet. Only those fields, so
    no prompt, spec body or credential can reach it.

    One `os.write` of one line to a file opened O_APPEND, under an exclusive
    `flock` where the platform has one: two sessions appending at once each
    land a whole line. A record that cannot be written costs the record its
    line, never the launch -- the work is already in SwarmCloud."""
    from . import session

    line: dict[str, Any] = (
        {"task_id": task_id} if task_id is not None else {"workflow_id": workflow_id}
    )
    line.update({
        "kind": kind,
        "title": _clip(workflows._flat(title) or "", RECORD_TITLE_CHARS),
        "claude_session": session.current(),
        "repository": _repository(repository),
        "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    if name is not None:
        line["name"] = name
    data = (json.dumps(line, ensure_ascii=True) + "\n").encode("ascii")
    try:
        path = run_folder() / RECORD_FILE
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        os.write(fd, data)
    except OSError:
        pass
    finally:
        os.close(fd)


# SC-WATCH (owner request 2026-10-14): `/sc check` attaches every running
# workflow of the project that has NO live row in this session, and never
# one twice. This bridge process lives exactly as long as the Claude Code
# session that started it, so it is the session's memory of what it handed
# a row: every workflow id an attach launch was written for, and every task
# id a task-row launch was written for (`/sc attach`, `/sc attach --all`,
# swarm_dispatch's rows, `/sc check`). `{attach: "all", new_only: true}`
# leaves those out. `/sc attach <id>` never consults it: attaching by hand
# always attaches, which is how a row the developer closed comes back.
_ATTACHED: set[str] = set()
_ATTACHED_LOCK = threading.Lock()


def remember_attached(ids: list[str]) -> None:
    with _ATTACHED_LOCK:
        _ATTACHED.update(str(i) for i in ids if i)


def attached() -> frozenset[str]:
    """The workflow and task ids this session was handed a row launch for."""
    with _ATTACHED_LOCK:
        return frozenset(_ATTACHED)


def forget_attached() -> None:
    """Empty the registry (tests; a new session starts empty anyway)."""
    with _ATTACHED_LOCK:
        _ATTACHED.clear()


#: What a run's description says when the API named no workspace for it:
#: said, never left out, so a missing workspace reads as missing.
WORKSPACE_NOT_REPORTED = "not reported"


def workspace_shown(workspace: Any, tenant: Any = None) -> str | None:
    """`<workspace> (<tenant>)` when the workspace is shown by a display name
    other than its tenant id, `<workspace>` when it is the tenant id, None
    when neither is known."""
    shown = workflows._flat(workspace)
    tenant = workflows._flat(tenant)
    if not shown:
        return tenant
    return f"{shown} ({tenant})" if tenant and tenant != shown else shown


def workspace_line(shown: str | None) -> str:
    """`Workspace: <shown>`, or `Workspace: not reported`: the head of every
    run's description (SC-WS-DESC)."""
    return f"Workspace: {shown or WORKSPACE_NOT_REPORTED}"


def _launch(
    title: str,
    description: str,
    args: dict[str, Any] | None,
    workspace: str | None,
    facts: dict[str, list] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write the titled copy. The DETAILS line (`details.line`: `workspace`,
    `workspace_shown`'s text or None when the API named none, first, then
    what `facts` hold) leads the description, ahead of anything clipped."""
    head = details.line(workspace, facts or details.empty(), labels)
    script = write_script(title, head + " · " + description)
    out: dict[str, Any] = {"title": title, "script_path": str(script), "details": head}
    if args is not None:
        # The copy this run is meant to be launched from: a session that
        # falls back to `sc:swarmcloud` by name hands it on in the same args,
        # and run.js's first line then says why the label is generic.
        out["args"] = {**args, "titled_copy": str(script)}
    return out


def _submitting(client: SwarmClient | None) -> tuple[str | None, str | None]:
    """`(workspace, submitter)` for a spec about to be submitted, from ONE
    `GET /v1/tenants/me`: the workspace as `workspace_shown` writes it -- the
    tenant the API says this client acts as (the API's answer, not this
    session's setting), with its display name when it gives one -- and the
    `principal.email` the submission will be stored under as `submitted_by`.
    `(None, None)` when it cannot be read: the description then says each was
    not reported, and every row still gets the workspace the submission's
    reply names."""
    if client is None:
        return None, None
    from .render import tenant_of

    try:
        me = client.request("GET", "/v1/tenants/me")
    except Exception:  # noqa: BLE001 - a title is never worth a failed launch
        return None, None
    tenant = tenant_of(me)
    record = me.get("tenant") if isinstance(me, dict) and isinstance(me.get("tenant"), dict) else me
    shown = record.get("display_name") if isinstance(record, dict) else None
    workspace = workflows.workspace_of({"tenant_id": tenant}, {tenant: shown} if tenant and isinstance(shown, str) else None)
    principal = me.get("principal") if isinstance(me, dict) and isinstance(me.get("principal"), dict) else {}
    return workspace_shown(workspace, tenant), principal.get("email") if isinstance(principal.get("email"), str) else None


def _submitting_workspace(client: SwarmClient | None) -> str | None:
    """The workspace half of `_submitting`."""
    return _submitting(client)[0]


def for_spec(document: dict[str, Any], client: SwarmClient | None = None) -> dict[str, Any]:
    """The titled copy for a spec about to be submitted. The workflow id is not
    known yet, so the title has none; the run's first row and its Result row
    carry it once SwarmCloud has answered. The workspace, at the head of the
    description, is the tenant the API says `client` acts as, which is where
    the workflow will be stored."""
    spec = workflows.read_spec(document, where="the spec")
    steps = len(spec["steps"])
    name = workflows.title_name(title=spec["title"], label=spec["label"])
    title = workflows.workflow_title(name, steps)
    description = (
        f"{title}: each step runs in SwarmCloud and is shown here as one [SwarmCloud] row, "
        "by the console's stage, with its console link"
    )
    workspace, submitter = _submitting(client)
    # Recorded now, before SwarmCloud names the workflow: by its name, which
    # `session.recorded` matches to this session's workflow of that name.
    record("submit", title=title, repository=spec.get("repository_url"), name=name)
    out = _launch(title, description, None, workspace, details.for_spec(spec, submitter))
    out["steps"] = steps
    # Each step row's own line: its profile and browser tool, no account yet.
    out["row_details"] = {"steps": {
        str(step["step_id"]): details.line(workspace, details.for_spec({"steps": [step]}, submitter))
        for step in spec["steps"]
    }}
    out["launch_with"] = (
        "the Workflow tool with {scriptPath: <script_path>, args: <the same args /sc:swarmcloud "
        "takes -- {spec, spec_path} or {spec}> plus `details`: this reply's `row_details`, so "
        "each step's row shows its own details when expanded>}"
    )
    return out


def _attach_launch(
    workflow_id: str, name: str, steps: int, workspace: str | None = None, tenant: str | None = None,
    envelope: dict[str, Any] | None = None, labels: dict[str, str] | None = None,
    repository: Any = None,
) -> dict[str, Any]:
    """`workspace` is the one the API's document names (a display name or the
    tenant id); `tenant` the tenant id behind it, for the description.
    `envelope`, the workflow as `GET /v1/workflows/{id}` served it, gives the
    DETAILS -- the run's, and each step row's in `args.details.steps` -- and
    `labels` the accounts' labels (`details.account_labels`). `repository`,
    the URL it clones (else read off `envelope`'s tasks), goes to the record
    as `host/owner/repo`."""
    if repository is None and envelope is not None:
        repository = next((t.get("repository_url") for t in envelope.get("tasks") or []
                           if isinstance(t, dict) and t.get("repository_url")), None)
    title = workflows.workflow_title(name, steps)
    description = (
        f"{title}: attached to SwarmCloud workflow {workflow_id}; each unfinished step is shown "
        "here as one [SwarmCloud] row, by the console's stage. Nothing is submitted"
    )
    args: dict[str, Any] = {"attach": workflow_id, "title": name}
    if workspace:
        # The workspace the API's document names: the attach run's results
        # and its details carry it before the workflow is read again.
        args["workspace"] = workspace
    shown = workspace_shown(workspace, tenant)
    rows = {
        step_id: details.line(shown, facts, labels)
        for step_id, facts in details.per_step(envelope).items()
    } if envelope is not None else {}
    if rows:
        args["details"] = {"steps": rows}
    record("attach", title=title, workflow_id=workflow_id, repository=repository)
    out = _launch(title, description, args, shown,
                  details.for_workflow(envelope) if envelope is not None else None, labels)
    out["workflow_id"] = workflow_id
    out["workspace"] = workspace
    remember_attached([workflow_id])
    return out


def for_attach(client: SwarmClient, workflow_id: str) -> dict[str, Any]:
    """The titled copy for attaching one workflow: one read, for the name
    SwarmCloud stored and the step count."""
    from . import session

    envelope = workflows.fetch(client, workflow_id)
    stored = workflows.stored_names(envelope)
    name = workflows.title_name(stored=stored["title"] or stored["label"], workflow_id=workflow_id)
    tenant = workflows._flat(envelope["workflow"].get("tenant_id")) or workflows._flat(envelope.get("tenant_id"))
    workspace = workflows.workspace_of({"tenant_id": tenant}, workflows.workspace_names(client))
    out = _attach_launch(workflow_id, name, len(envelope["workflow"].get("steps") or []), workspace, tenant,
                         envelope, details.account_labels(client))
    out["launch_with"] = "the Workflow tool with {scriptPath: <script_path>, args: <args>}"
    # BY HAND, ANY WORKFLOW THE TENANT MAY READ (SC-SESSION-SCOPE): the
    # session scope decides what is attached automatically, never what a
    # developer may ask for by id. The reply says whose it is.
    sid = session.current()
    owner = session.owner_of(envelope["workflow"]) or session.owner_of_tasks(envelope.get("tasks") or [])
    out["claude_session"] = owner
    out["this_session"] = session.ownership(owner, sid) == "own"
    not_ours = session.attached_note(owner, sid)
    if not_ours:
        out["not_this_session"] = not_ours
    return out


def _task_name(entry: dict[str, Any]) -> str:
    return workflows.title_name(stored=entry.get("label") or entry.get("title"), workflow_id=entry.get("task_id"))


def _tasks_workspace(entries: list[dict[str, Any]]) -> str | None:
    """The workspace(s) the tasks run in, as `workspace_shown` writes each, in
    order of first appearance and joined with `, ` when there are several;
    None when no entry names one. An entry's `tenant_id` is the tenant behind
    a `workspace` shown by its display name (`named_workspaces(keep_tenant)`)."""
    seen: list[str] = []
    for entry in entries:
        shown = workspace_shown(entry.get("workspace"), entry.get("tenant_id")) if entry.get("workspace") else None
        if shown and shown not in seen:
            seen.append(shown)
    return ", ".join(seen) or None


def tasks_title(entries: list[dict[str, Any]]) -> str:
    """`SC · <label> · task` for one task, `SC · <first label> +N · N tasks`
    for several, at most workflows.TITLE_CHARS: the name is cut, never the
    count. The workspace is in the run's description (SC-WS-DESC)."""
    count = len(entries)
    name = _task_name(entries[0]) if entries else "tasks"
    if count > 1:
        name = f"{name} +{count - 1}"
    title = workflows.workflow_title(name, count)
    # workflow_title ends in ` · N step(s)`; a single task is a task.
    head, _, _ = title.rpartition(" · ")
    return head + (" · task" if count == 1 else f" · {count} tasks")


def _read_tasks(client: SwarmClient, entries: list[dict[str, Any]]) -> None:
    """Give each entry with no `task` its task document (`GET /v1/tasks/{id}`),
    at most eight reads at a time; an entry whose read fails keeps none, and
    its details say `not reported`."""
    from concurrent.futures import ThreadPoolExecutor

    missing = [e for e in entries if not isinstance(e.get("task"), dict)]
    if not missing:
        return

    def read(entry: dict[str, Any]) -> None:
        try:
            entry["task"] = client.task(str(entry["task_id"]))
        except Exception:  # noqa: BLE001 - details are never worth a failed launch
            pass

    with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
        list(pool.map(read, missing))


def for_tasks(entries: list[dict[str, Any]], client: SwarmClient | None = None, *,
              dispatched: bool = True) -> dict[str, Any]:
    """ONE titled copy that starts an `sc:task` row per task in `entries` --
    each `{task_id, label?, console?, workspace?, tenant_id?, task?}` -- up to
    the live-row cap (`max_live_rows()`, 30 unless the plugin's option says
    otherwise). The rest come back in `not_followed`. Submits nothing.

    `task`, the task document as the API served it, gives each row its
    DETAILS (`details.line`); it is read off the row, never copied into the
    args. With `client`, an entry without one is read (`_read_tasks`) and the
    accounts' labels are read once (`details.account_labels`).

    `dispatched`: the tasks were just sent (`swarm_dispatch`'s rows, the one
    caller that does not say), so each gets its line in the project record
    -- every one, followed or not, and before the copy is written, so a copy
    that cannot be written still leaves the record. `for_all` attaches tasks
    already running and says False."""
    cap = max_live_rows()
    wanted = [e for e in entries if isinstance(e, dict) and e.get("task_id")]
    if dispatched:
        for entry in wanted:
            task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
            record("dispatch", title=tasks_title([entry]), task_id=str(entry["task_id"]),
                   repository=task.get("repository_url") or entry.get("repository_url"))
    follow = wanted[:cap]
    labels: dict[str, str] = {}
    if client is not None and follow:
        _read_tasks(client, follow)
        labels = details.account_labels(client)
    rows = []
    for entry in follow:
        row: dict[str, Any] = {"task_id": str(entry["task_id"]), "title": _task_name(entry)}
        if isinstance(entry.get("workspace"), str) and entry["workspace"].strip():
            row["workspace"] = entry["workspace"].strip()
        if isinstance(entry.get("console"), str) and entry["console"].strip():
            row["console"] = entry["console"].strip()
        shown = workspace_shown(entry.get("workspace"), entry.get("tenant_id")) if entry.get("workspace") else None
        row["details"] = details.line(shown, details.for_task(entry.get("task")), labels)
        rows.append(row)
    title = tasks_title(follow)
    description = (
        f"{title}: each SwarmCloud single task is shown here as one [SwarmCloud] row, "
        "titled from its label, with its console link. Nothing is submitted"
    )
    # `max_rows`: the cap this bridge used, so run.js -- which caps the rows
    # it starts too, at 30 when told nothing -- follows every task handed it.
    facts = details.merge(details.for_task(e.get("task")) for e in follow)
    out = _launch(title, description, {"attach_tasks": rows, "max_rows": cap}, _tasks_workspace(follow),
                  facts, labels)
    out["task_ids"] = [row["task_id"] for row in rows]
    remember_attached(out["task_ids"])
    out["not_followed"] = [
        {"task_id": str(e["task_id"]), "workspace": e.get("workspace"), "label": e.get("label"),
         "follow_with": "swarm_follow"}
        for e in wanted[cap:]
    ]
    out["launch_with"] = "the Workflow tool with {scriptPath: <script_path>, args: <args>}"
    return out


def named_workspaces(
    entries: list[dict[str, Any]], names: dict[str, str], *, keep_tenant: bool = False,
) -> list[dict[str, Any]]:
    """`entries` with each `workspace` (a tenant id, as the listing read it off
    the API's documents) shown as its display name where `names` -- the API's
    `GET /v1/tenants/mine` -- gives one that fits. The same entries, changed in
    place, in the same order. `keep_tenant` also keeps the tenant id as the
    entry's `tenant_id`, for a run's description (`for_all` only: the listings
    `swarm_workflows` and `sc workflows` print are unchanged)."""
    for entry in entries:
        if entry.get("workspace"):
            if keep_tenant:
                entry["tenant_id"] = entry["workspace"]
            entry["workspace"] = workflows.workspace_of({"tenant_id": entry["workspace"]}, names)
    return entries


def _scoped(
    entries: list[dict[str, Any]], kind: str, key: str | None, skip: frozenset[str],
    sid: str | None = None, record: Any = None, left_out: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """`(kept, already_attached, unread)`: `entries` in this project and not in
    `skip`. In this project is `session.ownership`'s rule: in the project's
    `record`, else -- the fallback for work the record does not hold -- in
    `key`'s repository (every repository when `key` is None). An entry the
    record does not hold whose repository was not read cannot be placed in a
    project: it is not launched, and comes back in `unread` with the command
    that attaches it. Each one dropped for being outside the project is
    counted in `left_out["not_this_project"]`."""
    from . import session
    from .check import repo_key

    id_key = "workflow_id" if kind == "workflow" else "task_id"
    kept: list[dict[str, Any]] = []
    already: list[str] = []
    unread: list[dict[str, Any]] = []
    for entry in entries:
        ident = str(entry.get(id_key) or "")
        name = workflows.title_name(stored=entry.get("title") or entry.get("label"), workflow_id=ident) \
            if kind == "workflow" else None
        if key is not None and not session.recorded(record, ident, name, sid):
            if not entry.get("repository_url"):
                if entry.get("steps_unread_because") or kind == "task":
                    unread.append({id_key: ident, "workspace": entry.get("workspace"),
                                   "why": "its repository was not read, so it cannot be placed in this project",
                                   "attach_with": f"/sc attach {ident}" if kind == "workflow" else "swarm_follow"})
                continue
            kind_of = session.ownership(session.owner_of(entry), sid, record=record, ident=ident, name=name,
                                        in_repository=repo_key(entry["repository_url"]) == key)
            if kind_of != "own":
                if left_out is not None and kind_of == "not_this_project":
                    left_out["not_this_project"] = left_out.get("not_this_project", 0) + 1
                continue
        if ident in skip:
            already.append(ident)
            continue
        kept.append(entry)
    return kept, already, unread


def _own(entries: list[dict[str, Any]], sid: str | None, left_out: dict[str, int]) -> list[dict[str, Any]]:
    """The entries tagged with session `sid`; the rest counted in `left_out`
    by why (`other_session`, `untagged`). With no `sid`, none."""
    from . import session

    kept = []
    for entry in entries:
        kind = session.ownership(session.owner_of(entry), sid)
        if kind == "own":
            kept.append(entry)
        else:
            left_out[kind] += 1
    return kept


def _read_workflows(client: SwarmClient, workflow_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`{workflow_id: envelope}` for each id whose `GET /v1/workflows/{id}`
    answered, at most eight reads at a time."""
    from concurrent.futures import ThreadPoolExecutor

    def read(workflow_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return workflow_id, workflows.fetch(client, workflow_id)
        except Exception:  # noqa: BLE001 - details are never worth a failed launch
            return workflow_id, None

    if not workflow_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(workflow_ids))) as pool:
        return {wid: env for wid, env in pool.map(read, workflow_ids) if env is not None}


def for_all(
    client: SwarmClient,
    *,
    repository: str | None = None,
    new_only: bool = False,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """One titled copy per running workflow, newest first and grouped by
    workspace (`sc.group_by_workspace`), up to the live-row cap, and
    one more for the caller's running single tasks (#830).

    SC-WATCH: `repository` keeps only the workflows and tasks that clone it
    (`check.resolve_scope`: a URL, `owner/repo`, "origin" for this checkout's
    remote, "all" for every one); `new_only` leaves out every id this
    session was already handed a row for (`attached()`), and `exclude` the
    ids the caller names -- rows it can see live. Without them, unchanged."""
    from . import sc, session

    filtering = repository is not None or new_only or bool(exclude)
    scope = None
    if repository is not None:
        from .check import resolve_scope

        scope = resolve_scope(repository)
    skip = frozenset((attached() if new_only else frozenset()) | {str(i) for i in exclude or [] if i})

    # The single tasks first, and on their own: a failure to read them is
    # reported beside the workflows, never instead of them.
    try:
        singles: dict[str, Any] | None = sc.running_tasks(client)
        singles_error = None
    except SwarmError as exc:
        singles, singles_error = None, str(exc)
    listing = sc.running_workflows(client)
    names = workflows.workspace_names(client)
    cap = max_live_rows()
    entries = sc.group_by_workspace(named_workspaces(
        [e for e in listing.get("workflows") or [] if isinstance(e, dict) and e.get("workflow_id")], names,
        keep_tenant=True,
    ))
    # THIS SESSION'S OWN, AND NOTHING ELSE (SC-SESSION-SCOPE, owner
    # 2026-10-14): another session's work in the same tenant, and untagged
    # work, is never a candidate -- before the cap, so the cap counts only
    # this session's. `/sc attach <wf_id>` still attaches any by hand.
    sid = session.current()
    left_out = {"workflows": session.counts(), "single_tasks": session.counts()}
    entries = _own(entries, sid, left_out["workflows"])
    if singles:
        singles = {**singles, "tasks": _own(singles["tasks"], sid, left_out["single_tasks"])}
    already: list[str] = []
    unread: list[dict[str, Any]] = []
    if filtering:
        # THIS SESSION'S WORK, WITHIN THE PROJECT (SC-TITLES, owner
        # 2026-10-18): work in the project's record is kept whatever
        # repository it clones; the repository filter is the fallback for
        # work the record does not hold.
        record = session.project_record()
        for group in left_out.values():
            group["not_this_project"] = 0
        entries, already, unread = _scoped(entries, "workflow", scope and scope["key"], skip,
                                           sid, record, left_out["workflows"])
        if singles:
            kept, more, unread_tasks = _scoped(singles["tasks"], "task", scope and scope["key"], skip,
                                               sid, record, left_out["single_tasks"])
            singles = {**singles, "tasks": kept}
            already += more
            unread += unread_tasks
    launches = []
    followed = entries[:cap]
    # THE DETAILS of each run launched (SC-DESC-DETAILS): one workflow read
    # each, at most eight at a time, and the accounts' labels once. A read
    # that fails costs that run its details -- `not reported` -- never its row.
    envelopes = _read_workflows(client, [str(e["workflow_id"]) for e in followed])
    labels = details.account_labels(client) if followed else {}
    for entry in followed:
        workflow_id = str(entry["workflow_id"])
        name = workflows.title_name(stored=entry.get("title") or entry.get("label"), workflow_id=workflow_id)
        launches.append(_attach_launch(
            workflow_id, name, int(entry.get("steps_total") or 0), entry.get("workspace"), entry.get("tenant_id"),
            envelopes.get(workflow_id), labels, entry.get("repository_url"),
        ))
    not_followed = [
        {"workflow_id": e["workflow_id"], "workspace": e.get("workspace"), "title": e.get("title") or e.get("label"),
         "attach_with": f"/sc attach {e['workflow_id']}"}
        for e in entries[cap:]
    ]
    out: dict[str, Any] = {
        "max_live_rows": cap,
        "count": len(entries),
        "launches": launches,
        "not_followed": not_followed,
        "launch_with": (
            "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
            "<args>}: one Claude Code run per SwarmCloud workflow"
        ),
    }
    if listing.get("complete") is False:
        out["complete"] = False
        out["incomplete_because"] = listing.get("incomplete_because")
    if singles_error is not None:
        out["single_tasks_error"] = singles_error
    elif singles and singles["tasks"]:
        task_launch = for_tasks(sc.group_by_workspace(named_workspaces(singles["tasks"], names, keep_tenant=True)),
                                client, dispatched=False)
        launches.append(task_launch)
        out["single_tasks"] = len(singles["tasks"])
        not_followed.extend(task_launch.pop("not_followed"))
        if singles.get("complete") is False:
            out["single_tasks_incomplete_because"] = singles.get("incomplete_because")
    else:
        out["single_tasks"] = 0
    out["launch_with"] = (
        "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
        "<args>}: one Claude Code run per SwarmCloud workflow, and one for your running "
        "single tasks (args `attach_tasks`), with a row per task"
    )
    out["session"] = {**session.describe(sid), "left_out": left_out}
    if filtering:
        out["repository"] = scope["key"] if scope else None
        if scope and scope.get("note"):
            out["repository_note"] = scope["note"]
        out["already_attached"] = already
        out["repository_unread"] = unread
    return out
