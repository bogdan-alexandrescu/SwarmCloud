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
2026-10-14, seeing the rows after plugin 0.5.35: "instead of [eng@example.com] in
the main row name, we should have it in the description of the workflow or
task instead"). The name is `SC · <name> · N steps` or `SC · <label> ·
task`, and the description STARTS with `Workspace: <workspace> (<tenant>) · `
-- first, because DESCRIPTION_CHARS clips the end, so a long description
loses its tail and never the workspace -- or `Workspace: not reported` when
the API named none.

THE DETAILS, AND NO WORKSPACE IN ANY NAME (SC-DESC-DETAILS, owner 2026-10-14
~22:00 PDT: "workflows in here should not have the [Engineering] or
[eng@example.com] prefix in front of the names. We do want to have a section in
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
over -- the plugin passes it in its MCP server's env (SC-TITLES-B), and an
empty or unexpanded `${...}` value counts as not handed over -- else the repo-local bridge's checkout, else the bridge's own working
directory when that is a git work tree -- Claude Code starts the bridge in the
session's directory, which `checkout.directory()` already relies on. Never
its git top level: a top level above the session's directory is outside it,
which is the refusal this exists to avoid. The temp folder is the last
fallback, when no project is known or its folder cannot be written, and the
bridge says on stderr which folder it used and why. Each copy is named after
its run, `SC-<slug of the title>-<12 hex>.js`, so a label taken from the file
name says something too.

OUTSIDE `.claude/` (PLUGIN-RUNDIR, part of #1511). Measured 2026-10-19 at
03:12Z on plugin 0.5.44: the Workflow tool refused
`<project>/.claude/sc-swarmcloud-runs/SC-...js` twice, the second time after
the session had Read it, and launched the same bytes from the system temp
folder at once -- Claude Code treats `.claude/` as protected configuration.
So the folder is `<project>/.sc-swarmcloud-runs/` (REPO_RUN_DIR), with the
same `.gitignore` of `*`. A `launched.jsonl` the bridges of 0.5.39-0.5.44
left at `<project>/.claude/sc-swarmcloud-runs/` (LEGACY_RUN_DIR) is moved
beside the copies on the next write when there is none there yet, and
`record_paths` keeps reading it wherever it still is, so no session loses
the record of what it launched.

THE PROJECT RECORD (owner, 2026-10-18). The same folder holds
`launched.jsonl`: one JSON object per line for every workflow submitted or
attached and every single task dispatched from this project (`record`),
append-only. It holds ids, the run's title, the Claude Code session, the
repository as `host/owner/repo` and the time -- never a prompt, a spec body or
anything from a URL's userinfo. `session.ownership` reads it: `/sc check` and
`/sc attach --all` take work that is this session's AND in this record, and
fall back to the repository filter only for this session's work the record
does not hold (older work). A submission is recorded when SwarmCloud has
answered it, by the workflow id it named (`server._workflow_created`,
SC-TITLES-B), so two of this session's workflows with one name are told
apart. A line written before that change carries the spec's `name` and a null
id; `session.recorded` still matches such a line to this session's workflow
of that name, and only such a line.

SINGLE TASKS (#830, owner 2026-10-07). A task sent with `swarm_dispatch`
belongs to no workflow, so it never had a row. `for_tasks` writes ONE copy
whose args are `{attach_tasks: [...]}`: run.js starts one `sc:task` row per
task in it, titled from the task's label. `for_all` adds that copy for the
caller's running single tasks, and `swarm_dispatch` hands one back for the
task ids it just sent, so a dispatched task gets a row without anyone asking.

ONE SUMMARY ROW, OR PER-STEP ROWS SPLIT OVER RUNS (PLUGIN-ROW-SPLIT, #1809,
owner decision 2026-10-21). Each live row is one Claude Code agent, and Claude
Code runs at most min(16, CPU cores - 2) agents per workflow run: an 18-step
attach on a 10-core machine showed 8 rows and queued 10. The plugin's `rows`
setting (`config.rows_mode`) picks the copy's shape. `summary`, the default:
ONE copy per workflow whose args say `rows: "summary"`, and run.js follows
the whole workflow with one `sc:summary` row -- one agent, never queued, a
line only when the step counts change. `per-step`: one row per unfinished
step, as before, but when there are more than `rows_per_run()` the bridge --
which runs on Claude Code's host, so it can read the core count -- writes one
copy per share, titled `SC · <name> · N steps · rows 1-8`, `rows 9-16`...,
each told its steps in `row_share`; the first reports the finished steps.
`max_live_rows` still caps the total of step rows. `swarm_workflow_launch`'s
`rows` argument picks the mode for one launch, an attach or an attach-all
(PLUGIN-ROW-SPLIT-2); an attach value is one workflow id, and a
`--per-step` or `--summary` after it is refused with a pointer to `rows`
(SC-ATTACH-SUFFIX-DROP).

THE PANE LAYOUT (SC-BROWSER-BUILD, #2318; BGD picked proposal B on
2026-10-25). With this project's layout set to `pane` (`/sc layout pane`, or
the plugin's `layout` option; `layout.row_mode` reads both, the saved choice
first), every copy for a workflow says `rows: "pane"`: ONE sc:finish row per
workflow, whose only job is to notice the finish -- it holds each follow
until the workflow's stage changes -- while the SwarmCloud pane beside the
chat browses stages, agents and details with direct MCP calls and no model
tokens. A batch of single tasks keeps its one summary row.

THE BAND LAYOUT (SC-BAND, part of #2318; BGD, 2026-10-26). With the layout
set to `band` (`/sc layout band`) the SwarmCloud view is a strip above the
prompt and a summary line below it, and the plugin starts NO /workflows row:
an attach, an attach-all and a dispatch's rows answer `launches: []` with
`rows: "band"` and `followed_by: "band"` (`_banded`), and read nothing to
build a row; a spec's copy is still the run that submits it, its args say
`rows: "band"`, and run.js submits and starts no follow row. The strip
re-reads the list once a minute while anything runs and queues one chat turn
when a workflow finishes or waits on a person, which is what the rows were
for. A launch's own `rows` still outranks the layout.

THE SPEC TRAVELS BY HANDLE (SC-LAUNCH-SPEC, #2034). Measured 2026-10-23
~01:35Z, lane SCAN-FP (wf_053776f6-dcb): the spec embedded in a launch's
args reached `swarm_workflow` retyped by the `sc:workflow` agent, ~23 KB of
JSON, and a brief holding JavaScript template-literal text (backticks around
`${...}`) arrived altered and was refused by the digest check
(NOT_SUBMITTED). The check worked; the retyping was the defect. So
`for_spec` also writes the exact bytes it embeds to `spec-<digest hex>-<12
hex>.json` beside the titled copy (`write_spec`) and hands back `args` with
that file's handle -- `{path, digest}` -- beside the spec. run.js gives the
submitting agent only the handle; `swarm_workflow {spec_handle, spec_digest}`
reads the bytes back (`handle_target`: only such a file in a folder this
bridge writes its copies to) and checks them against the digest as it checks
`spec_path`, so a changed file still submits nothing. A spec given to
/sc:swarmcloud only as text, with no launch behind it, keeps the by-value
path and its digest check.

THE ARGS CARRY THE HANDLE, NEVER THE SPEC (LAUNCH-LARGE-SPEC, #2875).
Measured 2026-10-31 (product-oracle-2f): a 24-step spec with ~90 KB of
prompts made this launch answer 94,498 characters -- over Claude Code's
tool-output limit, so the reply landed in a file -- and the session was told
to pass `args` as given, the whole spec included, which it cannot retype. So
`args` are `{spec_handle, details, rows, titled_copy}`: run.js has one
sc:workflow row read the handle's OUTLINE with `swarm_workflow_spec
{spec_handle, spec_digest}` (the digest checked where the bytes are read),
and the submit row passes the handle again. Args from before this change,
with `spec` beside the handle, still submit from the handle.
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

from . import checkout, config, details, layout, workflows
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
#: 100 since BOARD-ORDER (owner 2026-10-16, #1437: "rows for every launch"),
#: 30 since SC-ROWS, 10 before. Every unfinished step is a live row, an agent
#: of its own in the session, so it stays a cap: the plugin's `max_live_rows`
#: option moves it within 1..200 (`config.max_live_rows`, read per launch by
#: `max_live_rows()` below). The ones a cap leaves out are the ones furthest
#: from finishing (`board.order`), listed in `not_followed` with the call
#: that attaches each.
MAX_ATTACHED_WORKFLOWS = config.DEFAULT_MAX_LIVE_ROWS

#: The default cap on single-task rows in one copy: each is an agent of its
#: own in the session for as long as its task runs, like a step row, and
#: run.js holds the same number (MAX_ATTACHED_TASKS). The same option moves
#: it. The rest are listed with the command that shows them.
MAX_ATTACHED_TASKS = config.DEFAULT_MAX_LIVE_ROWS


#: Claude Code's own ceiling on the agents one workflow run runs at once, and
#: the cores it leaves the host: it runs min(16, cores - 2) (#1809, measured
#: 2026-10-21: 8 of 18 rows live on a 10-core machine, the rest queued).
AGENTS_PER_RUN_MAX = 16
HOST_RESERVED_CORES = 2


def rows_per_run(cpu_count: int | None = None) -> int:
    """How many rows one Claude Code run shows live: min(16, cores - 2), at
    least 1 -- an unknown core count is 1, never 0, which would split a batch
    into runs of nothing. The bridge runs on Claude Code's host, so its
    `os.cpu_count()` is the count Claude Code reads."""
    cores = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, min(AGENTS_PER_RUN_MAX, (cores or 0) - HOST_RESERVED_CORES))


def attach_id(given: str) -> str:
    """The one workflow id an attach value names. A `--per-step` or
    `--summary` after it was PLUGIN-ROW-SPLIT's way to pick a row mode for
    one attach; the `rows` argument does that since PLUGIN-ROW-SPLIT-2
    (owner 2026-10-21), the suffix was read one release more for sessions on
    the older skill, and it is refused now (SC-ATTACH-SUFFIX-DROP) with a
    message naming `rows`, so a caller learns the way that works instead of
    seeing an id SwarmCloud never heard of. Refused before any read."""
    words = str(given).split()
    flags = [w for w in words if w.startswith("--")]
    ids = [w for w in words if not w.startswith("--")]
    if flags:
        raise SwarmError(
            f"attach takes one workflow id and no flags; it was given {given!r}. Pick the row mode with "
            f'the `rows` argument instead: {{attach: "<wf_id>", rows: "per-step"}} or rows: "summary". '
            f"Nothing was written"
        )
    if len(ids) != 1:
        raise SwarmError(f"attach takes one workflow id; it was given {given!r}. Nothing was written")
    return ids[0]


def max_live_rows() -> int:
    """The live-row cap this launch uses: the sc plugin's `max_live_rows`,
    held to 1..200, else 100. Read per launch, so the copies and run.js (which
    each copy's args hand it as `max_rows`) agree on one number."""
    return config.max_live_rows()

#: The folder's name under the system's temporary directory, the last
#: fallback.
RUN_DIR = "sc-swarmcloud-runs"
#: Where the copies go, relative to the session's project (SC-TITLES, #1511)
#: and to the repo-local bridge's checkout (#888 box 101): the Workflow tool
#: refuses a script path in the system temp directory, and one under
#: `.claude/` too (PLUGIN-RUNDIR, measured 2026-10-19), so the folder is a
#: dot-folder of the project's own, never inside `.claude/`.
REPO_RUN_DIR = Path("." + RUN_DIR)
#: Where the bridges of plugin 0.5.39-0.5.44 put the copies and the record;
#: a record there is moved beside the copies, and read until it is.
LEGACY_RUN_DIR = Path(".claude") / RUN_DIR
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


def project_dir_given(environ: dict[str, str] | None = None) -> str | None:
    """CLAUDE_PROJECT_DIR as Claude Code handed it over, or None when it is
    absent, empty, or still reads `${...}`. The plugin passes it to the bridge
    as `"${CLAUDE_PROJECT_DIR:-}"` (SC-TITLES-B), and nobody has measured that
    Claude Code expands it for a plugin's MCP server: an unexpanded value is
    no directory, and taken as a relative path it would name a folder under
    the bridge's working directory. So it is absent, and the cwd rule decides."""
    environ = dict(os.environ) if environ is None else environ
    given = environ.get(PROJECT_DIR_ENV, "").strip()
    if not given or "${" in given:
        return None
    return given


def _project(environ: dict[str, str] | None = None) -> tuple[Path, str] | None:
    """`(directory, where it came from)` for the session's project, or None.

    CLAUDE_PROJECT_DIR first; then the repo-local bridge's checkout; then the
    bridge's working directory (`checkout.directory()`) when it is inside a
    git work tree -- a bare working directory may be the home directory, and
    that is no project. The directory itself, never its git top level."""
    environ = dict(os.environ) if environ is None else environ
    given = project_dir_given(environ)
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
    return record_paths()[0]


def record_paths() -> list[Path]:
    """Every file the project record may be in, `record_path()` first: then
    the one an older bridge left under the project's `.claude/`
    (LEGACY_RUN_DIR), which a session reads until a write moves it -- and
    after, should a bridge of 0.5.44 or older append there again. Found
    without creating anything."""
    found = _project()
    if found is not None:
        folder = found[0] / REPO_RUN_DIR
        legacy = found[0] / LEGACY_RUN_DIR / RECORD_FILE
        if folder.is_dir() or os.access(found[0], os.W_OK):
            return [folder / RECORD_FILE, legacy]
        return [Path(tempfile.gettempdir()) / RUN_DIR / RECORD_FILE, legacy]
    return [Path(tempfile.gettempdir()) / RUN_DIR / RECORD_FILE]


def _carry_record(project: Path, folder: Path) -> None:
    """Move an older bridge's `launched.jsonl` from the project's `.claude/`
    into `folder` when `folder` holds none yet. A hard link then an unlink,
    never a rename: a rename would replace a record another bridge created
    in `folder` meanwhile, and a link refuses to. A record already in
    `folder`, or a move that fails (another bridge moved it first, a file
    system without links), leaves both where they are -- `record_paths`
    reads both."""
    old = project / LEGACY_RUN_DIR / RECORD_FILE
    new = folder / RECORD_FILE
    if not old.is_file() or new.exists():
        return
    try:
        os.link(old, new)
    except OSError:
        return
    try:
        old.unlink()
    except OSError:
        return


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
    project's `.sc-swarmcloud-runs/` (`_project`), else the system temp
    directory's `sc-swarmcloud-runs/` -- only when no project is known or its
    folder cannot be written. Created on first use; the folder used is said
    on stderr."""
    found = _project()
    if found is not None:
        project, source = found
        folder = project / REPO_RUN_DIR
        if _prepared(folder):
            _carry_record(project, folder)
            _report(folder, f"the session's project, from {source}")
            return folder
        why = f"the project's folder {folder} could not be written"
    else:
        why = (f"no project directory is known ({PROJECT_DIR_ENV} is not set, is empty or was not "
               "expanded, and the bridge's working directory is not a git work tree)")
    folder = Path(tempfile.gettempdir()) / RUN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    _report(folder, f"the system temp directory: {why}")
    return folder


def _prune(folder: Path) -> None:
    """Remove this bridge's own run copies older than RUN_COPY_MAX_AGE_S. Only
    `SC-*-<12 hex>.js`, the spec handles `spec-<8 hex>-<12 hex>.json` and the
    older `swarmcloud-*.js` in its own folder --
    never the record or the `.gitignore`; a file that vanishes or cannot be
    removed meanwhile is left alone, never an error for the launch."""
    cutoff = time.time() - RUN_COPY_MAX_AGE_S
    copies = [p for p in folder.glob(f"{COPY_PREFIX}*.js") if _COPY_NAME.match(p.name)]
    copies += [p for p in folder.glob(f"{SPEC_PREFIX}*.json") if _SPEC_NAME.match(p.name)]
    for old in copies + list(folder.glob(OLD_COPY_GLOB)):
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


#: A copy's name as `write_script` writes it.
_COPY_NAME = re.compile(r"^SC-[A-Za-z0-9-]*-[0-9a-f]{12}\.js$")
#: A spec handle's file as `write_spec` writes it: the digest's eight hex
#: digits, then 12 random ones, so two launches never share a file.
_SPEC_NAME = re.compile(r"^spec-[0-9a-f]{8}-[0-9a-f]{12}\.json$")
SPEC_PREFIX = "spec-"
#: A lone surrogate, which `write_spec` escapes: JSON leaves it raw.
_SURROGATE = re.compile("[\ud800-\udfff]")


def write_spec(document: dict[str, Any], folder: Path | None = None) -> dict[str, str]:
    """Store `document` -- the spec a launch embeds in its args -- beside the
    titled copy, and return its handle `{path, digest}` (SC-LAUNCH-SPEC).

    The file holds `json.dumps(document, indent=2, ensure_ascii=False)`, the
    same object the args carry, so its bytes parse back to exactly what was
    embedded; `digest` is `workflows.spec_digest` of it, the digest run.js
    computes over the embedded object. A lone surrogate -- which a title can
    arrive as through JSON, and which UTF-8 cannot hold -- is written as its
    `\\uXXXX` escape, which parses back to the same character. Nothing in it
    is a credential: a spec never carries `mcp_credentials`
    (`workflows.SPEC_KEYS`)."""
    folder = run_folder() if folder is None else folder
    digest = workflows.spec_digest(document)
    target = folder / f"{SPEC_PREFIX}{digest.split(':', 1)[1]}-{secrets.token_hex(6)}.json"
    text = _SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(document, indent=2, ensure_ascii=False))
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "digest": digest}


def _handle_folders() -> list[Path]:
    """Every folder `run_folder` may write to, resolved: the project's
    REPO_RUN_DIR when a project is known, and the temp fallback."""
    folders = [Path(tempfile.gettempdir()) / RUN_DIR]
    found = _project()
    if found is not None:
        folders.insert(0, found[0] / REPO_RUN_DIR)
    return [f.expanduser().resolve() for f in folders]


def handle_target(path: str) -> Path:
    """The file a spec handle names, resolved, or a refusal.

    ONLY A HANDLE THIS BRIDGE COULD HAVE WRITTEN IS READ. The path arrives
    through a relay, as `spec_path` does, so it is resolved (symlinks too)
    before anything about it is looked at, and it must be a `spec-<8 hex>-<12
    hex>.json` directly in one of the run folders (`_handle_folders`). A
    refusal names the path, never what any file holds."""
    target = Path(str(path).strip()).expanduser().resolve()
    if not _SPEC_NAME.match(target.name) or target.parent not in _handle_folders():
        raise SwarmError(
            f"{path} is not a spec handle: a handle is the `spec-<digest>-<hex>.json` file "
            f"swarm_workflow_launch wrote beside its titled copy, in {RUN_DIR} folders only. "
            "Pass the handle the launch's args carry, exactly. Nothing was sent"
        )
    if not target.is_file():
        raise SwarmError(
            f"the spec handle {path} names no file: it was removed (copies older than a week "
            "are), or comes from another machine. Launch the spec again. Nothing was sent"
        )
    return target


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
        _PERSON_WAITS.clear()


# A ROW THAT ENDED WAITING ON A PERSON (#2441, BGD 2026-10-26). A row whose
# task parks on HUMAN_REQUIRED ends (`compact.person_wait_result`) instead of
# polling for hours, so its workflow is in `_ATTACHED` with no live row. When
# the step resumes -- the owner answered, a person said it is ready -- it
# needs a row again, and `new_only` would never give it one. So the bridge
# that answered the stop remembers the task here, and `{attach: "all",
# new_only: true}` attaches a new row for it once it has LEFT PARKED, and only
# for it: a step still parked stays left out, and an entry is forgotten as it
# is attached, so it is attached once. Only a HUMAN_REQUIRED park ends a row
# this way; an AWAITING_INPUT step's row keeps following it (`/sc answer`
# fills its form in), so it is never remembered here.
_PERSON_WAITS: dict[str, tuple[str | None, str | None]] = {}


def remember_person_wait(task_id: str, workflow_id: str | None, step_id: str | None) -> None:
    """A row of `task_id` (step `step_id` of `workflow_id`, or a single task
    when that is None) ended waiting on a person."""
    with _ATTACHED_LOCK:
        _PERSON_WAITS[str(task_id)] = (workflow_id or None, step_id or None)


def forget_person_wait(task_ids: Any) -> None:
    with _ATTACHED_LOCK:
        for task_id in task_ids:
            _PERSON_WAITS.pop(str(task_id), None)


def resumed_person_waits(client: SwarmClient) -> tuple[dict[str, dict[str, str]], set[str]]:
    """`(workflows, tasks)`: of the rows that ended waiting on a person, the
    ones whose task has since LEFT PARKED and not finished -- per workflow,
    `{step_id: task_id}`; and the single tasks' ids. A task that finished
    needs no row and is forgotten; one still PARKED, or not read, is kept for
    the next check."""
    with _ATTACHED_LOCK:
        waits = dict(_PERSON_WAITS)
    by_workflow: dict[str, dict[str, str]] = {}
    tasks: set[str] = set()
    finished = []
    for task_id, (workflow_id, step_id) in waits.items():
        try:
            state = client.task(task_id).get("state")
        except SwarmError:
            continue
        if state in _FINISHED:
            finished.append(task_id)
        elif state and state != "PARKED":
            if workflow_id and step_id:
                by_workflow.setdefault(workflow_id, {})[step_id] = task_id
            elif not workflow_id:
                tasks.add(task_id)
    forget_person_wait(finished)
    return by_workflow, tasks


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


def for_spec(
    document: dict[str, Any],
    client: SwarmClient | None = None,
    *,
    embedded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The titled copy for a spec about to be submitted. The workflow id is not
    known yet, so the title has none; the run's first row and its Result row
    carry it once SwarmCloud has answered. The workspace, at the head of the
    description, is the tenant the API says `client` acts as, which is where
    the workflow will be stored.

    `embedded` is the spec as it is submitted -- `document` is the same spec
    with what a title does not need already taken out (its model classes) --
    and defaults to `document`. It is written to the spec handle
    (`write_spec`), and the reply's `args` carry that handle, never the spec,
    so the run submits by reference (SC-LAUNCH-SPEC, #2034) and the reply
    stays small whatever the spec's size (LAUNCH-LARGE-SPEC, #2875)."""
    spec = workflows.read_spec(document, where="the spec")
    steps = len(spec["steps"])
    name = workflows.title_name(title=spec["title"], label=spec["label"])
    title = workflows.workflow_title(name, steps)
    mode = layout.row_mode(None)
    description = (
        f"{title}: each step runs in SwarmCloud and is shown here as one [SwarmCloud] row, "
        "by the console's stage, with its console link"
    ) if mode == config.ROWS_PER_STEP else (
        f"{title}: each step runs in SwarmCloud; ONE quiet [SwarmCloud] row notices the "
        "workflow's finish, a line each time its stage changes, and the SwarmCloud pane "
        "browses its stages"
    ) if mode == config.ROWS_PANE else (
        f"{title}: each step runs in SwarmCloud; this run submits it and starts no row -- the "
        "SwarmCloud strip above the prompt follows it and says when it finishes"
    ) if mode == layout.ROWS_BAND else (
        f"{title}: each step runs in SwarmCloud, and the whole workflow is shown here as ONE "
        "[SwarmCloud] summary row, a line each time its step counts change, with its console link"
    )
    workspace, submitter = _submitting(client)
    # NOT RECORDED HERE (SC-TITLES-B): no workflow exists yet. `swarm_workflow`
    # records the submission by the id SwarmCloud answers with
    # (`server._workflow_created`), so a same-named workflow this session did
    # not submit here is never taken for it.
    out = _launch(title, description, None, workspace, details.for_spec(spec, submitter))
    out["steps"] = steps
    # Each step row's own line: its profile and browser tool, no account yet.
    out["row_details"] = {"steps": {
        str(step["step_id"]): details.line(workspace, details.for_spec({"steps": [step]}, submitter))
        for step in spec["steps"]
    }}
    # The row mode reaches run.js in the args the session builds: `rows`.
    out["rows"] = mode
    # THE HANDLE, beside the copy: the spec's bytes, which the run reads back
    # (swarm_workflow_spec) and submits (swarm_workflow) by path and digest.
    # The args carry the handle and never the spec (LAUNCH-LARGE-SPEC, #2875).
    embedded = document if embedded is None else embedded
    handle = write_spec(embedded, Path(out["script_path"]).parent)
    out["spec_handle"] = handle
    out["args"] = {
        "spec_handle": handle,
        "details": out["row_details"],
        "rows": mode,
        "titled_copy": out["script_path"],
    }
    out["launch_with"] = (
        "the Workflow tool with {scriptPath: <script_path>, args: <this reply's `args`, as given>}. "
        "`args.spec_handle` is the file this bridge wrote the spec to: the run reads its outline "
        "and submits from it, checked against its digest, so no agent retypes the spec. Never add "
        "the spec to the args"
    )
    if mode == layout.ROWS_BAND:
        # The run is the submission alone: no follow row after it (run.js).
        out["followed_by"] = layout.ROWS_BAND
        out["note"] = layout.BAND_NOTE
    return out


def _banded(**fields: Any) -> dict[str, Any]:
    """What a launch answers in the band layout: no row to launch, and why.
    `fields` (the attach's id, a dispatch's task ids) go beside it."""
    return {
        **fields,
        "rows": layout.ROWS_BAND,
        "launches": [],
        "followed_by": layout.ROWS_BAND,
        "note": layout.BAND_NOTE,
        "launch_with": "nothing: no Workflow call. The SwarmCloud strip above the prompt follows this work",
    }


#: The states a step does not leave (swarm_common.states.TERMINAL_STATES, as
#: the API serves them): a step in one has no row to split.
_FINISHED = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"})


def _console_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`steps` in the console's order: by DAG level, the workflow's own order
    within a level -- run.js's `consoleStages`, so `rows 1-8` are the first
    eight rows the console lists. A parent not in the list, and a cycle,
    count as level 0, as there."""
    by_id = {s.get("step_id"): s for s in steps}
    depth: dict[Any, int] = {}
    visiting: set[Any] = set()

    def walk(step_id: Any) -> int:
        if step_id in depth:
            return depth[step_id]
        if step_id in visiting or step_id not in by_id:
            return 0
        visiting.add(step_id)
        deepest = 0
        for parent in by_id[step_id].get("depends_on") or []:
            deepest = max(deepest, walk(parent) + 1)
        visiting.discard(step_id)
        depth[step_id] = deepest
        return deepest

    for step in steps:
        walk(step.get("step_id"))
    ordered = sorted(enumerate(steps), key=lambda pair: (depth.get(pair[1].get("step_id"), 0), pair[0]))
    return [step for _, step in ordered]


def _open_steps(envelope: dict[str, Any]) -> list[str]:
    """The ids of the workflow's unfinished steps, in the console's order: a
    step whose task the read did not name a finished state for is unfinished,
    as run.js counts it."""
    rows = workflows.step_rows(envelope)
    return [str(r["step_id"]) for r in _console_order(rows)
            if r.get("step_id") and r.get("state") not in _FINISHED]


def share_title(name: str, steps: int, first: int, last: int) -> str:
    """`SC · <name> · N steps · rows <first>-<last>`, at most
    workflows.TITLE_CHARS: the name is cut further, never the count or the
    share."""
    suffix = f" · rows {first}-{last}"
    title = workflows.workflow_title(name, steps)
    over = len(title) + len(suffix) - workflows.TITLE_CHARS
    if over > 0:
        flat = workflows._flat(name) or "workflow"
        title = workflows.workflow_title(_clip(flat, max(1, len(flat) - over)), steps)
    return title + suffix


def _attach_launches(
    workflow_id: str, name: str, steps: int, workspace: str | None = None, tenant: str | None = None,
    envelope: dict[str, Any] | None = None, labels: dict[str, str] | None = None,
    repository: Any = None, *, rows: str | None = None, budget: int | None = None,
    only_steps: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """`(copies, steps_left_out, rows_used)` for attaching one workflow.

    `only_steps` (per-step, with an envelope): a row for these unfinished
    steps alone, reporting no other -- the steps that resumed after their row
    ended waiting on a person (#2441), whose workflow already had its rows.

    `workspace` is the one the API's document names (a display name or the
    tenant id); `tenant` the tenant id behind it, for the description.
    `envelope`, the workflow as `GET /v1/workflows/{id}` served it, gives the
    DETAILS -- the run's, and each step row's in `args.details.steps` -- and
    the unfinished steps a per-step attach splits; `labels` the accounts'
    labels (`details.account_labels`). `repository`, the URL it clones (else
    read off `envelope`'s tasks), goes to the record as `host/owner/repo`.

    `rows`, the mode (`layout.row_mode` when None): `summary` is ONE copy
    whose run follows the whole workflow with one row; `per-step` is one row
    per unfinished step, at most `budget` of them (`max_live_rows()` when
    None), in copies of at most `rows_per_run()` rows each when there are more
    -- each copy's `row_share` names its steps and the other copies' (so the
    first, and only the first, reports the steps finished at launch). A
    per-step attach with no envelope cannot split, and is one copy."""
    mode = layout.row_mode(rows)
    if repository is None and envelope is not None:
        repository = next((t.get("repository_url") for t in envelope.get("tasks") or []
                           if isinstance(t, dict) and t.get("repository_url")), None)
    title = workflows.workflow_title(name, steps)
    base: dict[str, Any] = {"attach": workflow_id, "title": name, "rows": mode}
    if workspace:
        # The workspace the API's document names: the attach run's results
        # and its details carry it before the workflow is read again.
        base["workspace"] = workspace
    shown = workspace_shown(workspace, tenant)
    step_details = {
        step_id: details.line(shown, facts, labels)
        for step_id, facts in details.per_step(envelope).items()
    } if envelope is not None else {}
    if step_details:
        base["details"] = {"steps": step_details}
    record("attach", title=title, workflow_id=workflow_id, repository=repository)
    facts = details.for_workflow(envelope) if envelope is not None else None

    def copy(copy_title: str, description: str, share: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(base)
        if share is not None:
            args["row_share"] = share
        out = _launch(copy_title, description, args, shown, facts, labels)
        out["workflow_id"] = workflow_id
        out["workspace"] = workspace
        out["rows"] = mode
        return out

    copies, left, used = _shaped(mode, copy, title, workflow_id, name, steps, envelope, budget, only_steps)
    # Remembered once its copies are written: a copy that could not be
    # written is no row, and `/sc check` must still offer it.
    remember_attached([workflow_id])
    return copies, left, used


def _shaped(
    mode: str, copy: Any, title: str, workflow_id: str, name: str, steps: int,
    envelope: dict[str, Any] | None, budget: int | None, only_steps: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """`_attach_launches`' copies, written by `copy(title, description,
    share)`, in `mode`'s shape."""
    if only_steps is not None and mode == config.ROWS_PER_STEP and envelope is not None:
        wanted = set(only_steps)
        resumed = [i for i in _open_steps(envelope) if i in wanted]
        others = [str(r["step_id"]) for r in workflows.step_rows(envelope)
                  if r.get("step_id") and str(r["step_id"]) not in wanted]
        description = (
            f"{title}: attached to SwarmCloud workflow {workflow_id} again for "
            f"{', '.join(resumed) or 'no step'}, which resumed after its row ended waiting on a "
            "person; one [SwarmCloud] row each. Nothing is submitted"
        )
        share = {"from": 1, "to": len(resumed), "steps": resumed, "others": others}
        return [copy(title, description, share)], [], len(resumed)
    if mode == config.ROWS_PANE:
        # THE PANE LAYOUT (SC-BROWSER-BUILD, #2318): one sc:finish row per
        # workflow, whose only job is to notice the finish; the pane browses.
        description = (
            f"{title}: attached to SwarmCloud workflow {workflow_id}; ONE quiet [SwarmCloud] row "
            "notices its finish, a line each time its stage changes, and the SwarmCloud pane "
            "browses its stages. Nothing is submitted"
        )
        return [copy(title, description)], [], 1
    if mode == config.ROWS_SUMMARY:
        description = (
            f"{title}: attached to SwarmCloud workflow {workflow_id}; the whole workflow is shown "
            "here as ONE [SwarmCloud] summary row, a line each time its step counts change. "
            "Nothing is submitted"
        )
        return [copy(title, description)], [], 1

    description = (
        f"{title}: attached to SwarmCloud workflow {workflow_id}; each unfinished step is shown "
        "here as one [SwarmCloud] row, by the console's stage. Nothing is submitted"
    )
    if envelope is None:
        return [copy(title, description)], [], steps
    open_ids = _open_steps(envelope)
    cap = max_live_rows() if budget is None else max(0, budget)
    followed, left = open_ids[:cap], open_ids[cap:]
    limit = rows_per_run()
    if not left and len(followed) <= limit:
        return [copy(title, description)], [], len(followed)
    copies = []
    for start in range(0, len(followed), limit):
        share = followed[start:start + limit]
        first, last = start + 1, start + len(share)
        copies.append(copy(
            share_title(name, steps, first, last),
            f"{title}: attached to SwarmCloud workflow {workflow_id}; unfinished steps {first}-{last} "
            f"of {len(open_ids)} are shown here as one [SwarmCloud] row each -- a run of its own, "
            f"within the {limit} agents Claude Code runs per run. Nothing is submitted",
            {"from": first, "to": last, "steps": share,
             "others": [i for i in followed if i not in share]},
        ))
    return copies, left, len(followed)


def _launch_with(copies: list[dict[str, Any]]) -> str:
    if len(copies) > 1:
        return (
            "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
            "<args>}: each copy is a run of its own with its share of the rows (`rows 1-8`, "
            "`rows 9-16`...), so every row is live at once"
        )
    return "the Workflow tool with {scriptPath: <script_path>, args: <args>}"


def for_attach(client: SwarmClient, workflow_id: str, rows: str | None = None) -> dict[str, Any]:
    """The titled copy -- or, per-step past the host's limit, copies -- for
    attaching one workflow: one read, for the name SwarmCloud stored, the step
    count and the unfinished steps. `rows` (`swarm_workflow_launch`'s
    argument) outranks the `rows` setting for this attach; a flag after the
    id is refused (`attach_id`). The reply's own fields are the first copy's;
    `launches` holds every copy."""
    from . import session

    workflow_id = attach_id(workflow_id)
    if layout.row_mode(rows) == layout.ROWS_BAND:
        return _banded(attach=workflow_id)
    envelope = workflows.fetch(client, workflow_id)
    stored = workflows.stored_names(envelope)
    name = workflows.title_name(stored=stored["title"] or stored["label"], workflow_id=workflow_id)
    tenant = workflows._flat(envelope["workflow"].get("tenant_id")) or workflows._flat(envelope.get("tenant_id"))
    workspace = workflows.workspace_of({"tenant_id": tenant}, workflows.workspace_names(client))
    copies, left, _ = _attach_launches(
        workflow_id, name, len(envelope["workflow"].get("steps") or []), workspace, tenant,
        envelope, details.account_labels(client), rows=rows)
    out = dict(copies[0])
    out["launches"] = copies
    out["not_followed"] = [
        {"workflow_id": workflow_id, "step_id": step_id,
         "why": f"past max_live_rows ({max_live_rows()})", "attach_with": f"/sc attach {workflow_id}"}
        for step_id in left
    ]
    out["launch_with"] = _launch_with(copies)
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
              dispatched: bool = True, rows: str | None = None) -> dict[str, Any]:
    """ONE titled copy that starts an `sc:task` row per task in `entries` --
    each `{task_id, label?, console?, workspace?, tenant_id?, task?}` -- up to
    the live-row cap (`max_live_rows()`, 100 unless the plugin's option says
    otherwise). The rest come back in `not_followed`. Submits nothing.

    `task`, the task document as the API served it, gives each row its
    DETAILS (`details.line`); it is read off the row, never copied into the
    args. With `client`, an entry without one is read (`_read_tasks`) and the
    accounts' labels are read once (`details.account_labels`).

    `dispatched`: the tasks were just sent (`swarm_dispatch`'s rows, the one
    caller that does not say), so each gets its line in the project record
    -- every one, followed or not, and before the copy is written, so a copy
    that cannot be written still leaves the record. `for_all` attaches tasks
    already running and says False.

    `rows`, the mode (`layout.row_mode` when None): `for_all` hands on
    the one its caller asked for."""
    cap = max_live_rows()
    # `rows`: the mode, read before the name is reused for the task rows.
    mode = layout.row_mode(rows)
    if mode == config.ROWS_PANE:
        # A batch of single tasks has no stages to follow: the pane layout
        # shows it as the one summary row it already is.
        mode = config.ROWS_SUMMARY
    wanted = [e for e in entries if isinstance(e, dict) and e.get("task_id")]
    if dispatched:
        for entry in wanted:
            task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
            record("dispatch", title=tasks_title([entry]), task_id=str(entry["task_id"]),
                   repository=task.get("repository_url") or entry.get("repository_url"))
    if mode == layout.ROWS_BAND:
        # Recorded above, as every dispatch is; no row (the strip follows them).
        return _banded(task_ids=[str(e["task_id"]) for e in wanted])
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
    # it starts too, at 100 when told nothing -- follows every task handed it.
    facts = details.merge(details.for_task(e.get("task")) for e in follow)
    # `rows`: the sc plugin's setting (PLUGIN-ROW-SPLIT). `summary` follows the
    # whole batch with ONE row; `per-step` starts a row per task, as before.
    if mode == config.ROWS_SUMMARY:
        description = (
            f"{title}: the SwarmCloud single tasks are shown here as ONE [SwarmCloud] summary row, "
            "a line each time their counts change. Nothing is submitted"
        )
    out = _launch(title, description, {"attach_tasks": rows, "max_rows": cap, "rows": mode},
                  _tasks_workspace(follow), facts, labels)
    out["rows"] = mode
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


def _own_or_project(entries: list[dict[str, Any]], sid: str | None, repository: str | None,
                    left_out: dict[str, int]) -> list[dict[str, Any]]:
    """The entries tagged with session `sid`, and -- `/sc attach --all
    --project`, at session start (SC-BROWSER-FOLLOWUP, BGD 2026-10-25) --
    those that clone `repository`, whichever session started them; each kept
    entry marked `this_session`. The rest counted in `left_out` by why."""
    from . import session
    from .check import repo_key

    kept = []
    for entry in entries:
        kind = session.ownership(session.owner_of(entry), sid)
        if kind == "own" or (repository is not None and repo_key(entry.get("repository_url")) == repository):
            kept.append({**entry, "this_session": kind == "own"})
        else:
            left_out[kind] += 1
    return kept


def _merged_listings(listings: list[dict[str, Any]]) -> dict[str, Any]:
    """Several `sc.running_workflows` answers as one: each workflow once, in
    board order again; incomplete when any was."""
    from . import sc

    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for listing in listings:
        for entry in listing.get("workflows") or []:
            ident = str(entry.get("workflow_id") or "")
            if isinstance(entry, dict) and ident and ident not in seen:
                seen.add(ident)
                found.append(entry)
    out: dict[str, Any] = {"workflows": sc.group_by_workspace(found), "count": len(found),
                           "tenant_id": next((l.get("tenant_id") for l in listings if l.get("tenant_id")), None),
                           "complete": all(l.get("complete") is not False for l in listings)}
    reasons = [l["incomplete_because"] for l in listings if l.get("incomplete_because")]
    if reasons:
        out["incomplete_because"] = "; ".join(reasons)
    return out


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
    rows: str | None = None,
    project: bool = False,
) -> dict[str, Any]:
    """One titled copy per running workflow, closest to finish first in
    workspace blocks (`sc.group_by_workspace`, `board.order`), up to the
    live-row cap -- so a cap leaves out the ones furthest from finishing, and
    the runs start top to bottom in that order -- and one more for the
    caller's running single tasks (#830), ordered the same way by their own
    state age.

    SC-WATCH: `repository` keeps only the workflows and tasks that clone it
    (`check.resolve_scope`: a URL, `owner/repo`, "origin" for this checkout's
    remote, "all" for every one); `new_only` leaves out every id this
    session was already handed a row for (`attached()`), and `exclude` the
    ids the caller names -- rows it can see live. Without them, unchanged.

    `rows` (`swarm_workflow_launch`'s argument, PLUGIN-ROW-SPLIT-2) is the
    mode for every copy, the single tasks' too; None reads the setting.

    `project` (`/sc attach --all --project`, SC-BROWSER-FOLLOWUP, BGD
    2026-10-25): what the SessionStart hook asks for in the rows layout --
    this session's work AND every workflow and single task still running that
    clones this checkout's origin repository (`repository` names another),
    whichever Claude Code session started it. At session start only: the
    plain attach-all, `/sc check` and the watch stay this session's. With no
    origin naming a repository it widens to nothing and says so."""
    from . import sc, session

    if layout.row_mode(rows) == layout.ROWS_BAND:
        # No row to start, so nothing to read: the strip lists the work itself.
        return _banded(attach="all")
    filtering = (repository is not None or new_only or bool(exclude)) and not project
    scope = None
    if repository is not None or project:
        from .check import resolve_scope

        scope = resolve_scope(repository if repository is not None else "origin")
    project_repo = scope["key"] if project and scope else None
    # A step whose row ended waiting on a person and that has since resumed
    # (#2441): its workflow is attached again, for that step alone -- in the
    # per-step layout, where a row ends that way; `exclude` (a row the caller
    # can see live) still wins, so nothing is attached twice.
    resumed: dict[str, dict[str, str]] = {}
    resumed_tasks: set[str] = set()
    if new_only and layout.row_mode(rows) == config.ROWS_PER_STEP:
        resumed, resumed_tasks = resumed_person_waits(client)
    skip = frozenset((attached() - set(resumed) - resumed_tasks if new_only else frozenset())
                     | {str(i) for i in exclude or [] if i})

    # The single tasks first, and on their own: a failure to read them is
    # reported beside the workflows, never instead of them.
    try:
        singles: dict[str, Any] | None = sc.running_tasks(client)
        singles_error = None
    except SwarmError as exc:
        singles, singles_error = None, str(exc)
    # THIS SESSION'S WORK, ASKED OF THE API (CHECK-FILTER, #1749): the
    # listing used to stop after 4 pages of the whole tenant's running set,
    # so a session's older work got no row. `check.server_scope` says what the
    # API may filter without losing a candidate; what it left out is counted
    # below as if it had been left out here.
    from .check import server_scope

    sid = session.current()
    record = session.project_record()
    if project:
        # Two reads the API can filter, each a page or two: this session's
        # work, and the project's repository's.
        listing = _merged_listings(
            ([sc.running_workflows(client, claude_session=sid)] if sid else [])
            + ([sc.running_workflows(client, repository=project_repo)] if project_repo else []))
    else:
        listing = sc.running_workflows(
            client, **server_scope(scope and scope["key"], sid, record))
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
    left_out = {"workflows": session.counts(), "single_tasks": session.counts()}
    by_api = (listing.get("filtered_by_api") or {}).get("left_out") or {}
    for kind in ("other_session", "untagged"):
        left_out["workflows"][kind] += int(by_api.get(kind) or 0)
    already: list[str] = []
    unread: list[dict[str, Any]] = []
    if project:
        entries = _own_or_project(entries, sid, project_repo, left_out["workflows"])
        if singles:
            singles = {**singles, "tasks": _own_or_project(singles["tasks"], sid, project_repo,
                                                           left_out["single_tasks"])}
        if skip:
            already = [str(e.get("workflow_id")) for e in entries if str(e.get("workflow_id")) in skip]
            entries = [e for e in entries if str(e.get("workflow_id")) not in skip]
            if singles:
                already += [str(t.get("task_id")) for t in singles["tasks"] if str(t.get("task_id")) in skip]
                singles = {**singles, "tasks": [t for t in singles["tasks"] if str(t.get("task_id")) not in skip]}
    else:
        entries = _own(entries, sid, left_out["workflows"])
        if singles:
            singles = {**singles, "tasks": _own(singles["tasks"], sid, left_out["single_tasks"])}
    if filtering:
        # THIS SESSION'S WORK, WITHIN THE PROJECT (SC-TITLES, owner
        # 2026-10-18): work in the project's record is kept whatever
        # repository it clones; the repository filter is the fallback for
        # work the record does not hold.
        for group in left_out.values():
            group["not_this_project"] = 0
        left_out["workflows"]["not_this_project"] += int(by_api.get("other_repository") or 0)
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
    # THE ROWS (PLUGIN-ROW-SPLIT): a summary row is one row per workflow, so
    # the cap counts workflows; per-step rows count steps, so the cap is a
    # budget the workflows spend in board order, and a workflow it does not
    # reach is not followed.
    mode = layout.row_mode(rows)
    budget = cap
    unreached: list[dict[str, Any]] = []
    steps_left: list[dict[str, Any]] = []
    for entry in followed:
        workflow_id = str(entry["workflow_id"])
        if mode == config.ROWS_PER_STEP and budget <= 0:
            unreached.append(entry)
            continue
        name = workflows.title_name(stored=entry.get("title") or entry.get("label"), workflow_id=workflow_id)
        copies, left, used = _attach_launches(
            workflow_id, name, int(entry.get("steps_total") or 0), entry.get("workspace"), entry.get("tenant_id"),
            envelopes.get(workflow_id), labels, entry.get("repository_url"),
            rows=mode, budget=budget if mode == config.ROWS_PER_STEP else None,
            only_steps=sorted(resumed[workflow_id]) if workflow_id in resumed else None,
        )
        if workflow_id in resumed:
            forget_person_wait(resumed[workflow_id].values())
        launches.extend(copies)
        budget -= used
        steps_left.extend({"workflow_id": workflow_id, "step_id": step_id,
                           "why": f"past max_live_rows ({cap})", "attach_with": f"/sc attach {workflow_id}"}
                          for step_id in left)
    not_followed = [
        {"workflow_id": e["workflow_id"], "workspace": e.get("workspace"), "title": e.get("title") or e.get("label"),
         "attach_with": f"/sc attach {e['workflow_id']}"}
        for e in unreached + entries[cap:]
    ] + steps_left
    out: dict[str, Any] = {
        "max_live_rows": cap,
        "rows": mode,
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
                                client, dispatched=False, rows=mode)
        launches.append(task_launch)
        forget_person_wait(str(t.get("task_id")) for t in singles["tasks"] if str(t.get("task_id")) in resumed_tasks)
        out["single_tasks"] = len(singles["tasks"])
        not_followed.extend(task_launch.pop("not_followed"))
        if singles.get("complete") is False:
            out["single_tasks_incomplete_because"] = singles.get("incomplete_because")
    else:
        out["single_tasks"] = 0
    out["launch_with"] = (
        "one Workflow call per entry of `launches`, each {scriptPath: <script_path>, args: "
        "<args>}: one Claude Code run per SwarmCloud workflow (per share of its rows, "
        "`rows 1-8`, `rows 9-16`..., when per-step rows are more than one run shows), and one "
        "for your running single tasks (args `attach_tasks`)"
    )
    out["session"] = {**session.describe(sid), "left_out": left_out}
    if project:
        out["project"] = {
            "repository": project_repo,
            "from_other_sessions": sum(1 for e in [*entries, *((singles or {}).get("tasks") or [])]
                                       if e.get("this_session") is False),
            "scope": ("this session's work and this project's running work, whichever session started "
                      "it -- at session start only; /sc check and the watch stay this session's"),
        }
        if project_repo is None:
            out["project"]["note"] = ("this checkout has no origin remote naming a repository, so "
                                      "only this session's work was attached")
        if skip:
            out["already_attached"] = already
    if filtering:
        out["repository"] = scope["key"] if scope else None
        if scope and scope.get("note"):
            out["repository_note"] = scope["note"]
        out["already_attached"] = already
        out["repository_unread"] = unread
    return out
