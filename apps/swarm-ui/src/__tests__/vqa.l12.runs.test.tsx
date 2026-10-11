/**
 * VISUAL QA LANE L12, THE RUNS LIST AND ONE RUN (#1038, report of 2026-10-11):
 *
 *   V036  the Run cell carried `.rn-cut` (`display: block`) on the `<td>`
 *         itself, so it left the table's row, ~16px off on a tall row.
 *   V038  the folded Overlaps card showed only its heading: `display: flex`
 *         on the summary removed the browser's disclosure marker.
 *   V040  a first page with no run drew "No run of the 0 read matches." over
 *         a bare header row, with no way to start one.
 *   V095  the run page's first line touched the tab strip's rule (0px).
 *   V096  the head's `?` sat alone on the second row under the title.
 *   V107  the issue ref in Linked could wrap right after `#`.
 *   V112  at 400px each run was seven labelled pairs split by a hairline.
 *
 * V037 (the run's Changes tab) is in changes.matrix.test.tsx, beside the
 * other RunChangesTab cases and their API mocks.
 *
 * MUTATIONS: put `className="rn-cut"` back on the Run `<td>`; set the fold's
 * summary back to `list-style: revert` with no `::before`; drop the
 * `rows.length === 0` branch or the `shown.length > 0` guard on the table;
 * drop `.rn-tabs`'s margin; give the title its 100% basis again; drop
 * `ISSUE_REF_BREAKS` from IssueLink; restore the stacked phone rows -- each
 * turns a case red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import COMPONENTS_CSS from '../styles/components.css?raw'
import INTAKE_CSS from '../styles/intake.css?raw'
import RUNS_CSS from '../styles/runs.css?raw'
import NAMES_CSS from '../styles/names.css?raw'
import STYLES from '../styles.css?raw'
import { cascade, type CascadeEnv } from './cssgate'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const OWNER = 'bogdan-alexandrescu'
const REF = `${OWNER}/SwarmCloud#454`
const ISSUE_URL = `https://github.com/${OWNER}/SwarmCloud/issues/454`
const RUN_ID = 'run_l12a1b2c3d4e5f607182'
const WF = 'wf_l12b2c3d4e5f60718293'

const SHEETS = [STYLES, COMPONENTS_CSS, NAMES_CSS, INTAKE_CSS, RUNS_CSS].join('\n')
const DESKTOP: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }
const painted = (el: Element, prop: string | string[], env: CascadeEnv, pseudo: string | null = null) => {
  const r = cascade(SHEETS, el, prop, env, pseudo)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

const PLAN = {
  summary: 'Fix the list.',
  requirements: ['The list fits'],
  overlaps: [{ ref: `${OWNER}/SwarmCloud#450`, kind: 'pull_request', note: 'No action: another screen.' }],
  steps: [{ step_id: 'impl', title: 'ui: the list', prompt: 'Build it.', depends_on: [] }],
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'DONE', terminal: true,
    issue: { ref: REF, owner: OWNER, repo: 'SwarmCloud', number: 454, url: ISSUE_URL,
      repository_url: `https://github.com/${OWNER}/SwarmCloud` },
    plan_approval: 'required', auto_merge: true, fix_rounds: 3, planner_task_id: 'task_planner454',
    plan: PLAN, plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-04T20:00:00Z', updated_at: '2026-10-04T21:00:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-04T20:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null, green_sha: null, requirements_met: true,
    pull_request: null,
    history: [
      { at: '2026-10-04T20:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-04T20:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
    ],
    issue_read: { title: 'A long issue title that the page head draws on its own row', state: 'open', labels: [], body: 'Body.',
      body_truncated: false, body_redacted: false, comments: 0, read_at: '2026-10-04T20:00:00Z', url: ISSUE_URL },
    issue_read_error: null,
    ...over,
  }
}

function serve(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mountRun() {
  serve({ [`/v1/runs/${RUN_ID}`]: { run: run() } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
}

async function mountList(page: { runs: unknown[]; next_page_token: string | null }) {
  serve({ '/v1/runs': { ...page, tenant_id: 'eng' } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const go = vi.fn()
  const utils = render(<RunsScreen view={null} go={go} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-list')).not.toBeNull(), WAIT)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('V036: the Run cell stays in the table\'s row', () => {
  it('cuts the id on a span inside the cell, never on the cell itself', async () => {
    const { container } = await mountList({ runs: [run()], next_page_token: null })
    await waitFor(() => expect(container.querySelector('.rn-row')).not.toBeNull(), WAIT)
    for (const td of container.querySelectorAll('.rn-row > td')) {
      expect(td.classList.contains('rn-cut'), td.getAttribute('data-label')!).toBe(false)
      expect(painted(td, 'display', DESKTOP) ?? 'table-cell', td.getAttribute('data-label')!).toBe('table-cell')
    }
    const cut = container.querySelector<HTMLElement>('.rn-row > td[data-label="Run"] > .rn-cut')!
    expect(cut.getAttribute('title')).toBe(RUN_ID)
    expect(painted(cut, 'text-overflow', DESKTOP)).toBe('ellipsis')
  })
})

describe('V038: a folded card says it opens', () => {
  it('draws its own chevron, which turns when open', async () => {
    const { container } = await mountRun()
    const fold = container.querySelector<HTMLDetailsElement>('.rn-overlaps > details.rn-fold')!
    expect(fold).not.toBeNull()
    const summary = fold.querySelector(':scope > summary')!
    expect(painted(summary, 'display', DESKTOP)).toBe('flex')
    expect(painted(summary, 'content', DESKTOP, 'before')).toMatch(/25B8/)
    fold.open = true
    expect(painted(summary, 'content', DESKTOP, 'before')).toMatch(/25BE/)
  })
})

describe('V040: a list with no run is an empty state with a way to start one', () => {
  it('says there is none yet and links Submit › From a GitHub issue, with no table', async () => {
    const { container, go } = await mountList({ runs: [], next_page_token: 'tok1' })
    expect(container.querySelector('.rn-table')).toBeNull()
    expect(container.querySelector('.rn-none')).toBeNull()
    expect(visible(container)).not.toMatch(/No run of the 0 read/)
    const empty = container.querySelector('.rn-empty')!
    expect(visible(empty)).toMatch(/No runs yet/)
    const link = within(empty as HTMLElement).getByRole('link', { name: /From a GitHub issue/ })
    expect(link.getAttribute('href')).toBe('/submit/issue')
    link.click()
    expect(go).toHaveBeenCalledWith('work/new-issue')
    // The older page is still offered.
    expect(within(container).getByRole('button', { name: 'Show older runs' })).toBeTruthy()
  })

  it('draws no bare header when a search matches nothing', async () => {
    const { container } = await mountList({ runs: [run()], next_page_token: null })
    await waitFor(() => expect(container.querySelector('.rn-row')).not.toBeNull(), WAIT)
    const search = within(container).getByRole('searchbox', { name: /Find a run/ })
    const { fireEvent } = await import('@testing-library/react')
    fireEvent.change(search, { target: { value: 'no such run' } })
    expect(visible(container.querySelector('.rn-none'))).toMatch(/No run of the 1 read matches/)
    expect(container.querySelector('.rn-table')).toBeNull()
  })
})

describe('V095: the run page leaves the workflow page\'s gap under its tabs', () => {
  it('puts 12px between the tab strip and the page', async () => {
    const { container } = await mountRun()
    const tabs = container.querySelector('.c-tabs.rn-tabs')!
    expect(tabs).not.toBeNull()
    expect(painted(tabs, 'margin-bottom', DESKTOP)).toBe('var(--ctl-s3)')
    expect(STYLES).toMatch(/--ctl-s3: 12px;/)
  })
})

describe('V096: the head\'s `?` shares the title\'s row', () => {
  for (const env of [DESKTOP, PHONE]) {
    it(`gives the title what the \`?\` leaves, and the actions the next row, at ${env.width}px`, async () => {
      const { container } = await mountRun()
      const head = container.querySelector('.rn-run .c-phead')!
      const block = head.querySelector(':scope > div.head')!
      const h1 = block.querySelector('h1')!
      expect(painted(h1, 'flex', env)).toBe('1 1 0')
      // The section's `?` is whatever the shell puts after the h1.
      const help = document.createElement('button')
      block.appendChild(help)
      expect(painted(help, 'flex', env)).toBe('none')
      const acts = head.querySelector(':scope > .c-acts')!
      expect(painted(acts, 'flex', env)).toBe('1 0 100%')
    })
  }
})

describe('V107: the issue ref never orphans its number', () => {
  it('breaks before `#`, never after it', async () => {
    const { container } = await mountRun()
    const ref = within(container).getByRole('region', { name: 'Linked' }).querySelector('a.rn-ref')!
    const pieces = [...ref.childNodes].map((n) => (n.nodeName === 'WBR' ? '|' : n.textContent)).join('')
    expect(pieces).toBe(`${OWNER}/|SwarmCloud|#454`)
    const { breakAt, ISSUE_REF_BREAKS } = await import('../Runs')
    const flat = (s: string) => breakAt(s, ISSUE_REF_BREAKS).map((p) => (typeof p === 'string' ? p : '|')).join('')
    expect(flat('o/r_x#9')).toBe('o/|r_|x|#9')
  })
})

describe('V112: a phone draws each run as one compact card', () => {
  it('lays a run out as three rows of a bordered card, labelling only the four facts', async () => {
    const { container } = await mountList({ runs: [run()], next_page_token: null })
    await waitFor(() => expect(container.querySelector('.rn-row')).not.toBeNull(), WAIT)
    const row = container.querySelector('.rn-row')!
    expect(painted(row, 'display', PHONE)).toBe('grid')
    expect(painted(row, 'grid-template-columns', PHONE)).toBe('repeat(4, minmax(0, 1fr))')
    expect(painted(row, ['border', 'border-top'], PHONE)).toMatch(/1px solid/)
    expect(painted(container.querySelector('.rn-table tbody')!, 'gap', PHONE)).toBe('8px')
    const cell = (label: string) => row.querySelector(`td[data-label="${label}"]`)!
    expect(painted(cell('Run'), 'grid-row', PHONE)).toBe('1')
    expect(painted(cell('State'), 'grid-row', PHONE)).toBe('1')
    expect(painted(cell('Issue'), 'grid-row', PHONE)).toBe('2')
    for (const label of ['Plan approval', 'Workflow', 'Created', 'By']) {
      expect(painted(cell(label), 'grid-row', PHONE), label).toBe('3')
      expect(painted(cell(label), 'content', PHONE, 'before'), label).toBe('attr(data-label)')
    }
    for (const label of ['Run', 'State', 'Issue']) {
      expect(painted(cell(label), 'content', PHONE, 'before'), label).toBe('none')
    }
    // The desktop table is untouched.
    expect(painted(row, 'display', DESKTOP)).toBeNull()
  })
})
