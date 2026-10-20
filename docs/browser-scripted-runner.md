# The scripted browser runner: what it waits for, and what it reports

`runner_profile: browser` runs `agent_worker/runners/browser.py`: no agent,
just Playwright and Chromium playing `input.url` and `input.actions`, then
extracting the page's text to `page.txt` and a screenshot to `final.png`. This
page records why it waits for what it waits for, and why some things that
used to fail an agent now only appear in its result. The inputs a caller may
give are unchanged by any of it (invariant 10): the catalogue declares them,
and nothing here added one.

## A page whose DOM is ready is a page that can be read

Measured 2026-10-15, QA batch full-1015, profile `browser` (#1363):

| case | before | why |
|---|---|---|
| S1-adheavy, S1-iframes | FAILED 3/3 | `Page.goto: Timeout 30000ms exceeded ... waiting until load` |
| S1-wikipedia | FAILED 3/3 | the final screenshot timed out in "waiting for fonts to load", with 111,583 chars already in `page.txt` |
| S1-cookie (stackoverflow.com) | SUCCEEDED 3/3 | with an empty `page.txt`, and nothing saying so |
| S1-example, S1-github, S1-news | passed | control |

The `load` event waits for every subresource: every ad, every iframe, every
tracker. On a page that keeps loading them it may never fire, although the
text the agent came for was parsed long before. So:

* **A goto waits for `domcontentloaded` unless the action names something
  else** (`DEFAULT_WAIT_UNTIL`). The action's `wait_until` is still honoured;
  it is not a new input.
* **A wait that times out after the navigation committed is "loaded enough".**
  Whatever the action asked for -- `load`, `networkidle` -- is stricter than
  extraction needs. The runner records it by code
  (`goto_load_timeout_after_commit`, in the result's `events` and on stderr),
  marks the action `load_timed_out: true` with `status: null` (there is no
  response object to read), adds a note, and goes on with the DOM as it is.
* **A navigation that never committed still fails**, with
  `action N (goto): the navigation never committed ...`. Committed means the
  page's URL moved off the one it had before the goto, or the main frame fired
  `framenavigated` during it (a same-URL reload commits without moving the
  URL). It is never read from the current document's readiness: on a second
  goto (A -> B) that document is still A, long since ready, and a B that never
  reached the site would pass as loaded and the run would extract A as a
  success. A page still on `about:blank` never reached the site either, and
  extracting it would report an empty success for a failure.

## A screenshot is evidence, not the product

Every screenshot -- the final one and a `screenshot` action -- is bounded at
`SCREENSHOT_TIMEOUT_MS` (15 s, below the 30 s action timeout) and taken with
animations disabled. Playwright waits for web fonts before it captures; a page
whose fonts never settle used to cost the whole agent.

A screenshot that fails is recorded as **missing**: a `screenshot_missing`
event, a note, `missing: true` on the action, and for the final one
`screenshot: {"artifact": "final.png", "missing": true, "error": "timeout"}`
in the result. The agent stays SUCCEEDED, because the text was extracted. The
one exception is an agent with `extract_text: false`: there the screenshot was
the only thing it was going to produce, so a failed final screenshot fails it.

## An empty page is stated, not hidden

Every result carries `page_text`:

```json
{"chars": 0, "title": "Just a moment...", "final_url": "https://...", "wall": "challenge"}
```

`chars` counts the extracted text without surrounding whitespace, and an
empty extraction writes no `page.txt`. The summary says
`...; 0 chars of text`, and a note says the extraction was empty, so a bot
wall or a consent wall shows in the agent list rather than hiding in a
SUCCEEDED row with an empty artifact.

`wall` is read from the page **title** alone -- `challenge` for "Just a
moment...", "Attention Required", "captcha" and the like; `consent` for
"Before you continue", "cookie", "consent" -- and is `null` otherwise. It is a
label for the reader, never a reason to fail or retry: a title match is a
guess, and a site titled "Cookie recipes" is not a wall. The lists are
`CHALLENGE_TITLES` and `CONSENT_TITLES` in the runner. The runner does not try
to get past a wall; that would be a different product, and one this platform
has not decided to build.

The tests are `tests/unit/worker/test_browser_scripted_load.py`, with a fake
Playwright page.
