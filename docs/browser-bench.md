# Browser benchmark: Playwright MCP vs dev-browser

Owner decision, 2026-10-14 (part of #1030): dev-browser v1 is built in
attach mode, CLI flavour, **opt-in** beside the Playwright MCP, and the
Playwright MCP **stays the default**. "A benchmark decides any default flip
later." This is that benchmark: the same ten real browser prompts, run with
each tool, with success checked by machine rather than taken from the agent's
own claim.

* `scripts/browser-bench.sh` submits a batch and later collects it.
* `scripts/browser_bench.py` holds the prompts, the plan and every number;
  `tests/unit/scripts/test_browser_bench.py` tests it offline.

Building the benchmark is not running it. A batch costs real money (below)
and **must wait for the live gate's CDP policy change** (the separate GATE
lane): until it is deployed the gate refuses `Performance.enable` and
`Audits.enable`, which Puppeteer sends when it opens a page, so the
dev-browser arm cannot open a page through the gate and would score zero for
a reason that has nothing to do with the tool.

## The two arms differ by one sentence

Both arms submit `claude-code-browser` with the same prompt. The tool is
chosen **only** by the sentence put in front of it:

| arm | sentence |
|---|---|
| `playwright-mcp` | Use the Playwright MCP browser tools for every browser action. |
| `dev-browser` | Use the dev-browser CLI for every browser action. |
| `jev` (opt-in) | Use the jev browser tools (browser_do, browser_read and the other `jev` tools) for every browser action. |

`jev` (JEV-RUNNER) is not in a default plan. Its tools exist only for a
submitter or tenant with a TypeSafe key
([design/live-browser.md §3.2.2](design/live-browser.md)), and each run also
spends TypeSafe credits. Name it: `--tools playwright-mcp,dev-browser,jev`.

No API input selects the tool. A caller picks a runner profile by name and
sends a prompt (invariant 10); a "tool" field would be a backend parameter
from a caller. The run's label, `bench-<tool>-<prompt>-<rep>`, rides in the
agent's free-form `metadata.bench`, which selects nothing. A unit test holds
the arms' bodies equal except for that sentence and the label: anything else
that differed would be what the benchmark measured.

The arms are **interleaved**: for each repeat and prompt, one run per tool
back to back. Running all of one arm and then all of the other would compare
two different hours of Google's rate limiting and the test sites' load.

## Egress must be held fixed between the arms

Google answers by source address. A Google search from the proxied egress
(docs/browser-egress.md) and the same search from the cluster's NAT are
different experiments: one gets results, the other gets `/sorry`. Do not
change the tenant's egress setting, the proxy vendor, the pool or the region
during a batch, and record which egress the batch ran on in the PR or issue
that reports it. A batch whose egress changed half-way is discarded, not
averaged. Interleaving keeps both arms on the same egress at the same time;
it cannot help if the egress itself was changed.

## The ten prompts

Every prompt ends with the same instruction: finish with one line
`RESULT: <answer>` in the stated format. The check reads the **last** such
line and requires its value to fully match the prompt's pattern
(`PROMPTS` in `scripts/browser_bench.py`; the patterns live there, once).

| id | kind | agent | success criterion (the `RESULT` value) |
|---|---|---|---|
| `google-python-release` | Google search | search Google for the Python 3.12.0 release date | `2023-10-02` |
| `google-rfc-http-semantics` | Google search | search Google for the RFC titled "HTTP Semantics" | `RFC 9110` |
| `saucedemo-checkout` | multi-page form | sign in to saucedemo.com with the public test user the login page prints, add the backpack, check out over the two-step form, finish | `Thank you for your order! \| $29.99` |
| `httpbin-pizza-form` | multi-page form | fill httpbin.org's order form (text, radio, checkboxes), submit, read the echo page | `custname=Bench Runner; size=medium` |
| `rfc9110-large-page` | large page read | find two section numbers in RFC 9110's single ~0.5 MB HTML page | `15.5.19; 8.6` |
| `herokuapp-new-tab` | multi-tab | click a link that opens a second tab, read both tabs' headings | `New Window / Opening a new window` |
| `github-signin-handoff` | sign-in hand-off | ask a person to sign in to GitHub through `swarm-handoff`, then read the account's user name | `login=<a valid GitHub user name>`, **and** the agent's events show the hand-off returned |
| `recaptcha-demo-handoff` | CAPTCHA hand-off | fill Google's reCAPTCHA demo form, ask a person to solve the CAPTCHA through `swarm-handoff`, submit | `Verification Success... Hooray!`, **and** the hand-off returned |
| `pypi-requests-license` | lookup | read the license from the requests project page on PyPI | Apache 2.0, as PyPI writes it |
| `herokuapp-dynamic-loading` | dynamic page | start a delayed load and wait for the element it renders | `Hello World!` |

The answers were chosen to be stable: release dates, RFC numbers and test
fixtures do not change between batches. The two hand-off prompts need a
person watching the console; run them when someone is, or a hand-off that
expires unanswered scores as the tool's failure.

### What counts as a success

A run is a success only when **all** of these hold:

1. the agent `SUCCEEDED`;
2. the `RESULT` value passes the prompt's check;
3. the agent drove the **live** browser: the agent's gate action log
   (`GET /v1/tasks/{id}/live/actions`) shows at least one CDP method the gate
   forwarded. An answer produced in a hidden browser of the agent's own is
   not a live-browser success. That is exactly what happened on
   task_7cda0d5335f049b4b442, and per the operator's assessment it is
   dev-browser's behaviour when it is not attached: without `--connect` it
   looks for Chrome on `PATH`, in `~/.cache/ms-playwright`, in its own
   `dev-browser install` download or at `DEV_BROWSER_CHROME`, and never reads
   `PLAYWRIGHT_BROWSERS_PATH`, so today's live-browser-only stub does not
   stop it. An agent with no action log at all is counted as not live;
4. for a hand-off prompt, a `human_control_returned` event.

A run whose agent has not finished counts **neither way**: `--collect` says how
many are unfinished, and is run again later.

## The pass bar

* **≥ 8/10 prompts done in the live browser.** A prompt is done for a tool when
  most of its repeats succeeded (2 of 3 at the default).
* dev-browser becomes the default only if, in addition, it is **at least as
  reliable** as Playwright MCP (success rate not lower) **and** its median
  $/task **and** median turns are **lower**.

The same bar holds `jev` against Playwright MCP, with its own verdict line.

`summary.md` ends with a verdict line computed from those rules: `BAR MET`,
`KEEP PLAYWRIGHT MCP` with the reasons, or `NOT DECIDED` when a figure the bar
needs was not measured on both arms. The verdict is the data's; flipping the
default is the owner's decision.

Its first consequence (batch `bench3-2026-10-23`, 10 prompts x 3 tools x 3
repeats, 83 runs measured: Playwright MCP 8/10 at a median $0.14 and 44.5 s a
run, dev-browser 7/10, Jev 6/10 at $0.17 and 51.3 s): BGD decided on
2026-10-23 that Visual QA's goal-driven flows default to Playwright, and a
target or one run may choose Jev
([visual-qa-any-app.md §4.1](design/visual-qa-any-app.md#41-the-steps)).

## What is measured, and where it comes from

| column | source |
|---|---|
| `success`, `check`, `result` | the agent's state, its answer (`GET /v1/tasks/{id}/answer`), the prompt's pattern |
| `live_browser` | forwarded CDP methods in `GET /v1/tasks/{id}/live/actions` |
| `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | summed over `GET /v1/tasks/{id}/attempts`, as `scripts/bench-cost.sh` reads them |
| `turns` | the answer's `num_turns`, else `result_summary.runner.usage.num_turns` |
| `duration_s` | `started_at` to `completed_at` |
| `handoffs_requested`, `handoffs_returned` | `human_handoff_requested` and `human_control_returned` in `GET /v1/tasks/{id}/events` |
| `cdp_method_refused` | action-log lines with `outcome: refused` (the gate records every refusal by method name, `cdpproxy.action_line`) |
| `google_sorry` | transcript steps (`GET /v1/tasks/{id}/transcript`) naming a Google `/sorry` page, "unusual traffic" or an HTTP 429 |
| `google_sorry_live` | Google `/sorry` navigations in the action log, by `scripts/live_report.py`'s rule |

A figure the API did not serve is **`not measured`**, never 0: an attempt
that reported no cost did not cost nothing, and an agent with no action log had
no gate refusals *measured*, not none.

## How to run it

```bash
# What would be submitted, and what it would cost. Calls nothing.
scripts/browser-bench.sh --dry-run

# Submit (asks for a typed confirmation; it spends money).
scripts/browser-bench.sh --batch devb-2026-10-20

# Later, when the tasks have finished: TSV and markdown per tool.
scripts/browser-bench.sh --collect devb-2026-10-20
```

`--repeats N`, `--prompts a,b` and `--tools playwright-mcp,dev-browser` narrow
a batch; `--usd-per-task` changes the estimate. Batches live under
`${SWARM_BROWSER_BENCH_DIR:-~/.swarm/browser-bench}/<batch>/`: `runs.tsv`
(label, tool, prompt, repeat, task id), the fetched `tasks/`, and, after
`--collect`, `results.tsv`, `results-<tool>.tsv` and `summary.md`. A
submission that failed half-way is resumed with the same `--batch`: a label
already in `runs.tsv` is not submitted again.

## What it costs

The measured median cost of one agent is **$0.66**. The default batch is
10 prompts × 2 tools × 3 repeats = **60 runs**, so:

* at the median: 60 × $0.66 = **$39.60**;
* browser runs have a long tail (a hand-off waits, a page loops), so budget
  twice that: 2 × $39.60 = **$79.20**.

That is the ~$40-80 a batch costs. `--dry-run` prints the same arithmetic for
whatever narrower batch is asked for (`--repeats 1` is 20 runs, $13.20), and
`--collect` reports what each run actually cost.
