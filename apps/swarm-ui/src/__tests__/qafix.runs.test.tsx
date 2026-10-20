/**
 * QAFIX-RUNS-TIMELINE, THE RUNS LIST (part of #1680, the first console visual
 * QA run, wf_9e9bf74834634db7bcc3, 2026-10-20):
 *
 *   QA-081  at 1440 the By column cut 'operator@example.co…' in both rows:
 *           150px held a 20-character address with no pixel to spare.
 *   QA-082  at 390 the two run cards laid their facts out differently: each
 *           fact started at its value's width, so a longer value moved By to
 *           a second line on one card and not the other.
 *   QA-084  the state filter read 'all 2 · active 2 · done 0 …', lowercase
 *           with All first, beside the Workflows filter's 'Running 2 ·
 *           Finished 0 · Failed 1 · All 3'.
 *
 * Every assertion names the mutation that turns it red.
 */
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, textPx } from './tablefit'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const OWNER = 'example-org'
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function run(id: string, over: Record<string, unknown> = {}) {
  return {
    id, tenant_id: 'eng', state: 'RUNNING', terminal: false,
    issue: { ref: `${OWNER}/api#12`, owner: OWNER, repo: 'api', number: 12, url: `https://github.com/${OWNER}/api/issues/12` },
    plan_approval: 'required', workflow_id: 'wf_' + 'a1'.repeat(10), created_by: 'operator@example.com',
    created_at: '2026-10-20T03:00:00Z', updated_at: '2026-10-20T03:10:00Z',
    issue_read: { title: 'Fix the list', state: 'open' },
    ...over,
  }
}

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
  await waitFor(() => expect(utils.container.querySelector('.rn-row')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('QA-081: the By column holds an operator address whole at 1440', () => {
  it('fits the 20-character address in By, with the run id, the workflow id and a 34-character reference still whole', async () => {
    const RUN_ID = 'run_' + 'b2'.repeat(10)
    const WF = 'wf_' + 'a1'.repeat(10)
    const REF = 'bogdan-alexandrescu/SwarmCloud#454'
    const { container } = await mountList([run(RUN_ID, { issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: null } })])
    // The 1056px card the list sits in at 1440 (D11, R11), by the table-fit
    // model: each column its declared width, Issue what is left, and text at
    // a conservative 0.56em (sans) or 0.6em (mono) a character.
    const { cols } = fixedColumns(container.querySelector<HTMLTableElement>('.rn-table')!, 1056, WIDE)
    const row = container.querySelector('.rn-row')!
    const room = (col: string, label: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-label="${label}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const by = row.querySelector('td[data-label="By"] .rn-cut')!
    expect(by.textContent).toBe('operator@example.com')
    // MUTATION: draw By at --t-meta again (drop `.rn-row > td[data-label='By']`'s
    // micro size) or set its column back to 150px, and this is red: the
    // address is cut 'operator@example.co…', as the QA run saw it.
    expect(room('by', 'By'), 'the address is cut').toBeGreaterThanOrEqual(textPx('operator@example.com', by, WIDE))
    // MUTATION: find By's room by taking it from Issue, Run or Workflow and
    // one of these is red.
    expect(room('issue', 'Issue'), 'the issue reference is cut').toBeGreaterThanOrEqual(textPx(REF, row.querySelector('td[data-label="Issue"] .rn-refcut')!, WIDE))
    expect(room('run', 'Run'), 'the run id is cut').toBeGreaterThanOrEqual(textPx(RUN_ID, row.querySelector('td[data-label="Run"] a')!, WIDE))
    expect(room('workflow', 'Workflow'), 'the workflow id is cut').toBeGreaterThanOrEqual(textPx(WF, row.querySelector('td[data-label="Workflow"] .rn-cut')!, WIDE))
  })
})

describe('QA-082: every run card at 390 lays its four facts on the same grid', () => {
  it('pairs Plan approval with Workflow and Created with By, in columns that do not depend on the values', async () => {
    const { container } = await mountList([
      run('run_' + 'd4'.repeat(10)),
      // A card whose values differ in width from the first one's.
      run('run_' + 'e5'.repeat(10), { plan_approval: 'auto', workflow_id: null, created_by: 'a.much.longer.operator@example.com' }),
    ])
    const rows = [...container.querySelectorAll('.rn-row')]
    expect(rows).toHaveLength(2)
    for (const row of rows) {
      const cell = (label: string) => row.querySelector(`td[data-label="${label}"]`)!
      const flex = (label: string) => painted(cell(label), ['flex', 'flex-basis'], PHONE)
      // The narrow column: a fixed basis, never the value's own width.
      // MUTATION: give Plan approval or Created `flex: 1 1 auto` back (V206's
      // content-sized facts) and this is red.
      const narrow = /^0 0 (\d+)px$/.exec(flex('Plan approval') ?? '')
      expect(narrow, `Plan approval is ${flex('Plan approval')}`).not.toBeNull()
      expect(flex('Created')).toBe(flex('Plan approval'))
      // The wide column: the rest of the line after the narrow one and the
      // row's 8px column gap, so Workflow and By each start a pair's second
      // column and Created cannot join Workflow's line.
      // MUTATION: drop the gap from the calc, or let Workflow or By start at
      // its value's width, and this is red.
      const gap = (painted(row, 'gap', PHONE) ?? '').split(/\s+/)
      expect(gap[1] ?? gap[0]).toBe('8px')
      const wide = `1 1 calc(100% - ${Number(narrow![1]) + 8}px)`
      expect(flex('Workflow')).toBe(wide)
      expect(flex('By')).toBe(wide)
      // The row still wraps, so the pairs are lines of one flex row (V206).
      // MUTATION: make the row a grid again and this is red.
      expect(painted(row, 'display', PHONE)).toBe('flex')
      expect(painted(row, 'flex-wrap', PHONE)).toBe('wrap')
    }
  })
})

describe('QA-084: the Runs state filter is worded as the Workflows one is', () => {
  it('capitalises each state and puts All last', async () => {
    const { container } = await mountList([run('run_' + 'f6'.repeat(10)), run('run_' + 'a7'.repeat(10), { state: 'DONE', terminal: true })])
    const chips = within(container).getByRole('group', { name: 'Which runs' })
    const labels = [...chips.querySelectorAll('button')].map((b) => visible(b))
    // MUTATION: label the chips with the lowercase keys again, or put `all`
    // back first in RUN_FILTERS, and this is red.
    expect(labels).toEqual(['Active1', 'Done1', 'Failed0', 'Not ready0', 'Rejected0', 'All2'])
    // The address keeps its lowercase key: `?state=done`, as before.
    // MUTATION: write the label into the address and this is red.
    fireEvent.click(within(chips).getByRole('button', { name: /^Done/ }))
    expect(container.querySelectorAll('.rn-row')).toHaveLength(1)
  })

  it('names the chosen state in its own words when nothing matches', async () => {
    const { container } = await mountList([run('run_' + 'b8'.repeat(10))])
    const chips = within(container).getByRole('group', { name: 'Which runs' })
    fireEvent.click(within(chips).getByRole('button', { name: /^Not ready/ }))
    // MUTATION: print the filter's key (`in not ready`) and this is red.
    expect(visible(container.querySelector('.rn-none'))).toBe('No run of the 1 read matches in Not ready.')
  })
})
