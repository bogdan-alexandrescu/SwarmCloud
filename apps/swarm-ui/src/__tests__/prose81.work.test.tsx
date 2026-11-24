// ISSUE 81, THE WORK PAGES: NO EXPLANATORY RUN ON THE SURFACE, EVERY SENTENCE
// STILL IN THE DOCUMENT (the owner's standing rule, 2026-11-05).
//
// Overview, Work › Timeline, the three submit forms and the agent page each
// drew a sentence or two of explanation as a visible run (proseruns.ts: nine
// words or more, or two sentences or more). Each is now a short line, or
// nothing, on the surface, and the sentence it was sits behind the (i) beside
// it (`[data-help-description]`), word for word. A must-stay disclosure -- that
// nothing runs before admission, whether anything is pushed, the fix-round
// cap, an auto-merge answer the API did not give -- stays visible as ONE short
// line.
//
// The App's pages are mounted at their addresses on the fixture build, as the
// prose sweep mounts them (proseapp.tsx); the issue form's states after a click
// (a read issue, the API's answers, a refusal) are mounted live against a
// stubbed fetch, as intake.issue.test.tsx mounts them.
//
// BREAK IT: put any listed sentence back on the surface, or drop it from its
// tip -- the case for its page turns red.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { longRuns } from './proseruns'
import { screenLoaded } from './screenLoaded'

/** What each render left mounted, unmounted after each case and between a case's mounts. */
const mounted: Array<() => void> = []
function unmountAll(): void {
  while (mounted.length > 0) mounted.pop()!()
}

const collapse = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()

/** What a reader sees: the document's text with the help copy taken out. */
function surface(root: Element = document.body): string {
  const clone = root.cloneNode(true) as Element
  for (const el of [...clone.querySelectorAll('[data-help-description]')]) el.remove()
  return collapse(clone.textContent)
}

/** The help copy: every (i)'s tip and help description, as text. */
function behind(root: Element = document.body): string[] {
  return [...root.querySelectorAll('[data-help-description]')].map((el) => collapse(el.textContent))
}

/** No visible long run contains `fragment`. */
function noLongRun(fragment: string): void {
  const hits = longRuns(document.body).filter((r) => r.includes(fragment))
  expect(hits, `a long run still shows "${fragment}"`).toEqual([])
}

/** `sentence` is no longer on the surface, and is word for word behind a mark. */
function moved(sentence: string): void {
  noLongRun(sentence.slice(0, 24))
  expect(surface(), `"${sentence}" is still on the surface`).not.toContain(sentence)
  expect(behind().some((t) => t.includes(sentence)), `"${sentence}" is behind no mark`).toBe(true)
}

/** `line` is on the surface, is one short line, and is no long run. */
function stays(line: string): void {
  expect(surface(), `"${line}" left the surface`).toContain(line)
  expect(longRuns(document.body).filter((r) => r.includes(line)), `"${line}" is part of a long run`).toEqual([])
}

// ---------------------------------------------------------------------------
// The App's pages, on the fixture build
// ---------------------------------------------------------------------------

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700)
    })
  }
}

function openAt(open: string): void {
  if (open.startsWith('/')) {
    window.location.hash = ''
    window.history.replaceState(null, '', open)
  } else {
    window.history.replaceState(null, '', '/')
    window.location.hash = `#${open}`
  }
}

async function page(open: string): Promise<void> {
  vi.resetModules()
  const { App } = await import('../App')
  const { FIXTURE_CLOCK } = await import('../api')
  localStorage.clear()
  sessionStorage.clear()
  vi.setSystemTime(new Date(FIXTURE_CLOCK))
  openAt(open)
  mounted.push(render(<App />).unmount)
  await screenLoaded()
  await settle()
}

describe('issue 81: the work pages, on the fixture build', () => {
  let zone: string | undefined
  beforeAll(() => {
    zone = process.env.TZ
    process.env.TZ = 'UTC'
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
    window.location.hash = ''
    window.history.replaceState(null, '', '/')
    localStorage.clear()
  })
  afterEach(() => unmountAll())

  it('overview/now: the setup card, the failures, the quota, the unjudged workflows and the spend clause', async () => {
    await page('overview/now')
    // Onboarding: one short line; the two sentences behind its (i).
    moved('This card leaves Overview by itself once every step is done.')
    moved('Hide keeps every step as it is, for you in every browser;')
    stays('Leaves Overview once every step is done.')
    // The failures: terminal said as a fact, the explanation behind the card's mark.
    noLongRun('Terminal: nothing will retry')
    stays('Terminal')
    expect(behind().some((t) => t.includes('Terminal: nothing will retry them.')), 'the terminal sentence is behind no mark').toBe(true)
    // The quota: facts on the surface, the sentence behind the mark.
    noLongRun('rate-limit responses so far')
    expect(behind().some((t) => t.includes(' rate-limit responses so far; the ceiling this derives is '))).toBe(true)
    // The unjudged workflows.
    noLongRun('their rollup came back incomplete')
    expect(behind().some((t) => t.includes('Their rollup came back incomplete'))).toBe(true)
    // The spend foot.
    noLongRun('that hold no capacity)')
    expect(longRuns(document.body).filter((r) => /newest read/.test(r))).toEqual([])
  }, 60_000)

  it('work/timeline: the lanes line is facts, the sentences behind its (i)', async () => {
    await page('work/timeline')
    noLongRun('The lanes cover')
    noLongRun('How these are counted')
    expect(surface()).not.toContain('Parks and fences count only the lanes drawn.')
    expect(behind().some((t) => /^The lanes cover .+\. Parks and fences count only the lanes drawn\.$/.test(t))).toBe(true)
    expect(screen.getByRole('link', { name: 'How these are counted' })).toBeTruthy()
  }, 60_000)

  it('work/new: one short line that nothing runs before admission; each option says whether anything is pushed', async () => {
    await page('work/new')
    moved('Nothing runs until the scheduler admits it into every pool it needs.')
    stays('Nothing runs until the scheduler admits it.')
    noLongRun('No pull request. Nothing is pushed.')
    noLongRun('none if it changed nothing')
    stays('Nothing is pushed; no pull request.')
    stays('One pull request, from this agent’s branch.')
    expect(behind().some((t) => t.includes('An agent that changed nothing opens none'))).toBe(true)
  }, 60_000)

  it('work/new-workflow: the failure options, the priority and the QA entry are short, each sentence behind its (i)', async () => {
    await page('work/new-workflow')
    moved('Steps not started are cancelled; running steps finish. Default.')
    moved("Only the failed step's dependents are cancelled; other branches keep starting.")
    stays('Unstarted steps cancelled; running ones finish. Default.')
    stays('Cancels only the failed step’s dependents.')
    moved('A whole number from −100 to 100; 0 is the default. Higher starts first among your tenant\'s own work; it never wins your tenant more turns than another.')
    stays('−100 to 100; 0 is the default.')
    moved('Runs from the app’s QA target: tick its environments on the target’s card, and each runs as its own workflow, shown together on one run page.')
    stays('Runs from the app’s QA target.')
    stays('Nothing is pushed; no pull request.')
  }, 60_000)

  it('agent/details: the live-readings note is one line, the second sentence behind its (i)', async () => {
    await page('work/task/task_a073aff5')
    moved('Live readings from the attempt record. Final figures are written when the attempt ends.')
    stays('Live readings from the attempt record.')
  }, 60_000)
})

// ---------------------------------------------------------------------------
// The issue form, live against a stubbed fetch: before and after each click
// ---------------------------------------------------------------------------

const JSON_HEADERS = { 'content-type': 'application/json' }
const admission = { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] }
const pool = (name: string) => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10,
  active: 2, available: 8, enabled: true, updated_at: '2026-10-02T10:00:00Z',
})
const CAPACITY = {
  pools: [pool('global'), pool('tenant:eng'), pool('resource:standard')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng', 'resource:standard'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
}
const PROVIDERS = { tenant_id: 'eng', providers: [{ provider: 'anthropic', credential_registered: true }], generated_at: '2026-10-02T10:00:00Z' }

function preview(over: Record<string, unknown> = {}, top: Record<string, unknown> = {}) {
  return {
    issue: {
      ref: 'example-org/infra#512', owner: 'example-org', repo: 'infra', number: 512,
      url: 'https://github.com/example-org/infra/issues/512',
      repository_url: 'https://github.com/example-org/infra',
      title: 'A short title', body: 'A body.', body_truncated: false, body_redacted: false,
      labels: ['ui'], state: 'open', comments: 1,
      ...over,
    },
    tenant_id: 'eng',
    ...top,
  }
}

type Reply = { status: number; body: unknown }

function serve(previewReply: Reply, runReply: Reply = { status: 201, body: { run: { id: 'run_new' } } }, runs: Reply = { status: 200, body: { runs: [] } }): void {
  const reply = (r: Reply) => new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  const answer = (url: string, method: string): Response => {
    if (url.startsWith('/v1/capacity')) return reply({ status: 200, body: CAPACITY })
    if (url.startsWith('/v1/providers')) return reply({ status: 200, body: PROVIDERS })
    if (url.startsWith('/v1/issues/preview')) return reply(previewReply)
    if (url.startsWith('/v1/runs') && method === 'POST') return reply(runReply)
    if (url.startsWith('/v1/runs')) return reply(runs)
    return reply({ status: 404, body: { code: 'not_found', message: `no stub for ${url}` } })
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(answer(input instanceof Request ? input.url : input.toString(), init?.method ?? 'GET'))))
}

const WAIT = { timeout: 4000 }

async function mount(): Promise<void> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  mounted.push(render(<IssueSubmitScreen go={vi.fn()} />).unmount)
  await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
  await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
}

function readIssue(ref = 'example-org/infra#512'): void {
  fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: ref } })
  fireEvent.click(screen.getByRole('button', { name: 'Read' }))
}

const planButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Plan this issue' })

describe('issue 81: the issue form, before and after each click', () => {
  afterEach(() => {
    unmountAll()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('before a read: the credential, the runner, the approval, auto-merge, the fix-round cap and the plan note are short', async () => {
    serve({ status: 200, body: preview() })
    await mount()
    moved('Read with this workspace’s own forge credential: only repositories it can read are reachable from here. Nothing is created by reading.')
    stays('Read with this workspace’s forge credential.')
    moved('The planner and every step it plans run as claude-code.')
    stays('Planner and every step run as claude-code.')
    moved('Required (default): the run waits, planned and holding no capacity, until someone in this workspace approves, edits or rejects the plan.')
    stays('Required (default): waits for approval, holding no capacity.')
    moved('Decided when the issue is read: the API says then whether this run may merge its own pull request.')
    stays('Decided when the issue is read.')
    moved('1–5. Once the pull request is open, each red CI reading spends one fix round; past the cap the run fails with the failing checks’ excerpt.')
    stays('1–5: each red CI reading spends one round.')
    moved('A planner agent reads the issue and the repository and writes a plan; nothing else runs until the plan is approved.')
    stays('Nothing else runs until the plan is approved.')
  })

  it('after a click on Auto: the approval and the plan note are short', async () => {
    serve({ status: 200, body: preview() })
    await mount()
    fireEvent.click(screen.getByRole('radio', { name: /Auto/ }))
    moved('Auto: the plan is approved the moment it is read, and the work starts without anyone being asked.')
    stays('Auto: approved when read; starts without asking anyone.')
    moved('A planner agent reads the issue and the repository and writes a plan; the plan is approved the moment it is written and the work starts without anyone being asked.')
    stays('The work starts without anyone being asked.')
  })

  it('after a reference that does not parse: one short line', async () => {
    serve({ status: 200, body: preview() })
    await mount()
    fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: 'not a ref' } })
    noLongRun('Not an issue reference yet')
    stays('Not an issue reference yet.')
    expect(behind().some((t) => t.includes('name it as owner/repo#N or paste its URL'))).toBe(true)
  })

  it('after a read the API did not answer for auto-merge: one short could-not-check line', async () => {
    serve({ status: 200, body: preview({ body_truncated: true, body_redacted: true }) })
    await mount()
    readIssue()
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    moved('This API did not say whether it takes auto-merge, so the run is created with it off and a person merges its pull request.')
    stays('The API did not say; it stays off.')
    moved('The preview holds the first part of the body only; the API cut it. The planner reads all of it again.')
    stays('Only the first part of the body shows.')
    moved('Values that looked like credentials were masked by the API before this was served.')
    stays('Credential-like values were masked by the API.')
  })

  it('after a read the API refused auto-merge for, or offered it for', async () => {
    const reason = 'not compiled yet'
    serve({ status: 200, body: preview({}, { auto_merge: { available: false, reason } }) })
    await mount()
    readIssue()
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    moved('Not available for this run: the API refuses auto-merge now, so a person merges the run’s pull request.')
    stays('Not available for this run: a person merges.')
    stays(reason)
    unmountAll()

    serve({ status: 200, body: preview({}, { auto_merge: { available: true, reason: null } }) })
    await mount()
    readIssue()
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.click(screen.getByRole('switch', { name: 'Merge the pull request when it is ready' }))
    moved('once every required check is green, the merge step merges the pull request at the sha it proved.')
    stays('merges when required checks are green.')
  })

  it('after reading a closed issue: the heading stays, the why behind its (i)', async () => {
    serve({ status: 200, body: preview({ state: 'closed' }) })
    await mount()
    readIssue()
    await screen.findByText('This issue is closed.', undefined, WAIT)
    moved('Planning it would reopen work someone marked done.')
    stays('This issue is closed.')
  })

  it('after an issue with open runs, or a runs read that failed', async () => {
    serve({ status: 200, body: preview() }, undefined, {
      status: 200,
      body: { runs: [{ id: 'run_a', state: 'PLANNED', terminal: false, created_at: '2026-10-01T10:00:00Z', issue: { owner: 'example-org', repo: 'infra', number: 512 } }] },
    })
    await mount()
    readIssue()
    await screen.findByText(/^Open runs:/, undefined, WAIT)
    noLongRun('Planning it again starts another run.')
    stays('planning it again starts another run')
    unmountAll()

    serve({ status: 200, body: preview() }, undefined, { status: 500, body: { code: 'internal', message: 'boom' } })
    await mount()
    readIssue()
    await screen.findByText(/^Open runs not checked/, undefined, WAIT)
    moved('Whether this issue already has open runs was not checked.')
    stays('Open runs not checked')
  })

  it('after a submit the API answered without a run, or failed on: one short line each', async () => {
    serve({ status: 200, body: preview() }, { status: 201, body: {} })
    await mount()
    readIssue()
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.click(planButton())
    await waitFor(() => expect(surface()).toContain('Accepted, but no run was named.'), WAIT)
    moved('The API accepted this and named no run. Open Runs to find it before submitting again.')
    unmountAll()

    serve({ status: 200, body: preview() }, { status: 500, body: { code: 'internal', message: 'boom' } })
    await mount()
    readIssue()
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.click(planButton())
    await waitFor(() => expect(surface()).toContain('Check Runs before submitting again.'), WAIT)
    moved('This failed on a write, so the run may exist anyway.')
  })

  it.each([
    ['not_found', 'No issue #512 visible to this workspace.', 'GitHub gives the same answer for an issue that does not exist'],
    ['no_access', 'This workspace’s forge credential was refused.', 'It lacks read access to the repository'],
    ['is_pull_request', '#512 is a pull request.', 'This form plans issues; a pull request that needs finishing is an agent on its branch.'],
    ['no_forge_credential', 'Workspace eng has no forge credential.', 'It is never pasted here.'],
    ['read_failed', 'The read did not finish.', 'The state of issue #512 is unknown, so it is shown as unknown, not as open.'],
  ])('after a %s refusal: its heading on the surface, the rest behind its (i)', async (code, heading, rest) => {
    serve({ status: code === 'not_found' ? 404 : 403, body: { code, message: 'x' } })
    await mount()
    readIssue()
    await screen.findByRole('button', { name: 'About this refusal' }, WAIT)
    stays(heading)
    noLongRun(rest.slice(0, 24))
    expect(behind().some((t) => t.includes(rest)), `"${rest}" is behind no mark`).toBe(true)
  })
})
