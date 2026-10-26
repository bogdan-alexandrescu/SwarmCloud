# Suite gate: a repository's whole Playwright suite, sharded, in SwarmCloud

Lane PLAYWRIGHT-GATE, part of #2253. BGD's decision of 2026-10-26: SwarmCloud
runs a repository's whole Playwright suite as a sharded gate. Then a project can
gate a merge train in SwarmCloud instead of on a laptop. The first project is
Product Oracle (`bogdan-alexandrescu/product-oracle`, about 1,230 mock specs).
Code: `apps/swarm-mcp/swarm_mcp/suitegate.py` and the bridge tool
`swarm_suite_gate`. Tests: `tests/unit/mcp/test_suite_gate.py`.

It depends on FIX-2253, which gives plain `claude-code` Chromium's system
libraries and fonts. Until that image is deployed, every shard fails at
`npx playwright install chromium` or at launch (`libglib-2.0.so.0` missing).
Each failing shard writes no blob, so the gate reads FAILED and never green.

## 1. What it is, and why it is a workflow

Invariant 10 says a caller names a runner profile and supplies data. It never
supplies an image, a command line or resources. So the gate is not a runtime.
It is a workflow that the bridge renders from a few named values and submits
like any other:

```json
{"repository": "bogdan-alexandrescu/product-oracle", "ref": "train/42",
 "suite": "playwright", "working_dir": "web", "shards": 8,
 "project": "chromium", "env_file": "web/.env.mock"}
```

| argument | what it is | checked |
|---|---|---|
| `repository` | `owner/repo`, registered by your workspace | an unregistered one is refused and nothing is sent |
| `ref` | the branch, tag or commit SHA to test | plain ref characters only, no leading `-`, no `..` |
| `suite` | `playwright` (the one suite today) | enum |
| `working_dir` | the directory with `package.json` and the Playwright config; default the root | a relative path of plain names, no `..` |
| `shards` | 1 to 20, default 8 | anything else is refused |
| `project` | a Playwright project name, passed as `--project=<name>` | a plain name |
| `env_file` | a repo-relative file of **non-secret** env the suite needs, such as a mock server's URL | a relative path of plain names |

The shell lines each agent runs are written by the bridge from those values.
That is why every value must match a plain-name or relative-path pattern: no
caller string can add a command to a line an agent runs. A secret never goes in
`env_file`. The file is in the repository, and a repository here is public.

## 2. What it runs

`shards` = N steps on `claude-code` (its `standard` class, by leaving the class
unnamed, at model class `sonnet`) and one `report` step:

* **shard k of N** runs these from `working_dir`: `npm ci`, then
  `npx playwright install chromium` (never `--with-deps`, because the image
  carries the libraries and the agent is not root), then
  `npx playwright test --shard k/N --retries=1 --reporter=blob,line`. When
  `env_file` is named, the same command line first loads it. The shard copies
  its blob report to its artifacts as `playwright-blob-k-of-N.zip`. It runs
  the command once, and `--retries=1` is Playwright's own retry of a flaky
  test. Its prompt says never to fix code, never to edit a test and never to
  commit.
* **report** depends on every shard and stages each shard's blob by name
  (`input_from`). It merges the blobs with `npx playwright merge-reports`
  twice, once as HTML and once as JSON. Then it writes these files to its
  artifacts:
  * `playwright-report.zip`, the merged HTML report;
  * `results.json`, the merged JSON report;
  * `summary.json`, holding `passed`, `failed`, `flaky`, `skipped`, `shards`
    and `failures`, which gives each failure's spec, title and first error
    line;
  * `summary.md`, whose first line is `PASSED` or `FAILED`, followed by the
    counts and one line per failure.

  Its final message is `summary.md`'s text.

Every step sets `allow_empty_diff`, the strategy is `collect` and `merge` is
`off`. So the gate never opens a pull request and never lands anything.

**A failing test is not a failing step.** A shard whose tests fail still writes
its blob and succeeds, so the report runs and names the failures. **A shard
that writes no blob is a failing step.** That happens when `npm ci` or the
install failed or Chromium did not start. The platform fails the attempt for
the missing output and retries it. Under the default `fail_workflow`, the
report is then cancelled. A slice nobody ran therefore turns the gate FAILED,
never green.

## 3. Reading the answer

`swarm_suite_gate` returns as soon as the workflow is accepted. The reply has
every step's task id and a `suite_gate` block that names the report step. Then:

1. Follow it with `swarm_workflow_status` (or `swarm_follow`).
2. When it ends, `swarm_result` on the `report` step's task id gives
   `summary.md`'s text.
3. `swarm_artifact` reads `summary.json` for a program and
   `playwright-report.zip` for a person.

| the workflow ends | `summary.json` | the gate |
|---|---|---|
| SUCCEEDED | `failed == 0` | green |
| SUCCEEDED | `failed > 0` | red: the failures are listed |
| FAILED (a shard or the report) | none | red: part of the suite did not run, so nothing can be said |
| SUCCEEDED | missing | red: the report merged nothing (a blob it was given was absent, or it stopped early), and a step with no dependants is not failed for a missing output |

Flaky tests (failed once, passed on retry) are counted and listed, and do not
turn the gate red. A project that wants them to should read `flaky`.

## 4. What it costs, and how to pick N

These are **estimates, not measurements**. No gate has run yet, because it
waits on FIX-2253's image. Replace them with the first runs' numbers.

One shard costs a fixed **F** plus its slice of the suite:

* F is roughly 3 to 4 minutes:
  * dispatch to running is about 25 s on GKE Autopilot (p50 23 s, measured
    for claude-code);
  * `npm ci` takes 1 to 2 minutes;
  * the Chromium download takes about 30 s;
  * the agent's own turns take about 1 minute.
* Its slice is **T / N**. T is the suite's run time at Playwright's default
  worker count on a 4-CPU `standard` pod (2 workers).

So:

* **wall-clock ≈ F + T/N + R**, where R ≈ 3 minutes is the report step, which
  runs its own `npm ci` and merge.
* **unit-minutes ≈ N × (F + T/N) + R = N × F + T + R**, at 1 unit per step
  (`standard`). While it runs, the gate holds N + 1 units of the workspace's
  ceiling, because concurrency counts from LEASED (invariant 3).
* **model tokens**: N + 1 short `sonnet` agents. Each runs a handful of
  commands and reads their output, which is estimated at 20 to 50 thousand
  input tokens (mostly cached) and 2 to 5 thousand output tokens. The model is
  a small share of the cost next to the unit-minutes.

Product Oracle's 1,230 mock specs at about 2 s each on 2 workers give T ≈ 20
minutes:

| N | wall-clock | unit-minutes | units held |
|---|---|---|---|
| 4 | ≈ 3.5 + 5 + 3 ≈ 12 min | ≈ 14 + 20 + 3 ≈ 37 | 5 |
| 8 (default) | ≈ 3.5 + 2.5 + 3 ≈ 9 min | ≈ 28 + 20 + 3 ≈ 51 | 9 |
| 16 | ≈ 3.5 + 1.3 + 3 ≈ 8 min | ≈ 56 + 20 + 3 ≈ 79 | 17 |

**Picking N.** Each shard adds F unit-minutes and saves T/N − T/(N+1)
minutes of wall-clock. Stop adding shards when the slice T/N falls to about F.
Past that point you mostly pay for `npm ci` and Chromium downloads. So pick
**N ≈ T / F**, capped by what the workspace can hold at once (N + 1 units)
and by 20. A small suite (T under 5 minutes) wants 1 or 2 shards. The
1,230-spec suite wants 5 to 8.

## 5. Wiring it into a merge train

The gate posts no GitHub check, so neither train can read it as a required
check (docs/merge-trains.md). The platform train judges the base's required
checks at the composed head. The project's merge train, the session or
script that composes it, runs the gate and judges by the gate's answer:

1. Compose the train's head and push it as a branch, or take its SHA. Pass
   that as `ref`. A SHA pins exactly what was tested.
2. Call `swarm_suite_gate` with it, and keep the workflow id.
3. Land the train only when the workflow is SUCCEEDED **and**
   `summary.json` exists **and** its `failed` is 0. On red, eject or bisect as the train's own
   rules say, using `failures[].spec` to name what broke.
4. Keep `playwright-report.zip` with the train's record, so the person who
   reads a red train sees the merged report rather than eight partial ones.

Until the gate posts a check of its own, which would be a change to the
platform train and is not part of this lane, a train that requires GitHub
checks still needs its CI to run them. The gate replaces the laptop run, not
the base's protection.

## 6. What this does not do

* It runs Playwright only. `suite` is an enum so that a second runner is a new
  entry, not a new tool.
* It posts no GitHub check and opens no pull request.
* It never fixes a failure. A fix is a separate lane, given the gate's
  `summary.json`.
* It does not run on `claude-code-browser`. A headless suite needs no watched
  browser, and the browser pool is the scarcer one.
