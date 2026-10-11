/**
 * LANE C4P, THE RUNS LIST AND ONE RUN (QA pass on deployed dev, 2026-10-07,
 * findings G2-02, -06, -17, -18, -19, -30, -31, -32):
 *
 *   G2-02  the right rail's cards clipped their own content: `overflow: hidden`
 *          made each card's automatic minimum height 0, so the grid shrank the
 *          three to fit the sticky rail instead of letting the rail scroll.
 *   G2-06  the PR card said "checks green" beside "by check: passed — ·
 *          pending — · failed — · skipped —", repeated "· green" on the merge
 *          line, and ended its title in a bare "· —".
 *   G2-17  "Open agent →" sent a finished step's task to `/agents/live/<id>`,
 *          a list that does not hold it.
 *   G2-18  the Runs list had no filter or search, and Workflow was plain text.
 *   G2-19  on a phone each cell's label ended in a dangling " · ".
 *   G2-30  the folded plan said "1 step" next to "Steps · 3".
 *   G2-31  Progress was all "3d ago": no order or duration could be read.
 *   G2-32  the folded Overlaps heading was the browser's default h3 -- bigger,
 *          with its own top margin -- unlike every other card title.
 *
 * MUTATIONS: put `overflow: hidden` back on `.rn-side > .rn-card`; draw the
 * four dashes in "by check" again, or `· green` on merge, or the title's dash;
 * hard-code `/agents/live/` in OpenAgent; drop the state chips, the search or
 * the workflow link; put `' · '` back in the phone label; print `pluralise(n,
 * 'step')` alone; print `timeAgo` in Progress; drop the folded Overlaps h3
 * rule -- each turns a case red.
 */
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import COMPONENTS_CSS from '../styles/components.css?raw'
import INTAKE_CSS from '../styles/intake.css?raw'
import RUNS_CSS from '../styles/runs.css?raw'
import STYLES from '../styles.css?raw'
import { spanText } from '../duration'
import { cascade, type CascadeEnv } from './cssgate'
import { task } from './runfixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const HEAD = '7138b1a' + '0f'.repeat(16) + '9'
const OWNER = 'bogdan-alexandrescu'
const REF = `${OWNER}/SwarmCloud#72`
const ISSUE_URL = `https://github.com/${OWNER}/SwarmCloud/issues/72`
const PR_URL = `https://github.com/${OWNER}/SwarmCloud/pull/564`
const RUN_ID = 'run_c4p0a1b2c3d4e5f60718'
const WF = 'wf_c4p1b2c3d4e5f6071829'

const SHEETS = [STYLES, COMPONENTS_CSS, INTAKE_CSS, RUNS_CSS].join('\n')
const DESKTOP: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const painted = (el: Element, prop: string | string[], env: CascadeEnv, pseudo: string | null = null) => {
  const r = cascade(SHEETS, el, prop, env, pseudo)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

const PLAN = {
  summary: 'Fix the run page.',
  requirements: ['The rail scrolls'],
  overlaps: [{ ref: `${OWNER}/SwarmCloud#560`, kind: 'pull_request', note: 'No action: touches another screen.' }],
  steps: [{ step_id: 'impl', title: 'ui: the rail', prompt: 'Build it.', depends_on: [] }],
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'DONE', terminal: true,
    issue: { ref: REF, owner: OWNER, repo: 'SwarmCloud', number: 72, url: ISSUE_URL,
      repository_url: `https://github.com/${OWNER}/SwarmCloud` },
    plan_approval: 'required', auto_merge: true, fix_rounds: 3, planner_task_id: 'task_planner72',
    plan: PLAN, plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-04T20:00:00Z', updated_at: '2026-10-04T21:00:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-04T20:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null, green_sha: HEAD, requirements_met: true,
    pull_request: { number: 564, url: PR_URL, head_sha: HEAD, checks: 'green' },
    history: [
      { at: '2026-10-04T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-04T20:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
      { at: '2026-10-04T20:09:30Z', from: 'PLANNED', to: 'APPROVED', by: 'operator@example.com' },
    ],
    issue_read: { title: 'The rail clips', state: 'open', labels: [], body: 'Body.', body_truncated: false,
      body_redacted: false, comments: 0, read_at: '2026-10-04T20:00:00Z', url: ISSUE_URL },
    issue_read_error: null,
    ...over,
  }
}

function workflow(tasks = [
  task({ id: 'task_impl', state: 'SUCCEEDED', workflow_id: WF, step_id: 'impl', created_at: '2026-10-04T20:10:00Z',
    started_at: '2026-10-04T20:11:00Z', completed_at: '2026-10-04T20:30:00Z', updated_at: '2026-10-04T20:30:00Z',
    attempt_count: 1, max_attempts: 3 }),
  task({ id: 'task_review', state: 'PARKED', workflow_id: WF, step_id: 'review', created_at: '2026-10-04T20:10:00Z',
    started_at: null, updated_at: '2026-10-04T20:31:00Z', attempt_count: 1, max_attempts: 3, park_reason: 'QUOTA_EXHAUSTED' }),
]) {
  return {
    workflow: {
      workflow_id: WF, tenant_id: 'eng', state: 'SUCCEEDED', stored_state: 'SUCCEEDED', state_source: 'derived',
      created_at: '2026-10-04T20:10:00Z', updated_at: '2026-10-04T20:30:00Z', submitted_by: 'operator@example.com',
      priority: 5, on_step_failure: 'stop', cancel_requested: false,
      steps: [
        { step_id: 'impl', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_impl' },
        { step_id: 'review', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['impl'], input_from: {}, task_id: 'task_review' },
      ],
    },
    tasks,
  }
}

function serve(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mountRun(served = run()) {
  serve({ [`/v1/runs/${RUN_ID}`]: { run: served }, [`/v1/workflows/${WF}`]: workflow() })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const go = vi.fn()
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={go} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('G2-02: the right rail scrolls instead of clipping its cards', () => {
  it('leaves each rail card its content height, so the sticky rail overflows and scrolls', async () => {
    const { container } = await mountRun()
    const side = container.querySelector<HTMLElement>('.rn-side')!
    const cards = [...side.querySelectorAll<HTMLElement>(':scope > .rn-card')]
    expect(cards.length).toBe(3)
    for (const card of cards) {
      // `hidden`, `auto` or `scroll` makes the card a scroll container, whose
      // automatic minimum height is 0: the grid then squeezes it to fit.
      const y = painted(card, ['overflow', 'overflow-y'], DESKTOP)
      expect(y === null || y === 'visible' || y === 'clip', visible(card.querySelector('h3'))).toBe(true)
      // Still cut sideways, so one nowrap id cannot widen the card (D3).
      expect(painted(card, ['overflow-x'], DESKTOP)).toBe('clip')
    }
    // The rail sizes its rows to their content and scrolls itself.
    expect(painted(side, 'grid-auto-rows', DESKTOP)).toBe('max-content')
    expect(painted(side, 'align-content', DESKTOP)).toBe('start')
    expect(painted(side, 'overflow-y', DESKTOP)).toBe('auto')
  })
})

describe('G2-06: the PR card asserts nothing its counts do not carry', () => {
  it('says the per-check counts are not served instead of four dashes beside "green"', async () => {
    const { container } = await mountRun()
    const ci = await waitFor(() => container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
    const counts = ci.querySelector('.rn-ci-counts')!
    expect(visible(counts.querySelector(':scope > :not(b)'))).toBe('per-check counts not served')
    expect(counts.querySelector('[title]')?.getAttribute('title')).toMatch(/pull_request\.checks/)
    expect(visible(counts)).not.toMatch(/passed|pending|skipped/)
  })

  it('titles the card "Pull request #564" with no trailing dash, and says green once', async () => {
    const { container } = await mountRun()
    const ci = await waitFor(() => container.querySelector<HTMLElement>('.rn-ci')!, WAIT)
    expect(visible(ci.querySelector('.c-card-h h3'))).toBe('Pull request #564')
    const merge = [...ci.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'merge')!
    expect(visible(merge.querySelector(':scope > :not(b)'))).toBe('merge not reported')
  })
})

describe('G2-17: Open agent routes by the task\'s state', () => {
  it('sends a finished task to Recent and a parked one to Waiting, never Live', async () => {
    const { container, go } = await mountRun()
    const card = await waitFor(() => {
      const c = within(container).getByRole('region', { name: 'Steps' })
      expect(c.querySelectorAll('.rn-srow [data-mark="succeeded"]').length).toBe(1)
      return c
    }, WAIT)
    const row = (step: string) => card.querySelector<HTMLElement>(`.rn-srow[data-step="${step}"]`)!
    const done = within(row('impl')).getByRole('link', { name: /Open agent/ })
    expect(done.getAttribute('href')).toBe('/agents/recent/task_impl')
    const parked = within(row('review')).getByRole('link', { name: /Open agent/ })
    expect(parked.getAttribute('href')).toBe('/agents/waiting/task_review')
    fireEvent.click(done)
    expect(go).toHaveBeenLastCalledWith('/agents/recent/task_impl')
  })
})

describe('G2-30: the folded plan counts what the Steps card counts', () => {
  it('says "1 planned step + review + fix"', async () => {
    const { container } = await mountRun()
    const summary = container.querySelector('.rn-plan > details.rn-fold > summary')!
    expect(visible(summary)).toMatch(/1 planned step \+ review \+ fix · approved$/)
  })
})

describe('G2-31: Progress prints clock times with the gap from the line above', () => {
  it('draws HH:MM:SS (or MM-DD HH:MM:SS), +Δ, and the full timestamp in the title', async () => {
    const { container } = await mountRun()
    const items = [...container.querySelectorAll<HTMLElement>('.rn-history li:not(.rn-hstep)')]
    expect(items.length).toBe(3)
    const times = items.map((li) => li.querySelector<HTMLElement>('time')!)
    for (const t of times) {
      expect(visible(t)).toMatch(/^(\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}( \+\S.*)?$/)
      expect(visible(t)).not.toMatch(/ago/)
    }
    expect(times[0]!.getAttribute('title')).toContain('2026-10-04T20:00:00.000Z')
    expect(times[0]!.getAttribute('dateTime')).toBe('2026-10-04T20:00:00Z')
    expect(visible(times[0]!)).not.toContain('+')
    expect(visible(times[1]!)).toMatch(new RegExp(`\\+${spanText(5 * 60_000)}$`))
    expect(visible(times[2]!)).toMatch(new RegExp(`\\+${spanText(4.5 * 60_000)}$`))
  })
})

describe('G2-32: the folded Overlaps heading is a card title', () => {
  it('takes the card-title size and no margin of its own', async () => {
    const { container } = await mountRun()
    const h = container.querySelector<HTMLElement>('.rn-overlaps > details.rn-fold > summary > h3')!
    expect(h).not.toBeNull()
    expect(painted(h, 'font-size', DESKTOP)).toBe('var(--t-lead)')
    expect(painted(h, ['margin', 'margin-top'], DESKTOP)).toBe('0')
    // The same token the canonical card head uses (`Context the planner was given`).
    expect(COMPONENTS_CSS).toMatch(/\.c-card-h > h3 \{[^}]*font-size: var\(--t-lead\)/)
  })
})

// ---------------------------------------------------------------------------
// the list
// ---------------------------------------------------------------------------

function listRow(id: string, over: Record<string, unknown>) {
  return run({ id, ...over })
}

const LIST = [
  listRow('run_a1', { state: 'RUNNING', terminal: false, workflow_id: 'wf_alpha' }),
  listRow('run_b2', { state: 'DONE', terminal: true, workflow_id: 'wf_beta',
    issue: { ...run().issue, ref: `${OWNER}/SwarmCloud#454`, number: 454 }, issue_read: null }),
  listRow('run_c3', { state: 'FAILED', terminal: true, workflow_id: null,
    issue_read: { ...run().issue_read, title: 'Flaky planner timeout' } }),
]

async function mountList() {
  serve({ '/v1/runs': { runs: LIST, next_page_token: null, tenant_id: 'eng' } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const go = vi.fn()
  const utils = render(<RunsScreen view={null} go={go} />)
  await waitFor(() => expect(utils.container.querySelectorAll('.rn-row').length).toBe(3), WAIT)
  return { ...utils, go }
}

const shown = (c: HTMLElement) => [...c.querySelectorAll('.rn-row td[data-label="Run"]')].map(visible)

describe('G2-18: the Runs list links its workflows and filters by state and text', () => {
  it('links the Workflow column to the workflow, in the app', async () => {
    const { container, go } = await mountList()
    const cell = container.querySelector('.rn-row td[data-label="Workflow"]')!
    const a = within(cell as HTMLElement).getByRole('link', { name: 'wf_alpha' })
    expect(a.getAttribute('href')).toBe('/workflows/wf_alpha')
    fireEvent.click(a)
    expect(go).toHaveBeenLastCalledWith('work/workflows?wf=wf_alpha')
  })

  it('filters by state chips, with counts', async () => {
    const { container } = await mountList()
    const chips = within(container).getByRole('group', { name: 'Which runs' })
    const chip = (name: RegExp) => within(chips).getByRole('button', { name })
    expect(visible(chip(/^all/))).toBe('all3')
    expect(visible(chip(/^active/))).toBe('active1')
    fireEvent.click(chip(/^failed/))
    expect(shown(container)).toEqual(['run_c3'])
    fireEvent.click(chip(/^done/))
    expect(shown(container)).toEqual(['run_b2'])
    fireEvent.click(chip(/^active/))
    expect(shown(container)).toEqual(['run_a1'])
    fireEvent.click(chip(/^all/))
    expect(shown(container)).toEqual(['run_a1', 'run_b2', 'run_c3'])
  })

  it('searches the issue ref and the issue title, and says when nothing matches', async () => {
    const { container } = await mountList()
    const search = within(container).getByRole('searchbox', { name: /Find a run/ })
    fireEvent.change(search, { target: { value: 'flaky' } })
    expect(shown(container)).toEqual(['run_c3'])
    fireEvent.change(search, { target: { value: '#454' } })
    expect(shown(container)).toEqual(['run_b2'])
    fireEvent.change(search, { target: { value: 'no such run' } })
    expect(shown(container)).toEqual([])
    expect(visible(container.querySelector('.rn-none'))).toMatch(/No run of the 3 read matches/)
  })
})

describe('G2-19: a phone cell\'s label is a micro label on its own line, with no separator', () => {
  it('prints attr(data-label) alone, as a block', async () => {
    const { container } = await mountList()
    // A fact's cell (V112 drew the id, state and issue with no label at all).
    const td = container.querySelector('.rn-row > td[data-label="Workflow"]')!
    expect(painted(td, 'content', PHONE, 'before')).toBe('attr(data-label)')
    expect(painted(td, 'display', PHONE, 'before')).toBe('block')
    expect(painted(td, 'font-size', PHONE, 'before')).toBe('var(--t-micro)')
  })
})
