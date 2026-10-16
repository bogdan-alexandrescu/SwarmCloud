/**
 * VISUAL QA RE-RUN LANE R8, THE RUNS LIST AND ONE RUN (#1427, report of
 * 2026-10-16):
 *
 *   V096  on a phone a reference heading's `?` dropped onto its own row.
 *   V205  a long issue ref in the list was cut from its end, losing `#N`.
 *   V206  the phone card's four facts were equal quarters: By read
 *         "operator@ex…" beside room under Plan approval and Workflow.
 *   V208  at 1440 the `?` sat at the title row's far end, "every 15 s" had a
 *         row of its own, and a ~60px band sat over the meta.
 *   V209  the run page's meta chip was one cut line: at 400px it showed the
 *         reference and hid the run id and its creator.
 *   V210  the screen's own empty state named "Submit › From a GitHub issue"
 *         in plain text, and its `none` chip sat a space from the heading.
 *   V214  the first Progress line's time wrapped under it at 400px.
 *   V215  the list's "Plan approval" header wrapped to two lines.
 *
 * V211 (the run's Changes tab) is in changes.matrix.test.tsx, beside V037
 * and the RunChangesTab API mocks.
 *
 * MUTATIONS: give the h1 `flex: 1 1 0` or the actions `flex: 1 0 100%` back,
 * or restore the phone `.is-ref` rule; render the list ref as one text;
 * restore `repeat(4, minmax(0, 1fr))` or drop the `contain`; render the meta
 * as one string or put `white-space: nowrap` back on `.rn-meta`; make the
 * empty body plain text again or drop the `:has(.rn-start)` margin; put
 * `flex-wrap: wrap` back on Progress rows; drop the header `nowrap` or the
 * 90px -- each turns a case red.
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
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const OWNER = 'customer-success-platform'
const REPO = 'agent-swarm-infrastructure-console'
const REF = `${OWNER}/${REPO}#14512`
const ISSUE_URL = `https://github.com/${OWNER}/${REPO}/issues/14512`
const RUN_ID = 'run_r8a1b2c3d4e5f6071829'
const WF = 'wf_r8b2c3d4e5f607182930'

const SHEETS = [STYLES, COMPONENTS_CSS, NAMES_CSS, INTAKE_CSS, RUNS_CSS].join('\n')
const DESKTOP: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }
const painted = (el: Element, prop: string | string[], env: CascadeEnv, pseudo: string | null = null) => {
  const r = cascade(SHEETS, el, prop, env, pseudo)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID, tenant_id: 'eng', state: 'RUNNING', terminal: false,
    issue: { ref: REF, owner: OWNER, repo: REPO, number: 14512, url: ISSUE_URL,
      repository_url: `https://github.com/${OWNER}/${REPO}` },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_plannerr8',
    plan: { summary: 'Fix the list.', steps: [{ step_id: 'impl', title: 'ui: the list', prompt: 'Build it.', depends_on: [] }] },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: WF,
    created_by: 'operator@example.com', created_at: '2026-10-16T10:00:00Z', updated_at: '2026-10-16T10:30:00Z',
    approved_by: 'operator@example.com', approved_at: '2026-10-16T10:09:00Z', approved_digest: DIGEST,
    rejected_by: null, rejection_reason: null, error: null,
    history: [
      { at: '2026-10-16T10:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-16T10:05:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
    ],
    issue_read: null,
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

async function mountRun(over: Record<string, unknown> = {}) {
  serve({ [`/v1/runs/${RUN_ID}`]: { run: run(over) } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-history')).not.toBeNull(), WAIT)
  return utils
}

async function mountList(page: { runs: unknown[]; next_page_token: string | null }, ready = '.rn-list') {
  serve({ '/v1/runs?view=summary': { ...page, tenant_id: 'eng' } })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const go = vi.fn()
  const utils = render(<RunsScreen view={null} go={go} />)
  await waitFor(() => expect(utils.container.querySelector(ready)).not.toBeNull(), WAIT)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('V096 and V208: the run head is the workflow page\'s', () => {
  const cases = [
    { name: 'a reference', over: {}, cls: 'rn-run is-ref' },
    { name: 'a title', over: { issue_read: { title: 'Cost column', state: 'open', labels: [], body: 'B.', body_truncated: false,
      body_redacted: false, comments: 0, read_at: '2026-10-16T10:00:00Z', url: ISSUE_URL } }, cls: 'rn-run' },
  ]
  for (const c of cases) {
    for (const env of [DESKTOP, PHONE]) {
      it(`keeps the \`?\` after ${c.name} and the actions on its row when they fit, at ${env.width}px`, async () => {
        const { container } = await mountRun(c.over)
        expect(container.querySelector('.rn-run')!.className).toBe(c.cls)
        const head = container.querySelector('.rn-run .c-phead')!
        const block = head.querySelector(':scope > div.head')!
        // One block that shrinks to the column, the `?` inside it after the title.
        expect(painted(block, 'display', env)).toBe('flex')
        expect(painted(block, 'flex', env)).toBe('0 1 auto')
        expect(painted(block, 'max-width', env)).toBe('100%')
        // A reference is never shrunk for the `?` (D11): the `?` wraps under
        // one too wide to share the row, and stays beside one that fits.
        expect(painted(block, 'flex-wrap', env) ?? 'nowrap').toBe(c.cls.includes('is-ref') ? 'wrap' : 'nowrap')
        const h1 = block.querySelector('h1')!
        expect(painted(h1, 'flex', env)).toBe('0 1 auto')
        expect(painted(h1, 'min-width', env)).toBe('0')
        const help = document.createElement('button')
        block.appendChild(help)
        expect(painted(help, 'flex', env)).toBe('none')
        // The actions follow on the same row, right-aligned; they wrap to the
        // next only when the row is full, never as a row of their own by rule.
        const acts = head.querySelector(':scope > .c-acts')!
        expect(painted(acts, 'flex', env)).toBe('0 1 auto')
        expect(painted(acts, 'margin-left', env)).toBe('auto')
        expect(painted(head, 'flex-wrap', env)).toBe('wrap')
      })
    }
  }
})

describe('V205: a long issue ref keeps its number', () => {
  it('cuts the repository and draws `#N` whole after it', async () => {
    const titled = run({ issue_read: { title: 'The list', state: 'open', url: ISSUE_URL, read_at: '2026-10-16T10:00:00Z' } })
    const { container } = await mountList({ runs: [titled, run({ id: 'run_r8bare', workflow_id: null })], next_page_token: null })
    await waitFor(() => expect(container.querySelectorAll('.rn-row')).toHaveLength(2), WAIT)
    for (const cell of container.querySelectorAll('.rn-row > td[data-label="Issue"]')) {
      const ref = cell.querySelector<HTMLElement>('.rn-refcut')!
      expect(ref.getAttribute('title')).toBe(REF)
      const link = ref.querySelector('a')!
      expect(link.getAttribute('href')).toBe(ISSUE_URL)
      expect(link.textContent).toBe(REF)
      const repo = link.querySelector('.rn-refcut-repo')!
      const n = link.querySelector('.rn-refcut-n')!
      expect(repo.textContent).toBe(`${OWNER}/${REPO}`)
      expect(n.textContent).toBe('#14512')
      expect(painted(ref, 'display', DESKTOP)).toBe('flex')
      expect(painted(link, 'min-width', DESKTOP)).toBe('0')
      expect(painted(repo, 'text-overflow', DESKTOP)).toBe('ellipsis')
      expect(painted(repo, 'min-width', DESKTOP)).toBe('0')
      expect(painted(n, 'flex', DESKTOP)).toBe('none')
      // The ref is no longer the one-line end cut that lost the number.
      expect(ref.classList.contains('rn-cut')).toBe(false)
    }
  })
})

describe('V206: the phone card sizes its facts to what they hold', () => {
  it('starts each column at its value and shares the rest, the spanning cells holding no say', async () => {
    const { container } = await mountList({ runs: [run()], next_page_token: null })
    await waitFor(() => expect(container.querySelector('.rn-row')).not.toBeNull(), WAIT)
    const row = container.querySelector('.rn-row')!
    expect(painted(row, 'grid-template-columns', PHONE)).toBe('repeat(4, minmax(0, auto))')
    for (const label of ['Run', 'State', 'Issue']) {
      expect(painted(row.querySelector(`td[data-label="${label}"]`)!, 'contain', PHONE), label).toBe('inline-size')
    }
    for (const label of ['Plan approval', 'Workflow', 'Created', 'By']) {
      expect(painted(row.querySelector(`td[data-label="${label}"]`)!, 'contain', PHONE), label).toBeNull()
    }
  })
})

describe('V209: the run page meta wraps between its facts', () => {
  it('draws the reference, the run id and the creator as pieces, each keeping its ` ·`', async () => {
    const { container } = await mountRun()
    const meta = container.querySelector<HTMLButtonElement>('button.rn-meta')!
    expect(meta.getAttribute('title')).toBe(`${REF} · ${RUN_ID} · created by operator@example.com`)
    const pieces = [...meta.querySelectorAll('.rn-meta-i')].map((p) => p.textContent)
    expect(pieces).toEqual([`${REF} ·`, `${RUN_ID} ·`, 'created by operator@example.com'])
    expect(visible(meta)).toBe(`${REF} · ${RUN_ID} · created by operator@example.com`)
    for (const env of [PHONE, DESKTOP]) {
      expect(painted(meta, 'white-space', env)).toBe('normal')
      const piece = meta.querySelector('.rn-meta-i')!
      expect(painted(piece, 'white-space', env)).toBe('nowrap')
      expect(painted(piece, 'text-overflow', env)).toBe('ellipsis')
      expect(painted(piece, 'max-width', env)).toBe('100%')
    }
    meta.click()
    await waitFor(() => expect(meta.classList.contains('is-open')).toBe(true))
    expect(painted(meta.querySelector('.rn-meta-i')!, 'white-space', PHONE)).toBe('normal')
  })
})

describe('V210: the empty Runs screen links the issue form', () => {
  it('links Submit › From a GitHub issue in the screen\'s own empty state, its mark off the heading', async () => {
    const { container, go } = await mountList({ runs: [], next_page_token: null }, '.ctl-empty')
    const empty = container.querySelector<HTMLElement>('.ctl-empty')!
    expect(visible(empty)).toMatch(/No runs yet/)
    const link = within(empty).getByRole('link', { name: 'Submit › From a GitHub issue' })
    expect(link.getAttribute('href')).toBe('/submit/issue')
    link.click()
    expect(go).toHaveBeenCalledWith('work/new-issue')
    expect(painted(empty.querySelector('h3 > .ctl-mark')!, 'margin-right', DESKTOP)).toBe('var(--ctl-s1)')
  })

  it('gives the list\'s empty state, older runs offered, the same link and spacing', async () => {
    const { container } = await mountList({ runs: [], next_page_token: 'tok1' })
    const empty = container.querySelector<HTMLElement>('.rn-empty')!
    expect(within(empty).getByRole('link', { name: 'Submit › From a GitHub issue' }).getAttribute('href')).toBe('/submit/issue')
    expect(painted(empty.querySelector('h3 > .ctl-mark')!, 'margin-right', DESKTOP)).toBe('var(--ctl-s1)')
  })
})

describe('V214: every Progress line keeps its time on its line', () => {
  for (const env of [PHONE, DESKTOP]) {
    it(`wraps the words inside their item, never the time under them, at ${env.width}px`, async () => {
      const { container } = await mountRun()
      const lines = container.querySelectorAll('.rn-history li')
      expect(lines.length).toBeGreaterThanOrEqual(2)
      for (const li of lines) {
        expect(painted(li, 'flex-wrap', env)).toBe('nowrap')
        const [mark, words, time] = [...li.children]
        expect(time!.tagName).toBe('TIME')
        expect(painted(time!, 'flex', env)).toBe('none')
        expect(painted(mark!, 'flex', env)).toBe('none')
        expect(painted(words!, 'min-width', env)).toBe('0')
        expect(painted(words!, 'flex', env)).toBe('0 1 auto')
      }
    })
  }
})

describe('V215: the list header is one line', () => {
  it('keeps every header on one line and gives Plan approval its width', async () => {
    const { container } = await mountList({ runs: [run()], next_page_token: null })
    await waitFor(() => expect(container.querySelector('.rn-table')).not.toBeNull(), WAIT)
    for (const th of container.querySelectorAll('.rn-table th')) {
      expect(painted(th, 'white-space', DESKTOP), th.textContent!).toBe('nowrap')
    }
    expect(painted(container.querySelector('.rn-table th[data-col="approval"]')!, 'width', DESKTOP)).toBe('90px')
  })
})
