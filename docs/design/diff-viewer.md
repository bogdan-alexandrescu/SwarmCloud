# The diff viewer: easier to reach, easier to read (design, lane DIFF0)

The owner, 2026-10-08:

> "I would really want to have a nicer and more easy to use and easy to get to
> diff viewer that will show the diff per file for any work that has done code
> edits/commits to. lets see 5 mockup for how this can done better than today"

The mock-ups are [`docs/web-ui/mockups/diff-viewer.html`](../web-ui/mockups/diff-viewer.html):
five variants, each a clickable 1280px screen with its entry points, a 390px
phone layout, and light and dark themes. **Nothing here is built.** The
viewer that exists today is described in
[`docs/web-ui/diff-viewer.md`](../web-ui/diff-viewer.md). This note records
why the five differ, what each would cost, and which one to build.

**Fixture.** Every diff in the mock-ups is real. It is 11 of the 29 files of
commit `dbf6708` on main (2026-10-04, #553), with that commit's
`git show -M --numstat` counts and hunks: an added file (`AgentLogs.tsx`,
+680), a deleted file (`LogDock.tsx`, −767), and a rename
(`logdock.test.tsx → logs.tab.test.tsx`, 79% similar). The page caps each
file at 3 hunks and each hunk at 40 lines, and says where it did. The one
binary file is a real file of this repository
(`docs/audits/2026-09-23/overflow/05-dock-route-paths-1440.png`, 114,775
bytes), but it is not part of `dbf6708`, and the page labels it as added. The
agent, workflow and run names, variant 5's split of the files across steps,
and variant 3's findings are example data, and the page labels them so.

## 1. Today, measured from the code (main at `e51e2f4`)

What the viewer already does well (`apps/swarm-ui/src/diff/`):

* a per-file list with +/− counts and a five-cell bar;
* unified and split views;
* collapse per file and for all files, with files over 400 changed lines
  (`LARGE_FILE_LINES`, rows.ts:25) opening folded;
* binary, renamed, copied and mode-change badges;
* find across the whole patch;
* `n`/`p` for files and `j`/`k` for changes;
* fixed-height windowed rows, so 50,000 lines cost about 300 DOM rows;
* a parser that never throws.

The pain is reach, room and scope.

| From | Clicks to the first file's diff | Evidence |
|---|---|---|
| Agents list, finished agent | **2 + a scroll**: row → Details, scroll to the Code card, **Show the diff** (closed by default) | `AgentDetail.tsx:4176-4190` |
| Agents › Artifacts | **3**: row → Artifacts → `swarm-work.patch` | `Artifacts.tsx:1146-1154`, `1211-1213` |
| A workflow page | **3** (4 for any other file): step → **Open agent →**, which leaves the workflow → **Show the diff** | `Workflows.tsx:3638-3652`; `WorkflowViews.tsx:1284-1295` |
| A step that handed a diff on | **2**: that diff opens by default, on the producing step's Details only | `AgentDetail.tsx:3612-3615`, `3897` |
| An issue run | **2-3, after guessing which step wrote code**: Steps card → **Open agent →** → **Show the diff** | `RunSteps.tsx:180-187` |
| The PR | leaves the console; the viewer has no PR link back | `Workflows.tsx:1385-1396`; `Runs.tsx:1469-1510` |

**Pain points:**

1. **There is no diff at workflow or run level.** A workflow has one patch per
   step. Nothing combines them, and nothing says which step changed a file.
   The run page's only file figure is a count (`RunIndex.tsx:182-199`).
   **Impact** lists the PR's files with +/− but no hunks
   (`RepoImpact.tsx:194-205`).
2. **Too narrow for split.** Split needs a 700px pane (`SPLIT_MIN_WIDTH`). The
   inspector's width is the viewport minus the 320px shell minus the 380px
   list: **580px at 1280**, about 740px at 1440 before card padding.
   *Computed from `styles.css:6203-6215`, not measured in a browser.*
3. **Nothing to link to.** The URL names the patch but never the file
   (`App.tsx:671-675`), and "viewed" is component memory.
4. **Counts need a read.** `result_summary.git` keeps per-commit totals and
   the patch name. The worker's `git log --numstat` sees per-file rows and
   discards them (`agent_worker/gitops.py:1795-1846`). The file list therefore
   costs a content read of up to **512 KiB** (`inspect.py:308`).
5. **Large patches.** Patches are read in 512 KiB windows, and only files
   wholly inside a window are drawn (`ArtifactViewer.tsx:690-800`). Over
   **16 MiB** the patch is discarded and `patch_omitted` says so
   (`config.py:263`).
6. **Missing features:**
   * syntax highlighting, which is deliberately absent so that text reaches
     the DOM only as React text nodes;
   * comments;
   * findings beside the lines they name: `verdict_gate.findings` is text
     (`verdict.py:101`), and a review's own finding objects carry `file` and a
     free-text `where`, but no line (`verdict.py:202`);
   * a PR link inside the viewer.

## 2. The five variants

### 1 · Review page: every file on one full-width page

A route of its own (`/changes/task|workflow|run/<id>`). It has a file tree
with "viewed" ticks, and every file stacked in one scroll with sticky
headers. The URL names the file.

* **Optimises:** reading a big change end to end. It is the most familiar
  layout.
* **Costs:**
  * a new screen;
  * `git.files` from the worker, for counts before the read;
  * persistence for "viewed";
  * a worker change to write a "Changes in SwarmCloud" link into the PR body.
* **Risks:**
  * **It reverses the owner's 2026-10-01 decision** that a patch is read one
    file at a time (`DiffView.tsx:15`).
  * Windowing must span files.
  * With a 512 KiB window, "every file" really means "every file in this
    window".

### 2 · A Changes tab wherever work happened (recommended)

The shipped viewer becomes a full-height tab:

* beside Details, Logs and Artifacts on an agent
  (`/agents/<tab>/<id>/changes`);
* on a workflow (`/workflows/<id>/changes`);
* on an issue run (`/runs/<id>/changes`).

Opening it folds the agent list to its 64px strip (the shipped `[`), so at
1280 the file gets about 850px and split is available. A summary line
carries the totals, the base, the patch, and the PR, linked both ways. The
Code card's "Show the diff" becomes "Changes ›", and the step inspector gets
"Changes · N files ›".

* **Optimises:** reach. One name and one place everywhere, and the least
  change to reading, because it is the viewer the owner already approved.
* **Costs:**
  * **nothing new is needed to ship**;
  * `git.files` for a tab count before the read (until then the count is a
    dash, not 0);
  * `initialFile` and `onFileChange` props on `DiffView`, so that the address
    can name a file.
* **Risks:**
  * Folding the list must be remembered, not forced.
  * A workflow tab that only lists step patches is not a diff, which is why
    variant 5 fills it.
  * The credential-refusal rule (no lines of a refused file, ever,
    `AgentDetail.tsx:3617-3620`) gains a second place it must hold.

### 3 · Review-first: files by area, findings beside their lines

The page opens on the verdict and its findings. Files are grouped by area
and change kind ("named by the review", removed, renamed, source, tests,
docs, binary), never by a "risk" score the console cannot measure. Each
finding is pinned in a gutter beside the line it names. A finding without a
line is listed as **not placed**, never guessed.

* **Optimises:** judging a change against its review.
* **Costs:**
  * the worker keeps `{severity, file, line, side, summary}` per finding,
    plus the digest of the patch the review read;
  * the fix step's patch, to show "fixed by fix-1".
* **Risks:**
  * After a fix round the line numbers drift, and a pin on the wrong line is
    worse than none, so pins need the digest check.
  * The gutter takes split's room.

### 4 · A changes drawer over any screen

One chip, **Changes · 12 files +886 −866**, in every agent, workflow and run
head (and `c` on the keyboard; `c` is unbound in `shortcuts.ts`). It opens an
820px drawer over the page. The drawer:

* is keyboard-first, with the shipped keys: `n`/`p` file, `j`/`k` change,
  `/` find, `Esc` close;
* can be pinned open;
* can switch source between the integrator's patch and any one step's.

The brief proposed `j`/`k` for files. The mock keeps the shipped map, because
one viewer with two key maps is worse than either map.

* **Optimises:** a quick look without losing your place.
* **Costs:** `git.files` for the chip's counts on every page, and a global
  drawer host in `Shell.tsx` with its own Escape layer and focus trap.
* **Risks:** **it overlaps the page**, which the owner rejected for logs on
  2026-10-04. It is an overlay on an overlay inside the agent split, and a
  second host with a second set of focus rules.

### 5 · The workflow's changes as a files × steps matrix

On a workflow or run: files down the side, steps across the top, each cell
that step's +/− on that file, and an integrate (PR) column. A step filter
narrows rows and columns. Choosing a file shows each step's hunks for it,
stacked and tagged by step. A step that changed nothing is a measured zero
(hollow ring), and a step whose patch was not read is hatched.

* **Optimises:** "which step changed this file, and what did the fix step
  change after the review?" No other variant answers that.
* **Costs:**
  * **Phase 1 needs nothing new:** one content read per step that wrote a
    patch, in parallel and capped.
  * `git.files` per step, so the cells need no read.
  * **Line-level attribution** needs the integrator to record which input
    patch each hunk came from (`integrated_from`). Steps diff against
    different bases, so their line numbers cannot be lined up client-side.
    The mock shows that column as "not recorded".
* **Risks:**
  * N reads for N steps.
  * With `carrier: branches`, a dependant already holds its parent's work,
    so the same change can appear twice
    ([dispatch-and-integration.md](dispatch-and-integration.md)).

## 3. Recommendation

**Build variant 2, use variant 5 as its workflow and run content, and add
variant 3's pinned findings once findings carry a line.**

* **Variant 2** fixes reach, which is what the owner named, without
  reopening the one-file-at-a-time decision. It reuses `DiffView` whole and
  needs no API to ship.
* **Variant 5** is what "Changes" means for a workflow, because a list of
  per-step patches is not a diff.
* **Variant 3's** gutter is the step after that, once the worker keeps a
  `file` and `line` per finding.
* **Variants 1 and 4 stay as the record:** 1 reverses an owner decision, and
  4 overlaps the page in the way the owner already turned down for logs.

Click counts with the recommendation, from the frames:

| From | Today | Recommended |
|---|---|---|
| Agents | 2 + scroll | 2 |
| Workflow | 3 | 1 |
| Issue run | 2-3, after guessing the step | 1 |

**Syntax highlighting** is the owner's call, given the bundle (see
`questions.json`). The proposal is a budget of **+6 kB gz**, lazy-loaded with
the Changes tab, for a token-level highlighter that emits React text nodes
only. That rules out any `innerHTML` path, so Prism's `highlight()` and
Shiki's HTML output are out; Prism's `tokenize()` output or a small
hand-written lexer would do. These sizes are estimates. No build was run in
this lane, so they are not measured.

## 4. Tenant isolation and redaction

Every read in every variant goes through the existing routes:

* `GET /v1/tasks/{id}/artifacts/content`: tenant-scoped, redacted by window;
* `GET /v1/tasks/{id}/artifacts/raw`: the same check, redacted text.

No variant reads GCS directly or uses a signed URL. A masked secret stays
`********`, with the masked count shown. A workflow or run view is composed
client-side from per-task reads, so each read passes its own tenant check. If
a combined `/v1/workflows/{id}/changes` route is ever added, it must call the
same per-task service path, never a bucket listing. A task whose publish was
refused for a credential shows **withheld** in every entry point, with no
lines.

## 5. Lane plan (after the owner picks)

Territory follows files, and two issues that edit one file are one lane.

| Lane | Territory | Work | Depends on |
|---|---|---|---|
| **DIFF1 · viewer** | `apps/swarm-ui/src/diff/*`, its tests in `src/__tests__/diff/` | full-height mode; `initialFile` and `onFileChange`; a PR-link slot in the bar; a per-step source header for variant 5; the highlighter, if approved, behind the budget | none |
| **DIFF2 · agent entry** | `AgentSplit.tsx`, `AgentDetail.tsx` (Code card), `paths.ts`, `App.tsx` | the Changes tab and route `/agents/<tab>/<id>/changes`, folding the list on open, "Changes ›" in the Code card, and the withheld/zero/omitted states | DIFF1 |
| **DIFF3 · workflow and run entry** | `Workflows.tsx`, `WorkflowViews.tsx`, `Runs.tsx`, `RunSteps.tsx`, plus a new `WorkflowChanges.tsx` | the workflow and run Changes tab (variant 5 matrix), and the step inspector link | DIFF2 (routes) |
| **DIFF4 · data** | `apps/agent-worker/agent_worker/gitops.py`, `apps/swarm-ui/src/types.ts` (`GitSummary`) | keep per-file numstat as `git.files` (path, old_path, status, insertions, deletions, binary), capped with `files_truncated`. `result_summary` is `dict[str, Any]` in the frozen contract (`models.py:334`), so this is **not a contract change** | none; DIFF2 and DIFF3 use it once it lands, and fall back to a dash |
| later · findings | `agent_worker/verdict.py`, `wfreview.ts` | keep `file`, `line`, `side` and the patch digest per finding, for variant 3 | owner pick |

Every lane runs `scripts/changed-guards.sh`. DIFF1 keeps the existing
50,000-line windowing test and the no-`innerHTML` test green.
