/**
 * VISUAL QA OF THE CONSOLE, WORK › RUNS (#2391, BGD, 2026-10-26):
 *
 *   VQ-017  at 1440 a run with no workflow drew 'none · the run ended before
 *           a workflow' over the Created column ('a wor7h ago').
 *   VQ-018  at 390 every run's card was laid out ~8,600px wide and clipped:
 *           the issue title cut mid-word at the edge, the right border gone.
 *
 * VQ-019 (the Steps card) is in qa.u14.runs.test.tsx, beside its fixtures.
 *
 * MUTATIONS: draw the note as a bare `<i>` again or drop its title; give the
 * phone `tbody` back its implicit `auto` track, take `min-width: 0` off the
 * card, or make the State cell one `flex: none` item again -- each turns a
 * case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import COMPONENTS_CSS from '../styles/components.css?raw'
import INTAKE_CSS from '../styles/intake.css?raw'
import STYLES from '../styles.css?raw'
import { cascade, type CascadeEnv } from './cssgate'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ef'.repeat(32)
const REF = 'bogdan-alexandrescu/SwarmCloud#2217'
const ISSUE_URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/2217'
const NOTE = 'none · the run ended before a workflow'

const SHEETS = [STYLES, COMPONENTS_CSS, INTAKE_CSS].join('\n')
const DESKTOP: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const painted = (el: Element, prop: string, env: CascadeEnv) => {
  const r = cascade(SHEETS, el, prop, env)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

function run(id: string, over: Record<string, unknown> = {}) {
  return {
    id, tenant_id: 'eng', state: 'RUNNING', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 2217, url: ISSUE_URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'auto', auto_merge: true, fix_rounds: 3, planner_task_id: `task_${id}`,
    plan: null, plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: 'wf_vq0a1b2c3d4e5f60718293',
    created_by: 'operator@example.com', created_at: '2026-10-25T18:00:00Z', updated_at: '2026-10-25T18:30:00Z',
    approved_by: null, approved_at: null, approved_digest: null,
    rejected_by: null, rejection_reason: null, error: null, history: [],
    issue_read: null,
    ...over,
  }
}

// A planner's reason and an issue title as long as the ones on the page:
// each one nowrap line far wider than a 358px card.
const REASON = 'Duplicates an open pull request that already does this whole issue; '.repeat(12).trim()
const TITLE = 'An issue run compiles implement -> review -> fix gated on NOT_YET, with no re-review, and it may ask the operator twice'
const ENDED = run('run_vq17aa1b2c3d4e5f6071', { state: 'NOT_READY', terminal: true, workflow_id: null,
  not_ready: { reason: REASON }, issue_read: { title: TITLE, state: 'open', labels: [], body: 'B.', body_truncated: false,
    body_redacted: false, comments: 0, read_at: '2026-10-25T18:00:00Z', url: ISSUE_URL } })
const RUNNING = run('run_vq17bb1b2c3d4e5f6071')

async function mountList(runs: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/v1/runs?view=summary') {
      return new Response(JSON.stringify({ runs, next_page_token: null, tenant_id: 'eng' }), { status: 200, headers: JSON_HEADERS })
    }
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={null} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelectorAll('.rn-row').length).toBe(runs.length), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('VQ-017: a run with no workflow keeps its note inside the Workflow column', () => {
  it('cuts the note with an ellipsis at 1440, whole in its title', async () => {
    const { container } = await mountList([ENDED])
    const cell = container.querySelector('.rn-row td[data-label="Workflow"]')!
    expect(visible(cell)).toBe(NOTE)
    const note = cell.querySelector('.rn-cut')!
    expect(note, 'the note is a cut value, as every other cell\'s is').not.toBeNull()
    expect(note.getAttribute('title')).toBe(NOTE)
    expect(painted(note, 'display', DESKTOP)).toBe('block')
    expect(painted(note, 'overflow', DESKTOP)).toBe('hidden')
    expect(painted(note, 'text-overflow', DESKTOP)).toBe('ellipsis')
    expect(painted(note, 'white-space', DESKTOP)).toBe('nowrap')
    // Created, the column it ran over, is still its own value.
    expect(visible(container.querySelector('.rn-row td[data-label="Created"]'))).toMatch(/ago$/)
  })

  it('lets the note wrap on a phone, where its line has room for it', async () => {
    const { container } = await mountList([ENDED])
    const note = container.querySelector('.rn-row td[data-label="Workflow"] .rn-cut')!
    expect(painted(note, 'white-space', PHONE)).toBe('normal')
  })
})

describe('VQ-018: a phone card is the column\'s width, never its content\'s', () => {
  it('holds the cards to one shrinkable track', async () => {
    const { container } = await mountList([ENDED, RUNNING])
    const body = container.querySelector('.rn-table tbody')!
    expect(painted(body, 'display', PHONE)).toBe('grid')
    expect(painted(body, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    for (const row of container.querySelectorAll('.rn-row')) {
      expect(painted(row, 'min-width', PHONE)).toBe('0')
      // The title is cut inside the card, whole in its title.
      const title = row.querySelector('.rn-issue-t')
      if (title !== null) {
        expect(title.getAttribute('title')).toBe(TITLE)
        expect(painted(title, 'text-overflow', PHONE)).toBe('ellipsis')
      }
    }
  })

  it('gives the state\'s reason a line of its own instead of the State cell\'s width', async () => {
    const { container } = await mountList([ENDED])
    const row = container.querySelector('.rn-row')!
    const state = row.querySelector('td[data-label="State"]')!
    // The cell was one `flex: none` item: its width was the reason's whole line.
    expect(painted(state, 'display', PHONE)).toBe('contents')
    const pill = state.querySelector('.c-pill')!
    const why = state.querySelector('.rn-why')!
    expect(visible(why)).toBe(`Not ready: ${REASON}`)
    expect(why.getAttribute('title')).toBe(`Not ready: ${REASON}`)
    expect(painted(pill, 'flex', PHONE)).toBe('none')
    expect(painted(why, 'flex', PHONE)).toBe('0 0 100%')
    expect(painted(why, 'min-width', PHONE)).toBe('0')
    expect(painted(why, 'text-overflow', PHONE)).toBe('ellipsis')
    // The mark beside the run id; the reason after the issue.
    const order = (el: Element) => Number(painted(el, 'order', PHONE) ?? 0)
    const issue = row.querySelector('td[data-label="Issue"]')!
    expect(order(row.querySelector('td[data-label="Run"]')!)).toBeLessThan(order(pill))
    expect(order(pill)).toBeLessThan(order(issue))
    expect(order(why)).toBe(order(issue))
    expect(issue.compareDocumentPosition(why) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Desktop is unchanged: the State cell is a table cell with the reason cut in it.
    expect(painted(state, 'display', DESKTOP)).toBeNull()
  })
})
