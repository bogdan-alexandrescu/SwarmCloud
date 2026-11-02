// AUTOMATE › APPROVALS, VISUAL QA PASS 1 (#2596, lane QA1-FIX-39).
//
// WHAT EACH CASE HOLDS:
//   * automate-05: a kind filter keeps every chip's count, read off the whole
//     inbox, and an empty kind says how many wait under the other kinds, with
//     a link to Every kind;
//   * automate-22: a kind with nothing waiting draws no chip, unless it is
//     the one chosen;
//   * automate-06: a plan's steps are drawn on its decision page, numbered,
//     each with its prompt;
//   * automate-07: a decided item headlines what happened, not the question,
//     and its "What you approve" card is said in the past tense;
//   * automate-08: a superseded plan says its plan changed, never "head";
//   * automate-10: the reader reads "workspace", never "tenant";
//   * automate-23: a merge green at its head with no counts served says so
//     on one line (a missing verdict draws no row since pass 2, automate2-20).
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const HEAD = 'dbd00f0' + '6'.repeat(33)
const MAY = { approve: true, reject: true, why: null, deciders: ['members'] }

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#2364' },
    digest: { plan: 'p1' },
    summary: `example-org/example-api#2364: a plan waits (${id})`,
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-6),
    expires_at: hoursFromNow(66),
    may_decide: MAY,
    ...over,
  }
}

const INBOX = [
  item('apr_merge', { kind: 'merge', requested_at: hoursFromNow(-30) }),
  item('apr_plan'),
  item('apr_run', { kind: 'run' }),
]

/** The inbox, filtered by `?kind=` as the route filters it. */
function routes(inbox: Json[], detail: Json | null = null): Handler {
  return (m, url) => {
    const [path, query] = url.split('?') as [string, string | undefined]
    if (m === 'GET' && path === '/v1/approvals') {
      const kind = new URLSearchParams(query ?? '').get('kind')
      return { status: 200, body: { approvals: kind === null ? inbox : inbox.filter((i) => i.kind === kind), tenant_id: 'eng' } }
    }
    if (m === 'GET' && path.startsWith('/v1/approvals/') && detail !== null) return { status: 200, body: detail }
    if (m === 'GET' && path.startsWith('/v1/approvals/')) return { status: 404, body: { detail: { code: 'not_found', message: 'no such approval' } } }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, go }
}

const page = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

const chipWords = () =>
  [...within(screen.getByRole('group', { name: 'Kind' })).getAllByRole('button')].map((b) => visible(b))

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the kind chips (automate-05, automate-22)', () => {
  it('keeps every chip’s count on a filtered list, read off the whole inbox', async () => {
    await mount('kind=plan', routes(INBOX))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: count only the filtered read, and every other chip loses its number.
    await waitFor(() => expect(chipWords()).toEqual(['Every kind · 3', 'Run · 1', 'Plan · 1', 'Merge · 1']), WAIT)
  })

  it('draws no chip for a kind with nothing waiting, but keeps the chosen one', async () => {
    await mount('', routes(INBOX))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: draw all eight chips, five of them at 0.
    expect(chipWords()).toEqual(['Every kind · 3', 'Run · 1', 'Plan · 1', 'Merge · 1'])
  })

  it('says on an empty kind how many wait under the other kinds, with a link to Every kind', async () => {
    const { go } = await mount('kind=spec', routes(INBOX))
    const empty = await screen.findByTestId('apl-elsewhere', undefined, WAIT)
    // MUTATION: say only "Nothing waiting" while three wait elsewhere.
    expect(visible(empty)).toContain('3 items wait under other kinds.')
    const every = within(empty).getByRole('link', { name: 'Read every kind ›' })
    expect(every.getAttribute('href')).toBe('/automate/approvals')
    every.click()
    expect(go).toHaveBeenCalledWith('automate/approvals')
    await waitFor(() => expect(chipWords()).toEqual(['Every kind · 3', 'Run · 1', 'Plan · 1', 'Merge · 1', 'Spec · 0']), WAIT)
  })
})

describe('a plan’s decision page (automate-06, automate-07, automate-08)', () => {
  const plan = {
    summary: 'Split the parser and add its tests.',
    files: ['src/parse.py'],
    steps: [
      { step_id: 'parse', title: 'Split the parser', prompt: 'Move the tokenizer out of parse.py.' },
      { step_id: 'tests', title: 'Test the parser', prompt: 'Add a test for each token kind.' },
    ],
  }
  const detail = (over: Json = {}) => ({ approval: item('apr_plan', over), run: { id: 'run_8f2a', state: 'awaiting_approval', plan } })

  it('lists the plan’s steps, numbered, each with its prompt', async () => {
    await mount('approval=apr_plan', routes(INBOX, detail()))
    const d = await page()
    // MUTATION: draw only the summary and the files, and the steps are read nowhere.
    const steps = await within(d).findByRole('list', { name: 'The plan’s steps' }, WAIT)
    const li = [...steps.querySelectorAll('li')]
    expect(li).toHaveLength(2)
    expect(visible(li[0]!)).toContain('Split the parser')
    expect(visible(li[0]!)).toContain('Move the tokenizer out of parse.py.')
    expect(steps.tagName).toBe('OL')
  })

  it('headlines what became of a decided plan, not the question, and says its card in the past tense', async () => {
    const cases: [Json, string][] = [
      [{ state: 'rejected', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1), reason: 'too wide' }, 'This plan was rejected.'],
      [{ state: 'expired', expires_at: hoursFromNow(-1) }, 'This plan expired.'],
      [{ state: 'superseded' }, 'This plan was superseded.'],
      [{ state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1) }, 'This plan was approved.'],
    ]
    for (const [over, said] of cases) {
      const view = await mount('approval=apr_plan', routes(INBOX, detail(over)))
      const d = await page()
      // MUTATION: keep the question "Start the run on this plan?" once decided.
      await waitFor(() => expect(visible(d.querySelector('.ad-sent'))).toBe(said), WAIT)
      expect(visible(d)).not.toContain('Start the run on this plan?')
      // MUTATION: keep "What you approve" and its present tense over a decided item.
      expect(within(d).queryByRole('region', { name: 'What you approve' })).toBeNull()
      const asked = within(d).getByRole('region', { name: 'What it asked for' })
      expect(visible(asked)).toContain('It asked to start the run on this plan.')
      expect(visible(asked)).not.toMatch(/Approve sends back/)
      view.unmount()
    }
  })

  it('keeps the question and "What you approve" while the plan waits', async () => {
    await mount('approval=apr_plan', routes(INBOX, detail()))
    const d = await page()
    await waitFor(() => expect(visible(d.querySelector('.ad-sent'))).toBe('Start the run on this plan?'), WAIT)
    expect(within(d).getByRole('region', { name: 'What you approve' })).toBeTruthy()
  })

  it('says a superseded plan’s plan changed, never a head', async () => {
    await mount('approval=apr_plan', routes(INBOX, detail({ state: 'superseded' })))
    const d = await page()
    const bar = await within(d).findByTestId('ap-decided', undefined, WAIT)
    // MUTATION: the merge's words, "No newer request is served for the new head yet."
    expect(visible(bar)).not.toMatch(/\bhead\b/)
    expect(visible(bar)).toContain('Superseded: the run’s plan changed.')
    expect(visible(bar)).toContain('The new plan has its own request; none is served yet.')
  })
})

describe('the reader’s words (automate-10)', () => {
  it('says workspace, never tenant, in the list, the decision page and a missing item', async () => {
    const view = await mount('', routes(INBOX))
    const card = await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: "any member of this tenant".
    expect(visible(card)).toContain('any member of this workspace')
    expect(visible(document.body)).not.toMatch(/\btenant\b/)
    view.unmount()
    const v2 = await mount('approval=apr_plan', routes(INBOX, { approval: item('apr_plan') }))
    const d = await page()
    await waitFor(() => expect(visible(within(d).getByTestId('ad-who'))).toBe('any member of this workspace'), WAIT)
    expect(visible(document.body)).not.toMatch(/\btenant\b/)
    v2.unmount()
    await mount('approval=apr_x', routes(INBOX))
    await screen.findByText(/No approval apr_x in this workspace/, undefined, WAIT)
  })
})

describe('a merge’s checks (automate-23)', () => {
  const merge = (over: Json = {}) => ({
    approval: item('apr_merge', { kind: 'merge', digest: { head_sha: HEAD } }),
    merge: {
      green_sha: HEAD,
      pull_request: { number: 2392, url: 'https://github.com/example-org/example-api/pull/2392' },
      requirements_met: true,
      requirements_unmet: [],
      files_checked_head: HEAD,
      ...over,
    },
  })

  it('says green at the head and counts not served on one line, and draws no verdict it was not given', async () => {
    await mount('approval=apr_merge', routes(INBOX, merge()))
    const d = await page()
    const ci = await waitFor(() => {
      const row = d.querySelector<HTMLElement>('li[data-check="ci"]')
      expect(row).not.toBeNull()
      return row!
    }, WAIT)
    // MUTATION: draw "Checks at this head: —" above the green row again.
    expect(within(d).queryByTestId('ad-check-counts')).toBeNull()
    expect(visible(ci)).toContain('Required checks green at the head')
    expect(visible(ci)).toContain('green at dbd00f0; per-check counts not served')
    // Visual QA pass 2 automate2-20: a digest with no verdict draws no
    // Verdict row (approvals.qa2.test.tsx holds it); the head row stays.
    expect(within(within(d).getByRole('region', { name: 'What you approve' })).queryByText('Verdict')).toBeNull()
  })

  it('keeps the counts line, with its dash and reason, when the head is not green', async () => {
    await mount('approval=apr_merge', routes(INBOX, merge({ green_sha: null })))
    const d = await page()
    const counts = await within(d).findByTestId('ad-check-counts', undefined, WAIT)
    expect(visible(counts)).toContain('no check counts are served')
  })
})
