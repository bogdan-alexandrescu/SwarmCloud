/**
 * BROWSER QA U11b (owner, 2026-10-04; live console at main 69416faf, 1440x900
 * light and dark and 390px): Runs and one run's page.
 *
 *   D3   The Linked card was 300px with 353px of content: values read
 *        "plan co", "status co", "SwarmClou", "the workflow c". The card's
 *        grid track grew to its content's min-content (a nowrap id) and the
 *        card clipped it. The track is `minmax(0, 1fr)`, labels never shrink,
 *        and every value is cut with its whole text in its title.
 *   D11  A run heading that is a reference was cut at 323px with room to
 *        spare beside it; the Runs list cut the Run id and the Issue
 *        reference. The heading takes the row before the meta does, and the
 *        two columns fit what they hold.
 *   N15  A Done run whose workflow opened PR #545 said "Pull request: none yet
 *        · the workflow opens it", and drew no CI card. The PR is read from the
 *        workflow's integrator result when the run records none, and a
 *        finished run with no CI says so in the CI card.
 *   N18  The plan's lead, prompts and requirements showed raw backticks, and
 *        a lead that already ended in an ellipsis got a second one ("… …").
 *   D35  The run page's side cards scrolled away with the page; they stick at
 *        the same top offset as the Submit send card.
 *
 * MUTATIONS: drop the track rule, let a label shrink, give the meta the
 * heading's room again, put Run back to 17%, stop reading the workflow's
 * result, drop the finished-run CI note, print backticks, or add a second
 * ellipsis -- each turns a case red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { planLead } from '../Runs'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'c3'.repeat(32)
const REF = 'bogdan-alexandrescu/SwarmCloud#454'
const URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454'
const RUN_ID = 'run_d13a2f1b7e1e4eafabfb'
const WF = 'wf_9c4be0e52b3a4cbe9f1f'

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_7f3e2c9a1b8d4e6f0a5c',
    plan: {
      summary: 'Wire the write-back so `Closes #454` lands in the PR body. Keep `apps/swarm-api` untouched.',
      requirements: ['The PR body says `Closes #454`'],
      steps: [
        { step_id: 'impl', title: 'api: edit `routes/runs.py`', prompt: 'Edit `apps/swarm-api/swarm_api/routes/runs.py` and its tests.', depends_on: [] },
      ],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-03T14:02:00Z', updated_at: '2026-10-03T14:09:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-10-03T14:02:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: null,
    issue_read_error: null,
    ...over,
  }
}

const WORKFLOW_READ = {
  workflow: {
    workflow_id: WF, tenant_id: 'eng', state: 'SUCCEEDED', created_at: '2026-10-03T14:10:00Z', updated_at: '2026-10-03T16:10:00Z',
    steps: [
      { workflow_id: WF, step_id: 'impl', depends_on: [], runner_profile: 'claude-code', task_id: 'task_impl' },
      { workflow_id: WF, step_id: 'integrate', depends_on: ['impl'], runner_profile: 'claude-code', task_id: 'task_int' },
    ],
  },
  tasks: [
    { id: 'task_impl', state: 'SUCCEEDED', result_summary: null },
    { id: 'task_int', state: 'SUCCEEDED', result_summary: { git: { role: 'integrator', pull_request: { number: 545, url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/545' } } } },
  ],
}

function serve(body: unknown, workflow: unknown = null) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: body }), { status: 200, headers: JSON_HEADERS })
    if (workflow !== null && url === `/v1/workflows/${WF}`) return new Response(JSON.stringify(workflow), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mount(served: unknown, workflow: unknown = null) {
  serve(served, workflow)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
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

describe('D3: the Linked card cuts values, never labels', () => {
  it('sizes every side card\'s track to the card, keeps each label whole and cuts each value with its title', async () => {
    const { container } = await mount(run({ workflow_id: WF, state: 'RUNNING' }))
    const linked = within(container).getByRole('region', { name: 'Linked' })
    const side = linked.parentElement!
    for (const card of side.querySelectorAll(':scope > .rn-card')) {
      expect(painted(card, 'grid-template-columns', WIDE), card.getAttribute('aria-label')!).toBe('minmax(0, 1fr)')
    }
    for (const fact of linked.querySelectorAll('.ctl-fact')) {
      const b = fact.querySelector(':scope > b')!
      expect(painted(b, 'flex', WIDE)).toBe('none')
      expect(painted(b, 'white-space', WIDE), visible(b)).toBe('nowrap')
      const value = fact.querySelector(':scope > :not(b)') as HTMLElement
      expect(painted(value, 'min-width', WIDE), visible(fact)).toBe('0')
      expect(painted(value, 'text-overflow', WIDE), visible(fact)).toBe('ellipsis')
      expect(value.getAttribute('title') ?? value.querySelector('[title]')?.getAttribute('title'), visible(fact)).toBeTruthy()
    }
    // Every label fits beside a usable value in a 300px card (14px padding a side).
    const room = 300 - 28 - 2
    for (const b of linked.querySelectorAll('.ctl-fact > b')) {
      expect(textPx(visible(b), b, WIDE) + 12 + 60, visible(b)).toBeLessThanOrEqual(room)
    }
  })
})

describe('D11: a reference heading and the list\'s columns fit', () => {
  it('gives a reference heading the row before the meta', async () => {
    const { container } = await mount(run({ issue_read: null }))
    const sub = container.querySelector('.rn-run.is-ref .c-phead > .sub')!
    expect(sub).not.toBeNull()
    const flex = painted(sub, ['flex', 'flex-basis'], WIDE) ?? ''
    expect(flex, 'the meta keeps a basis as wide as its text').toMatch(/(^|\s)0(px|%)?$/)
  })

  it('fits the Run id and the Issue reference whole in a 1056px list', async () => {
    const { container } = await mountList([run({ state: 'DONE', workflow_id: WF, created_by: 'bogdan@sagadigital.example.com' })])
    const table = container.querySelector<HTMLTableElement>('.rn-table')!
    const { cols } = fixedColumns(table, 1056, WIDE)
    const row = container.querySelector('.rn-row')!
    const room = (col: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-label="${col === 'run' ? 'Run' : 'Issue'}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const id = row.querySelector('td[data-label="Run"] a')!
    expect(room('run'), 'the run id is cut').toBeGreaterThanOrEqual(textPx(RUN_ID, id, WIDE))
    const ref = row.querySelector('td[data-label="Issue"] .rn-cut')!
    expect(room('issue'), 'the issue reference is cut').toBeGreaterThanOrEqual(textPx(REF, ref, WIDE))
  })
})

describe('N15: a finished run names the pull request its workflow opened', () => {
  it('reads the PR from the workflow\'s integrator result when the run records none', async () => {
    const { container } = await mount(run({ state: 'DONE', terminal: true, workflow_id: WF, pull_request: null }), WORKFLOW_READ)
    const linked = within(container).getByRole('region', { name: 'Linked' })
    const fact = await waitFor(() => {
      const f = [...linked.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'Pull request')!
      expect(visible(f)).toContain('#545')
      return f
    }, WAIT)
    expect(visible(fact)).not.toMatch(/none yet/)
    expect(fact.querySelector('a')!.getAttribute('href')).toBe('https://github.com/bogdan-alexandrescu/SwarmCloud/pull/545')
    expect(fact.querySelector('[title]')!.getAttribute('title')).toMatch(/integrator/)
  })

  it('draws the CI card for a finished run, saying no CI was recorded', async () => {
    const { container } = await mount(run({ state: 'DONE', terminal: true, workflow_id: WF, pull_request: null }), WORKFLOW_READ)
    const ci = await waitFor(() => {
      const c = container.querySelector('.rn-ci')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    expect(visible(ci)).toMatch(/no CI recorded for this run/i)
  })
})

describe('N18: inline code and one ellipsis', () => {
  it('draws backticked text as code in the lead, the requirements and the prompts', async () => {
    const { container } = await mount(run())
    const plan = container.querySelector('.rn-plan')!
    expect(visible(plan.querySelector('.rn-summary'))).not.toContain('`')
    expect(plan.querySelector('.rn-summary code')?.textContent).toBe('Closes #454')
    expect(visible(plan.querySelector('.rn-reqs'))).not.toContain('`')
    expect(visible(plan.querySelector('.rn-prompt'))).not.toContain('`')
    expect(plan.querySelector('.rn-prompt code')?.textContent).toBe('apps/swarm-api/swarm_api/routes/runs.py')
    expect(visible(plan.querySelector('.rn-step-h'))).not.toContain('`')
  })

  it('never ends the lead in two ellipses', async () => {
    // The lead's third sentence is the planner's own cut, "...", and more follows it.
    const long = `First. Second one. Then the planner cut it... ${'Filler words. '.repeat(30)}`
    const { container } = await mount(run({ plan: { summary: long, steps: [] } }))
    const lead = visible(container.querySelector('.rn-summary'))
    expect(lead).not.toMatch(/(…|\.\.\.)\s*…/)
    expect(planLead('One. Two. Three…  Four is more. ' + 'x '.repeat(200)).lead).not.toMatch(/…\s*…/)
  })
})

describe('D35: the run page\'s side sticks below the top edge', () => {
  it('sticks the side column at the Submit card\'s offset', () => {
    document.body.innerHTML =
      '<div class="rn-page"><section class="rn-main"></section><aside class="rn-side"></aside></div>' +
      '<form class="sbf"><div class="sbf-build"></div><aside class="sbf-side"><div class="sbf-send"></div></aside></form>'
    const side = document.querySelector('.rn-side')!
    const send = document.querySelector('.sbf-send')!
    expect(painted(side, 'position', WIDE)).toBe('sticky')
    const top = painted(side, 'top', WIDE)
    expect(top).not.toBeNull()
    expect(top).not.toBe('0')
    expect(top).toBe(painted(send, 'top', WIDE))
    document.body.innerHTML = ''
  })
})
