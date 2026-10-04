/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440 and 390): Work ›
 * Runs and one run.
 *
 *   D3   The Linked and Read-from-issue cards let the issue link, the planner
 *        task id and the workflow id run past the card (x1448 in a card ending
 *        at 1408): a grid item's min-width is its content's, and a nowrap
 *        reference is wide content. Every card shrinks to its track and every
 *        id is one line, cut, with its whole text as the title.
 *   D11  The heading of an old run is its reference, which wrapped at the
 *        owner's hyphen; such a run printed "not kept: …" five times; the state
 *        was ALL CAPS; the list's By column was cut and the table was 1085px in
 *        1056. The reference is one line, the card says one short honest line,
 *        the state is a sentence-case pill, and the list fits.
 *
 * MUTATIONS: drop `min-width: 0` from the cards, take a title off an id, set
 * the pill's word back to the API's state, print the five absent rows again,
 * or drop the list's fixed layout -- each turns a case red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { fixedColumns } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'c3'.repeat(32)
const REF = 'bogdan-alexandrescu/SwarmCloud#454'
const URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454'

/** The planner's summary as the owner measured it: one long paragraph. */
const LONG_SUMMARY =
  'Build the issue intake end to end. The run page leads with the issue and keeps what the preview read. ' +
  'The plan is drawn from its schema rather than as one paragraph. ' +
  Array.from({ length: 24 }, (_, i) => `Detail ${i + 1} of the change touches another part of the console and its tests.`).join(' ')

const ISSUE_READ = {
  title: 'Issue intake: plan an issue, show the plan, run it once approved',
  labels: ['enhancement', 'Where: Runs'],
  state: 'open',
  comments: 4,
  url: URL,
  body: 'The console should take a GitHub issue and plan it.\n',
  body_truncated: false,
  body_redacted: false,
  read_at: '2026-10-03T14:02:00Z',
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: 'run_d13a2f1b7e1e4eafabfb', tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner1',
    plan: {
      summary: LONG_SUMMARY,
      steps: [
        { step_id: 'impl-plan-schema-and-overlaps', title: 'api: the plan schema and its overlaps',
          prompt: 'Extend PlanSpec.\nAdd overlaps.\nWrite the tests first.', depends_on: [] },
        { step_id: 'impl-forge-write-back', title: 'worker: write the plan back to the issue',
          prompt: 'Post the plan comment.', depends_on: ['impl-plan-schema-and-overlaps'],
          files: ['apps/agent-worker/agent_worker/forge.py'], tests: ['tests/unit/worker/test_forge_comment.py'],
          estimate: 'about 40 min' },
      ],
      overlaps: ['PR #498 also edits Runs.tsx'],
      risks: ['The forge token must never reach a log line.'],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    plan_shape: '2 steps in 2 stages, then review and fix',
    created_by: 'operator@example.com', created_at: '2026-10-03T14:02:00Z', updated_at: '2026-10-03T14:09:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-10-03T14:02:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: ISSUE_READ,
    issue_read_error: null,
    ...over,
  }
}

function serve(body: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const ok = url === '/v1/runs/run_d13a2f1b7e1e4eafabfb'
    return new Response(JSON.stringify(ok ? { run: body } : { code: 'not_found', message: url }), {
      status: ok ? 200 : 404, headers: JSON_HEADERS,
    })
  }) as unknown as typeof fetch
}

async function mount(served: unknown) {
  serve(served)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view="run=run_d13a2f1b7e1e4eafabfb" go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
}

function card(container: HTMLElement, name: string): HTMLElement {
  return within(container).getByRole('region', { name })
}

function fact(region: HTMLElement, key: string): HTMLElement {
  const li = [...region.querySelectorAll<HTMLElement>('.ctl-fact')].find((f) => visible(f.querySelector('b')) === key)
  expect(li, `no "${key}" fact`).toBeDefined()
  return li!
}

async function mountList(runs: unknown[]) {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ runs, next_page_token: null }), { status: 200, headers: JSON_HEADERS })) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={null} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-table')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('D3: no id runs past its card', () => {
  it('lets every side card shrink to its track and cuts each id with its title', async () => {
    const { container } = await mount(run({ workflow_id: 'wf_9c4be0e52b3a4cbe9f1f7a3d0c2e8b61', planner_task_id: 'task_7f3e2c9a1b8d4e6f0a5c3b2d1e9f8a7c' }))
    const linked = within(container).getByRole('region', { name: 'Linked' })
    const read = within(container).getByRole('region', { name: 'Read from the issue' })
    for (const c of [linked, read]) {
      expect(painted(c, 'min-width', WIDE), c.getAttribute('aria-label')!).toBe('0')
      expect(painted(c, 'overflow-wrap', WIDE) ?? 'normal').not.toBe('anywhere')
    }
    const ids = [...linked.querySelectorAll<HTMLElement>('.ctl-fact > a')]
    expect(ids.length).toBe(3)
    for (const a of ids) {
      expect(a.getAttribute('title'), a.textContent!).toBe(a.textContent)
      expect(painted(a, 'white-space', WIDE)).toBe('nowrap')
      expect(painted(a, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(painted(a, 'overflow', WIDE)).toBe('hidden')
      expect(painted(a, 'min-width', WIDE)).toBe('0')
    }
  })
})

describe('D11: an old run reads honestly and the state is a pill', () => {
  it('keeps a reference heading on one line', async () => {
    await mount(run({ issue_read: null }))
    const h1 = await screen.findByRole('heading', { level: 1, name: REF }, WAIT)
    expect(painted(h1, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(h1, 'text-overflow', WIDE)).toBe('ellipsis')
  })

  it('says once, briefly, that an old run kept no copy of the issue', async () => {
    const { container } = await mount(run({ issue_read: null }))
    const read = within(container).getByRole('region', { name: 'Read from the issue' })
    expect(read.querySelectorAll('.ctl-fact.is-absent').length).toBe(0)
    const note = read.querySelector('.rn-read-none')!
    expect(visible(note)).toBe('Not kept: this run is older than the issue snapshot.')
    expect((visible(read).match(/not kept/gi) ?? []).length).toBe(1)
  })

  it('draws the state as a sentence-case pill on the page and in the list', async () => {
    const { container } = await mount(run({ state: 'RUNNING', workflow_id: 'wf_1' }))
    const pill = container.querySelector('.rn-state .c-pill')!
    expect(pill).not.toBeNull()
    expect(visible(pill)).toBe('Running')
    expect(visible(container.querySelector('.rn-state'))).not.toMatch(/RUNNING/)
  })

  it('fits the list in a 1056px card, with the issue title and every cell cut with its title', async () => {
    const { container } = await mountList([run({ state: 'DONE', created_by: 'bogdan@sagadigital.example.com', workflow_id: 'wf_9c4be0e52b3a4cbe9f1f7a3d0c2e8b61' })])
    const table = container.querySelector<HTMLTableElement>('.rn-table')!
    const { width, cols } = fixedColumns(table, 1056, WIDE)
    expect(width).toBe(1056)
    expect(cols[cols.length - 1]!.end).toBeLessThanOrEqual(1056)
    const row = container.querySelector('.rn-row')!
    expect(visible(row.querySelector('.c-pill'))).toBe('Done')
    expect(visible(row)).not.toMatch(/DONE/)
    expect(visible(row.querySelector('[data-label="Issue"]'))).toContain(ISSUE_READ.title)
    for (const cell of row.querySelectorAll<HTMLElement>('.rn-cut')) {
      expect(cell.getAttribute('title'), cell.textContent!).toBe(cell.textContent)
      expect(painted(cell, 'text-overflow', WIDE)).toBe('ellipsis')
    }
    expect(row.querySelectorAll('.rn-cut').length).toBeGreaterThanOrEqual(4)
  })
})
