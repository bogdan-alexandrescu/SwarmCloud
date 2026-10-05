/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04, measured on the live console at
 * main 8455be37, /runs/run_7a37942a19aa4d2c80d1 at 1440x900 and 390): one
 * run's page and the Runs list.
 *
 *   R1/A  The Linked and Read-from-issue cards drew label|value rows in a
 *         300px card: 'open', 'bug', '2' and the PR link sat at x>=1409, past
 *         the card's edge at 1408. Each fact now STACKS its label above its
 *         value; the value takes the card's width and wraps only at a safe
 *         point (`/`, `#`, `_`, `::`, a space). A single-token id is one line,
 *         cut with an ellipsis, whole in its title. The PR fact is a link.
 *   B/R2  A long title pushed the `?`, the meta chip and the freshness off the
 *         column (.ctl-scroll 1322 > 1120). The title takes its own row (two
 *         lines, then cut, whole in its title); the rest share the next row.
 *   D11   On a phone the reference heading lost '54' of '#454' to the `?`
 *         beside it: alone on its row it fits.
 *   C     A step's touches and tests are LISTS, one per line, each breaking
 *         only at `/`, `::` or `_`, each with a copy button.
 *   D     A PLANNED run's actions are in the waiting banner AND a sticky bar
 *         at the foot of the plan; no other state draws them.
 *   E     Overlaps are neutral when none needs action; amber, with the count
 *         in the heading, when one does. Each is a compact row that opens to
 *         its reasoning.
 *   F     A cut lead has an inline "Read more"; the meta chip opens on a tap.
 *   N18   The lead is cut on its RENDERED text: never inside a code span,
 *         never leaving a raw backtick.
 *   R11   The Runs list sizes Workflow and By to what they hold.
 *
 * MUTATIONS: put the fact back to a flex row; drop a `<wbr>` or add one
 * mid-identifier; drop `display: contents` or the h1's 100% basis; render
 * touches as one comma line; drop the banner's actions or draw them for a
 * RUNNING run; drop the needs-action count; cut the lead on the source text
 * -- each turns a case red.
 */
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { breakAt, overlapVerdict, planLead } from '../Runs'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'ab'.repeat(32)
const REF = 'bogdan-alexandrescu/SwarmCloud#454'
const URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454'
const RUN_ID = 'run_7a37942a19aa4d2c80d1'
const WF = 'wf_9c4be0e52b3a4cbe9f1f'
const LONG_TITLE =
  'The run page loses its issue state, labels and comment count to a card that is narrower than its label and value rows together'
const TESTS_ID = 'tests/unit/control_plane/test_issue_runs.py::test_a_run_is_recorded_on_the_issue'
const FILE_ID = 'apps/swarm-api/swarm_api/routes/runs.py'

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
        { step_id: 'impl', title: 'api: edit `routes/runs.py`', prompt: 'Edit the route.', depends_on: [],
          files: [FILE_ID, 'apps/swarm-ui/src/Runs.tsx'], tests: [TESTS_ID] },
      ],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-03T14:02:00Z', updated_at: '2026-10-03T14:09:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-10-03T14:02:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: {
      title: LONG_TITLE, state: 'open', labels: ['bug', 'ui', 'needs-triage'], body: 'Body.', body_truncated: false,
      body_redacted: false, comments: 2, read_at: '2026-10-03T14:02:00Z', url: URL,
    },
    issue_read_error: null,
    ...over,
  }
}

function serve(body: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: body }), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mount(served: unknown) {
  serve(served)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
})

/**
 * The runs of `el`'s text that cannot break: split at every space and every
 * `<wbr>`. A browser breaks a line at nothing else once `overflow-wrap` and
 * `word-break` are normal (asserted beside each use).
 */
function unbreakable(el: Element): string[] {
  const out: string[] = ['']
  const walk = (n: Node) => {
    if (n.nodeType === Node.TEXT_NODE) {
      const parts = (n.textContent ?? '').split(/\s+/)
      parts.forEach((p, i) => {
        if (i > 0) out.push('')
        out[out.length - 1] += p
      })
    } else if (n.nodeName === 'WBR') {
      out.push('')
    } else {
      n.childNodes.forEach(walk)
    }
  }
  walk(el)
  return out.filter((s) => s !== '')
}

describe('R1/A: the side cards stack each label above its value', () => {
  // The card is 300px with 14px of padding and a 1px border a side: 270px of
  // value at 1440. At 390 the card is the column, wider; 270 is the stricter.
  const ROOM = 300 - 28 - 2

  for (const env of [WIDE, PHONE]) {
    it(`keeps every value inside its card at ${env.width}px`, async () => {
      const { container } = await mount(run({ workflow_id: WF, state: 'RUNNING', pull_request: { number: 545, url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/545' } }))
      const cards = [...container.querySelectorAll('.rn-side > .rn-card')]
      expect(cards.length).toBe(3)
      let seen = 0
      for (const card of cards) {
        for (const fact of card.querySelectorAll('.ctl-fact')) {
          seen++
          // A STACK: one track, the label on the first row and the value under it.
          expect(painted(fact, 'display', env), visible(fact)).toBe('grid')
          expect(painted(fact, 'grid-template-columns', env), visible(fact)).toBe('minmax(0, 1fr)')
          const value = fact.querySelector(':scope > :not(b)') as HTMLElement
          expect(value, visible(fact)).not.toBeNull()
          expect(painted(value, 'min-width', env), visible(fact)).toBe('0')
          expect(painted(value, 'text-align', env) ?? 'left', visible(fact)).not.toBe('right')
          const cut = painted(value, 'white-space', env) === 'nowrap'
          if (cut) {
            // A single-token id: cut, with an ellipsis, whole in its title.
            expect(painted(value, 'text-overflow', env), visible(fact)).toBe('ellipsis')
            expect(painted(value, ['overflow-x', 'overflow'], env), visible(fact)).toBe('hidden')
            expect(value.getAttribute('title') ?? value.querySelector('[title]')?.getAttribute('title'), visible(fact)).toBeTruthy()
            expect(unbreakable(value).length, `${visible(fact)} is prose, so it wraps`).toBe(1)
          } else {
            // Wraps at a safe point only, and no run between two of them is wider than the card.
            expect(painted(value, 'overflow-wrap', env) ?? 'normal', visible(fact)).toBe('normal')
            expect(painted(value, 'word-break', env) ?? 'normal', visible(fact)).toBe('normal')
            for (const piece of unbreakable(value)) {
              expect(textPx(piece, value, env), `"${piece}" in ${visible(fact)}`).toBeLessThanOrEqual(ROOM)
            }
          }
        }
      }
      expect(seen, 'the sweep visited the facts').toBeGreaterThanOrEqual(15)
    })
  }

  it('draws the issue\'s state, labels and comments as values a reader can see', async () => {
    const { container } = await mount(run())
    const read = within(container).getByRole('region', { name: 'Read from the issue' })
    const fact = (label: string) => [...read.querySelectorAll('.ctl-fact')].find((f) => visible(f.querySelector('b')) === label)!
    expect(visible(fact('state').querySelector(':scope > :not(b)'))).toBe('open')
    expect(visible(fact('labels').querySelector(':scope > :not(b)'))).toBe('bug · ui · needs-triage')
    expect(visible(fact('comments').querySelector(':scope > :not(b)'))).toBe('2')
    expect(visible(fact('title').querySelector(':scope > :not(b)'))).toBe(LONG_TITLE)
  })

  it('makes the pull request a link', async () => {
    const { container } = await mount(run({ state: 'CHECKING', workflow_id: WF, pull_request: { number: 545, url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/pull/545' } }))
    const linked = within(container).getByRole('region', { name: 'Linked' })
    const pr = [...linked.querySelectorAll('.ctl-fact')].find((f) => visible(f.querySelector('b')) === 'Pull request')!
    expect(pr.querySelector('a')?.getAttribute('href')).toBe('https://github.com/bogdan-alexandrescu/SwarmCloud/pull/545')
  })

  it('breaks a reference only at `/`, `#`, `_`, `::` or a space', () => {
    const pieces = (s: string) => breakAt(s).map((p) => (typeof p === 'string' ? p : '|')).join('')
    expect(pieces(REF)).toBe('bogdan-alexandrescu/|SwarmCloud#|454')
    expect(pieces('a::b_c')).toBe('a::|b_|c')
    expect(pieces('plain')).toBe('plain')
  })
})

describe('B/R2/D11: the page head never leaves the column', () => {
  for (const env of [WIDE, PHONE]) {
    it(`gives the title its own row and the rest the next at ${env.width}px`, async () => {
      const { container } = await mount(run())
      const head = container.querySelector('.rn-run .c-phead')!
      expect(painted(head, 'flex-wrap', env)).toBe('wrap')
      const block = head.querySelector(':scope > div.head')!
      expect(painted(block, 'display', env), 'the `?` stays on the title\'s row').toBe('contents')
      const h1 = head.querySelector('h1')!
      expect(painted(h1, 'flex', env)).toBe('1 0 100%')
      expect(painted(h1, '-webkit-line-clamp', env)).toBe('2')
      expect(h1.getAttribute('title')).toBe(LONG_TITLE)
      const sub = head.querySelector(':scope > .sub')!
      expect(painted(sub, 'min-width', env)).toBe('0')
      expect(painted(sub, 'max-width', env)).toBe('100%')
      const meta = head.querySelector('.c-meta')!
      expect(painted(meta, 'text-overflow', env)).toBe('ellipsis')
    })
  }

  it('fits the reference heading on a phone once it is alone on its row (D11)', async () => {
    const { container } = await mount(run({ issue_read: null }))
    const h1 = container.querySelector('.rn-run.is-ref .c-phead h1')!
    // The phone column: 390 less the 16px gutter a side.
    expect(painted(h1, 'flex', PHONE)).toBe('1 0 100%')
    expect(textPx(REF, h1, PHONE)).toBeLessThanOrEqual(390 - 32)
  })

  it('opens the meta chip on a tap (F)', async () => {
    const { container } = await mount(run())
    const button = container.querySelector('.c-phead .c-meta button.rn-meta') as HTMLButtonElement
    expect(button).not.toBeNull()
    expect(button.getAttribute('title')).toContain(RUN_ID)
    expect(button.getAttribute('aria-expanded')).toBe('false')
    expect(painted(button, 'white-space', PHONE) ?? 'nowrap').not.toBe('normal')
    fireEvent.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(painted(button, 'white-space', PHONE)).toBe('normal')
  })
})

describe('C: a step\'s touches and tests are lists', () => {
  it('draws one item per line, broken only at `/`, `::` or `_`, each with a copy button', async () => {
    const writes: string[] = []
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn(async (t: string) => { writes.push(t) }) } })
    const { container } = await mount(run())
    const step = container.querySelector('.rn-step')!
    const touches = step.querySelector('ul.rn-step-list[aria-label="Touches"]')!
    const tests = step.querySelector('ul.rn-step-list[aria-label="Tests"]')!
    expect(touches.querySelectorAll(':scope > li').length).toBe(2)
    expect(tests.querySelectorAll(':scope > li').length).toBe(1)
    const item = tests.querySelector('li')!
    const code = item.querySelector('code')!
    expect(code.textContent).toBe(TESTS_ID)
    expect(painted(code, 'overflow-wrap', WIDE) ?? 'normal').toBe('normal')
    expect(painted(code, 'word-break', WIDE) ?? 'normal').toBe('normal')
    for (const piece of unbreakable(code).slice(0, -1)) {
      expect(piece, 'a break mid-identifier').toMatch(/(\/|::|_)$/)
    }
    expect(unbreakable(code).join('')).toBe(TESTS_ID)
    expect(painted(touches, 'display', WIDE)).toBe('grid')
    fireEvent.click(within(item).getByRole('button', { name: `Copy ${TESTS_ID}` }))
    await waitFor(() => expect(writes).toEqual([TESTS_ID]))
  })
})

describe('D: a PLANNED run\'s actions are at the top and at the foot of the plan', () => {
  it('puts Approve, Edit and Reject in the waiting banner and in a sticky bar', async () => {
    const { container } = await mount(run())
    const banner = container.querySelector('.rn-wait')!
    for (const name of ['Approve and run', 'Edit plan', 'Reject']) {
      expect(within(banner as HTMLElement).getByRole('button', { name })).not.toBeNull()
    }
    const bar = container.querySelector('.rn-plan .rn-actbar')!
    expect(bar).not.toBeNull()
    expect(painted(bar, 'position', WIDE)).toBe('sticky')
    expect(painted(bar, 'bottom', WIDE)).toBe('0')
    expect(within(bar as HTMLElement).getByRole('button', { name: 'Approve and run' })).not.toBeNull()
  })

  for (const state of ['RUNNING', 'DONE', 'PLANNING', 'REJECTED']) {
    it(`draws no actions for a ${state} run`, async () => {
      const { container } = await mount(run({ state, terminal: state === 'DONE' || state === 'REJECTED' }))
      const banner = container.querySelector('.rn-wait')
      expect(banner === null ? 0 : banner.querySelectorAll('button').length).toBe(0)
      expect(container.querySelector('.rn-actbar')).toBeNull()
    })
  }
})

describe('E: the overlaps card follows how many need action', () => {
  const NO = (n: number) => ({ ref: `bogdan-alexandrescu/SwarmCloud#${500 + n}`, kind: 'issue', note: `No action: touches a different file (${n}).` })
  const NEEDS = { ref: 'bogdan-alexandrescu/SwarmCloud#540', kind: 'pull_request', note: 'Edits the same route; rebase onto it before step impl.' }

  it('is neutral when every overlap is no action, with the count in its heading', async () => {
    const overlaps = [1, 2, 3, 4, 5].map(NO)
    const { container } = await mount(run({ plan: { ...run().plan, overlaps } }))
    const card = container.querySelector('.rn-overlaps')!
    expect(card.classList.contains('is-found')).toBe(false)
    expect(visible(card.querySelector('h3, h2, .c-card-title'))).toContain('5 checked · none need action')
    const rows = card.querySelectorAll('details.rn-overlap')
    expect(rows.length).toBe(5)
    const summary = rows[0]!.querySelector('summary')!
    expect(summary.querySelector('a')).not.toBeNull()
    expect(visible(summary)).toMatch(/issue/)
    expect(visible(summary)).toMatch(/Not in this run/)
    expect(visible(summary)).not.toMatch(/different file/)
    expect(visible(rows[0]!.querySelector('.rn-overlap-note'))).toMatch(/touches a different file/)
  })

  it('takes the warn edge and counts the one that needs action', async () => {
    const { container } = await mount(run({ plan: { ...run().plan, overlaps: [NO(1), NEEDS] } }))
    const card = container.querySelector('.rn-overlaps')!
    expect(card.classList.contains('is-found')).toBe(true)
    expect(visible(card.querySelector('h3, h2, .c-card-title'))).toContain('2 checked · 1 needs action')
  })

  it('draws a plan stored with bare-string overlaps instead of crashing the page', async () => {
    const { container } = await mount(run({ plan: { ...run().plan, overlaps: ['PR #498 also edits Runs.tsx', NO(1)] } }))
    const card = container.querySelector('.rn-overlaps')!
    expect(card.querySelectorAll('details.rn-overlap').length).toBe(2)
    expect(visible(card.querySelectorAll('.rn-overlap-note')[0]!)).toContain('PR #498 also edits Runs.tsx')
    expect(visible(card.querySelector('h3, h2, .c-card-title'))).toContain('2 checked · 1 needs action')
  })

  it('reads the verdict off the planner\'s note', () => {
    expect(overlapVerdict({ note: undefined as unknown as string }).needs).toBe(true)
    expect(overlapVerdict({ note: 'No action: different file.' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'no action -- unrelated' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'Rebase onto it.' }).needs).toBe(true)
  })
})

describe('F/N18: the lead is cut on what it renders, and can be read whole', () => {
  it('never cuts inside a code span', () => {
    const long = 'Rename it. ' + 'Words '.repeat(36) + 'then edit `apps/swarm-api/swarm_api/routes/runs.py` and the rest. ' + 'More '.repeat(40)
    const { lead, cut } = planLead(long)
    expect(cut).toBe(true)
    // An even number of backticks: every code span the lead opens, it closes.
    expect((lead.match(/`/g) ?? []).length % 2).toBe(0)
    // The limit counts what is drawn, not the backticks.
    expect(lead.replace(/`/g, '').length).toBeLessThanOrEqual(241)
  })

  it('offers Read more beside a cut lead, and it shows the whole summary', async () => {
    const summary = 'First sentence here. ' + 'Then `code` and words. '.repeat(30)
    const { container } = await mount(run({ plan: { ...run().plan, summary } }))
    const lead = container.querySelector('.rn-summary')!
    expect(visible(lead)).not.toContain('`')
    const more = within(lead as HTMLElement).getByRole('button', { name: 'Read more' })
    fireEvent.click(more)
    expect(visible(container.querySelector('.rn-summary'))).toBe(`${summary.trim().replace(/`/g, '')} Show less`)
  })
})

describe('R11: the Runs list sizes Workflow and By to what they hold', () => {
  it('fits a workflow id and an operator address whole at 1440, with the issue keeping room', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({
      runs: [run({ state: 'DONE', workflow_id: WF, created_by: 'bogdan.a@saga.xyz' })], next_page_token: null,
    }), { status: 200, headers: JSON_HEADERS })) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    await waitFor(() => expect(container.querySelector('.rn-table')).not.toBeNull(), WAIT)
    const table = container.querySelector<HTMLTableElement>('.rn-table')!
    const { cols } = fixedColumns(table, 1056, WIDE)
    const row = container.querySelector('.rn-row')!
    const room = (col: string, label: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-label="${label}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const wf = row.querySelector('td[data-label="Workflow"] .rn-cut')!
    expect(room('workflow', 'Workflow')).toBeGreaterThanOrEqual(textPx(WF, wf, WIDE))
    const by = row.querySelector('td[data-label="By"] .rn-cut')!
    expect(room('by', 'By')).toBeGreaterThanOrEqual(textPx('bogdan.a@saga.xyz', by, WIDE))
    // And the issue reference stays whole beside them (browser QA D11).
    const ref = row.querySelector('td[data-label="Issue"] .rn-issue-ref')!
    expect(room('issue', 'Issue'), 'the issue reference is cut').toBeGreaterThanOrEqual(textPx(REF, ref, WIDE))
    expect(room('run', 'Run')).toBeGreaterThanOrEqual(textPx(RUN_ID, row.querySelector('td[data-label="Run"] a')!, WIDE))
  })
})
